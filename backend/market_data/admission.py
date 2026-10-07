"""Bind fresh normalized market evidence to one immutable execution intent."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, model_validator

from execution.models import OrderIntent

from .models import Instrument, MarketSnapshot
from .session import MarketDataError, MarketDataSession


class RegimeEvidence(BaseModel):
    """Phase 50 output bound to the exact snapshot it classified."""

    model_config = ConfigDict(str_strip_whitespace=True, frozen=True, extra="forbid")

    instrument: Instrument
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
    instrument: Instrument,
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


# --- UK practice price rules (Phase 66, D-03, D-18, D-26) -------------------------
#
# Pure functions, no I/O. Prices in a GBX instrument are pence; money in the ledger
# is pounds, so a notional is quantity x limit price / 100 for GBX.

SLIPPAGE_LIMIT = "SLIPPAGE_LIMIT"
SLIPPAGE_CAP_UNAVAILABLE = "SLIPPAGE_CAP_UNAVAILABLE"
SLIPPAGE_QUOTE_UNAVAILABLE = "SLIPPAGE_QUOTE_UNAVAILABLE"

_BPS = Decimal("10000")
_DECIMAL_TEXT = re.compile(r"^(0|[1-9][0-9]*)(\.[0-9]+)?$")


class RecordedQuoteReading(BaseModel):
    """One bid and ask the operator typed from the practice app, with the time of reading.

    A missing bid or ask is allowed here on purpose: admission then denies with a
    stable reason instead of the request being a bare validation error.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    bid: Decimal | None = Field(default=None, allow_inf_nan=False, max_digits=20, decimal_places=8)
    ask: Decimal | None = Field(default=None, allow_inf_nan=False, max_digits=20, decimal_places=8)
    observed_at: datetime

    @model_validator(mode="after")
    def _aware(self) -> "RecordedQuoteReading":
        if self.observed_at.tzinfo is None:
            raise ValueError("a recorded reading needs a timezone-aware time")
        return self


def parse_max_slippage_bps(raw: object) -> Decimal | None:
    """The configured cap in basis points, or None when it is not a usable positive number.

    Accepts a JSON integer or a plain decimal string. Absent, null, a boolean, a
    list, non-numeric text, zero and negative values all return None, and None
    means DENY. It never means "no cap".
    """

    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, int):
        value = Decimal(raw)
    elif isinstance(raw, str) and _DECIMAL_TEXT.fullmatch(raw.strip()):
        value = Decimal(raw.strip())
    else:
        return None
    return value if value.is_finite() and value > 0 else None


def slippage_bps(
    side: str, limit_price: Decimal, bid: Decimal | None, ask: Decimal | None
) -> Decimal | None:
    """Side-adjusted slippage against the recorded quote, in basis points.

    BUY is measured against the recorded ask: (limit - ask) / ask x 10000. SELL
    against the recorded bid: (bid - limit) / bid x 10000. A positive value means
    the order is worse than the quote; a far limit is negative. None when the
    side's reference price is missing.
    """

    reference = ask if side == "BUY" else bid
    if reference is None or reference <= 0:
        return None
    gap = (limit_price - reference) if side == "BUY" else (reference - limit_price)
    return gap / reference * _BPS


def slippage_denial(
    side: str,
    limit_price: Decimal,
    bid: Decimal | None,
    ask: Decimal | None,
    cap_bps: Decimal | None,
) -> str | None:
    """A stable denial code, or None when the order is inside the slippage cap.

    A missing cap or a missing recorded price denies. Exactly at the cap passes;
    anything above it is ``SLIPPAGE_LIMIT``. The comparison is cross-multiplied so
    it is exact, with no division rounding at the boundary.
    """

    if cap_bps is None:
        return SLIPPAGE_CAP_UNAVAILABLE
    reference = ask if side == "BUY" else bid
    if reference is None or reference <= 0:
        return SLIPPAGE_QUOTE_UNAVAILABLE
    gap = (limit_price - reference) if side == "BUY" else (reference - limit_price)
    if gap * _BPS > cap_bps * reference:
        return SLIPPAGE_LIMIT
    return None


def practice_notional(quantity: Decimal, limit_price: Decimal, price_divisor: Decimal) -> Decimal:
    """Worst-case order value in pounds: quantity x limit price, pence divided by 100 (D-18)."""

    return quantity * limit_price / price_divisor
