"""AC-4 (holdout seal, D-12) and AC-5 (D-19 verdict, PROPOSED)."""

from __future__ import annotations

import copy
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest

from costs.core import LookaheadError
from costs.fills import load_fill_scenarios

from strategy_india import benchmark
from strategy_india.data import DatasetView, DecisionView
from strategy_india.engine import simulate_segment
from strategy_india.errors import HoldoutSpent, HoldoutViolation, RegistryError, StrategyIndiaError
from strategy_india.holdout import (
    FAIL,
    INCONCLUSIVE,
    PASS,
    HoldoutEvidence,
    HoldoutGrant,
    HoldoutRange,
    check_gate_scenario,
    criteria_sha256,
    evaluate_verdict,
    holdout_digest,
    holdout_range_for,
    is_grant,
    open_holdout,
)
from strategy_india.registry import Registry
from strategy_india.signals import SignalTable
from strategy_india.data import DividendEvents

from test_strategy_india_support import (
    default_criteria,
    ETF_ISINS,
    SESSION_START,
    default_names,
    etf_names,
    make_context,
    make_rows,
    params,
    registration_record,
    weekday_sessions,
)

SESSIONS = weekday_sessions(SESSION_START, 120)
HOLDOUT = HoldoutRange(SESSIONS[-30], SESSIONS[-1])
ROWS = make_rows(SESSIONS, default_names(8) + etf_names())


def _view() -> DatasetView:
    return DatasetView.from_rows(ROWS, holdout=HOLDOUT)


# ---- AC-4: the research loader --------------------------------------------------------------
@pytest.mark.parametrize("offset", [0, 1, 29])
def test_research_loader_refuses_every_holdout_session(offset):
    view = _view()
    day = SESSIONS[-30 + offset]
    anchor = "INE000A01000"
    with pytest.raises(HoldoutViolation):
        view.bar(anchor, day)
    with pytest.raises(HoldoutViolation):
        view.bars_on(day)
    with pytest.raises(HoldoutViolation):
        view.sessions(end=day)
    with pytest.raises(HoldoutViolation):
        view.series(anchor, end=day)
    with pytest.raises(HoldoutViolation):
        view.previous_bar(anchor, day)
    with pytest.raises(HoldoutViolation):
        view.calendar_index(day)


def test_development_data_is_readable_and_stops_before_the_holdout():
    view = _view()
    assert view.visible_end == SESSIONS[-31]
    assert view.sessions()[-1] == SESSIONS[-31]
    assert view.bar("INE000A01000", SESSIONS[-31]) is not None
    assert view.next_session(SESSIONS[-31]) is None  # the next session is not exposed


def test_feature_warm_up_and_decisions_cannot_reach_the_holdout():
    view = _view()
    ctx = make_context(ROWS, HOLDOUT, view=view)
    table = SignalTable(view, params(), DividendEvents())
    assert max(table._calendar) < HOLDOUT.start  # warm-up tables are built from development rows only
    with pytest.raises(HoldoutViolation):
        DecisionView(view, HOLDOUT.start).close("INE000A01000", HOLDOUT.start)
    with pytest.raises(HoldoutViolation):
        simulate_segment(ctx, sessions=SESSIONS[-35:-25], scenario=ctx.scenarios.gate(), slope=Decimal("0.01"),
                         regime_cash=None, mode="base", fold="x")


def test_benchmark_paths_cannot_reach_the_holdout():
    view = _view()
    spec = params().benchmark
    with pytest.raises(HoldoutViolation):
        benchmark.choose_etf(view, spec, start=SESSIONS[0], end=SESSIONS[-1])
    ctx = make_context(ROWS, HOLDOUT, view=view)
    with pytest.raises(HoldoutViolation):
        benchmark.etf_buy_and_hold(view, ETF_ISINS[0], start=SESSIONS[0], end=SESSIONS[-1], capital=Decimal(50000),
                                   ticks=ctx.ticks, schedules=ctx.schedules, pricing_basis=ctx.pricing_basis)
    assert benchmark.choose_etf(view, spec, start=SESSIONS[0], end=view.visible_end).anchor_isin == ETF_ISINS[0]


