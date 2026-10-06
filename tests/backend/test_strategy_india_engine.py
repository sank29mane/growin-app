"""AC-14 (fill and cost schedules, D-14, D-16) and AC-20 (end to end and determinism)."""

from __future__ import annotations

import json
import os
import socket
import stat
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pytest

from costs.core import IST, LookaheadError, Side, TickSizeUnavailable
from costs.fills import FillOutcome, LimitOrder
from costs.run import simulate_and_price
from costs.schedule import PricingBasis

from strategy_india import study
from strategy_india.engine import run_walk_forward, simulate_segment
from strategy_india.errors import GateRefused, HoldoutSpent, RegistryMismatch
from strategy_india.folds import FoldRules
from strategy_india.holdout import FAIL, INCONCLUSIVE, PASS, HoldoutRange
from strategy_india.registry import Registry
from strategy_india.report import write_report
from strategy_india.signals import MODE_BASE
from strategy_india.ticks import EQUITY, resolve_tick

from test_strategy_india_support import (
    GIT_COMMIT,
    SCHEDULE_VERSION,
    SESSION_START,
    costs_inputs,
    default_names,
    etf_names,
    make_context,
    make_rows,
    study_inputs,
    tick_tables,
    weekday_sessions,
)

SESS = weekday_sessions(SESSION_START, 160)
HOLDOUT = HoldoutRange(SESS[-20], SESS[-1])
ROWS = make_rows(SESS, default_names(8) + etf_names())
GATE_ID = "pessimistic"
NAMES = default_names(10)


def _segment(ctx, sessions=None, scenario=None):
    return simulate_segment(ctx, sessions=sessions or ctx.view.sessions(), scenario=scenario or ctx.scenarios.gate(),
                            slope=Decimal("0.01"), regime_cash=None, mode=MODE_BASE, fold="x")


# ---- AC-14 -----------------------------------------------------------------------------------------
def test_every_run_executes_base_adverse_and_pessimistic_and_pessimistic_is_the_gate():
    ctx = make_context(ROWS, HOLDOUT)
    walk = run_walk_forward(ctx, FoldRules(n_folds=2, test_sessions=30, min_train_sessions=60))
    assert ctx.scenarios.gate().scenario_id == GATE_ID and ctx.scenarios.gate().k_ticks == 3
    for fold in walk.folds:
        assert set(fold.scenarios) == {"base", "adverse", "pessimistic"}
        assert fold.gate is fold.scenarios[GATE_ID]
        for sid, k in (("base", 1), ("adverse", 2), ("pessimistic", 3)):
            seg = fold.scenarios[sid]
            assert {f.result.k_ticks for f in seg.fills} <= {k} and seg.scenario_refs[0][0] == sid
            assert {f.result.scenarios_hash for f in seg.fills} == {ctx.scenarios.scenarios_hash}
    base, pess = walk.folds[0].scenarios["base"], walk.folds[0].scenarios[GATE_ID]
    assert base.run_chain_sha256 != pess.run_chain_sha256, "each scenario is its own run, not a relabel"


