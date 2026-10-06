"""AC-18: D-20 engine side (operator-confirmed 2026-10-07). Events tagged dividend_amount_unknown, price return across the ex-date."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from costs.core import Side

from strategy_india import study
from strategy_india.data import DatasetView, DividendEvents, DividendUnknownEvent
from strategy_india.engine import simulate_segment
from strategy_india.holdout import HoldoutRange
from strategy_india.signals import MODE_BASE, MODE_SENSITIVITY, SignalTable

from test_strategy_india_portfolio import new_book, order, run, sbar, with_position, D
from test_strategy_india_support import (
    SESSION_START,
    default_names,
    etf_names,
    limits,
    make_context,
    make_rows,
    params,
    study_inputs,
    weekday_sessions,
)

FAR = HoldoutRange(date(2030, 1, 1), date(2030, 2, 1))
SESSIONS = weekday_sessions(SESSION_START, 150)
ANCHOR = "INE000A01000"
EX = SESSIONS[100]
GAP = {(ANCHOR, EX): Decimal("-0.15")}
EVENTS = DividendEvents([DividendUnknownEvent(ANCHOR, "EV1", EX)])
TOL = Decimal("1e-20")
FACTOR = Decimal("0.98")  # the sealed D-19 factor in the example criteria fixture


def _view(rows):
    return DatasetView.from_rows(rows, holdout=FAR)


def _bars(rows, anchor=ANCHOR):
    return {r.trade_date: r for r in rows if r.anchor_isin == anchor}


def test_pre_ex_date_bars_give_signals_and_the_ex_date_gap_is_zero_in_signal_returns():
    rows = make_rows(SESSIONS, default_names(5), ex_gaps=GAP)
    by_day = _bars(rows)
    prev, ex = by_day[SESSIONS[99]], by_day[EX]
    assert ex.adj_close / prev.adj_close - 1 < Decimal("-0.1"), "the raw and adjusted series both carry the gap"
    base = SignalTable(_view(rows), params(), EVENTS, mode=MODE_BASE)
    plain = SignalTable(_view(rows), params(), DividendEvents(), mode=MODE_BASE)
    assert base.raw_score(ANCHOR, SESSIONS[99]) is not None  # a pre-ex-date bar gives a signal
    assert abs(base.last_gap_neutral_return(ANCHOR, EX) - (ex.adj_close / ex.adj_open - 1)) < TOL  # gap excluded
    assert abs(plain.last_gap_neutral_return(ANCHOR, EX) - (ex.adj_close / prev.adj_close - 1)) < TOL
    # volatility windows see the same zeroed gap: the squared-return prefix sum barely moves at the ex-date
    b, p = base._series[ANCHOR], plain._series[ANCHOR]
    i = b.dates.index(EX)
    assert b.s2[i] - b.s2[i - 1] < Decimal("0.001") and p.s2[i] - p.s2[i - 1] > Decimal("0.015")
    assert base.raw_score(ANCHOR, SESSIONS[104]) > plain.raw_score(ANCHOR, SESSIONS[104])
    other = "INE000A01001"  # a name without an event is untouched
    assert base.raw_score(other, SESSIONS[104]) == plain.raw_score(other, SESSIONS[104])


def test_the_sensitivity_run_assumes_a_two_percent_dividend_on_adjusted_prices_before_the_ex_date():
    rows = make_rows(SESSIONS, default_names(5), ex_gaps=GAP)
    by_day = _bars(rows)
    sens = SignalTable(_view(rows), params(), EVENTS, mode=MODE_SENSITIVITY, sensitivity_factor=FACTOR)
    expected = by_day[EX].adj_close / (FACTOR * by_day[SESSIONS[99]].adj_close) - 1
    assert abs(sens.last_gap_neutral_return(ANCHOR, EX) - expected) < TOL
    plain_step = _bars(rows)[SESSIONS[50]].adj_close / by_day[SESSIONS[49]].adj_close - 1
    assert abs(sens.last_gap_neutral_return(ANCHOR, SESSIONS[50]) - plain_step) < TOL  # before the ex-date nothing moves


def test_with_no_tag_the_quarantine_rule_applies_unchanged():
    rows = make_rows(SESSIONS, default_names(5), ex_gaps=GAP, quarantined=[(ANCHOR, d) for d in SESSIONS[:EX and 100]])
    table = SignalTable(_view(rows), params(), DividendEvents(), mode=MODE_BASE)
    assert all(table.raw_score(ANCHOR, d) is None for d in SESSIONS[40:100])  # no signal from quarantined bars
    assert table.raw_score(ANCHOR, SESSIONS[149]) is not None
    ctx = make_context(rows, FAR)
    assert not ctx.events  # no tag: no sensitivity run and no gap handling


# ---- stop logic and portfolio rules --------------------------------------------------------------
def test_an_ex_date_gap_that_alone_would_trip_the_position_stop_does_not():
    gap_open, gap_close = Decimal("85"), Decimal("85.5")
    plain = with_position(100)
    plain.mark(D[2], {"A": gap_close})
    plain.update_risk(D[2], 2, regime_cash=False)
    assert plain.positions["A"].exit.reason == "stop"  # without the event the -15% gap stops the name out
    tagged = with_position(100)
    tagged.mark(D[2], {"A": gap_close}, ex_date_open={"A": gap_open})
    tagged.update_risk(D[2], 2, regime_cash=False)
    assert tagged.positions["A"].exit is None
    assert tagged.positions["A"].stop_ratio > Decimal("1")


def test_a_position_bought_on_the_ex_date_has_no_gap_to_exclude():
    book = new_book()
    order(book, Side.BUY, 100, "100.00", D[0], D[1])
    run(book, D[1], 1, sbar(D[1]))
    book.mark(D[1], {"A": Decimal("85")}, ex_date_open={"A": Decimal("101")})  # entry session: close over the fill price
    book.update_risk(D[1], 1, regime_cash=False)
    assert book.positions["A"].exit.reason == "stop"


def test_minus_8_and_minus_15_still_fire_on_raw_marks_across_an_ex_date():
    halting = with_position(400)  # 40,000 of 50,000
    halting.mark(D[2], {"A": Decimal("86")}, ex_date_open={"A": Decimal("85.5")})  # -14 on the position, -11 percent on the book
    halting.update_risk(D[2], 2, regime_cash=False)
    assert halting.halted and halting.positions["A"].exit.reason == "halve"
    flat = with_position(450)
    flat.mark(D[2], {"A": Decimal("80")}, ex_date_open={"A": Decimal("79.8")})
    flat.update_risk(D[2], 2, regime_cash=False)
    assert flat.flattened and flat.positions["A"].exit.reason == "flatten"
    assert flat.positions["A"].stop_ratio > Decimal("0.99"), "the stop never saw the gap; the portfolio rule did"


def test_engine_does_not_stop_out_a_name_on_its_ex_date_gap_but_does_without_the_tag():
    plain_rows = make_rows(SESSIONS, default_names(8) + etf_names())
    holdout = HoldoutRange(SESSIONS[-20], SESSIONS[-1])
    ctx0 = make_context(plain_rows, holdout)
    dev = ctx0.view.sessions()
    control = simulate_segment(ctx0, sessions=dev, scenario=ctx0.scenarios.gate(), slope=Decimal("0.01"),
                               regime_cash=None, mode=MODE_BASE, fold="c")
    trade = next(t for t in control.closed if t.entry_date > SESSIONS[40] and (dev.index(t.exit_date) - dev.index(t.entry_date)) > 6)
    ex = dev[dev.index(trade.entry_date) + 3]
    gap = {(trade.anchor_isin, ex): Decimal("-0.15")}
    rows = make_rows(SESSIONS, default_names(8) + etf_names(), ex_gaps=gap)

    def exit_reason(events):
        ctx = make_context(rows, holdout, events=events)
        res = simulate_segment(ctx, sessions=dev, scenario=ctx.scenarios.gate(), slope=Decimal("0.01"), regime_cash=None,
                               mode=MODE_BASE, fold="g")
        hit = [t for t in res.closed if t.anchor_isin == trade.anchor_isin and t.entry_date == trade.entry_date]
        return hit[0].exit_reason if hit else None

    assert exit_reason(DividendEvents()) == "stop"
    tagged = exit_reason(DividendEvents([DividendUnknownEvent(trade.anchor_isin, "EV1", ex)]))
    assert tagged != "stop"


# ---- the report -----------------------------------------------------------------------------------
def test_report_carries_the_sensitivity_rerun_beside_the_base_run_and_lists_the_events(tmp_path):
    sessions = weekday_sessions(SESSION_START, 400)
    ex = sessions[250]  # inside the last fold's test window
    gaps = {(ANCHOR, ex): Decimal("-0.06")}
    inputs = study_inputs(tmp_path, ex_gaps=gaps, events=DividendEvents([DividendUnknownEvent(ANCHOR, "EV1", ex)]))
    study.register(inputs, hypothesis="h")
    report = study.run_research(inputs, expected_head=inputs.registry.head_hash())
    assert report.dividend.sensitivity_factor == Decimal("0.98") and report.dividend.sensitivity_run_present
    assert report.dividend.events_tagged_dividend_amount_unknown == [
        {"anchor_isin": ANCHOR, "event_id": "EV1", "ex_date": ex.isoformat()}]
    for unit in report.units:
        assert unit.dividend_sensitivity is not None and unit.dividend_sensitivity.mode == "sens2pct"
        assert unit.scenarios["pessimistic"].mode == "base"
    changed = [u for u in report.units if u.dividend_sensitivity.net_return != u.scenarios["pessimistic"].net_return]
    assert changed, "the 2% assumption moves at least one fold, so the sensitivity is a real second run"


def test_without_tagged_events_the_report_has_no_sensitivity_run(tmp_path):
    inputs = study_inputs(tmp_path)
    study.register(inputs, hypothesis="h")
    report = study.run_research(inputs, expected_head=inputs.registry.head_hash())
    assert not report.dividend.sensitivity_run_present and report.dividend.events_tagged_dividend_amount_unknown == []
    assert all(unit.dividend_sensitivity is None for unit in report.units)
