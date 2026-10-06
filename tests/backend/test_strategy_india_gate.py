"""AC-1: the Phase 59 band coverage gate (D-01, D-01a, D-02)."""

from __future__ import annotations

import json
import os
import stat
from datetime import date
from pathlib import Path

import pytest

from strategy_india import __main__ as cli
from strategy_india.errors import GateRefused
from strategy_india.gate import load_coverage_report

from test_strategy_india_support import TARGETS_SHA, coverage_report, sha, write_coverage

START, END = date(2025, 5, 1), date(2026, 3, 31)
FIXTURES = Path(__file__).parent / "fixtures" / "strategy_india"


def _write(tmp_path: Path, **kw) -> Path:
    return write_coverage(tmp_path, coverage_report(kw.pop("start", START), kw.pop("end", END), **kw))


def _args(path: Path, *extra: str) -> list[str]:
    return ["check-gate", "--coverage-report", str(path), "--run-start", START.isoformat(), "--run-end", END.isoformat(), "--targets-sha256", TARGETS_SHA, *extra]


def test_blocked_report_refuses_with_typed_error_and_cli_exit_3(tmp_path, capsys):
    path = _write(tmp_path, blocked=("band_convention_unverified",))
    with pytest.raises(GateRefused) as err:
        load_coverage_report(path, run_start=START, run_end=END, targets_sha256=TARGETS_SHA)
    assert err.value.code == "phase62_blocked"
    assert err.value.exit_code == 3
    assert cli.main(_args(path)) == 3
    assert "phase62_blocked" in capsys.readouterr().out


def test_missing_report_refuses(tmp_path, capsys):
    gone = tmp_path / "reports" / "band-coverage-nothing.json"
    with pytest.raises(GateRefused) as err:
        load_coverage_report(gone, run_start=START, run_end=END, targets_sha256=TARGETS_SHA)
    assert err.value.code == "report_missing"
    assert cli.main(_args(gone)) == 3


def test_unreadable_report_refuses(tmp_path):
    path = tmp_path / "junk.json"
    path.write_text("{not json")
    with pytest.raises(GateRefused) as err:
        load_coverage_report(path, run_start=START, run_end=END, targets_sha256=TARGETS_SHA)
    assert err.value.code == "report_invalid"


def _rewrite(path: Path, mutate) -> Path:
    os.chmod(path, stat.S_IWUSR | stat.S_IRUSR)
    body = json.loads(path.read_text())
    mutate(body)
    path.write_text(json.dumps(body))
    return path


def test_tampered_blocked_flag_refuses(tmp_path):
    path = _write(tmp_path, blocked=("x: 3 sessions",))
    _rewrite(path, lambda body: body.update(phase62_blocked=False))
    with pytest.raises(GateRefused) as err:
        load_coverage_report(path, run_start=START, run_end=END, targets_sha256=TARGETS_SHA)
    assert err.value.code == "report_tampered"


def test_tampered_sha_refuses_against_registered_and_file_name(tmp_path):
    path = _write(tmp_path)
    report_sha = json.loads(path.read_text())["report_sha256"]
    load_coverage_report(path, run_start=START, run_end=END, targets_sha256=TARGETS_SHA, expected_report_sha256=report_sha)
    with pytest.raises(GateRefused) as err:
        load_coverage_report(path, run_start=START, run_end=END, targets_sha256=TARGETS_SHA, expected_report_sha256=sha("other"))
    assert err.value.code == "report_tampered"
    _rewrite(path, lambda body: body.update(report_sha256=sha("forged")))
    with pytest.raises(GateRefused) as err:  # the file name still carries the original prefix
        load_coverage_report(path, run_start=START, run_end=END, targets_sha256=TARGETS_SHA)
    assert err.value.code == "report_tampered"


def test_edited_file_bytes_refuse_against_registered_file_hash(tmp_path):
    import hashlib

    path = _write(tmp_path, unavailable=[("AAA", "INE000A01000", date(2025, 6, 2))])
    registered = hashlib.sha256(path.read_bytes()).hexdigest()
    _rewrite(path, lambda body: body.update(sessions=101))
    with pytest.raises(GateRefused) as err:
        load_coverage_report(path, run_start=START, run_end=END, targets_sha256=TARGETS_SHA, expected_file_sha256=registered)
    assert err.value.code == "report_tampered"


def test_unavailable_list_must_match_counts(tmp_path):
    path = _write(tmp_path, unavailable=[("AAA", "INE000A01000", date(2025, 6, 2))])
    _rewrite(path, lambda body: body.update(unavailable_bands=[]))
    with pytest.raises(GateRefused) as err:
        load_coverage_report(path, run_start=START, run_end=END, targets_sha256=TARGETS_SHA)
    assert err.value.code == "report_tampered"


