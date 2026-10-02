"""Phase 60 tracer: one delivery buy and one next-session sell, end to end.

Every price here is a SYNTHETIC test value computed from published rates, not
a contract note.
"""

from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal

import pytest

from costs.charges import ContractNoteEstimate
from costs.core import (
    LINE_ORDER,
    CostModelError,
    InputError,
    Side,
    TickSizeUnavailable,
    canonical_json,
    sha256_hex,
)
from costs.fills import (
    MISSED_THRESHOLD,
    MISSED_VOLUME_CAP_ZERO,
    PARTIAL_VOLUME_CAP,
    FillOutcome,
    FillScenario,
    LimitOrder,
    PriceBand,
    SessionBar,
    TickSize,
)
from costs.run import simulate_and_price
from costs.schedule import PricingBasis, load_schedule_set

D = Decimal
ISIN = "INE0TEST0001"
VERSION = "icici-prime9999-ivalue-nse-cash-2024-10-01.r1"
IST_OFFSET = "+05:30"
TICK = TickSize(D("0.01"), date(2025, 4, 15), "test-explicit", sha256_hex("test-explicit"))
SCENARIO = FillScenario(
    "base",
    1,
    D("0.01"),
    False,
    time(9, 0),
    "Simulation assumptions, not evidence of actual fill probability.",
    "test-inline",
    sha256_hex("test-inline"),
)


def band(day: date) -> PriceBand:
    return PriceBand("fixed", D("90.00"), D("110.00"), day, "test-band", sha256_hex("test-band"))


def buy_bar(*, low="99.50", volume=100000) -> SessionBar:
    return SessionBar(
        ISIN, "NSE", date(2026, 10, 5), D("100.20"), D("101.00"), D(low), D("100.40"),
        volume, "raw", band(date(2026, 10, 5)), "test-bar",
    )


def sell_bar(*, volume=80000) -> SessionBar:
    return SessionBar(
        ISIN, "NSE", date(2026, 10, 6), D("100.10"), D("100.60"), D("99.70"), D("100.30"),
        volume, "raw", band(date(2026, 10, 6)), "test-bar",
    )


def buy_order(*, reference="99.80", tick=TICK) -> LimitOrder:
    return LimitOrder(
        "tr-buy", ISIN, "NSE", Side.BUY, 70, D("100.00"), D(reference), date(2026, 10, 5),
        datetime.fromisoformat("2026-10-02T18:00:00" + IST_OFFSET), date(2026, 10, 2), tick,
    )


def sell_order(*, reference="100.40") -> LimitOrder:
    return LimitOrder(
        "tr-sell", ISIN, "NSE", Side.SELL, 70, D("100.00"), D(reference), date(2026, 10, 6),
        datetime.fromisoformat("2026-10-05T18:00:00" + IST_OFFSET), date(2026, 10, 5), TICK,
    )


def run(*, buy_ref="99.80", sell_ref="100.40", bars=None, orders=None, workspace="india",
        currency="INR", scenario=SCENARIO):
    return simulate_and_price(
        workspace=workspace,
        currency=currency,
        orders=orders if orders is not None else [buy_order(reference=buy_ref), sell_order(reference=sell_ref)],
        bars=bars if bars is not None else [buy_bar(), sell_bar()],
        scenario=scenario,
        schedules=load_schedule_set(),
        pricing_basis=PricingBasis.trade_date(),
    )


def lines(estimate: ContractNoteEstimate) -> dict[str, Decimal]:
    return {line.name: line.amount for line in estimate.lines}


BUY_DAY = {
    "brokerage": D("4.90"), "exchange_transaction": D("0.21"), "sebi_fee": D("0.01"),
    "ipft": D("0.00"), "gst": D("0.92"), "stt": D("7.00"), "stamp_duty": D("1.05"),
    "dp_charge": D("0.00"), "dp_gst": D("0.00"),
}
SELL_DAY = {
    "brokerage": D("4.90"), "exchange_transaction": D("0.21"), "sebi_fee": D("0.01"),
    "ipft": D("0.00"), "gst": D("0.92"), "stt": D("7.00"), "stamp_duty": D("0.00"),
    "dp_charge": D("20.00"), "dp_gst": D("3.60"),
}


def test_worked_example_end_to_end():
    result = run()
    buy_fill, sell_fill = result.fills
    for fill in (buy_fill, sell_fill):
        assert fill.outcome is FillOutcome.FILLED
        assert fill.filled_quantity == 70
        assert fill.fill_price == D("100.00")
    buy_day, sell_day = result.days
    assert lines(buy_day) == BUY_DAY
    assert lines(sell_day) == SELL_DAY
    assert tuple(line.name for line in buy_day.lines) == LINE_ORDER
    assert buy_day.total == D("14.09")
    assert sell_day.total == D("36.64")
    assert result.total_charges == D("50.73")


