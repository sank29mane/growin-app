"""AC-10 (fold-level unknown reporting, D-01a) and AC-17 (reporting, D-18)."""

from __future__ import annotations

import json
import math
import os
import stat
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from pilot_data.core import CaveatedResult, canonical_sha256

from strategy_india import metrics, study
from strategy_india.report import StudyReport, write_report

from test_strategy_india_support import (
    SCHEDULE_VERSION,
    SESSION_START,
    default_names,
    study_inputs,
    weekday_sessions,
    write_tri,
)

SESSIONS = weekday_sessions(SESSION_START, 400)
# 3 folds of 50 sessions end the 340 development sessions: fold 1 = [190, 240), 2 = [240, 290), 3 = [290, 340)
FOLD1, FOLD2, FOLD3 = SESSIONS[190:240], SESSIONS[240:290], SESSIONS[290:340]
NAMES = default_names(10)


def _block(days):
    return [(n.code, n.anchor, d) for d in days for n in NAMES]


def _run(tmp_path, **kw):
    inputs = study_inputs(tmp_path, **kw)
    study.register(inputs, hypothesis="h")
    return inputs, study.run_research(inputs, expected_head=inputs.registry.head_hash())


# ---- AC-10 -----------------------------------------------------------------------------------------
def test_unknown_bands_in_folds_1_and_3_are_reported_by_target_and_fold_and_fold_2_shows_zero(tmp_path):
    unknown = _block(FOLD1[10:30]) + _block(FOLD3[10:30])
    _, report = _run(tmp_path, unavailable=unknown)
    cov = report.unknown_band_coverage
    assert cov.total_in_run_window == len(unknown) == 400
    assert cov.by_fold == {"1": 200, "2": 0, "3": 200}
    assert cov.outside_test_windows == 0
    assert set(cov.by_target) == {n.code for n in NAMES} and all(v == 40 for v in cov.by_target.values())
    assert cov.affected_attempts_by_fold["2"] == {"entry": 0, "exit": 0}
    for fold in ("1", "3"):
        counts = cov.affected_attempts_by_fold[fold]
        assert counts["entry"] + counts["exit"] > 0, f"fold {fold} has affected attempts"
        assert counts["entry"] > 0 or counts["exit"] > 0
    assert cov.affected_attempts_by_target  # per target
    for target, folds in cov.affected_attempts_by_target_and_fold.items():
        assert set(folds) <= {"1", "3"}, f"{target} must not show fold 2"
    by_label = {u.label: u for u in report.units}
    assert by_label["2"].unknown_bands_in_window == 0 and by_label["2"].status == "evidence"
    assert by_label["1"].status == by_label["3"].status == "missing_evidence"


def test_missing_evidence_folds_are_never_neutral_or_wins(tmp_path):
    unknown = _block(FOLD1[10:30]) + _block(FOLD3[10:30])
    _, report = _run(tmp_path, unavailable=unknown)
    by_label = {u.label: u for u in report.units}
    assert by_label["1"].beats_etf is None and by_label["3"].beats_etf is None  # not a win and not a loss
    assert by_label["2"].beats_etf in (True, False)
    agg = report.aggregate
    assert agg.missing_evidence_units == ["1", "3"] and agg.evidence_units == ["2"]
    assert agg.units_beating_etf <= 1, "only the evidence fold can count as a win"
    assert agg.complete is False
    clean_inputs = study_inputs(tmp_path / "clean")
    study.register(clean_inputs, hypothesis="h")
    clean = study.run_research(clean_inputs, expected_head=clean_inputs.registry.head_hash())
    assert clean.aggregate.complete and clean.aggregate.missing_evidence_units == []
    assert clean.unknown_band_coverage.total_in_run_window == 0


