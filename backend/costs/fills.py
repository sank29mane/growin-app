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
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from decimal import ROUND_HALF_UP, Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Literal

from .core import (
    COST_CONTEXT,
    IST,
    InputError,
    LookaheadError,
    ScheduleError,
    Side,
    TickSizeUnavailable,
    TradeFill,
    canonical_json,
    canonical_value,
    floor_to,
    load_strict_json,
    positive_decimal,
    require_aware,
    require_date,
    require_sha256,
    require_text,
    require_time,
    seal,
    set_field,
    sha256_hex,
    strict_decimal,
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


def _validate_bar(bar: SessionBar) -> None:
    if bar.price_basis != "raw":
        raise InputError(f"bar price_basis must be 'raw' for fills, got {bar.price_basis!r}")
    if bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
        raise InputError("bar is inconsistent: low must not exceed open or close, high must not be below them")
    band = bar.price_band
    if (
        isinstance(band, PriceBand)
        and band.category == "fixed"
        and band.effective_date == bar.session_date
    ):
        assert band.lower is not None and band.upper is not None
        if bar.low < band.lower or bar.high > band.upper:
            raise InputError("bar trades outside its own fixed price band")


def _validate_order(order: LimitOrder, bar: SessionBar, scenario: FillScenario) -> TickSize:
    if (order.isin, order.exchange, order.session_date) != (bar.isin, bar.exchange, bar.session_date):
        raise InputError(f"order {order.order_id!r} does not match the bar's isin, exchange and session date")
    if order.information_as_of >= bar.session_date:
        raise LookaheadError(
            f"order {order.order_id!r}: sizing information as of {order.information_as_of.isoformat()} "
            f"is not before the fill session {bar.session_date.isoformat()}"
        )
    cutoff = datetime.combine(bar.session_date, scenario.submission_cutoff, tzinfo=IST)
    if order.submitted_at >= cutoff:
        raise LookaheadError(
            f"order {order.order_id!r} was submitted at or after the {cutoff.isoformat()} cutoff"
        )
    return _check_tick(order, order.tick)


Verdict = tuple[FillOutcome, str]


def _band_status(order: LimitOrder, bar: SessionBar) -> tuple[str, Verdict | None]:
    """D15 band rules. Returns the band_check label and a terminal verdict, if any."""
    band = bar.price_band
    if isinstance(band, BandUnavailable):
        return f"unavailable:{band.reason}", (FillOutcome.NO_ASSUMED_FILL, NO_FILL_BAND_UNAVAILABLE)
    if band.effective_date != bar.session_date:
        return (
            f"date_mismatch:{band.effective_date.isoformat()}",
            (FillOutcome.NO_ASSUMED_FILL, NO_FILL_BAND_DATE_MISMATCH),
        )
    single_price = bar.high == bar.low
    if band.category == "no_band":
        if single_price:
            return "no_band", (FillOutcome.NO_ASSUMED_FILL, NO_FILL_AMBIGUOUS_SINGLE_PRICE)
        return "no_band", None
    assert band.lower is not None and band.upper is not None
    if not band.lower <= order.limit_price <= band.upper:
        return "outside_band", (FillOutcome.REJECTED, REJECTED_OUTSIDE_BAND)
    if single_price and (
        (order.side is Side.BUY and bar.high == band.upper)
        or (order.side is Side.SELL and bar.low == band.lower)
    ):
        return "locked_at_band", (FillOutcome.MISSED, MISSED_LOCKED_AT_BAND)
    return "inside_band", None


def _screen(order: LimitOrder, bar: SessionBar, scenario: FillScenario, tick: TickSize) -> tuple[str, Verdict | None]:
    """Everything that can end an order before the shared volume cap is considered."""
    band_check, verdict = _band_status(order, bar)
    if order.limit_price % tick.value != 0:
        return band_check, (FillOutcome.REJECTED, REJECTED_OFF_TICK)
    if verdict is not None:
        return band_check, verdict
    offset = scenario.k_ticks * tick.value
    if order.side is Side.BUY:
        eligible = bar.low <= order.limit_price - offset
    else:
        eligible = bar.high >= order.limit_price + offset
    if not eligible:
        return band_check, (FillOutcome.MISSED, MISSED_THRESHOLD)
    return band_check, None


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
    _validate_bar(bar)
    ids = [order.order_id for order in orders]
    if len(set(ids)) != len(ids):
        raise InputError("duplicate order_id in one session")
    # First-in-first-out by (submitted_at, order_id): deterministic, no RNG.
    ordered = sorted(orders, key=lambda o: (o.submitted_at.astimezone(timezone.utc), o.order_id))
    ticks = {order.order_id: _validate_order(order, bar, scenario) for order in ordered}

    session_cap = int(floor_to(Decimal(bar.volume) * scenario.volume_participation, Decimal(1)))
    remaining = session_cap
    results: list[FillResult] = []
    for order in ordered:
        tick = ticks[order.order_id]
        band_check, verdict = _screen(order, bar, scenario, tick)
        if verdict is not None:
            outcome, reason = verdict
            filled = 0
        else:
            filled = min(order.quantity, remaining)
            remaining -= filled
            if filled == 0:
                outcome, reason = FillOutcome.MISSED, MISSED_VOLUME_CAP_ZERO
            elif filled < order.quantity:
                outcome, reason = FillOutcome.PARTIAL, PARTIAL_VOLUME_CAP
            else:
                outcome, reason = FillOutcome.FILLED, FILLED_AT_LIMIT
        results.append(
            _build_result(
                order, bar, scenario, tick, filled=filled, outcome=outcome, reason=reason,
                cap=session_cap, band_check=band_check,
            )
        )
    return tuple(results)


# ---- scenario file -----------------------------------------------------------

DEFAULT_SCENARIOS_PATH = Path(__file__).parent / "schedules" / "daily_bar_fill_scenarios.json"
SCENARIOS_SCHEMA = "growin.costs.fill_scenarios/1"
_SCENARIO_FILE_KEYS = (
    "schema",
    "version",
    "assumption_note",
    "fill_price_rule",
    "validity",
    "submission_cutoff_ist",
    "scenarios",
)
_SCENARIO_KEYS = ("id", "k_ticks", "volume_participation", "phase62_gate")
_CUTOFF_TEXT = re.compile(r"(?:[01]\d|2[0-3]):[0-5]\d", re.ASCII)


@dataclass(frozen=True)
class FillScenarioSet:
    version: str
    scenarios_hash: str
    assumption_note: str
    fill_price_rule: str
    submission_cutoff: time
    scenarios: tuple[FillScenario, ...]

    def get(self, scenario_id: str) -> FillScenario:
        for scenario in self.scenarios:
            if scenario.scenario_id == scenario_id:
                return scenario
        raise InputError(f"unknown fill scenario {scenario_id!r}")

    def all(self) -> tuple[FillScenario, ...]:
        return self.scenarios

    def gate(self) -> FillScenario:
        for scenario in self.scenarios:
            if scenario.phase62_gate:
                return scenario
        raise ScheduleError("no scenario is flagged as the Phase 62 gate")


def _exact(raw: Mapping[str, Any], keys: tuple[str, ...], path: str) -> None:
    for key in raw:
        if key not in keys:
            raise ScheduleError(f"{path}.{key}: unknown key")
    for key in keys:
        if key not in raw:
            raise ScheduleError(f"{path}.{key}: missing required key")


def _text(raw: Mapping[str, Any], key: str, path: str) -> str:
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise ScheduleError(f"{path}.{key}: expected a non-empty string")
    return value


def load_fill_scenarios(path: Path | None = None) -> FillScenarioSet:
    source = DEFAULT_SCENARIOS_PATH if path is None else Path(path)
    with decimal.localcontext(COST_CONTEXT):
        raw = load_strict_json(source.read_text(encoding="utf-8"), ScheduleError, source.name)
        if not isinstance(raw, dict):
            raise ScheduleError("$: expected an object")
        _exact(raw, _SCENARIO_FILE_KEYS, "$")
        if raw["schema"] != SCENARIOS_SCHEMA:
            raise ScheduleError(f"$.schema: expected {SCENARIOS_SCHEMA!r}")
        version = _text(raw, "version", "$")
        note = _text(raw, "assumption_note", "$")
        _text(raw, "validity", "$")
        if _text(raw, "fill_price_rule", "$") != "at_limit":
            raise ScheduleError("$.fill_price_rule: only 'at_limit' is supported")
        cutoff_text = _text(raw, "submission_cutoff_ist", "$")
        if not _CUTOFF_TEXT.fullmatch(cutoff_text):
            raise ScheduleError("$.submission_cutoff_ist: expected HH:MM")
        cutoff = time.fromisoformat(cutoff_text)
        items = raw["scenarios"]
        if not isinstance(items, list) or not items:
            raise ScheduleError("$.scenarios: expected a non-empty list")
        scenarios_hash = sha256_hex(canonical_json(raw))
        built: list[FillScenario] = []
        for n, item in enumerate(items):
            item_path = f"$.scenarios[{n}]"
            if not isinstance(item, dict):
                raise ScheduleError(f"{item_path}: expected an object")
            _exact(item, _SCENARIO_KEYS, item_path)
            k_ticks = item["k_ticks"]
            if isinstance(k_ticks, bool) or not isinstance(k_ticks, int) or k_ticks < 1:
                raise ScheduleError(f"{item_path}.k_ticks: expected an integer of at least 1")
            raw_participation = item["volume_participation"]
            if not isinstance(raw_participation, str):
                raise ScheduleError(f"{item_path}.volume_participation: numerics must be JSON strings")
            try:
                participation = strict_decimal(raw_participation, f"{item_path}.volume_participation")
            except InputError as exc:
                raise ScheduleError(str(exc)) from exc
            if participation <= 0 or participation > 1:
                raise ScheduleError(f"{item_path}.volume_participation: must be above 0 and at most 1")
            if not isinstance(item["phase62_gate"], bool):
                raise ScheduleError(f"{item_path}.phase62_gate: expected a boolean")
            built.append(
                FillScenario(
                    _text(item, "id", item_path),
                    k_ticks,
                    participation,
                    item["phase62_gate"],
                    cutoff,
                    note,
                    version,
                    scenarios_hash,
                )
            )
        ids = [scenario.scenario_id for scenario in built]
        if len(set(ids)) != len(ids):
            raise ScheduleError("$.scenarios: duplicate scenario id")
        if sum(1 for scenario in built if scenario.phase62_gate) != 1:
            raise ScheduleError("$.scenarios: exactly one scenario must set phase62_gate")
    return FillScenarioSet(version, scenarios_hash, note, "at_limit", cutoff, tuple(built))
