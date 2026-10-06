"""AC-15: cost hurdle, no-trade band, minimum hold and turnover (D-15)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from costs.charges import estimate_delivery_round_trip
from costs.core import InputError

from strategy_india import hurdle
from strategy_india.engine import simulate_segment
from strategy_india.errors import ParamsError
from strategy_india.folds import Observation
from strategy_india.holdout import HoldoutRange
from strategy_india.params import parse_params, placeholder_params
from strategy_india.registry import HASH_FIELDS
from strategy_india.report import summarise_segment

from test_strategy_india_support import (
    SCHEDULE_VERSION,
    SESSION_START,
    costs_inputs,
    default_names,
    etf_names,
    make_context,
    make_rows,
    params,
    weekday_sessions,
)

_, SCHEDULES, _, BASIS = costs_inputs()
DAY = date(2025, 6, 2)


def decide(*, z="2", slope="0.01", qty=20, price="100", p=None):
    return hurdle.evaluate_candidate(
        z=Decimal(z), slope=Decimal(slope), isin="INE000A01000", quantity=qty, reference_price=Decimal(price),
        session_date=DAY, params=p or params(), schedules=SCHEDULES, pricing_basis=BASIS,
    )


def test_expected_edge_below_the_round_trip_cost_plus_band_is_not_traded():
    small = decide(z="1", qty=20)  # Rs 2,000: the fixed DP charge alone is over 1%
    assert small.cost_fraction > Decimal("0.01") and small.edge == Decimal("0.01")
    assert not small.clears and small.net_edge < 0
    big = decide(z="1", qty=400)  # Rs 40,000: the same edge now clears
    assert big.cost_fraction < Decimal("0.01") and big.clears
    assert big.cost_fraction < small.cost_fraction, "the hurdle is evaluated at the candidate's own size"


def test_the_hurdle_uses_the_60_round_trip_estimate_at_full_brokerage():
    verdict = decide(qty=400)
    rt = estimate_delivery_round_trip(
        workspace="india", currency="INR", isin="INE000A01000", exchange="NSE", quantity=400, buy_price=Decimal("100"),
        sell_price=Decimal("100"), buy_date=DAY, sell_date=date(2025, 6, 4), schedules=SCHEDULES, pricing_basis=BASIS,
    )
    assert verdict.cost_total == rt.total
    brokerage = sum((o.amount for o in rt.buy_day.order_brokerage + rt.sell_day.order_brokerage), Decimal(0))
    assert brokerage == Decimal("56.00")  # 0.07% on each 40,000 leg: no prepaid credit, no minimum


def test_the_no_trade_band_is_added_to_the_hurdle():
    edge_only = decide(qty=400, slope="0.0045")  # edge 0.9% against about 0.5% cost
    assert edge_only.net_edge > 0 and edge_only.clears
    banded = decide(qty=400, slope="0.0045", p=params(no_trade_band="0.01"))
    assert banded.net_edge == edge_only.net_edge and not banded.clears
    assert not decide(z="-1").clears and decide(z="-1").edge == 0


def test_a_same_day_round_trip_raises():
    with pytest.raises(InputError):
        hurdle.round_trip_cost(isin="X", quantity=10, price=Decimal("100"), buy_date=DAY, sell_date=DAY,
                               schedules=SCHEDULES, pricing_basis=BASIS)
    with pytest.raises(InputError):
        hurdle.round_trip_cost(isin="X", quantity=10, price=Decimal("100"), buy_date=DAY, sell_date=date(2025, 6, 1),
                               schedules=SCHEDULES, pricing_basis=BASIS)


def test_minimum_hold_is_at_least_one_session():
    raw = placeholder_params()
    raw["min_hold_sessions"] = 0
    with pytest.raises(ParamsError):
        parse_params(raw)
    forced = params().model_copy(update={"min_hold_sessions": 0})  # bypass validation: the hurdle still refuses
    with pytest.raises(ParamsError):
        decide(p=forced)


def test_score_to_edge_map_hash_changes_with_the_map_and_is_a_registered_hash():
    base = hurdle.hurdle_map_sha256(params())
    assert base == hurdle.hurdle_map_sha256(params())
    assert base != hurdle.hurdle_map_sha256(params(edge_map={"mode": "fixed", "slope": "0.02", "min_obs": 5, "ridge": "1"}))
    assert base != hurdle.hurdle_map_sha256(params(no_trade_band="0.002"))
    assert base != hurdle.hurdle_map_sha256(params(min_hold_sessions=3))
    assert "hurdle_map_sha256" in HASH_FIELDS


def obs(score, ret):
    return Observation(DAY, DAY, Decimal(score), Decimal(ret))


def test_edge_slope_is_a_ridge_fit_on_training_trades_with_a_prior_fallback():
    spec = params(edge_map={"mode": "fit", "slope": "0.01", "min_obs": 3, "ridge": "1"}).edge_map
    slope, used, fitted = hurdle.fit_slope([obs(1, "0.02"), obs(2, "0.04"), obs(1, "0.02")], spec)
    assert fitted and used == 3 and slope == Decimal("0.12") / Decimal("7")  # (0.02+0.08+0.02) / (1+4+1+1)
    assert hurdle.fit_slope([obs(1, "0.02")], spec) == (Decimal("0.01"), 1, False)  # too few: the registered prior
    assert hurdle.fit_slope([obs(1, "-0.5")] * 3, spec)[0] == 0  # never negative
    fixed = params().edge_map
    assert hurdle.fit_slope([obs(1, "0.02")] * 5, fixed) == (Decimal("0.01"), 0, False)


def _segment(**overrides):
    sessions = weekday_sessions(SESSION_START, 150)
    rows = make_rows(sessions, default_names(8) + etf_names())
    holdout = HoldoutRange(sessions[-20], sessions[-1])
    ctx = make_context(rows, holdout, params_obj=params(**overrides))
    dev = ctx.view.sessions()
    return ctx, simulate_segment(ctx, sessions=dev, scenario=ctx.scenarios.gate(), slope=Decimal("0.01"),
                                 regime_cash=None, mode="base", fold="x")


def test_the_engine_does_not_trade_candidates_that_fail_the_hurdle():
    _, traded = _segment()
    _, blocked = _segment(no_trade_band="0.5")
    assert traded.entries > 0
    assert blocked.entries == 0 and blocked.fills == () and blocked.hurdle_rejections > 0


def test_realised_swaps_per_week_and_cost_drag_are_reported_against_the_budget():
    ctx, seg = _segment()
    summary = summarise_segment(seg, ctx.params)
    assert summary.swaps == seg.swaps > 0
    assert summary.swaps_per_week == Decimal(seg.swaps) * 5 / len(seg.sessions)
    assert summary.turnover_budget_swaps_per_week == Decimal("1")
    assert summary.within_turnover_budget == (summary.swaps_per_week <= Decimal("1"))
    assert summary.cost_drag == seg.charges_total / seg.start_equity > 0
    tight = summarise_segment(seg, params(turnover_budget_swaps_per_week="0.01"))
    assert not tight.within_turnover_budget
    assert SCHEDULE_VERSION in {ref[0] for ref in summary.schedule_refs}