def _registry(tmp_path, holdout=HOLDOUT, **overrides) -> Registry:
    reg = Registry(tmp_path / "registry.jsonl")
    reg.register(registration_record(
        holdout_range=holdout.as_payload(), **overrides))
    return reg


def test_first_open_is_a_logged_registry_event_and_unlocks_the_view(tmp_path):
    reg = _registry(tmp_path)
    head = reg.verify()
    grant = open_holdout(reg, criteria=default_criteria(), expected_head=head, logged_at="2026-10-06T00:00:00+00:00")
    events = reg.holdout_events()
    assert len(events) == 1 and events[0].entry_hash == grant.event_hash
    assert events[0].payload["logged_at"] == "2026-10-06T00:00:00+00:00"
    assert events[0].payload["holdout_range"] == HOLDOUT.as_payload()
    opened = _view().open(grant)
    assert opened.bar("INE000A01000", SESSIONS[-1]) is not None
    assert opened.opened


def test_second_open_is_refused(tmp_path):
    reg = _registry(tmp_path)
    open_holdout(reg, criteria=default_criteria(), expected_head=reg.verify())
    with pytest.raises(HoldoutSpent):
        open_holdout(reg, criteria=default_criteria(), expected_head=reg.verify())
    assert len(reg.holdout_events()) == 1


def test_open_refused_when_criteria_absent_or_hash_differs(tmp_path):
    reg = _registry(tmp_path)
    head = reg.verify()
    for bad in (None, {}):
        with pytest.raises(RegistryError, match="absent"):
            open_holdout(reg, criteria=bad, expected_head=head)
    changed = default_criteria()
    changed["max_annualised_swaps"] = "80"
    with pytest.raises(RegistryError, match="differ"):
        open_holdout(reg, criteria=changed, expected_head=head)
    assert reg.holdout_events() == ()


def test_open_refused_when_pinned_head_differs(tmp_path):
    reg = _registry(tmp_path)
    with pytest.raises(RegistryError, match="head"):
        open_holdout(reg, criteria=default_criteria(), expected_head="0" * 64)


def test_deleting_the_open_event_cannot_reopen_when_the_head_is_pinned(tmp_path):
    reg = _registry(tmp_path)
    open_holdout(reg, criteria=default_criteria(), expected_head=reg.verify())
    pinned = reg.verify()
    lines = reg.path.read_text().splitlines()
    reg.path.write_text(lines[0] + "\n")
    with pytest.raises(RegistryError):
        open_holdout(reg, criteria=default_criteria(), expected_head=pinned)


def test_forged_grant_does_not_open_the_view():
    forged = HoldoutGrant("a" * 64, "b" * 64, HOLDOUT, "c" * 64)
    assert not is_grant(forged)
    with pytest.raises(HoldoutViolation):
        _view().open(forged)


def test_new_registration_overlapping_a_spent_holdout_is_refused(tmp_path):
    reg = _registry(tmp_path)
    open_holdout(reg, criteria=default_criteria(), expected_head=reg.verify())
    spent = [e.entry_hash for e in reg.holdout_events()]
    overlapping = HoldoutRange(SESSIONS[-10], SESSIONS[-1]).as_payload()
    with pytest.raises(HoldoutSpent):
        reg.register(registration_record(holdout_range=overlapping, spent_holdout_event_hashes=spent))


def test_holdout_range_and_digest():
    assert holdout_range_for(SESSIONS, 30) == HOLDOUT
    with pytest.raises(HoldoutViolation):
        holdout_range_for(SESSIONS[:20], 30)
    a = holdout_digest("d" * 64, HOLDOUT, SESSIONS)
    assert a == holdout_digest("d" * 64, HOLDOUT, SESSIONS)
    assert a != holdout_digest("e" * 64, HOLDOUT, SESSIONS)
    assert a != holdout_digest("d" * 64, HOLDOUT, SESSIONS[:-1])


# ---- AC-5: the D-19 verdict ------------------------------------------------------------------
GOOD = HoldoutEvidence(
    net_return=Decimal("0.09"), benchmark_net_return=Decimal("0.05"), max_drawdown=Decimal("-0.06"),
    flatten_events=0, swaps=40, holdout_sessions=250, no_assumed_fill_attempts=0,
)