def test_aggregates_use_evidence_units_only_and_report_the_excluded_count(tmp_path):
    unknown = _block(FOLD1[10:30]) + _block(FOLD3[10:30])
    _, report = _run(tmp_path, unavailable=unknown)
    agg = report.aggregate
    only = {u.label: u for u in report.units}["2"]
    gate = only.scenarios["pessimistic"]
    assert agg.units_excluded_from_aggregates == 2 and agg.aggregates_basis == "evidence units only"
    assert agg.net_return_compounded == gate.net_return, "fold 2 alone; folds 1 and 3 are left out"
    assert agg.significance.bars == len(FOLD2) - 1
    assert abs(agg.max_drawdown_concatenated - gate.max_drawdown) < Decimal("1e-20")
    everything = [u.scenarios["pessimistic"].net_return for u in report.units]
    assert agg.net_return_compounded != (1 + everything[0]) * (1 + everything[1]) * (1 + everything[2]) - 1


def test_with_no_evidence_unit_the_aggregates_are_empty_not_computed_from_missing_evidence(tmp_path):
    _, report = _run(tmp_path / "all", unavailable=_block(SESSIONS[190:340]))
    agg = report.aggregate
    assert agg.evidence_units == [] and agg.units_excluded_from_aggregates == 3
    assert agg.net_return_compounded is None and agg.max_drawdown_concatenated is None
    assert agg.significance.bars == 0 and agg.significance.dsr is None and agg.survivorship_haircut_rows == []
    assert agg.units_beating_etf == 0 and agg.complete is False


def test_an_unknown_band_with_no_attempt_still_marks_the_fold(tmp_path):
    only = [(NAMES[0].code, NAMES[0].anchor, FOLD2[0])]
    _, report = _run(tmp_path, unavailable=only)
    unit = {u.label: u for u in report.units}["2"]
    assert unit.unknown_bands_in_window == 1 and unit.unknown_bands_by_target == {NAMES[0].code: 1}
    assert unit.status == "missing_evidence"


def test_all_three_unavailable_bands_appear_in_the_unknown_counts(tmp_path):
    three = [(NAMES[0].code, NAMES[0].anchor, SESSIONS[200]), (NAMES[1].code, NAMES[1].anchor, SESSIONS[260]),
             (NAMES[0].code, NAMES[0].anchor, SESSIONS[300])]
    _, report = _run(tmp_path, unavailable=three)
    cov = report.unknown_band_coverage
    assert cov.total_in_run_window == 3 and sum(cov.by_target.values()) == 3 and sum(cov.by_fold.values()) == 3
    assert cov.by_reason == {"band_crosscheck_row_conflict": 3}


def test_a_band_outside_every_test_window_is_counted_apart(tmp_path):
    early = [(NAMES[0].code, NAMES[0].anchor, SESSIONS[30])]
    _, report = _run(tmp_path, unavailable=early)
    cov = report.unknown_band_coverage
    assert cov.total_in_run_window == 1 and cov.outside_test_windows == 1 and set(cov.by_fold.values()) == {0}


def test_ticks_before_the_2025_revision_surface_as_fold_level_unknown(tmp_path):
    revision = date(2025, 4, 15)  # no tick table is encoded before this date (F1)
    inputs = study_inputs(tmp_path, start=date(2024, 1, 1))
    study.register(inputs, hypothesis="h")
    report = study.run_research(inputs, expected_head=inputs.registry.head_hash())
    pre = [u for u in report.units if u.test_end < revision]
    assert len(pre) >= 2
    for unit in pre:
        gate = unit.scenarios["pessimistic"]
        assert gate.entries == 0 and gate.swaps == 0, "no tick, no order, no default"
        assert gate.affected_entry_attempts > 0 and unit.status == "missing_evidence" and unit.beats_etf is None
        assert unit.etf_unknown_reason and unit.etf_unknown_reason.startswith("tick_unavailable") and unit.etf_net_return is None
        assert {ref[0] for ref in gate.tick_refs} == set(), "no tick source was ever used"
    assert report.aggregate.complete is False and report.aggregate.units_beating_etf == 0


