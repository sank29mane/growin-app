"""The PR impact report collects, validates, compares, and renders CI metrics."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / ".github" / "scripts" / "pr_impact.py"
BUDGET = REPO_ROOT / ".github" / "pr-impact-budget.json"
SAFETY = REPO_ROOT / ".github" / "safety-paths.txt"

spec = importlib.util.spec_from_file_location("pr_impact", SCRIPT)
pri = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pri)

JUNIT = """<?xml version="1.0"?>
<testsuites><testsuite name="pytest" errors="1" failures="2" skipped="3" tests="20" time="12.34">
<testcase classname="tests.backend.test_performance_benchmarks" name="test_a" time="0.25"/>
</testsuite></testsuites>"""


def metrics(failed=0, skipped=0, total=100, duration=60.0, files=None, bench=None):
    m = {"schema": 1, "tests": {"total": total, "failed": failed, "skipped": skipped, "duration_s": duration}}
    if files is not None:
        m["coverage"] = {"files": files}
    if bench is not None:
        m["benchmarks"] = {"duration_s": sum(bench.values()), "tests": bench}
    return m


def write(tmp_path: Path, name: str, data) -> Path:
    p = tmp_path / name
    p.write_text(data if isinstance(data, str) else json.dumps(data))
    return p


def run_report(tmp_path, head, base=None, **kw):
    args = ["report", "--head", str(write(tmp_path, "head.json", head)),
            "--budget", str(BUDGET), "--safety-paths", str(SAFETY),
            "--head-sha", "abcdef1234567", "--ci-conclusion", "success",
            "--out", str(tmp_path / "comment.md")]
    if base is not None:
        args += ["--base", str(write(tmp_path, "base.json", base)), "--base-sha", "1234567abcdef"]
    code = pri.main(args)
    return code, (tmp_path / "comment.md").read_text()


# --- collect ---------------------------------------------------------------

def test_collect_reduces_junit_and_coverage(tmp_path):
    junit = write(tmp_path, "j.xml", JUNIT)
    cov = write(tmp_path, "c.json", {"files": {
        "backend/execution/gate.py": {"summary": {"covered_lines": 8, "num_statements": 10}},
        "backend/.venv/lib/x.py": {"summary": {"covered_lines": 1, "num_statements": 1}},
        "backend/empty.py": {"summary": {"covered_lines": 0, "num_statements": 0}},
        "scripts/other.py": {"summary": {"covered_lines": 1, "num_statements": 1}},
    }})
    out = tmp_path / "m.json"
    assert pri.main(["collect", "--junit", str(junit), "--bench-junit", str(junit),
                     "--coverage", str(cov), "--out", str(out)]) == 0
    m = json.loads(out.read_text())
    assert m["tests"] == {"total": 20, "failed": 3, "skipped": 3, "duration_s": 12.34}
    assert m["benchmarks"]["tests"] == {"test_performance_benchmarks::test_a": 0.25}
    assert m["coverage"]["files"] == {"backend/execution/gate.py": [8, 10]}
    pri.load_metrics(out)  # what collect writes must pass validation


def test_collect_writes_nothing_without_reports(tmp_path):
    out = tmp_path / "m.json"
    assert pri.main(["collect", "--junit", str(tmp_path / "none.xml"), "--out", str(out)]) == 0
    assert not out.exists()


# --- validation of untrusted input ----------------------------------------

@pytest.mark.parametrize("mutate", [
    lambda m: m.update(schema=2),
    lambda m: m["tests"].update(failed=-1),
    lambda m: m["tests"].update(failed=True),
    lambda m: m["tests"].update(total=1.5),
    lambda m: m["tests"].update(duration_s=float("inf")),
    lambda m: m.update(coverage={"files": {"backend/a`b.py": [1, 2]}}),
    lambda m: m.update(coverage={"files": {"backend/a.py\n| x": [1, 2]}}),
    lambda m: m.update(coverage={"files": {"backend/a.py": [3, 2]}}),
    lambda m: m.update(coverage={"files": {"backend/a.py": [1]}}),
    lambda m: m.update(benchmarks={"duration_s": 1, "tests": {"bad`name": 1}}),
])
def test_load_metrics_rejects_bad_input(tmp_path, mutate):
    m = metrics()
    mutate(m)
    with pytest.raises(pri.MetricsError):
        pri.load_metrics(write(tmp_path, "m.json", json.dumps(m, allow_nan=True)))


def test_load_metrics_rejects_oversize_and_garbage(tmp_path):
    with pytest.raises(pri.MetricsError):
        pri.load_metrics(write(tmp_path, "m.json", "{" + " " * (pri.MAX_METRICS_BYTES + 1) + "}"))
    with pytest.raises(pri.MetricsError):
        pri.load_metrics(write(tmp_path, "m.json", "not json"))


# --- budget rules ----------------------------------------------------------

def row(rule):
    return {"id": "cov.total_pct", "rule": rule, "warn": True}


@pytest.mark.parametrize("rule,head,base,state", [
    ({"max": 0}, 1, None, "warn"),
    ({"max": 0}, 0, None, "ok"),
    ({"min_delta": -0.5}, 80.0, 80.4, "ok"),
    ({"min_delta": -0.5}, 79.4, 80.0, "warn"),
    ({"min_delta": -0.5}, 79.0, None, "na"),          # no baseline: delta rules skip
    ({"max_delta_pct": 25}, 130, 100, "warn"),
    ({"max_delta_pct": 25}, 125, 100, "ok"),
    ({"max_delta_pct": 25}, 5, 0, "na"),              # zero baseline: no percentage
    ({"max_delta": 0}, 4, 3, "warn"),
    ({"min": 70}, 69.9, None, "warn"),
])
def test_evaluate(rule, head, base, state):
    r = row(rule)
    got = pri.evaluate(r, {"cov.total_pct": head}, None if base is None else {"cov.total_pct": base})
    assert got == state


def test_advisory_breach_warns_and_missing_metric_is_na():
    assert pri.evaluate(row({"max": 0}), {"cov.total_pct": 1}, None) == "warn"
    assert pri.evaluate(row({"max": 0}), {}, None) == "na"


def test_shipped_budget_is_valid():
    rows = pri.load_budget(BUDGET)
    assert {r["id"] for r in rows} == pri.METRIC_IDS
    assert all(r["warn"] is True and "enforce" not in r for r in rows)


def test_safety_coverage_uses_the_guard_globs():
    patterns = pri.load_patterns(SAFETY)
    files = {"backend/execution/a/b.py": (5, 10), "backend/utils/misc.py": (0, 10)}
    vals = pri.derive({"tests": None, "benchmarks": None, "coverage": files}, patterns)
    assert vals["cov.total_pct"] == 25.0
    assert vals["cov.safety_pct"] == 50.0  # `*` crosses `/`, as in safety-guard.sh


# --- report ----------------------------------------------------------------

def test_report_passes_and_renders_the_table(tmp_path):
    files = {"backend/execution/a.py": [8, 10]}
    code, body = run_report(tmp_path, metrics(files=files), metrics(files=files))
    assert code == 0
    assert body.startswith(pri.MARKER)
    assert "Advisory only" in body
    assert "Baseline: `1234567` · PR result: `abcdef1` · Source CI: success" in body


def test_report_warns_on_coverage_drop_and_failing_tests(tmp_path):
    head = metrics(failed=2, files={"backend/execution/a.py": [5, 10]})
    base = metrics(files={"backend/execution/a.py": [9, 10]})
    code, body = run_report(tmp_path, head, base)
    assert code == 0
    assert "3 budget(s) exceeded" in body
    assert "-40.00 pp" in body


def test_report_without_baseline_still_applies_absolute_budgets(tmp_path):
    code, body = run_report(tmp_path, metrics(failed=1))
    assert code == 0
    assert "No main baseline yet" in body


def test_report_with_advisory_breach_exits_zero(tmp_path):
    code, body = run_report(tmp_path, metrics(duration=100.0), metrics(duration=60.0))
    assert code == 0
    assert "Advisory only" in body and "⚠️" in body


def test_report_notice_when_head_metrics_are_missing(tmp_path):
    code = pri.main(["report", "--head", str(tmp_path / "none.json"), "--budget", str(BUDGET),
                     "--safety-paths", str(SAFETY), "--head-sha", "abcdef1",
                     "--ci-conclusion", "failure", "--out", str(tmp_path / "c.md")])
    assert code == 0
    assert "Metrics not evaluated for this commit" in (tmp_path / "c.md").read_text()


def test_report_degrades_when_head_metrics_fail_validation(tmp_path):
    code, body = run_report(tmp_path, '{"schema": 1, "tests": {"total": -5}}')
    assert code == 0
    assert "failed validation" in body


def test_hostile_sha_and_conclusion_never_reach_the_comment(tmp_path):
    args = ["report", "--head", str(write(tmp_path, "h.json", metrics())), "--budget", str(BUDGET),
            "--safety-paths", str(SAFETY), "--head-sha", "x`](http://evil)", "--base-sha", "<b>",
            "--ci-conclusion", "success\n@everyone", "--out", str(tmp_path / "c.md")]
    pri.main(args)
    body = (tmp_path / "c.md").read_text()
    assert "evil" not in body and "@everyone" not in body and "<b>" not in body


# --- workflow wiring -------------------------------------------------------

def test_ci_and_report_workflows_share_the_artifact_name():
    ci = (REPO_ROOT / ".github/workflows/ci.yml").read_text()
    report = (REPO_ROOT / ".github/workflows/pr-impact.yml").read_text()
    assert "name: pr-impact-metrics" in ci
    assert "-n pr-impact-metrics" in report
    assert "--cov-config=.github/pr-impact.coveragerc" in ci
    assert 'workflows: [ "Growin Backend CI" ]' in report
    assert "pull_request_target" not in report
    assert "ref: ${{ github.event.repository.default_branch }}" in report


@pytest.mark.parametrize("pair", [[0, 0], [1, 0]])
def test_zero_coverage_denominator_is_rejected_and_report_degrades(tmp_path, pair):
    payload = metrics(files={"backend/execution/a.py": pair})
    with pytest.raises(pri.MetricsError):
        pri.load_metrics(write(tmp_path, "m.json", payload))
    code, body = run_report(tmp_path, payload, payload)
    assert code == 0
    assert "not evaluated" in body
    assert "✅" not in body


@pytest.mark.parametrize("payload", [
    '{"schema":1,"tests":{"total":' + '9' * 400 + ',"failed":0,"skipped":0,"duration_s":1}}',
    '{"schema":1,"nested":' + '[' * 200000 + '0' + ']' * 200000 + '}',
])
def test_numeric_overflow_and_deep_json_produce_degraded_comment(tmp_path, payload):
    with pytest.raises(pri.MetricsError):
        pri.load_metrics(write(tmp_path, "m.json", payload))
    code, body = run_report(tmp_path, payload)
    assert code == 0
    assert "failed validation" in body and "not evaluated" in body
    assert "✅" not in body


def test_missing_metrics_and_missing_baseline_are_never_green(tmp_path):
    code, body = run_report(tmp_path, {"schema": 1})
    assert code == 0
    assert body.count("➖ not evaluated") == len(pri.METRIC_IDS) + 1
    assert "✅" not in body
    code, body = run_report(tmp_path, metrics())
    total_row = next(line for line in body.splitlines() if "Tests collected" in line)
    assert "not evaluated" in total_row and "✅" not in total_row


def test_approximate_baseline_has_sha_and_age(tmp_path):
    head = pri.load_metrics(write(tmp_path, "h.json", metrics()))
    body, code = pri.render_report(pri.load_budget(BUDGET), head, head, [],
                                   "abcdef1", "1234567", "success", True,
                                   "2026-01-01T00:00:00Z")
    assert code == 0
    assert "approximate baseline (`1234567`, " in body
    assert "hours old)" in body


@pytest.mark.parametrize("exact_available", [True, False])
def test_baseline_prefers_merge_base_and_labels_fallback(tmp_path, monkeypatch, capsys, exact_available):
    exact = {"databaseId": 1, "headSha": "a" * 40, "createdAt": "2026-10-01T00:00:00Z"}
    latest = {"databaseId": 2, "headSha": "b" * 40, "createdAt": "2026-10-02T00:00:00Z"}
    calls = []

    def gh_json(*args):
        if args[0] == "api":
            return {"merge_base_commit": {"sha": exact["headSha"]}}
        return [exact] if "--commit" in args else [latest, exact]

    def download(args, **kwargs):
        calls.append(args[3])
        if args[3] == "1" and not exact_available:
            raise pri.subprocess.CalledProcessError(1, args)
        write(tmp_path, "metrics.json", metrics())

    monkeypatch.setattr(pri, "_gh_json", gh_json)
    monkeypatch.setattr(pri.subprocess, "run", download)
    assert pri.main(["baseline", "--repo", "owner/repo", "--head-sha", "c" * 40,
                     "--out-dir", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    expected = exact if exact_available else latest
    assert f"sha={expected['headSha']}" in output
    assert f"created_at={expected['createdAt']}" in output
    assert f"approximate={str(not exact_available).lower()}" in output
    assert calls == (["1"] if exact_available else ["1", "2"])


def test_baseline_lookup_errors_leave_no_artifact(tmp_path, monkeypatch, capsys):
    write(tmp_path, "metrics.json", metrics())

    def unavailable(*args):
        raise pri.subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(pri, "_gh_json", unavailable)
    assert pri.main(["baseline", "--repo", "owner/repo", "--head-sha", "c" * 40,
                     "--out-dir", str(tmp_path)]) == 0
    assert not (tmp_path / "metrics.json").exists()
    assert "sha=\n" in capsys.readouterr().out


def test_workflow_security_and_advisory_wiring():
    import re
    ci = (REPO_ROOT / ".github/workflows/ci.yml").read_text()
    report = (REPO_ROOT / ".github/workflows/pr-impact.yml").read_text()
    for action in re.findall(r"uses: (.+)", ci + report):
        assert re.fullmatch(r"[\w/-]+@[0-9a-f]{40} # v[\w.]+", action)
    assert "issues: write" not in report
    assert "group: pr-impact-${{ github.event.workflow_run.pull_requests[0].number || github.event.workflow_run.head_sha }}" in report
    assert "enforced" not in report.lower()
    posting = report.split("- name: Post or update the comment")[1]
    assert 'current=$(gh api' in posting
    assert '"$current" != "$HEAD_SHA"' in posting
    assert '"$latest_attempt" != "$SOURCE_ATTEMPT"' in posting
    assert posting.index('"$current" != "$HEAD_SHA"') < posting.index('gh api -X PATCH')
    assert "name: Run SOTA Test Suite" in ci
    assert "name: Run SOTA Unit & Integration Tests\n        timeout-minutes: 20" in ci


@pytest.mark.parametrize("current,attempt,posts", [
    ("new-head", "1", False),
    ("reported-head", "2", False),
    ("reported-head", "1", True),
])
def test_posting_step_skips_stale_head_or_attempt(tmp_path, current, attempt, posts):
    import os
    import subprocess
    import textwrap
    import yaml

    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/pr-impact.yml").read_text())
    step = next(s for s in workflow["jobs"]["report"]["steps"]
                if s["name"] == "Post or update the comment")
    gh = tmp_path / "gh"
    gh.write_text(textwrap.dedent("""\
        #!/bin/bash
        echo "$*" >> "$CALL_LOG"
        case "$*" in
          *--paginate*) echo "42" ;;
          *pulls/544*) echo "$CURRENT_HEAD" ;;
          *actions/runs/123*) echo "$CURRENT_ATTEMPT" ;;
        esac
    """))
    gh.chmod(0o755)
    (tmp_path / "comment.md").write_text("advisory report")
    env = dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}",
               RUNNER_TEMP=str(tmp_path), REPO="owner/repo", PR="544", RUN_ID="123",
               HEAD_SHA="reported-head", SOURCE_ATTEMPT="1", CURRENT_HEAD=current,
               CURRENT_ATTEMPT=attempt, CALL_LOG=str(tmp_path / "calls"))
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", step["run"]],
                            env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert ("-X PATCH" in (tmp_path / "calls").read_text()) is posts
