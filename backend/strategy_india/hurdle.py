"""Cost hurdle, no-trade band and minimum hold (D-15).

A candidate is traded only when its expected edge, from the registered
score-to-edge map, exceeds the 60 full-cost delivery round trip at its own
liquidity-scaled size plus the no-trade band. The map and the hurdle settings
are hashed into the registration (``hurdle_map_sha256``) so the hurdle is not a
hidden free parameter. Same-day square-off is an error path: the minimum hold is
at least one session and 60 refuses a round trip that does not span a later day.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from costs.charges import estimate_delivery_round_trip
from costs.schedule import PricingBasis, ScheduleSet

from .errors import ParamsError
from .folds import Observation
from .params import EdgeMapSpec, StrategyParams
from .registry import canonical_sha256

ZERO = Decimal(0)


def hurdle_map_sha256(params: StrategyParams) -> str:
    """Hash of the score-to-edge map plus the no-trade band and minimum hold."""
    return canonical_sha256(
        {
            "edge_map": {
                "mode": params.edge_map.mode,
                "slope": str(params.edge_map.slope),
                "min_obs": params.edge_map.min_obs,
                "ridge": str(params.edge_map.ridge),
            },
            "no_trade_band": str(params.no_trade_band),
            "min_hold_sessions": params.min_hold_sessions,
            "limit_offset_bps": str(params.limit_offset_bps),
        }
    )


def edge_fraction(z: Decimal, slope: Decimal) -> Decimal:
    """Expected edge as a fraction of notional. A non-positive score carries no edge."""
    return slope * z if z > 0 and slope > 0 else ZERO


def fit_slope(observations: Sequence[Observation], spec: EdgeMapSpec) -> tuple[Decimal, int, bool]:
    """Ridge slope through the origin on purged training trades: sum(z*y) / (sum(z*z) + ridge), floored at zero.

    Falls back to the registered prior when there are fewer than ``min_obs`` observations.
    Returns (slope, observations used, fitted).
    """
    prior = Decimal(spec.slope)
    if spec.mode != "fit" or len(observations) < spec.min_obs:
        return prior, len(observations) if spec.mode == "fit" else 0, False
    numerator = sum((obs.score * obs.net_return for obs in observations), ZERO)
    denominator = sum((obs.score * obs.score for obs in observations), ZERO) + Decimal(spec.ridge)
    slope = numerator / denominator
    return (slope if slope > 0 else ZERO), len(observations), True


@dataclass(frozen=True)
class HurdleDecision:
    clears: bool
    edge: Decimal
    cost_fraction: Decimal
    cost_total: Decimal
    net_edge: Decimal
    quantity: int


def round_trip_cost(
    *,
    isin: str,
    quantity: int,
    price: Decimal,
    buy_date: date,
    sell_date: date,
    schedules: ScheduleSet,
    pricing_basis: PricingBasis,
) -> Decimal:
    """Full-cost delivery round trip from 60. A same-day (or earlier) sell raises ``InputError``."""
    estimate = estimate_delivery_round_trip(
        workspace="india", currency="INR", isin=isin, exchange="NSE", quantity=quantity, buy_price=price,
        sell_price=price, buy_date=buy_date, sell_date=sell_date, schedules=schedules, pricing_basis=pricing_basis,
    )
    return estimate.total


def evaluate_candidate(
    *,
    z: Decimal,
    slope: Decimal,
    isin: str,
    quantity: int,
    reference_price: Decimal,
    session_date: date,
    params: StrategyParams,
    schedules: ScheduleSet,
    pricing_basis: PricingBasis,
) -> HurdleDecision:
    """Does the expected edge beat round-trip cost at this size plus the no-trade band?"""
    if params.min_hold_sessions < 1:
        raise ParamsError("the minimum hold must be at least one session")
    cost = round_trip_cost(
        isin=isin, quantity=quantity, price=reference_price, buy_date=session_date,
        sell_date=session_date + timedelta(days=params.min_hold_sessions), schedules=schedules,
        pricing_basis=pricing_basis,
    )
    fraction = cost / (reference_price * quantity)
    edge = edge_fraction(z, slope)
    net = edge - fraction
    return HurdleDecision(
        clears=edge > 0 and net > Decimal(params.no_trade_band), edge=edge, cost_fraction=fraction,
        cost_total=cost, net_edge=net, quantity=quantity,
    )