def test_brokerage_is_the_full_seven_basis_points_plus_gst_with_no_prepaid_credit():
    ctx = make_context(ROWS, HOLDOUT)
    seg = _segment(ctx)
    schedule = ctx.schedules.get(SCHEDULE_VERSION)
    assert schedule.brokerage.delivery_rate == Decimal("0.0007") and schedule.brokerage.delivery_min_per_order == 0
    assert seg.contract_notes
    for note in seg.contract_notes:
        for ob in note.order_brokerage:
            assert ob.classification == "delivery"
            assert ob.amount == (ob.traded_value * Decimal("0.0007")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        gst_base = sum((note.line(name) for name in ("brokerage", "exchange_transaction", "sebi_fee", "ipft")), Decimal(0))
        assert note.line("gst") == (gst_base * Decimal("0.18")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        assert note.line("brokerage") == sum((ob.amount for ob in note.order_brokerage), Decimal(0))
        assert all(line.amount >= 0 for line in note.lines)
        assert note.pricing_basis == PricingBasis.pinned(SCHEDULE_VERSION)
    assert seg.charges_total == sum((n.total for n in seg.contract_notes), Decimal(0))


def test_scenario_schedule_and_tick_hashes_and_the_pinned_version_are_in_the_result():
    ctx = make_context(ROWS, HOLDOUT)
    seg = _segment(ctx)
    gate = ctx.scenarios.gate()
    assert seg.scenario_refs == ((GATE_ID, gate.scenarios_version, gate.scenarios_hash),)
    assert seg.schedule_refs == ((SCHEDULE_VERSION, ctx.schedules.get(SCHEDULE_VERSION).schedule_hash),)
    (source, source_hash), = seg.tick_refs
    version = tick_tables().table_for(EQUITY).versions[0]
    assert source == f"nse-cash-price-band-ticks:{version.version}" and source_hash == version.version_hash


def test_no_assumed_fill_produces_no_trade():
    blocked = {(r.isin, r.trade_date): "band_crosscheck_row_conflict" for r in ROWS}
    seg = _segment(make_context(ROWS, HOLDOUT, unavailable=blocked))
    assert seg.fills == () and seg.closed == () and seg.entries == 0 and seg.open_positions == ()
    assert seg.end_equity == seg.start_equity and seg.charges_total == 0
    assert seg.attempts and all(a.affected and a.outcome == FillOutcome.NO_ASSUMED_FILL.value for a in seg.attempts)


def test_a_pre_revision_order_without_an_encoded_tick_fails_closed_with_no_default_tick():
    early = weekday_sessions(date(2025, 1, 6), 70)
    rows = make_rows(early, default_names(8) + etf_names())
    ctx = make_context(rows, HoldoutRange(early[-10], early[-1]))
    seg = _segment(ctx)
    assert seg.fills == () and seg.entries == 0 and seg.tick_refs == ()
    assert seg.attempts and all(a.outcome == "TICK_UNAVAILABLE" and a.affected for a in seg.attempts)
    assert seg.end_equity == seg.start_equity
    # the building blocks refuse too
    with pytest.raises(TickSizeUnavailable):
        resolve_tick(tick_tables(), session_date=date(2025, 4, 14), band_reference_price=Decimal("100"),
                     instrument_class=EQUITY, series="EQ")
    scenarios, schedules, _, basis = costs_inputs()
    bar = _sbar(early[0])
    order = LimitOrder("o1", "A", "NSE", Side.BUY, 10, Decimal("100.00"), Decimal("100.00"), early[0],
                       datetime.combine(early[0], datetime.min.time().replace(hour=8), tzinfo=IST), early[0] - timedelta(days=1), None)
    with pytest.raises(TickSizeUnavailable):
        simulate_and_price(workspace="india", currency="INR", orders=[order], bars=[bar], scenario=scenarios.gate(),
                           schedules=schedules, pricing_basis=basis)


def _sbar(day):
    from costs.fills import PriceBand, SessionBar

    return SessionBar("A", "NSE", day, Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), 10000, "raw",
                      PriceBand("fixed", Decimal("80"), Decimal("120"), day, "s", "0" * 64), "s")


# ---- AC-20 -----------------------------------------------------------------------------------------
def _pipeline(tmp_path, **kw):
    inputs = study_inputs(tmp_path, sessions_n=780, holdout_sessions=125, **kw)  # three years, six month holdout
    entry = study.register(inputs, hypothesis="synthetic momentum with a swing exit")
    head = inputs.registry.head_hash()
    research = study.run_research(inputs, expected_head=head)
    outcome = study.run_holdout(inputs, expected_head=head, logged_at=None)
    out = tmp_path / "out"
    return inputs, entry, research, outcome, write_report(out, research), write_report(out, outcome.report)


def test_end_to_end_register_walk_forward_holdout_report_twice_is_byte_identical(tmp_path, monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    a = _pipeline(tmp_path / "a")
    b = _pipeline(tmp_path / "b")
    assert a[4].read_bytes() == b[4].read_bytes(), "research report"
    assert a[5].read_bytes() == b[5].read_bytes(), "holdout report"
    assert a[1].entry_hash == b[1].entry_hash and a[3].event_hash == b[3].event_hash
    inputs, entry, research, outcome, research_path, holdout_path = a
    assert len(research.units) == 3 and research.registration_entry_hash == entry.entry_hash
    assert outcome.verdict.verdict in (PASS, FAIL, INCONCLUSIVE)
    assert outcome.verdict.criteria_sha256 == entry.payload["holdout_criteria_sha256"]
    assert outcome.report.holdout_verdict["criteria_status"].startswith("PROPOSED")
    assert outcome.report.kind == "holdout" and [u.label for u in outcome.report.units] == ["holdout"]
    for path in (research_path, holdout_path):
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o444
    events = inputs.registry.holdout_events()
    assert len(events) == 1 and events[0].entry_hash == outcome.event_hash
    inputs.registry.verify()


def test_the_holdout_is_spent_after_one_evaluation(tmp_path):
    inputs, _, _, outcome, _, _ = _pipeline(tmp_path)
    with pytest.raises(HoldoutSpent):
        study.run_holdout(inputs, expected_head=inputs.registry.head_hash(), logged_at=None)
    assert len(inputs.registry.holdout_events()) == 1
    assert outcome.verdict.verdict in (PASS, FAIL, INCONCLUSIVE)


def test_research_report_never_touches_the_holdout_sessions(tmp_path):
    inputs = study_inputs(tmp_path, sessions_n=780, holdout_sessions=125)
    study.register(inputs, hypothesis="h")
    report = study.run_research(inputs, expected_head=inputs.registry.head_hash())
    holdout_start = study.prepare(inputs).holdout.start
    assert report.run_window_end < holdout_start and all(u.test_end < holdout_start for u in report.units)
    assert inputs.registry.holdout_events() == ()


def test_holdout_with_unavailable_bands_is_not_a_pass(tmp_path):
    sessions = weekday_sessions(SESSION_START, 780)
    block = [(n.code, n.anchor, d) for d in sessions[-90:-60] for n in NAMES]
    inputs = study_inputs(tmp_path, sessions_n=780, holdout_sessions=125, unavailable=block)
    study.register(inputs, hypothesis="h")
    outcome = study.run_holdout(inputs, expected_head=inputs.registry.head_hash())
    assert outcome.verdict.verdict in (FAIL, INCONCLUSIVE) and not outcome.verdict.passed
    if not outcome.verdict.breaches:
        assert outcome.verdict.verdict == INCONCLUSIVE and "no_assumed_fill_attempt" in outcome.verdict.missing_evidence


def test_a_changed_input_after_registration_refuses_the_run(tmp_path):
    inputs = study_inputs(tmp_path)
    study.register(inputs, hypothesis="h")
    head = inputs.registry.head_hash()
    inputs.params_raw = {**inputs.params_raw, "seed": 8}
    with pytest.raises(RegistryMismatch):
        study.run_research(inputs, expected_head=head)
    inputs.params_raw = {**inputs.params_raw, "seed": 7}
    study.run_research(inputs, expected_head=head)
    inputs.git_commit = "c" * 40
    with pytest.raises(RegistryMismatch) as err:
        study.run_research(inputs, expected_head=head)
    assert err.value.field == "git_commit"
    inputs.git_commit = GIT_COMMIT
    inputs.dataset_sha256 = "d" * 64
    with pytest.raises(RegistryMismatch):
        study.run_holdout(inputs, expected_head=head)
    assert inputs.registry.holdout_events() == ()


def test_a_tampered_coverage_report_refuses_before_any_evaluation(tmp_path):
    inputs = study_inputs(tmp_path)
    study.register(inputs, hypothesis="h")
    os.chmod(inputs.coverage_path, 0o644)
    body = json.loads(inputs.coverage_path.read_text())
    body["sessions"] = 999
    inputs.coverage_path.write_text(json.dumps(body))
    with pytest.raises(GateRefused) as err:
        study.run_research(inputs, expected_head=inputs.registry.head_hash())
    assert err.value.code == "report_tampered"


def test_a_missing_registry_refuses_to_run(tmp_path):
    inputs = study_inputs(tmp_path)
    with pytest.raises(Exception, match="registry file is missing"):
        study.run_research(inputs, expected_head=inputs.registry.head_hash())
    with pytest.raises(Exception, match="registry file is missing"):
        study.run_holdout(inputs, expected_head="0" * 64)


class _PeekNextDay:
    """Reads one session ahead through the decision view."""

    def scores(self, ctx):
        tomorrow = ctx.as_of + timedelta(days=1)
        return {anchor: ctx.view.close(anchor, tomorrow) for anchor in sorted(ctx.eligible)}


class _PeekViaScores:
    def scores(self, ctx):
        return {anchor: ctx.raw_score(anchor, ctx.as_of + timedelta(days=1)) for anchor in sorted(ctx.eligible)}


@pytest.mark.parametrize("provider", [_PeekNextDay(), _PeekViaScores()], ids=["close", "score"])
def test_a_strategy_that_peeks_one_session_ahead_is_caught(provider):
    ctx = make_context(ROWS, HOLDOUT)
    ctx.provider = provider
    with pytest.raises(LookaheadError):
        _segment(ctx)


def test_cli_refuses_a_missing_or_incomplete_config_with_exit_2(tmp_path, capsys):
    from strategy_india import __main__ as cli

    assert cli.main(["run", "--config", str(tmp_path / "nope.json")]) == 2
    partial = tmp_path / "partial.json"
    partial.write_text(json.dumps({"private_dir": "x"}))
    assert cli.main(["holdout", "--config", str(partial)]) == 2
    assert "config_invalid" in capsys.readouterr().out


def test_band_limits_come_from_the_previous_sessions_raw_close_not_the_fill_days(monkeypatch):
    from strategy_india import engine

    seen = []
    real = engine.session_bar_for

    def spy(bar, **kw):
        sb = real(bar, **kw)
        seen.append((bar, kw["previous_raw_close"], sb.price_band))
        return sb

    monkeypatch.setattr(engine, "session_bar_for", spy)
    ctx = make_context(ROWS, HOLDOUT)
    _segment(ctx)
    assert len(seen) > 20
    off_from_fill_day = 0
    for bar, previous, band in seen:
        assert previous == ctx.view.previous_bar(bar.anchor_isin, bar.session).raw_close
        assert abs(band.lower - previous * Decimal("0.8")) < Decimal("0.2")  # 20 percent, widened by under a tick
        assert abs(band.upper - previous * Decimal("1.2")) < Decimal("0.2")
        if abs(band.lower - bar.raw_close * Decimal("0.8")) > Decimal("0.2"):
            off_from_fill_day += 1
    assert off_from_fill_day > 0, "a band built on the fill day's own close would have passed the tolerance above"


def test_the_peek_guard_allows_reads_up_to_the_decision_date():
    ctx = make_context(ROWS, HOLDOUT)

    class Today:
        def scores(self, c):
            return {a: c.raw_score(a, c.as_of) for a in sorted(c.eligible) if c.raw_score(a, c.as_of) is not None}

    ctx.provider = Today()
    assert _segment(ctx).entries > 0
