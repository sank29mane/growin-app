"""Round-trip hurdle, prepaid-credit cash view and D-11 account overhead.

All balances and prices here are SYNTHETIC labelled test values.
"""

from __future__ import annotations

import dataclasses
import inspect
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from costs.charges import RoundTripEstimate, estimate_delivery_round_trip, price_trade_day
from costs.core import CostModelError, InputError, Side, TradeFill
from costs.overhead import (
    METRIC_NAME,
    PrepaidCredit,
    apply_prepaid_credit,
    brokerage_gst_attributable,
    incremental_account_overhead,
    return_after_incremental_account_overhead,
)
from costs.schedule import PricingBasis, load_schedule_set

D = Decimal
ISIN = "INE0TEST0001"
VERSION = "icici-prime9999-ivalue-nse-cash-2024-10-01.r1"
COSTS_DIR = Path(__file__).resolve().parents[2] / "backend" / "costs"


def hurdle(**overrides):
    kwargs = dict(
        workspace="india", currency="INR", isin=ISIN, exchange="NSE", quantity=70,
        buy_price=D("100.00"), sell_price=D("100.00"), buy_date=date(2026, 10, 5),
        sell_date=date(2026, 10, 6), schedules=load_schedule_set(), pricing_basis=PricingBasis.trade_date(),
    )
    kwargs.update(overrides)
    return estimate_delivery_round_trip(**kwargs)


def day(order_id, side, trade_date, quantity=70, price="100.00"):
    schedule = load_schedule_set().for_date(trade_date)
    return price_trade_day(
        [TradeFill(order_id, ISIN, "NSE", side, quantity, D(price), trade_date)],
        schedule, workspace="india", currency="INR", pricing_basis=PricingBasis.trade_date(),
    )


def worked_days():
    return [day("b", Side.BUY, date(2026, 10, 5)), day("s", Side.SELL, date(2026, 10, 6))]


def credit(balance="100.00", expires=date(2027, 1, 14), source="test-synthetic-balance"):
    return PrepaidCredit(D(balance), expires, source)


# ---- hurdle ------------------------------------------------------------------


def test_round_trip_hurdle_matches_the_worked_example():
    estimate = hurdle()
    assert estimate.total == D("50.73")
    assert estimate.cost_bps_of_buy_value == D("72.47")
    assert estimate.buy_day.schedule_version == VERSION
    assert estimate.sell_day.schedule_version == VERSION
    assert estimate.buy_day.schedule_hash == estimate.sell_day.schedule_hash
    assert len(estimate.buy_day.schedule_hash) == 64


def test_same_day_or_reversed_dates_are_not_a_hurdle_input():
    with pytest.raises(InputError):
        hurdle(sell_date=date(2026, 10, 5))
    with pytest.raises(InputError):
        hurdle(sell_date=date(2026, 10, 2))


def test_hurdle_signature_has_no_credit_or_prepaid_input():
    names = inspect.signature(estimate_delivery_round_trip).parameters
    assert not [name for name in names if "credit" in name or "prepaid" in name]


def test_hurdle_has_no_drift_or_slippage_input():
    banned = ("drift", "slippage", "decision", "reference")
    params = inspect.signature(estimate_delivery_round_trip).parameters
    assert not [name for name in params if any(word in name for word in banned)]
    fields = [f.name for f in dataclasses.fields(RoundTripEstimate)]
    assert not [name for name in fields if any(word in name for word in banned)]
    estimate = hurdle()
    assert estimate.total == estimate.buy_day.total + estimate.sell_day.total


def test_hurdle_is_deterministic():
    assert hurdle().estimate_hash == hurdle().estimate_hash


def test_hurdle_fails_closed_on_wrong_workspace():
    with pytest.raises(CostModelError):
        hurdle(workspace="uk")


# ---- prepaid credit cash view ------------------------------------------------


def test_brokerage_gst_attributable_for_the_worked_buy_day():
    schedule = load_schedule_set().for_date(date(2026, 10, 5))
    assert brokerage_gst_attributable(day("b", Side.BUY, date(2026, 10, 5)), schedule) == D("0.88")


def test_credit_view_reduces_cash_charges_without_touching_the_hurdle():
    view = apply_prepaid_credit(worked_days(), credit("100.00"), schedules=load_schedule_set())
    assert view.credit_source == "test-synthetic-balance"
    assert view.full_total == D("50.73")
    assert view.credit_applied == D("11.56")
    assert view.with_credit_total == D("39.17")
    assert view.credit_remaining == D("88.44")
    assert [d.credit_applied for d in view.days] == [D("5.78"), D("5.78")]
    assert hurdle().total == D("50.73")