# ---- AC-17 -----------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def full(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("full")
    tri = tmp / "tri.csv"
    digest = write_tri(tri, SESSIONS)
    inputs = study_inputs(tmp, tri=(tri, digest))
    study.register(inputs, hypothesis="h")
    return inputs, study.run_research(inputs, expected_head=inputs.registry.head_hash()), tmp


def test_result_is_a_caveated_result_with_survivorship_and_hindsight_caveats(full):
    _, report, _ = full
    assert isinstance(report, CaveatedResult) and isinstance(report, StudyReport)
    codes = {c.code for c in report.caveats}
    assert {"SURVIVORSHIP_BIAS", "HINDSIGHT_BIAS", "RAW_PRICE_RETURN_BASIS", "HOLDOUT_IS_A_VETO"} <= codes


def test_headline_metrics_are_present_for_every_scenario_and_benchmark(full):
    _, report, _ = full
    assert {"base", "adverse", "pessimistic"} == set(report.units[0].scenarios)
    for unit in report.units:
        for summary in unit.scenarios.values():
            for name in ("net_return", "max_drawdown", "trailing_drawdown", "turnover_ratio", "cost_drag", "average_exposure",
                         "swaps_per_week", "charges_total"):
                assert isinstance(getattr(summary, name), Decimal)
            assert summary.max_drawdown <= 0 and summary.trailing_drawdown <= 0 and summary.cost_drag >= 0
            assert summary.hit_rate is None or Decimal(0) <= summary.hit_rate <= 1
        assert unit.etf_net_return is not None and unit.excess_vs_etf is not None  # against the ETF
        assert unit.excess_vs_tri is not None  # and against the TRI, derived only
    assert report.benchmark.tri_available


def test_decision_drift_is_reported_apart_from_execution_slippage(full):
    _, report, _ = full
    stats = report.units[0].scenarios["pessimistic"].execution
    names = set(type(stats).model_fields)
    assert {"decision_drift_bps_mean", "decision_drift_adverse_share", "execution_slippage_bps_mean",
            "execution_slippage_bps_max_abs"} <= names
    assert stats.fills > 0 and stats.decision_drift_bps_mean is not None and stats.decision_drift_bps_mean != 0
    assert stats.execution_slippage_bps_mean == 0 and stats.execution_slippage_bps_max_abs == 0  # at-limit fills (60 D-10)


def test_significance_uses_the_registered_trial_count_and_a_bootstrap_interval(full):
    _, report, _ = full
    sig = report.aggregate.significance
    assert sig.registered_trials == 12 and sig.bars > 100
    assert sig.psr_vs_zero is not None and Decimal(0) <= sig.psr_vs_zero <= 1
    assert sig.min_trl_bars is None or sig.min_trl_bars > 1
    assert sig.dsr is not None and Decimal(0) <= sig.dsr <= 1
    assert sig.psr_vs_etf is not None
    lo, hi = sig.excess_vs_etf_sum_interval_95
    assert lo < hi
    rows = report.aggregate.survivorship_haircut_rows
    assert [r["haircut_points_per_year"] for r in rows] == [Decimal("0.03"), Decimal("0.05"), Decimal("0.07")]
    assert rows[0]["net_return_after_haircut"] > rows[2]["net_return_after_haircut"]


def test_provisional_statutory_note_records_the_schedule_version_and_hash(full):
    inputs, report, _ = full
    assert "provisional" in report.statutory_note.lower() and "60 D12" in report.statutory_note
    assert report.pricing_schedule_version == SCHEDULE_VERSION and report.pricing_basis == "pinned"
    assert report.pricing_schedule_hash == inputs.schedules.get(SCHEDULE_VERSION).schedule_hash
    assert any(c.code == "STATUTORY_CLASSIFICATION_PROVISIONAL" for c in report.caveats)


def test_money_is_decimal_and_the_dump_holds_no_floats(full):
    _, report, _ = full
    assert isinstance(report.capital, Decimal) and report.capital == Decimal("50000")
    dumped = json.dumps(report.model_dump(mode="json"))
    json.loads(dumped, parse_float=lambda token: pytest.fail(f"float in the report: {token}"))