def test_template_is_the_confirmed_d19_set_and_hashes_stably():
    t = default_criteria()
    assert (t["gate_k_ticks"], t["max_drawdown_floor"], t["min_annualised_excess_return"], t["max_annualised_swaps"]) == (
        3, "-0.10", "0.035", "65")
    assert criteria_sha256(t) == criteria_sha256(copy.deepcopy(t))


def test_gate_scenario_is_60s_k3_pessimistic():
    gate = load_fill_scenarios().gate()
    check_gate_scenario(default_criteria(), k_ticks=gate.k_ticks, phase62_gate=gate.phase62_gate)
    assert gate.scenario_id == "pessimistic" and gate.k_ticks == 3
    with pytest.raises(StrategyIndiaError):
        check_gate_scenario(default_criteria(), k_ticks=1, phase62_gate=False)


def test_pass_only_when_every_criterion_holds_and_records_the_criteria_hash():
    verdict = evaluate_verdict(default_criteria(), GOOD)
    assert verdict.verdict == PASS and verdict.passed
    assert verdict.criteria_sha256 == criteria_sha256(default_criteria())
    assert verdict.annualised_swaps == Decimal(40)


@pytest.mark.parametrize(
    "change, breach",
    [
        ({"net_return": Decimal("0.05")}, "net_return_not_above_benchmark"),  # equal is not above
        ({"net_return": Decimal("0.01")}, "net_return_not_above_benchmark"),
        ({"max_drawdown": Decimal("-0.10")}, "max_drawdown_at_or_below_floor"),  # at the floor is a breach
        ({"max_drawdown": Decimal("-0.15")}, "max_drawdown_at_or_below_floor"),
        ({"max_drawdown": Decimal("-0.2")}, "max_drawdown_at_or_below_floor"),
        ({"flatten_events": 1}, "flatten_event"),
        ({"swaps": 66}, "annualised_swaps_above_budget"),
    ],
)
def test_each_single_breach_fails(change, breach):
    verdict = evaluate_verdict(default_criteria(), replace(GOOD, **change))
    assert verdict.verdict == FAIL and not verdict.passed
    assert breach in verdict.breaches


def test_drawdown_floor_is_ten_percent():
    assert evaluate_verdict(default_criteria(), replace(GOOD, max_drawdown=Decimal("-0.099"))).verdict == PASS
    fail = evaluate_verdict(default_criteria(), replace(GOOD, max_drawdown=Decimal("-0.101")))
    assert fail.verdict == FAIL and fail.breaches == ("max_drawdown_at_or_below_floor",)


@pytest.mark.parametrize(
    "strategy, verdict",
    [("0.084", FAIL),   # beats the ETF by 3.4 percent
     ("0.085", PASS),   # exactly the 3.5 percent bar passes (the breach is "below")
     ("0.086", PASS)],  # beats it by 3.6 percent
)
def test_strategy_must_beat_the_etf_by_the_annualised_minimum(strategy, verdict):
    ev = replace(GOOD, net_return=Decimal(strategy), benchmark_net_return=Decimal("0.05"))
    out = evaluate_verdict(default_criteria(), ev)
    assert out.verdict == verdict and out.passed is (verdict == PASS)
    if verdict == FAIL:
        assert out.breaches == ("excess_return_below_minimum",)  # still above the ETF, so only the new code fires
        assert out.annualised_excess_return == Decimal("0.034")
    else:
        assert out.breaches == ()


def test_excess_return_is_compared_annualised_not_raw():
    # 125 sessions: 4 percent vs 2 percent raw is a 2.0 point gap, which would fail 3.5. Annualised it is
    # 1.04**2 - 1.02**2 = 0.0816 - 0.0404 = 4.12 points, which passes.
    ev = replace(GOOD, net_return=Decimal("0.04"), benchmark_net_return=Decimal("0.02"), holdout_sessions=125, swaps=20)
    out = evaluate_verdict(default_criteria(), ev)
    assert out.verdict == PASS and out.annualised_excess_return == Decimal("0.0412")
    # 500 sessions: the raw gap is 4.12 points, which would pass 3.5, but annualised (square root) it is
    # 4.0 - 2.0 = 2.0 points, which fails.
    slow = replace(GOOD, net_return=Decimal("0.0816"), benchmark_net_return=Decimal("0.0404"), holdout_sessions=500, swaps=100)
    out = evaluate_verdict(default_criteria(), slow)
    assert out.verdict == FAIL and out.breaches == ("excess_return_below_minimum",)
    assert out.annualised_excess_return < Decimal("0.035")