def test_lineage_on_days_and_run():
    schedule = load_schedule_set().for_date(date(2026, 10, 5))
    result = run()
    assert len(schedule.schedule_hash) == 64
    assert schedule.schedule_hash == schedule.schedule_hash.lower()
    for day in result.days:
        assert day.schedule_version == VERSION
        assert day.schedule_hash == schedule.schedule_hash
    assert result.schedule_refs == (
        (date(2026, 10, 5), VERSION, schedule.schedule_hash),
        (date(2026, 10, 6), VERSION, schedule.schedule_hash),
    )
    assert result.workspace == "india"
    assert result.currency == "INR"


def test_decision_drift_and_slippage_values():
    buy_fill, sell_fill = run().fills
    assert buy_fill.decision_drift_per_share == D("0.20")
    assert buy_fill.decision_drift_bps == D("20.04")
    assert buy_fill.drift_adverse is True
    assert buy_fill.execution_slippage_per_share == D("0.00")
    assert buy_fill.execution_slippage_bps == D("0.00")
    assert buy_fill.forgone_improvement_per_share == D("0.00")
    assert sell_fill.decision_drift_per_share == D("0.40")
    assert sell_fill.decision_drift_bps == D("39.84")
    assert sell_fill.drift_adverse is True
    assert sell_fill.execution_slippage_per_share == D("0.00")
    assert sell_fill.execution_slippage_bps == D("0.00")
    assert sell_fill.forgone_improvement_per_share == D("0.10")


def test_decision_drift_is_reported_never_charged():
    base = run()
    moved = run(buy_ref="95.00", sell_ref="104.00")
    moved_buy, moved_sell = moved.fills
    assert moved_buy.decision_drift_per_share == D("5.00")
    assert moved_sell.decision_drift_per_share == D("4.00")
    assert moved_buy.decision_drift_per_share != base.fills[0].decision_drift_per_share
    for before, after in zip(base.days, moved.days):
        assert lines(before) == lines(after)
        assert before.total == after.total
    assert [d.total for d in moved.days] == [D("14.09"), D("36.64")]
    assert moved.total_charges == D("50.73")
    assert moved_buy.execution_slippage_per_share == D("0.00")
    assert moved_sell.execution_slippage_per_share == D("0.00")
    for name in LINE_ORDER:
        assert "drift" not in name and "slippage" not in name
    for day in moved.days:
        for line in day.lines:
            assert "drift" not in line.name and "slippage" not in line.name


def test_rerun_identity():
    first = run()
    second = run()
    assert canonical_json(first) == canonical_json(second)
    assert first.run_hash == second.run_hash
    assert len(first.run_hash) == 64


def test_provenance_on_fills():
    for fill in run().fills:
        assert fill.scenarios_version == "test-inline"
        assert fill.scenarios_hash == SCENARIO.scenarios_hash
        assert fill.tick_source_hash == sha256_hex("test-explicit")


def test_tick_source_hash_must_be_sha256_hex():
    for bad in ("abc", "G" * 64, "A" * 64, ""):
        with pytest.raises(InputError):
            TickSize(D("0.01"), date(2025, 4, 15), "test-explicit", bad)


def test_threshold_miss():
    result = run(bars=[buy_bar(low="100.00"), sell_bar()])
    buy_fill = result.fills[0]
    assert buy_fill.outcome is FillOutcome.MISSED
    assert buy_fill.reason_code == MISSED_THRESHOLD
    assert buy_fill.filled_quantity == 0
    assert buy_fill.to_trade_fill() is None
    assert [d.trade_date for d in result.days] == [date(2026, 10, 6)]


def test_volume_cap_partial_and_zero():
    partial = run(bars=[buy_bar(volume=5000), sell_bar()]).fills[0]
    assert partial.outcome is FillOutcome.PARTIAL
    assert partial.filled_quantity == 50
    assert partial.reason_code == PARTIAL_VOLUME_CAP
    zero = run(bars=[buy_bar(volume=99), sell_bar()]).fills[0]
    assert zero.outcome is FillOutcome.MISSED
    assert zero.reason_code == MISSED_VOLUME_CAP_ZERO
    assert zero.filled_quantity == 0


def test_missing_tick_fails_closed():
    with pytest.raises(TickSizeUnavailable):
        run(orders=[buy_order(tick=None), sell_order()])


def test_wrong_workspace_or_currency_fails_closed():
    with pytest.raises(CostModelError):
        run(workspace="uk")
    with pytest.raises(CostModelError):
        run(currency="GBP")


def test_order_without_bar_raises():
    with pytest.raises(InputError):
        run(bars=[buy_bar()])


def test_duplicate_bars_raise():
    with pytest.raises(InputError):
        run(bars=[buy_bar(), buy_bar(), sell_bar()])
