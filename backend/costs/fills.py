"""Daily-bar limit-fill model (decision D-10).

An order is active before the next session opens and expires at that
session's close. A buy fills only when the session low reaches
``limit - k * tick``; a sell only when the high reaches ``limit + k * tick``.
Fills happen at the limit, capped by a share of the session volume.

Two measures are kept apart (review decision D11) and neither is ever charged:

* ``decision_drift``: the move from the decision price (``reference_price``)
  to the fill price. Reported, never a transaction cost.
* ``execution_slippage``: the move from the limit to the fill price. Zero for
  every at-limit fill under D-10. An arrival-price benchmark needs intraday
  data and is out of scope for daily bars.
"""

from __future__ import annotations

import decimal
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from typing import Literal

from .core import (
    COST_CONTEXT,
    InputError,
    Side,
    TickSizeUnavailable,
    TradeFill,
    canonical_value,
    floor_to,
    positive_decimal,
    require_aware,
    require_date,
    require_sha256,
    require_text,
    require_time,
    seal,
    set_field,
    strict_int,
)

# Reason codes.
FILLED_AT_LIMIT = "FILLED_AT_LIMIT"
PARTIAL_VOLUME_CAP = "PARTIAL_VOLUME_CAP"
MISSED_THRESHOLD = "MISSED_THRESHOLD"
MISSED_VOLUME_CAP_ZERO = "MISSED_VOLUME_CAP_ZERO"
REJECTED_OFF_TICK = "REJECTED_OFF_TICK"
REJECTED_OUTSIDE_BAND = "REJECTED_OUTSIDE_BAND"
MISSED_LOCKED_AT_BAND = "MISSED_LOCKED_AT_BAND"
# D15 codes: unsupported simulation data, never a market miss.
NO_FILL_BAND_UNAVAILABLE = "NO_FILL_BAND_UNAVAILABLE"
NO_FILL_BAND_DATE_MISMATCH = "NO_FILL_BAND_DATE_MISMATCH"
NO_FILL_AMBIGUOUS_SINGLE_PRICE = "NO_FILL_AMBIGUOUS_SINGLE_PRICE"

_BPS = Decimal("0.01")


class FillOutcome(str, Enum):
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    MISSED = "MISSED"
    REJECTED = "REJECTED"
    NO_ASSUMED_FILL = "NO_ASSUMED_FILL"


@dataclass(frozen=True)
class TickSize:
    value: Decimal
    effective_from: date
    source: str
    source_hash: str
    effective_to: date | None = None

    def __post_init__(self) -> None:
        set_field(self, "value", positive_decimal(self.value, "tick.value"))
        require_date(self.effective_from, "tick.effective_from")
        if self.effective_to is not None:
            require_date(self.effective_to, "tick.effective_to")
            if self.effective_to < self.effective_from:
                raise InputError("tick.effective_to precedes tick.effective_from")
        require_text(self.source, "tick.source")
        require_sha256(self.source_hash, "tick.source_hash")


@dataclass(frozen=True)
class PriceBand:
    category: Literal["fixed", "no_band"]
    lower: Decimal | None
    upper: Decimal | None
    effective_date: date
    source: str
    source_hash: str

    def __post_init__(self) -> None:
        if self.category == "fixed":
            lower = positive_decimal(self.lower, "band.lower")
            upper = positive_decimal(self.upper, "band.upper")
            if not lower < upper:
                raise InputError("band.lower must be below band.upper")
            set_field(self, "lower", lower)
            set_field(self, "upper", upper)
        elif self.category == "no_band":
            if self.lower is not None or self.upper is not None:
                raise InputError("a no_band price band carries no lower or upper")
        else:
            raise InputError(f"band.category must be fixed or no_band, got {self.category!r}")
        require_date(self.effective_date, "band.effective_date")
        require_text(self.source, "band.source")
        require_sha256(self.source_hash, "band.source_hash")


@dataclass(frozen=True)
class BandUnavailable:
    reason: str

    def __post_init__(self) -> None:
        require_text(self.reason, "band unavailable reason")