def test_annualised_return_edges():
    from strategy_india.holdout import annualised_return
    assert annualised_return(Decimal("0.07"), 250, 250) == Decimal("0.07")
    assert annualised_return(Decimal("-1"), 125, 250) == Decimal(-1)
    assert annualised_return(Decimal("-1.5"), 125, 250) == Decimal(-1)
    with pytest.raises(StrategyIndiaError):
        annualised_return(Decimal("0.1"), 0, 250)


def test_unknown_benchmark_cannot_pass_even_when_the_net_return_flag_is_off():
    crit = default_criteria()
    crit["require_net_return_above_benchmark"] = False
    out = evaluate_verdict(crit, replace(GOOD, benchmark_net_return=None))
    assert out.verdict == INCONCLUSIVE and out.annualised_excess_return is None


def test_excess_return_flip_under_sensitivity_is_inconclusive():
    sens = replace(GOOD, net_return=Decimal("0.07"))  # 2 points over the ETF in the sensitivity run
    out = evaluate_verdict(default_criteria(), GOOD, sens)
    assert out.verdict == INCONCLUSIVE and out.sensitivity_flips == ("excess_return_below_minimum",)


def test_swap_budget_boundary_and_annualisation():
    assert evaluate_verdict(default_criteria(), replace(GOOD, swaps=65)).verdict == PASS
    # 33 swaps in 125 sessions is 66 a year
    assert evaluate_verdict(default_criteria(), replace(GOOD, swaps=33, holdout_sessions=125)).verdict == FAIL


def test_missing_evidence_is_inconclusive_not_pass():
    verdict = evaluate_verdict(default_criteria(), replace(GOOD, no_assumed_fill_attempts=1))
    assert verdict.verdict == INCONCLUSIVE and not verdict.passed
    assert verdict.missing_evidence == ("no_assumed_fill_attempt",)
    unknown_etf = evaluate_verdict(default_criteria(), replace(GOOD, benchmark_net_return=None))
    assert unknown_etf.verdict == INCONCLUSIVE and "benchmark_unavailable" in unknown_etf.missing_evidence


def test_sensitivity_that_flips_a_criterion_is_inconclusive():
    sens = replace(GOOD, max_drawdown=Decimal("-0.16"))
    verdict = evaluate_verdict(default_criteria(), GOOD, sens)
    assert verdict.verdict == INCONCLUSIVE
    assert verdict.sensitivity_flips == ("max_drawdown_at_or_below_floor",)
    assert evaluate_verdict(default_criteria(), GOOD, GOOD).verdict == PASS
    assert evaluate_verdict(default_criteria(), GOOD, replace(GOOD, no_assumed_fill_attempts=2)).verdict == INCONCLUSIVE


def test_a_breach_outranks_missing_evidence():
    bad = replace(GOOD, flatten_events=1, no_assumed_fill_attempts=3)
    assert evaluate_verdict(default_criteria(), bad).verdict == FAIL


def test_changing_a_number_is_a_template_edit_and_a_new_hash_not_code():
    tight = default_criteria()
    tight["max_annualised_swaps"] = "52"
    ev = replace(GOOD, swaps=60)
    assert evaluate_verdict(default_criteria(), ev).verdict == PASS
    assert evaluate_verdict(tight, ev).verdict == FAIL
    assert criteria_sha256(tight) != criteria_sha256(default_criteria())


def test_unsupported_policy_values_are_refused():
    odd = default_criteria()
    odd["missing_evidence"] = "pass"
    with pytest.raises(StrategyIndiaError):
        evaluate_verdict(odd, GOOD)