def test_report_hash_is_the_canonical_hash_of_the_content(full):
    _, report, _ = full
    body = report.model_copy(update={"report_sha256": ""}).model_dump(mode="json")
    assert report.report_sha256 == canonical_sha256(body)


def test_report_is_written_read_only_atomically_and_idempotently(full, tmp_path):
    _, report, _ = full
    path = write_report(tmp_path, report)
    assert path.parent == tmp_path / "reports" and path.name == f"strategy-india-research-{report.report_sha256[:12]}.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o444
    first_inode, first_text = os.stat(path).st_ino, path.read_text()
    again = write_report(tmp_path, report)
    assert again == path and os.stat(path).st_ino == first_inode and path.read_text() == first_text
    assert [p.name for p in path.parent.iterdir()] == [path.name], "no temp file is left behind"
    assert json.loads(first_text)["caveats"], "caveats come first, like the 59 reports"
    assert list(json.loads(first_text))[0] == "caveats"


# ---- metrics --------------------------------------------------------------------------------------
def test_normal_cdf_and_inverse_match_the_float_library():
    for x in (-3, -1.5, 0, 0.7, 1.96, 3):
        expected = 0.5 * (1 + math.erf(x / math.sqrt(2)))
        assert abs(float(metrics.norm_cdf(Decimal(str(x)))) - expected) < 1e-12
    assert abs(float(metrics.norm_ppf(Decimal("0.975"))) - 1.959963984540054) < 1e-9
    with pytest.raises(ValueError):
        metrics.norm_ppf(Decimal(1))


def test_psr_mintrl_and_dsr_behave():
    sr, n = Decimal("0.1"), 200
    p = metrics.psr(sr, Decimal(0), n, Decimal(0), Decimal(3))
    expected = 0.5 * (1 + math.erf((0.1 * math.sqrt(199) / math.sqrt(1 + 0.5 * 0.01)) / math.sqrt(2)))
    assert abs(float(p) - expected) < 1e-9
    assert metrics.psr(sr, Decimal("0.2"), n, Decimal(0), Decimal(3)) < Decimal("0.5")
    trl = metrics.min_track_record_length(sr, Decimal(0), Decimal(0), Decimal(3), Decimal("0.95"))
    assert abs(float(trl) - (1 + 1.005 * (1.6448536269514722 / 0.1) ** 2)) < 1e-6
    assert metrics.min_track_record_length(Decimal("0.05"), Decimal("0.05"), Decimal(0), Decimal(3), Decimal("0.95")) is None
    few = metrics.dsr(sr, n, Decimal(0), Decimal(3), 2, Decimal("0.5"))
    many = metrics.dsr(sr, n, Decimal(0), Decimal(3), 200, Decimal("0.5"))
    assert many < few, "more trials deflate the Sharpe ratio"


def test_drawdowns_and_bootstrap_are_deterministic():
    curve = [Decimal(v) for v in (100, 110, 99, 105, 90, 95)]
    assert metrics.max_drawdown(curve) == Decimal(90) / Decimal(110) - 1
    assert metrics.trailing_drawdown(curve) == Decimal(95) / Decimal(110) - 1
    values = [Decimal("0.001") * (i % 5 - 2) + Decimal("0.0005") for i in range(120)]
    a = metrics.stationary_bootstrap_interval(values, seed=3, resamples=200, mean_block=5, lower=Decimal("0.025"), upper=Decimal("0.975"))
    b = metrics.stationary_bootstrap_interval(values, seed=3, resamples=200, mean_block=5, lower=Decimal("0.025"), upper=Decimal("0.975"))
    c = metrics.stationary_bootstrap_interval(values, seed=4, resamples=200, mean_block=5, lower=Decimal("0.025"), upper=Decimal("0.975"))
    assert a == b and a != c and a[0] < sum(values) < a[1]