@pytest.mark.parametrize(
    "start, end",
    [(date(2025, 6, 1), END), (START, date(2026, 3, 1))],
    ids=["starts_late", "ends_early"],
)
def test_short_period_report_refuses(tmp_path, start, end):
    path = _write(tmp_path, start=start, end=end)
    with pytest.raises(GateRefused) as err:
        load_coverage_report(path, run_start=START, run_end=END, targets_sha256=TARGETS_SHA)
    assert err.value.code == "report_period_short"


def test_unblocked_report_with_three_unavailable_bands_runs(tmp_path):
    unknowns = [("AAA", "INE000A01000", date(2025, 6, 2)), ("BBB", "INE000A01001", date(2025, 7, 1)),
                ("AAA", "INE000A01000", date(2025, 9, 3))]
    path = _write(tmp_path, unavailable=unknowns)
    result = load_coverage_report(path, run_start=START, run_end=END, targets_sha256=TARGETS_SHA)
    assert len(result.unavailable) == 3
    assert result.index() == {(isin, day): "band_crosscheck_row_conflict" for _, isin, day in unknowns}
    assert cli.main(_args(path)) == 0


def test_committed_synthetic_fixtures_gate_as_named():
    clear = next((FIXTURES / "coverage_clear").glob("band-coverage-*.json"))
    blocked = next((FIXTURES / "coverage_blocked").glob("band-coverage-*.json"))
    assert len(load_coverage_report(clear, run_start=START, run_end=END, targets_sha256=TARGETS_SHA).unavailable) == 3
    with pytest.raises(GateRefused) as err:
        load_coverage_report(blocked, run_start=START, run_end=END, targets_sha256=TARGETS_SHA)
    assert err.value.code == "phase62_blocked"


def test_a_tampered_but_internally_consistent_report_is_refused_by_the_recomputed_hash(tmp_path):
    """Edits that keep every cross-check consistent (flags, counts, name, registered sha) still change the content hash."""
    path = _write(tmp_path)
    original = json.loads(path.read_text())["report_sha256"]
    _rewrite(path, lambda body: body.update(fixed_count=body["fixed_count"] + 1))  # a quietly inflated coverage count
    with pytest.raises(GateRefused) as err:
        load_coverage_report(path, run_start=START, run_end=END, targets_sha256=TARGETS_SHA, expected_report_sha256=original)
    assert err.value.code == "report_tampered" and "content" in str(err.value)


def test_the_recomputed_hash_depends_on_the_target_universe(tmp_path):
    path = _write(tmp_path)
    load_coverage_report(path, run_start=START, run_end=END, targets_sha256=TARGETS_SHA)
    with pytest.raises(GateRefused) as err:
        load_coverage_report(path, run_start=START, run_end=END, targets_sha256=sha("another universe"))
    assert err.value.code == "report_tampered"


def test_a_file_name_that_does_not_follow_the_59_pattern_or_the_report_is_refused(tmp_path):
    path = _write(tmp_path)
    renamed = path.with_name("coverage.json")
    renamed.write_bytes(path.read_bytes())
    with pytest.raises(GateRefused) as err:
        load_coverage_report(renamed, run_start=START, run_end=END, targets_sha256=TARGETS_SHA)
    assert err.value.code == "report_tampered" and "file name" in str(err.value)
    other_period = path.with_name(path.name.replace("2025-05-01", "2025-05-02"))
    other_period.write_bytes(path.read_bytes())
    with pytest.raises(GateRefused):
        load_coverage_report(other_period, run_start=START, run_end=END, targets_sha256=TARGETS_SHA)
    wrong_prefix = path.with_name(path.name[:-17] + "ffffffffffff.json")
    wrong_prefix.write_bytes(path.read_bytes())
    with pytest.raises(GateRefused):
        load_coverage_report(wrong_prefix, run_start=START, run_end=END, targets_sha256=TARGETS_SHA)


def test_a_report_built_by_the_real_59_code_passes_the_recomputation(tmp_path):
    """The derivation here must equal 59's own, so build a report with 59 build_band_coverage and gate it."""
    from pilot_data.price_bands import write_coverage_report
    from pilot_data.store import PilotDataStore

    import test_pilot_data_price_bands as band_tests

    with PilotDataStore(tmp_path / "pilot", workspace="india") as store:
        band_tests.full_world(store)
        real = band_tests.coverage(store)
        path = write_coverage_report(tmp_path / "real", real)
    targets = band_tests.TARGETS.target_sha256
    result = load_coverage_report(path, run_start=band_tests.S1, run_end=band_tests.S5, targets_sha256=targets)
    assert result.report_sha256 == real.report_sha256
    with pytest.raises(GateRefused):
        load_coverage_report(path, run_start=band_tests.S1, run_end=band_tests.S5, targets_sha256=sha("x"))