@dataclass(frozen=True)
class SessionBar:
    isin: str
    exchange: str
    session_date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    price_basis: str
    price_band: PriceBand | BandUnavailable
    source: str

    def __post_init__(self) -> None:
        require_text(self.isin, "bar.isin")
        require_text(self.exchange, "bar.exchange")
        require_date(self.session_date, "bar.session_date")
        for name in ("open", "high", "low", "close"):
            set_field(self, name, positive_decimal(getattr(self, name), f"bar.{name}"))
        strict_int(self.volume, "bar.volume", minimum=0)
        require_text(self.price_basis, "bar.price_basis")
        if not isinstance(self.price_band, (PriceBand, BandUnavailable)):
            raise InputError("bar.price_band must be a PriceBand or BandUnavailable")
        require_text(self.source, "bar.source")


@dataclass(frozen=True)
class LimitOrder:
    order_id: str
    isin: str
    exchange: str
    side: Side
    quantity: int
    limit_price: Decimal
    reference_price: Decimal
    session_date: date
    submitted_at: datetime
    information_as_of: date
    tick: TickSize | None

    def __post_init__(self) -> None:
        require_text(self.order_id, "order_id")
        require_text(self.isin, "isin")
        require_text(self.exchange, "exchange")
        if not isinstance(self.side, Side):
            raise InputError("side must be a Side")
        strict_int(self.quantity, "quantity", minimum=1)
        set_field(self, "limit_price", positive_decimal(self.limit_price, "limit_price"))
        set_field(self, "reference_price", positive_decimal(self.reference_price, "reference_price"))
        require_date(self.session_date, "session_date")
        require_aware(self.submitted_at, "submitted_at")
        require_date(self.information_as_of, "information_as_of")
        if self.tick is not None and not isinstance(self.tick, TickSize):
            raise InputError("tick must be a TickSize or None")


@dataclass(frozen=True)
class FillScenario:
    scenario_id: str
    k_ticks: int
    volume_participation: Decimal
    phase62_gate: bool
    submission_cutoff: time
    assumption_note: str
    scenarios_version: str
    scenarios_hash: str

    def __post_init__(self) -> None:
        require_text(self.scenario_id, "scenario_id")
        strict_int(self.k_ticks, "k_ticks", minimum=1)
        participation = positive_decimal(self.volume_participation, "volume_participation")
        if participation > 1:
            raise InputError("volume_participation must be at most 1")
        set_field(self, "volume_participation", participation)
        if not isinstance(self.phase62_gate, bool):
            raise InputError("phase62_gate must be a bool")
        require_time(self.submission_cutoff, "submission_cutoff")
        require_text(self.assumption_note, "assumption_note")
        require_text(self.scenarios_version, "scenarios_version")
        require_sha256(self.scenarios_hash, "scenarios_hash")


@dataclass(frozen=True)
class FillResult:
    order_id: str
    isin: str
    exchange: str
    side: Side
    session_date: date
    requested_quantity: int
    filled_quantity: int
    fill_price: Decimal | None
    notional: Decimal
    outcome: FillOutcome
    reason_code: str
    scenario_id: str
    scenarios_version: str
    scenarios_hash: str
    k_ticks: int
    volume_participation: Decimal
    session_volume_cap: int
    tick_size: Decimal
    tick_source: str
    tick_source_hash: str
    tick_effective_from: date
    band_check: str
    reference_price: Decimal
    decision_drift_per_share: Decimal | None
    decision_drift_bps: Decimal | None
    drift_adverse: bool
    execution_slippage_per_share: Decimal | None
    execution_slippage_bps: Decimal | None
    forgone_improvement_per_share: Decimal | None
    result_hash: str

    def to_trade_fill(self) -> TradeFill | None:
        if self.filled_quantity <= 0 or self.fill_price is None:
            return None
        return TradeFill(
            self.order_id, self.isin, self.exchange, self.side,
            self.filled_quantity, self.fill_price, self.session_date,
        )


def simulate_session(
    orders: Sequence[LimitOrder], bar: SessionBar, scenario: FillScenario
) -> tuple[FillResult, ...]:
    with decimal.localcontext(COST_CONTEXT):
        return _simulate_session(orders, bar, scenario)


def _band_check(bar: SessionBar, order: LimitOrder) -> str:
    band = bar.price_band
    if isinstance(band, PriceBand):
        if band.category == "no_band":
            return "no_band"
        assert band.lower is not None and band.upper is not None
        if band.lower <= order.limit_price <= band.upper:
            return "inside_band"
    return "unchecked"


def _check_tick(order: LimitOrder, tick: TickSize | None) -> TickSize:
    if tick is None:
        raise TickSizeUnavailable(f"order {order.order_id!r} has no tick size; nothing defaults")
    if tick.effective_from > order.session_date or (
        tick.effective_to is not None and tick.effective_to < order.session_date
    ):
        raise TickSizeUnavailable(
            f"order {order.order_id!r}: tick from {tick.source!r} is not effective on "
            f"{order.session_date.isoformat()}"
        )
    return tick


