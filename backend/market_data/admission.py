"""Bind fresh normalized market evidence to one immutable execution intent."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from execution.models import OrderIntent

from .models import IndiaInstrument, MarketSnapshot
from .session import MarketDataError, MarketDataSession


class RegimeEvidence(BaseModel):
    """Phase 50 output bound to the exact snapshot it classified."""

    model_config = ConfigDict(str_strip_whitespace=True, frozen=True, extra="forbid")

    instrument: IndiaInstrument
    regime_id: int = Field(..., ge=0)
    observed_at: datetime
    model_version: str = Field(..., min_length=1, max_length=128)
    source_snapshot_id: str = Field(..., min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_timestamp(self) -> "RegimeEvidence":
        if self.observed_at.tzinfo is None:
            raise ValueError("regime evidence timestamp must be timezone-aware")
        return self


class MarketPreflightContext(BaseModel):
    """Market-owned subset of the Phase 54 execution admission arguments."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot: MarketSnapshot
    snapshot_id: str = Field(..., min_length=64, max_length=64)
    regime: RegimeEvidence
    tick_window: dict[str, list[float]]
    evidence_at: datetime

    def execution_kwargs(self) -> dict[str, Any]:
        return {
            "price": self.snapshot.mid,
            "tick_window": self.tick_window,
            "regime_id": self.regime.regime_id,
            "current_spread_pct": self.snapshot.spread_pct,
            "evidence_at": self.evidence_at,
        }


def build_market_preflight_context(
    session: MarketDataSession,
    *,
    intent: OrderIntent,
    instrument: IndiaInstrument,
    regime: RegimeEvidence,
    now: datetime | None = None,
    max_regime_age_seconds: float = 30.0,
    clock: Callable[[], datetime] | None = None,
) -> MarketPreflightContext:
    """Validate identity/freshness and derive internally consistent evidence."""

    checked_now = now or (clock or (lambda: datetime.now(timezone.utc)))()
    if checked_now.tzinfo is None:
        raise MarketDataError("INVALID_CLOCK", "preflight clock must be timezone-aware")
    if max_regime_age_seconds <= 0:
        raise ValueError("max_regime_age_seconds must be positive")
    if intent.workspace != instrument.workspace:
        raise MarketDataError("WORKSPACE_MISMATCH", "intent and market workspace do not match")
    if intent.ticker != instrument.execution_ticker:
        raise MarketDataError("INSTRUMENT_MISMATCH", "intent and market instrument do not match")
    if regime.instrument != instrument:
        raise MarketDataError("REGIME_INSTRUMENT_MISMATCH", "regime instrument does not match")

    snapshot = session.snapshot(instrument, now=checked_now)
    snapshot_id = snapshot.snapshot_id
    if regime.source_snapshot_id != snapshot_id:
        raise MarketDataError("REGIME_SNAPSHOT_MISMATCH", "regime evidence is for another snapshot")
    max_age = timedelta(seconds=max_regime_age_seconds)
    if regime.observed_at > checked_now or checked_now - regime.observed_at > max_age:
        raise MarketDataError("STALE_REGIME", "regime evidence is stale or from the future")

    return MarketPreflightContext(
        snapshot=snapshot,
        snapshot_id=snapshot_id,
        regime=regime,
        tick_window=session.tick_window(instrument, now=checked_now),
        evidence_at=min(snapshot.quote_observed_at, regime.observed_at),
    )