def test_small_balance_is_exhausted_and_never_negative():
    view = apply_prepaid_credit(worked_days(), credit("3.00"), schedules=load_schedule_set())
    assert view.credit_applied == D("3.00")
    assert view.with_credit_total == D("47.73")
    assert view.credit_remaining == D("0.00")
    assert [d.credit_applied for d in view.days] == [D("3.00"), D("0.00")]


def test_credit_expires_after_its_date():
    days = [day("b", Side.BUY, date(2027, 1, 14)), day("s", Side.SELL, date(2027, 1, 15))]
    view = apply_prepaid_credit(days, credit("100.00"), schedules=load_schedule_set())
    assert [d.credit_applied for d in view.days] == [D("5.78"), D("0.00")]
    assert view.credit_remaining == D("94.22")


def test_credit_walks_days_in_date_order_regardless_of_input_order():
    forward = apply_prepaid_credit(worked_days(), credit("3.00"), schedules=load_schedule_set())
    backward = apply_prepaid_credit(list(reversed(worked_days())), credit("3.00"), schedules=load_schedule_set())
    assert forward == backward


def test_credit_rejects_bad_inputs():
    with pytest.raises(CostModelError):
        credit("-1.00")
    with pytest.raises(CostModelError):
        credit(source="")
    tampered = dataclasses.replace(worked_days()[0], schedule_hash="0" * 64)
    with pytest.raises(CostModelError):
        apply_prepaid_credit([tampered], credit(), schedules=load_schedule_set())
    with pytest.raises(CostModelError):
        brokerage_gst_attributable(tampered, load_schedule_set().for_date(date(2026, 10, 5)))


# ---- D-11 overhead disclosure ------------------------------------------------


def disclosure(**overrides):
    kwargs = dict(
        schedule=load_schedule_set().versions[0],
        period_start=date(2026, 11, 1),
        period_end=date(2027, 1, 30),
    )
    kwargs.update(overrides)
    return incremental_account_overhead(**kwargs)


def test_overhead_disclosure_without_the_flag():
    result = disclosure(pilot_keeps_account_open=False)
    assert result.metric_name == "return after incremental account overhead"
    assert METRIC_NAME == result.metric_name
    assert result.amc_annual_incl_gst == D("354.00")
    assert result.incremental_allocation == D("0.00")
    assert [fee.item for fee in result.plan_fees_excluded] == ["prime_9999_one_time_fee", "ivalue_one_time_fee"]
    assert result.excluded_from_delivery_model == ("shares_as_margin_interest",)
    assert result.schedule_version == VERSION


def test_overhead_allocation_with_the_flag():
    result = disclosure(pilot_keeps_account_open=True)
    assert result.incremental_allocation == D("88.26")
    assert result.amc_annual_incl_gst == D("354.00")


def test_amc_applies_only_when_pilot_keeps_account_open():
    result = disclosure()
    assert result.pilot_keeps_account_open is False
    assert result.incremental_allocation == D("0.00")
    assert result.amc_annual_incl_gst == D("354.00")
    default = inspect.signature(incremental_account_overhead).parameters["pilot_keeps_account_open"].default
    assert default is False
    for path in COSTS_DIR.rglob("*.py"):
        assert "pilot_is_reason_account_stays_open" not in path.read_text(encoding="utf-8")


def test_overhead_period_must_be_ordered():
    with pytest.raises(InputError):
        disclosure(period_start=date(2027, 1, 30), period_end=date(2026, 11, 1))


def test_return_after_incremental_account_overhead():
    without = disclosure(pilot_keeps_account_open=False)
    with_amc = disclosure(pilot_keeps_account_open=True)
    assert return_after_incremental_account_overhead(
        net_pnl_after_charges=D("1000"), capital=D("50000"), overhead=without
    ) == D("0.020000")
    assert return_after_incremental_account_overhead(
        net_pnl_after_charges=D("1000"), capital=D("50000"), overhead=with_amc
    ) == D("0.018235")
    with pytest.raises(InputError):
        return_after_incremental_account_overhead(net_pnl_after_charges=D("1000"), capital=D("0"), overhead=without)
    parameters = inspect.signature(return_after_incremental_account_overhead).parameters
    assert not [name for name in parameters if "fee" in name or "plan" in name or "interest" in name]
