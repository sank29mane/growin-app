"""R1 review fixes: exit costs on open positions at the holdout end, the confirmed D-19 label, one decimal context."""

from __future__ import annotations

import decimal
import json
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest

from costs.core import Side

from strategy_india.engine import simulate_segment
from strategy_india.holdout import (
    CRITERIA_STATUS,
    FAIL,
    PASS,
    HoldoutRange,
    annualised_excess_return,
    annualised_return,
    annualised_swaps,
    evaluate_verdict,
)
from strategy_india.report import holdout_evidence
from strategy_india.signals import MODE_BASE

from test_strategy_india_portfolio import BASIS, D, SCHEDULES, new_book, order, run, sbar, with_position
from test_strategy_india_support import (
    SESSION_START,
    default_criteria,
    default_names,
    etf_names,
    make_context,
    make_rows,
    weekday_sessions,
)


# ---- 1. the D-19 label ------------------------------------------------------------------------------
def test_example_criteria_and_the_sealed_label_are_confirmed_not_proposed():
    criteria = default_criteria()
    assert criteria["status"] == CRITERIA_STATUS == "CONFIRMED, operator 2026-10-07"
    assert "PROPOSED" not in json.dumps(criteria) and "PROPOSED" not in CRITERIA_STATUS


# ---- 2. exit costs on open positions ----------------------------------------------------------------
def test_exit_costs_are_exactly_what_a_real_filled_exit_pays_and_the_book_is_untouched():
    book = with_position(50)
    before = (book.cash, book.equity(), book.charges_total, dict(book.positions))
    cost = book.exit_costs_at_mark(D[2], schedules=SCHEDULES, pricing_basis=BASIS)
    assert (book.cash, book.equity(), book.charges_total, dict(book.positions)) == before  # read-only
    # The same 50 shares sold for real at the same price on the same day, through the real fill path.
    order(book, Side.SELL, 50, "100.00", D[1], D[2], reason="swing_max_hold")
    charges_before = book.charges_total
    run(book, D[2], 2, sbar(D[2], high="110"))
    assert book.closed and book.positions == {}
    assert cost > 0 and cost == book.charges_total - charges_before


def test_no_open_positions_means_no_exit_costs():
    assert new_book().exit_costs_at_mark(D[2], schedules=SCHEDULES, pricing_basis=BASIS) == 0


def test_a_segment_ending_with_open_positions_carries_their_exit_costs_net_of_equity():
    sessions = weekday_sessions(SESSION_START, 160)
    hold = HoldoutRange(sessions[-20], sessions[-1])
    ctx = make_context(make_rows(sessions, default_names(8) + etf_names()), hold)
    seg = simulate_segment(ctx, sessions=ctx.view.sessions(), scenario=ctx.scenarios.gate(), slope=Decimal("0.01"),
                           regime_cash=None, mode=MODE_BASE, fold="x")
    assert seg.open_positions, "the fixture must leave positions open for this test to mean anything"
    assert seg.exit_costs_at_end > 0
    assert seg.end_equity_after_exit_costs == seg.end_equity - seg.exit_costs_at_end
    assert seg.net_return_after_exit_costs < seg.net_return  # the mark-to-market figure used elsewhere is unchanged
    ev = holdout_evidence(seg, SimpleNamespace(net_return=Decimal("0.01")))
    assert ev.net_return == seg.net_return_after_exit_costs and ev.exit_costs_at_end == seg.exit_costs_at_end


def test_excess_of_3_6_percent_before_exit_costs_and_under_3_5_after_must_fail():
    sessions = weekday_sessions(SESSION_START, 250)  # 250 sessions: annualised equals the raw return
    seg = _bare_segment(sessions, start=Decimal(50000), end=Decimal(50000) * Decimal("1.086"), exit_costs=Decimal(0))
    etf = SimpleNamespace(net_return=Decimal("0.05"))  # benchmark already net of its own round trip
    criteria = default_criteria()
    before = evaluate_verdict(criteria, holdout_evidence(seg, etf))
    assert before.verdict == PASS and before.annualised_excess_return == Decimal("0.036")
    # Open positions would cost 100 rupees (0.2 percent of capital) to close: 8.6 percent becomes 8.4 percent.
    seg = replace(seg, exit_costs_at_end=Decimal(100))
    after = evaluate_verdict(criteria, holdout_evidence(seg, etf))
    assert after.verdict == FAIL and after.breaches == ("excess_return_below_minimum",)
    assert after.annualised_excess_return == Decimal("0.034") < Decimal("0.035")
    assert not after.passed


def _bare_segment(sessions, *, start, end, exit_costs):
    """A real segment shell (so every field has its true type) with the money figures set by hand."""
    ctx_sessions = weekday_sessions(SESSION_START, 160)
    hold = HoldoutRange(ctx_sessions[-20], ctx_sessions[-1])
    ctx = make_context(make_rows(ctx_sessions, default_names(8) + etf_names()), hold)
    seg = simulate_segment(ctx, sessions=ctx.view.sessions()[:30], scenario=ctx.scenarios.gate(),
                           slope=Decimal("0.01"), regime_cash=None, mode=MODE_BASE, fold="x")
    return replace(seg, sessions=tuple(sessions), start_equity=start, end_equity=end, exit_costs_at_end=exit_costs,
                   curve=((sessions[-1], end),), flatten_events=0, swaps=10, attempts=())


# ---- 3. one fixed decimal context -------------------------------------------------------------------
@pytest.mark.parametrize("ambient", [
    decimal.Context(prec=5, rounding=decimal.ROUND_DOWN),
    decimal.Context(prec=9, rounding=decimal.ROUND_CEILING),
    decimal.Context(prec=120, rounding=decimal.ROUND_FLOOR),
])
def test_annualisation_digits_do_not_depend_on_the_ambient_decimal_context(ambient):
    ev = SimpleNamespace(net_return=Decimal("0.0816"), benchmark_net_return=Decimal("0.0404"), holdout_sessions=137)
    reference = (annualised_return(ev.net_return, 137, 250), annualised_excess_return(ev, 250), annualised_swaps(33, 137, 250))
    with decimal.localcontext(ambient):
        assert (annualised_return(ev.net_return, 137, 250), annualised_excess_return(ev, 250),
                annualised_swaps(33, 137, 250)) == reference
    assert all(len(str(part).replace(".", "").lstrip("-0")) > 28 for part in reference)  # more digits than the default context keeps
