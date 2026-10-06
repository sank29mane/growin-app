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

from test_strategy_india_support import coverage_report, sha, write_coverage

START, END = date(2025, 5, 1), date(2026, 3, 31)
FIXTURES = Path(__file__).parent / "fixtures" / "strategy_india"


def _write(tmp_path: Path, **kw) -> Path:
    return write_coverage(tmp_path, coverage_report(kw.pop("start", START), kw.pop("end", END), **kw))


def _args(path: Path, *extra: str) -> list[str]:
    return ["check-gate", "--coverage-report", str(path), "--run-start", START.isoformat(), "--run-end", END.isoformat(), *extra]


def test_blocked_report_refuses_with_typed_error_and_cli_exit_3(tmp_path, capsys):
    path = _write(tmp_path, blocked=("band_convention_unverified",))
    with pytest.raises(GateRefused) as err:
        load_coverage_report(path, run_start=START, run_end=END)
    assert err.value.code == "phase62_blocked"
    assert err.value.exit_code == 3
    assert cli.main(_args(path)) == 3
    assert "phase62_blocked" in capsys.readouterr().out


def test_missing_report_refuses(tmp_path, capsys):
    gone = tmp_path / "reports" / "band-coverage-nothing.json"
    with pytest.raises(GateRefused) as err:
        load_coverage_report(gone, run_start=START, run_end=END)
    assert err.value.code == "report_missing"
    assert cli.main(_args(gone)) == 3


def test_unreadable_report_refuses(tmp_path):
    path = tmp_path / "junk.json"
    path.write_text("{not json")
    with pytest.raises(GateRefused) as err:
        load_coverage_report(path, run_start=START, run_end=END)
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
        load_coverage_report(path, run_start=START, run_end=END)
    assert err.value.code == "report_tampered"


def test_tampered_sha_refuses_against_registered_and_file_name(tmp_path):
    path = _write(tmp_path)
    report_sha = json.loads(path.read_text())["report_sha256"]
    load_coverage_report(path, run_start=START, run_end=END, expected_report_sha256=report_sha)
    with pytest.raises(GateRefused) as err:
        load_coverage_report(path, run_start=START, run_end=END, expected_report_sha256=sha("other"))
    assert err.value.code == "report_tampered"
    _rewrite(path, lambda body: body.update(report_sha256=sha("forged")))
    with pytest.raises(GateRefused) as err:  # the file name still carries the original prefix
        load_coverage_report(path, run_start=START, run_end=END)
    assert err.value.code == "report_tampered"


def test_edited_file_bytes_refuse_against_registered_file_hash(tmp_path):
    import hashlib

    path = _write(tmp_path, unavailable=[("AAA", "INE000A01000", date(2025, 6, 2))])
    registered = hashlib.sha256(path.read_bytes()).hexdigest()
    _rewrite(path, lambda body: body.update(sessions=101))
    with pytest.raises(GateRefused) as err:
        load_coverage_report(path, run_start=START, run_end=END, expected_file_sha256=registered)
    assert err.value.code == "report_tampered"


def test_unavailable_list_must_match_counts(tmp_path):
    path = _write(tmp_path, unavailable=[("AAA", "INE000A01000", date(2025, 6, 2))])
    _rewrite(path, lambda body: body.update(unavailable_bands=[]))
    with pytest.raises(GateRefused) as err:
        load_coverage_report(path, run_start=START, run_end=END)
    assert err.value.code == "report_tampered"


@pytest.mark.parametrize(
    "start, end",
    [(date(2025, 6, 1), END), (START, date(2026, 3, 1))],
    ids=["starts_late", "ends_early"],
)
def test_short_period_report_refuses(tmp_path, start, end):
    path = _write(tmp_path, start=start, end=end)
    with pytest.raises(GateRefused) as err:
        load_coverage_report(path, run_start=START, run_end=END)
    assert err.value.code == "report_period_short"


def test_unblocked_report_with_three_unavailable_bands_runs(tmp_path):
    unknowns = [("AAA", "INE000A01000", date(2025, 6, 2)), ("BBB", "INE000A01001", date(2025, 7, 1)),
                ("AAA", "INE000A01000", date(2025, 9, 3))]
    path = _write(tmp_path, unavailable=unknowns)
    result = load_coverage_report(path, run_start=START, run_end=END)
    assert len(result.unavailable) == 3
    assert result.index() == {(isin, day): "band_crosscheck_row_conflict" for _, isin, day in unknowns}
    assert cli.main(_args(path)) == 0


def test_committed_synthetic_fixtures_gate_as_named():
    clear = FIXTURES / "synthetic_coverage_report_clear.json"
    blocked = FIXTURES / "synthetic_coverage_report_blocked.json"
    assert len(load_coverage_report(clear, run_start=START, run_end=END).unavailable) == 3
    with pytest.raises(GateRefused) as err:
        load_coverage_report(blocked, run_start=START, run_end=END)
    assert err.value.code == "phase62_blocked"
