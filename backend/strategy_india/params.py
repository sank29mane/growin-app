"""Strategy parameter schema (D-06).

Concrete values live only in ``private/india/strategy.json`` (58
``IndiaStrategy.params``). Tracked code holds this schema and a synthetic
placeholder that is plainly not a research choice. Numerics are decimal strings;
a JSON float is refused upstream by the 58 loader and again here.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from private_config.schemas import DecimalStr

from .errors import ParamsError

_STRICT = ConfigDict(extra="forbid", frozen=True)


class EdgeMapSpec(BaseModel):
    """Score-to-expected-edge map (D-15). Registered by hash; ``fit`` is re-estimated per fold on purged training trades."""

    model_config = _STRICT
    mode: Literal["fixed", "fit"]
    slope: DecimalStr  # expected edge as a fraction of notional per unit of cross-sectional z-score
    min_obs: int = Field(ge=1)
    ridge: DecimalStr


class RegimeSpec(BaseModel):
    model_config = _STRICT
    enter_cash: DecimalStr  # enter cash at or above this risk-off posterior
    exit_cash: DecimalStr  # leave cash at or below this posterior
    min_dwell: int = Field(ge=1)
    n_init: int = Field(ge=1)
    reg_covar: DecimalStr
    min_component_weight: DecimalStr
    vol_window: int = Field(ge=2)
    drawdown_window: int = Field(ge=2)
    breadth_window: int = Field(ge=2)


class BenchmarkSpec(BaseModel):
    model_config = _STRICT
    candidate_isins: tuple[str, ...] = Field(min_length=1)
    min_completeness: DecimalStr


class StrategyParams(BaseModel):
    model_config = _STRICT

    lookback_sessions: int = Field(ge=2)
    skip_sessions: int = Field(ge=0)
    vol_window: int = Field(ge=2)
    vol_adjusted: bool
    rebalance_every: int = Field(ge=2, le=3)
    max_positions: int = Field(ge=5, le=10)
    min_positions: int = Field(ge=5, le=10)
    hold_rank_cutoff: int = Field(ge=1)
    min_hold_sessions: int = Field(ge=1)
    max_hold_sessions: int = Field(ge=1)
    limit_offset_bps: DecimalStr
    adv_window: int = Field(ge=1)
    liquidity_adv_fraction: DecimalStr
    no_trade_band: DecimalStr
    halt_release: DecimalStr  # drawdown level at which a halt lifts, e.g. "-0.04"
    min_universe_for_entry: int = Field(ge=1)
    edge_map: EdgeMapSpec
    regime: RegimeSpec
    benchmark: BenchmarkSpec
    turnover_budget_swaps_per_week: DecimalStr
    seed: int

    @model_validator(mode="after")
    def _rules(self) -> "StrategyParams":
        if self.min_positions > self.max_positions:
            raise ValueError("min_positions exceeds max_positions")
        if self.max_hold_sessions < self.min_hold_sessions:
            raise ValueError("max_hold_sessions is below min_hold_sessions")
        if self.hold_rank_cutoff < self.max_positions:
            raise ValueError("hold_rank_cutoff must be at least max_positions")
        if not Decimal(0) <= Decimal(self.no_trade_band):
            raise ValueError("no_trade_band must not be negative")
        if not Decimal(0) < Decimal(self.liquidity_adv_fraction) <= Decimal(1):
            raise ValueError("liquidity_adv_fraction must be in (0, 1]")
        if not Decimal(-1) < Decimal(self.halt_release) < Decimal(0):
            raise ValueError("halt_release must be a drawdown between -1 and 0")
        if not Decimal(0) <= Decimal(self.regime.exit_cash) < Decimal(self.regime.enter_cash) <= Decimal(1):
            raise ValueError("regime hysteresis needs 0 <= exit_cash < enter_cash <= 1")
        return self


def parse_params(params: dict[str, Any]) -> StrategyParams:
    """Validate a raw ``IndiaStrategy.params`` mapping. A minimum hold below one session is refused here too."""
    try:
        return StrategyParams.model_validate(params)
    except ValidationError as exc:
        # Field names only: a private value must never reach an error message.
        fields = sorted({".".join(str(part) for part in err["loc"]) for err in exc.errors()})
        raise ParamsError(f"strategy params are invalid in: {', '.join(fields)}") from None


def params_sha256(params: dict[str, Any]) -> str:
    """Same canonical form the 58 loader hashes (``IndiaStrategy.params_sha256``)."""
    encoded = json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def placeholder_params() -> dict[str, Any]:
    """Synthetic placeholder for tests. Not a research choice and never a default for a real run."""
    return {
        "lookback_sessions": 20,
        "skip_sessions": 1,
        "vol_window": 10,
        "vol_adjusted": True,
        "rebalance_every": 2,
        "max_positions": 5,
        "min_positions": 5,
        "hold_rank_cutoff": 8,
        "min_hold_sessions": 2,
        "max_hold_sessions": 15,
        "limit_offset_bps": "20",
        "adv_window": 20,
        "liquidity_adv_fraction": "0.05",
        "no_trade_band": "0.001",
        "halt_release": "-0.04",
        "min_universe_for_entry": 8,
        "edge_map": {"mode": "fixed", "slope": "0.01", "min_obs": 5, "ridge": "1"},
        "regime": {
            "enter_cash": "0.8",
            "exit_cash": "0.4",
            "min_dwell": 2,
            "n_init": 3,
            "reg_covar": "0.000001",
            "min_component_weight": "0.05",
            "vol_window": 10,
            "drawdown_window": 60,
            "breadth_window": 20,
        },
        "benchmark": {"candidate_isins": ["INF000000ETF1", "INF000000ETF2"], "min_completeness": "0.95"},
        "turnover_budget_swaps_per_week": "1",
        "seed": 7,
    }