def _build_result(
    order: LimitOrder,
    bar: SessionBar,
    scenario: FillScenario,
    tick: TickSize,
    *,
    filled: int,
    outcome: FillOutcome,
    reason: str,
    cap: int,
    band_check: str,
) -> FillResult:
    fill_price = order.limit_price if filled > 0 else None
    drift = drift_bps = slip = slip_bps = forgone = None
    drift_adverse = False
    if fill_price is not None:
        if order.side is Side.BUY:
            drift = fill_price - order.reference_price
            slip = fill_price - order.limit_price
            forgone = max(Decimal(0), order.limit_price - bar.open)
        else:
            drift = order.reference_price - fill_price
            slip = order.limit_price - fill_price
            forgone = max(Decimal(0), bar.open - order.limit_price)
        drift_bps = (drift / order.reference_price * 10000).quantize(_BPS, rounding=ROUND_HALF_UP)
        drift_adverse = drift > 0
        slip_bps = (slip / order.limit_price * 10000).quantize(_BPS, rounding=ROUND_HALF_UP)
    result = FillResult(
        order_id=order.order_id,
        isin=order.isin,
        exchange=order.exchange,
        side=order.side,
        session_date=order.session_date,
        requested_quantity=order.quantity,
        filled_quantity=filled,
        fill_price=fill_price,
        notional=filled * order.limit_price if filled > 0 else Decimal(0),
        outcome=outcome,
        reason_code=reason,
        scenario_id=scenario.scenario_id,
        scenarios_version=scenario.scenarios_version,
        scenarios_hash=scenario.scenarios_hash,
        k_ticks=scenario.k_ticks,
        volume_participation=scenario.volume_participation,
        session_volume_cap=cap,
        tick_size=tick.value,
        tick_source=tick.source,
        tick_source_hash=tick.source_hash,
        tick_effective_from=tick.effective_from,
        band_check=band_check,
        reference_price=order.reference_price,
        decision_drift_per_share=drift,
        decision_drift_bps=drift_bps,
        drift_adverse=drift_adverse,
        execution_slippage_per_share=slip,
        execution_slippage_bps=slip_bps,
        forgone_improvement_per_share=forgone,
        result_hash="",
    )
    return seal(result, "result_hash", extra={"bar": canonical_value(bar)})


def _simulate_session(
    orders: Sequence[LimitOrder], bar: SessionBar, scenario: FillScenario
) -> tuple[FillResult, ...]:
    if not orders:
        raise InputError("simulate_session needs at least one order")
    if len(orders) > 1:
        raise InputError(
            "more than one order for one instrument and session; "
            "D-10 aggregate participation lands in Plan 03 Task 2"
        )
    order = orders[0]
    if (order.isin, order.exchange, order.session_date) != (bar.isin, bar.exchange, bar.session_date):
        raise InputError(f"order {order.order_id!r} does not match the bar's isin, exchange and session date")
    tick = _check_tick(order, order.tick)
    cap = int(floor_to(Decimal(bar.volume) * scenario.volume_participation, Decimal(1)))
    band_check = _band_check(bar, order)
    if order.limit_price % tick.value != 0:
        return (
            _build_result(
                order, bar, scenario, tick, filled=0, outcome=FillOutcome.REJECTED,
                reason=REJECTED_OFF_TICK, cap=cap, band_check=band_check,
            ),
        )
    offset = scenario.k_ticks * tick.value
    if order.side is Side.BUY:
        eligible = bar.low <= order.limit_price - offset
    else:
        eligible = bar.high >= order.limit_price + offset

    if not eligible:
        filled, outcome, reason = 0, FillOutcome.MISSED, MISSED_THRESHOLD
    else:
        filled = min(order.quantity, cap)
        if filled == 0:
            outcome, reason = FillOutcome.MISSED, MISSED_VOLUME_CAP_ZERO
        elif filled < order.quantity:
            outcome, reason = FillOutcome.PARTIAL, PARTIAL_VOLUME_CAP
        else:
            outcome, reason = FillOutcome.FILLED, FILLED_AT_LIMIT
    return (
        _build_result(
            order, bar, scenario, tick, filled=filled, outcome=outcome, reason=reason,
            cap=cap, band_check=band_check,
        ),
    )
