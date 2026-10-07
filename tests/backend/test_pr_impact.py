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


# --- safety-path matcher (port of safety-guard.sh) -------------------------

GLOB_CASES = [
    # `*` and `?` cross `/`, and a leading `.` needs no special match
    ("backend/execution/a/b.py", "backend/execution/*", True),
    (".github/workflows/ci.yml", ".github/*", True),
    ("backend/execution", "backend/execution/*", False),
    ("backend/server.py", "backend/server.py", True),
    ("backend/serverXpy", "backend/server.py", False),
    ("a.py", "?.py", True),
    ("ab.py", "?.py", False),
    ("/.py", "?.py", True),
    # brackets
    ("v1.txt", "v[0-9].txt", True),
    ("va.txt", "v[!0-9].txt", True),
    ("v1.txt", "v[^0-9].txt", False),
    ("]", "[]]", True),
    ("-", "[a-]", True),
    ("A", "[[:upper:]]", True),
    ("a", "[[:upper:]]", False),
    ("[a", "[a", True),
    # escapes
    ("a*b", "a\\*b", True),
    ("axb", "a\\*b", False),
    # extglob
    ("backend/x.py", "backend/@(x|y).py", True),
    ("backend/z.py", "backend/@(x|y).py", False),
    ("ab", "?(a)b", True),
    ("b", "?(a)b", True),
    ("aab", "?(a)b", False),
    ("aaab", "*(a)b", True),
    ("b", "*(a)b", True),
    ("b", "+(a)b", False),
    ("aab", "+(a)b", True),
    ("foo.py", "!(*.md)", True),
    ("foo.md", "!(*.md)", False),
    ("backend/execution/x.py", "backend/!(execution)/*", False),
    ("backend/brokers/x.py", "backend/!(execution)/*", True),
    ("abcaxc", "+(a@(b|x)c)", True),
    ("abcayc", "+(a@(b|x)c)", False),
    ("Growin/Security/Keys.swift", "Growin/@(Security|Auth)/*.swift", True),
]


@pytest.mark.parametrize("path,pattern,expected", GLOB_CASES)
def test_glob_match_follows_bash_extglob(path, pattern, expected):
    assert pri.glob_match(path, pattern) is expected


def _bash_match(path, pattern):
    import shutil
    import subprocess
    bash = shutil.which("bash")
    if bash is None:
        pytest.skip("bash not available")
    # Same construct as safety-guard.sh: unquoted pattern inside [[ ]].
    # -O extglob makes bash 3.2 behave like bash >= 4.1 inside [[ ]].
    r = subprocess.run([bash, "-O", "extglob", "-c", '[[ "$1" == $2 ]]', "_", path, pattern])
    return r.returncode == 0


@pytest.mark.parametrize("path,pattern,expected", GLOB_CASES)
def test_glob_cases_agree_with_real_bash(path, pattern, expected):
    assert _bash_match(path, pattern) is expected


@pytest.mark.parametrize("path", [
    "backend/execution/gate.py", "backend/executions.py", ".github/safety-paths.txt",
    "Growin/Security/Signer.swift", "Growin/SecurityView.swift", "private/x/y",
    "backend/server.py", "backend/server.pyc", "tests/backend/test_private_config.py",
    "docs/README.md", "backend/utils/risk_engine.py", "backend/utils/risk_engine_v2.py",
])
def test_shipped_patterns_agree_with_real_bash(path):
    patterns = pri.load_patterns(SAFETY)
    assert pri.matches_safety(path, patterns) is any(_bash_match(path, p) for p in patterns)


def test_empty_path_never_matches_like_the_guard():
    assert pri.glob_match("", "*") is True       # raw bash semantics
    assert pri.matches_safety("", ["*"]) is False  # guard's `matches` returns 1
    assert pri.matches_safety(None, ["*"]) is False


def test_glob_match_refuses_to_guess_on_long_input():
    with pytest.raises(pri.GlobUnevaluable):
        pri.glob_match("a" * 5000, "*")
    assert pri.glob_match("a" * 200 + "b", "*(*(a))b") is True


LONG_SAFETY = "backend/execution/" + "a" * 1010 + ".py"
LONG_OTHER = "docs/" + "b" * 1023 + ".md"


def test_unevaluable_paths_fail_closed_as_safety_paths():
    # C1: bash flags a 1,031-char backend/execution/ path. The matcher cannot
    # evaluate it, so it must count as a safety path, not silently pass.
    assert len(LONG_SAFETY) == 1031 and len(LONG_OTHER) == 1031
    patterns = pri.load_patterns(SAFETY)
    assert pri.matches_safety(LONG_SAFETY, patterns) is True
    assert pri.matches_safety(LONG_OTHER, patterns) is True    # unknown means required
    assert pri.matches_safety("docs/short.md", patterns) is False


def test_unevaluable_path_is_flagged_in_the_footprint_and_readiness():
    files = [{"filename": LONG_SAFETY, "status": "added", "additions": 1, "deletions": 0}]
    ctx = ctx_for(files=files)
    assert ctx["safety_required"] is True
    assert ctx["footprint"]["safety"][0]["kind"] == "unchecked"
    body = pri.render_footprint(ctx)
    assert "(unprintable path) (too long to check, so treated as a safety path)" in body
    assert "`safety-reviewed`: required, missing" in pri.readiness_line(ctx)
    renamed = [{"filename": "docs/x.md", "previous_filename": LONG_OTHER, "status": "renamed"}]
    assert ctx_for(files=renamed)["footprint"]["safety"][0]["kind"] == "unchecked"


# --- change footprint ------------------------------------------------------

FILES = [
    {"filename": "backend/routes/x.py", "status": "modified", "additions": 10, "deletions": 2},
    {"filename": "backend/execution/gate.py", "status": "modified", "additions": 3, "deletions": 1},
    {"filename": "tests/backend/test_x.py", "status": "added", "additions": 50, "deletions": 0},
    {"filename": "tests/backend/test_gone.py", "status": "removed", "additions": 0, "deletions": 9},
    {"filename": "scripts/moved.py", "status": "renamed", "previous_filename": "tests/backend/moved.py",
     "additions": 0, "deletions": 0},
    {"filename": "backend/new_name.py", "status": "renamed", "previous_filename": "backend/brokers/old.py",
     "additions": 1, "deletions": 1},
    {"filename": "Growin/Views/A.swift", "status": "modified", "additions": 4, "deletions": 4},
    {"filename": "Growin.xcodeproj/project.pbxproj", "status": "modified", "additions": 1, "deletions": 1},
    {"filename": "GrowinTests/ATests.swift", "status": "added", "additions": 7, "deletions": 0},
    {"filename": "GrowinUITests/BTests.swift", "status": "modified", "additions": 1, "deletions": 0},
    {"filename": ".github/workflows/ci.yml", "status": "modified", "additions": 2, "deletions": 2},
    {"filename": "gateway/vm/a.py", "status": "added", "additions": 5, "deletions": 0},
    {"filename": "README.md", "status": "modified", "additions": 1, "deletions": 0},
    {"filename": "junk", "status": "modified", "additions": "9", "deletions": True},  # bad numbers
    {"status": "modified"},                                                           # no filename
    "not a dict",
]


def test_footprint_groups_by_area_in_fixed_order():
    fp = pri.footprint(FILES, pri.load_patterns(SAFETY))
    assert list(fp["areas"]) == ["Backend app", "Backend tests", "Swift app", "Swift tests",
                                 "CI", "Gateway", "Docs and other"]
    assert fp["areas"]["Backend app"] == [3, 14, 4]
    assert fp["areas"]["Backend tests"] == [2, 50, 9]
    assert fp["areas"]["Swift app"] == [2, 5, 5]
    assert fp["areas"]["Swift tests"] == [2, 8, 0]
    assert fp["areas"]["Docs and other"] == [3, 1, 0]  # scripts/moved.py, README.md, junk


def test_footprint_flags_what_safety_guard_flags():
    fp = pri.footprint(FILES, pri.load_patterns(SAFETY))
    flagged = {(h["kind"], h["path"]) for h in fp["safety"]}
    assert flagged == {
        ("path", "backend/execution/gate.py"),
        ("path", "backend/new_name.py"),          # renamed out of backend/brokers/
        ("deleted test", "tests/backend/test_gone.py"),
        ("test moved out", "scripts/moved.py"),
        ("path", ".github/workflows/ci.yml"),
    }


def ctx_for(files=FILES, labeled=False, draft=False, base="main", parent=None,
            mergeable=True, state="clean"):
    pull = {"draft": draft, "base": {"ref": base, "repo": {"default_branch": "main"}},
            "mergeable": mergeable, "mergeable_state": state,
            "labels": [{"name": "safety-reviewed"}] if labeled else [{"name": "enhancement"}]}
    return pri.build_context(pull, files, parent, pri.load_patterns(SAFETY))


def test_render_footprint_lists_safety_paths_and_label_note():
    body = pri.render_footprint(ctx_for())
    assert "| Backend app | 3 | +14 | -4 |" in body
    assert "| **Total** | **14** | **+85** | **-20** |" in body  # two malformed entries skipped
    assert "**Safety paths (5).**" in body and "`safety-reviewed` label" in body
    assert "Label: missing." in body
    assert "- `backend/new_name.py` (renamed from `backend/brokers/old.py`)" in body
    assert "- `tests/backend/test_gone.py` (deleted test)" in body
    assert "- `scripts/moved.py` (test moved out of `tests/backend/moved.py`)" in body
    assert "No safety paths touched." in pri.render_footprint(ctx_for(files=FILES[:1]))
    assert "Unavailable" in pri.render_footprint(None)


def test_render_footprint_never_prints_hostile_file_names():
    files = [{"filename": "backend/execution/a`](http://evil)|x.py", "status": "pwn<b>",
              "additions": 1, "deletions": 0}]
    body = pri.render_footprint(ctx_for(files=files))
    assert "evil" not in body and "<b>" not in body and "pwn" not in body
    assert "(unprintable path) (changed)" in body


# --- merge readiness and stacked detection ---------------------------------

def test_readiness_line_states():
    line = pri.readiness_line(ctx_for())
    assert line == ("**Merge readiness:** Ready for review · base `main` · mergeable: clean"
                    " · `safety-reviewed`: required, missing")
    line = pri.readiness_line(ctx_for(labeled=True, draft=True, base="feat/a", parent=552,
                                      mergeable=False, state="dirty"))
    assert "Draft · base `feat/a` (stacked on #552) · mergeable: no, conflicts" in line
    assert "required, present" in line
    line = pri.readiness_line(ctx_for(files=FILES[:1], base="feat/b", mergeable=None, state="unknown"))
    assert "(no open parent PR found)" in line and "still computing" in line
    assert "`safety-reviewed`: not required" in line


OPEN_PRS = [
    {"number": 553, "head": {"ref": "feat/x", "repo": {"full_name": "fork/growin-app"}}},
    {"number": 552, "head": {"ref": "feat/x", "repo": {"full_name": "owner/repo"}}},
    {"number": 551, "head": {"ref": "feat/y", "repo": None}},
]


def test_find_parent_ignores_forks_with_the_same_branch_name():
    assert pri.find_parent("feat/x", "owner/repo", OPEN_PRS) == 552
    assert pri.find_parent("feat/x", "other/repo", OPEN_PRS) is None
    assert pri.find_parent("feat/y", "owner/repo", OPEN_PRS) is None
    assert pri.find_parent("feat/x", "owner/repo", [{"number": True, "head": OPEN_PRS[1]["head"]}]) is None


def fake_api(routes, calls=None):
    """Route GETs by path prefix. Unknown paths fail like `gh api` does."""
    def api(path, method="GET", payload=None):
        if calls is not None:
            calls.append((method, path, payload))
        for prefix, value in routes:
            if path.startswith(prefix):
                return value
        raise pri.subprocess.CalledProcessError(1, ["gh", "api", path])
    return api


def test_fetch_pr_context_detects_stack_and_retries_mergeable():
    calls, slept = [], []
    pull_unknown = {"state": "open", "draft": False, "mergeable": None, "mergeable_state": "unknown",
                    "base": {"ref": "feat/x", "repo": {"default_branch": "main"}}, "labels": []}
    pull_known = dict(pull_unknown, mergeable=True, mergeable_state="clean")
    seq = iter([pull_unknown, pull_known])
    base = fake_api([
        ("repos/owner/repo/pulls/554/files", FILES[:2]),
        ("repos/owner/repo/pulls?state=open&head=owner%3Afeat%2Fx", OPEN_PRS),
    ], calls)

    def api(path, method="GET", payload=None):
        if path == "repos/owner/repo/pulls/554":
            calls.append((method, path, payload))
            return next(seq)
        return base(path, method, payload)

    ctx = pri.fetch_pr_context("owner/repo", 554, pri.load_patterns(SAFETY), api, slept.append)
    assert ctx["parent"] == 552 and ctx["mergeable"] is True and slept == [3]
    assert ctx["safety_required"] is True
    assert all(method == "GET" for method, _, _ in calls)
    assert "repos/owner/repo/pulls/554/files?per_page=100&page=1" in [p for _, p, _ in calls]


def test_fetch_pr_context_skips_parent_lookup_on_main_and_rejects_bad_repo():
    calls = []
    pull = {"state": "open", "mergeable": True, "base": {"ref": "main", "repo": {"default_branch": "main"}}}
    api = fake_api([("repos/o/r/pulls/9/files", []), ("repos/o/r/pulls/9", pull)], calls)
    ctx = pri.fetch_pr_context("o/r", 9, ["x"], api, lambda s: None)
    assert ctx["parent"] is None and not any("head=" in p for _, p, _ in calls)
    with pytest.raises(ValueError):
        pri.fetch_pr_context("o/r;rm -rf", 9, ["x"], api, lambda s: None)


def test_api_pages_follows_pages_and_stops_at_limit():
    pages = {1: list(range(100)), 2: list(range(100, 150))}
    seen = []

    def api(path, method="GET", payload=None):
        seen.append(path)
        return pages[int(path.rsplit("page=", 1)[1])]

    assert pri.api_pages(api, "repos/o/r/pulls/1/files", 3000) == list(range(150))
    assert seen == ["repos/o/r/pulls/1/files?per_page=100&page=1",
                    "repos/o/r/pulls/1/files?per_page=100&page=2"]
    assert len(pri.api_pages(api, "x?state=open", 120)) == 120
    with pytest.raises(ValueError):
        pri.api_pages(lambda *a, **k: {"message": "nope"}, "x", 10)


# --- headline verdict and summary line --------------------------------------

ROWS = pri.load_budget(BUDGET)


def vals(**kw):
    out = {"tests.total": 100, "tests.failed": 0, "tests.skipped": 0, "tests.duration_s": 60.0,
           "cov.total_pct": 70.0, "cov.safety_pct": 80.0}
    out.update({k.replace("__", "."): v for k, v in kw.items()})
    return out


def states_for(hv, bv):
    return [pri.evaluate(r, hv, bv) for r in ROWS]


@pytest.mark.parametrize("ci,hv,bv,expected", [
    ("success", vals(), vals(), "🟢 Healthy"),
    ("success", vals(), None, "🟢 Healthy (no main baseline to compare)"),
    ("failure", vals(), vals(), "🔴 CI failed"),
    ("timed_out", vals(), vals(), "🔴 CI timed out"),
    ("failure", vals(tests__failed=2), vals(), "🔴 CI failed; 2 failing tests"),
    ("success", vals(tests__failed=1), vals(), "🔴 1 failing test"),
    ("success", vals(tests__duration_s=100.0), vals(),
     "🟡 Healthy, needs attention: over budget: Suite time"),
    ("success", vals(cov__total_pct=60.0, tests__total=90), vals(),
     "🟡 Healthy, needs attention: over budget: Tests collected, Backend line coverage"),
    ("success", {}, None, "🟡 Healthy, needs attention: test metrics missing"),
    ("success", {"tests.total": 1, "tests.failed": 0}, None,
     "🟡 Healthy, needs attention: coverage missing"),
    ("weird`@everyone", vals(), vals(), "🟡 Healthy, needs attention: CI result unknown"),
])
def test_headline_rules(ci, hv, bv, expected):
    assert pri.headline(ci, hv, ROWS, states_for(hv, bv), None, has_base=bv is not None) == expected


def test_headline_includes_readiness_problems():
    hv = vals()
    ctx = ctx_for(mergeable=False, state="dirty")
    got = pri.headline("success", hv, ROWS, states_for(hv, hv), ctx)
    assert got == "🟡 Healthy, needs attention: merge conflicts; needs `safety-reviewed` label"
    assert pri.headline("success", hv, ROWS, states_for(hv, hv), ctx_for(labeled=True)) == "🟢 Healthy"
    assert pri.headline("success", {}, [], [], None, notice="metrics not evaluated").startswith("🟡")
    assert pri.headline("failure", {}, [], [], None, notice="metrics not evaluated") == "🔴 CI failed"


def test_summary_line_with_and_without_baseline():
    head = vals(tests__total=112, tests__duration_s=66.0, cov__total_pct=70.25)
    line = pri.summary_line(head, vals())
    assert line == ("Tests 112 (+12) · Failed 0 · Coverage 70.25% (+0.25 pp) · "
                    "Safety-path coverage 80.00% (+0.00 pp) · Suite time 1m 06s (+10.0%)")
    assert pri.summary_line({}, None) == ("Tests n/a · Failed n/a · Coverage n/a · "
                                          "Safety-path coverage n/a · Suite time n/a")
    assert pri.summary_line(vals(), None).startswith("Tests 100 · Failed 0 · Coverage 70.00% ·")


# --- report v2 layout -------------------------------------------------------

def report_args(tmp_path, head, base=None, ci="success"):
    args = ["report", "--head", str(head), "--budget", str(BUDGET), "--safety-paths", str(SAFETY),
            "--head-sha", "abcdef1", "--ci-conclusion", ci, "--repo", "owner/repo", "--pr", "7",
            "--out", str(tmp_path / "c.md")]
    if base is not None:
        args += ["--base", str(base), "--base-sha", "1234567"]
    return args


def test_report_v2_layout_with_pr_context(tmp_path, monkeypatch):
    pull = {"state": "open", "draft": False, "mergeable": True, "mergeable_state": "clean",
            "base": {"ref": "main", "repo": {"default_branch": "main"}}, "labels": []}
    calls = []
    monkeypatch.setattr(pri, "gh_api", fake_api(
        [("repos/owner/repo/pulls/7/files", FILES[:3]), ("repos/owner/repo/pulls/7", pull)], calls))
    m = metrics(files={"backend/execution/a.py": [8, 10]})
    assert pri.main(report_args(tmp_path, write(tmp_path, "h.json", m), write(tmp_path, "b.json", m))) == 0
    body = (tmp_path / "c.md").read_text()
    lines = body.splitlines()
    assert lines[0] == pri.MARKER
    assert lines[3] == "**🟡 Healthy, needs attention: needs `safety-reviewed` label**"
    assert lines[5].startswith("Tests 100 (+0) · Failed 0 · Coverage 80.00% (+0.00 pp)")
    assert lines[7].startswith("**Merge readiness:** Ready for review · base `main`")
    assert lines[9] == pri.LABEL_NOTE  # O7
    assert "refreshes on the next CI run" in pri.LABEL_NOTE and "tracks label changes live" in pri.LABEL_NOTE
    order = [body.index(s) for s in ("**Merge readiness:**", "| Area | Metric |",
                                     "### Change footprint", "Baseline: `1234567`")]
    assert order == sorted(order)
    assert "- `backend/execution/gate.py` (modified)" in body
    assert all(method == "GET" for method, _, _ in calls)


def test_report_degrades_when_the_pr_lookup_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(pri, "gh_api", fake_api([]))
    assert pri.main(report_args(tmp_path, write(tmp_path, "h.json", metrics()))) == 0
    body = (tmp_path / "c.md").read_text()
    assert "### Change footprint\n\nUnavailable" in body and "Merge readiness" not in body
    # O1: a failed lookup must never read as healthy, even with perfect metrics.
    m = metrics(files={"backend/execution/a.py": [8, 10]})
    assert pri.main(report_args(tmp_path, write(tmp_path, "h.json", m), write(tmp_path, "b.json", m))) == 0
    body = (tmp_path / "c.md").read_text()
    assert body.splitlines()[3] == "**🟡 Healthy, needs attention: merge state unavailable**"
    assert "🟢" not in body and pri.LABEL_NOTE not in body


def test_headline_yellow_when_merge_context_is_missing():
    hv = vals()
    states = states_for(hv, hv)
    assert pri.headline("success", hv, ROWS, states) == "🟢 Healthy"
    assert pri.headline("success", hv, ROWS, states, ctx_missing=True) == \
        "🟡 Healthy, needs attention: merge state unavailable"


def test_headline_carries_draft_and_stack_position():
    # O2
    hv = vals()
    states = states_for(hv, hv)
    ok = dict(files=FILES[:1])
    assert pri.headline("success", hv, ROWS, states, ctx_for(draft=True, **ok)) == "🟢 Healthy · Draft"
    assert pri.headline("success", hv, ROWS, states,
                        ctx_for(draft=True, base="feat/a", parent=552, **ok)) == \
        "🟢 Healthy · Draft · stacked on #552"
    assert pri.headline("failure", hv, ROWS, states, ctx_for(base="feat/b", **ok)) == \
        "🔴 CI failed · stacked, no open parent PR"
    assert pri.headline("success", hv, ROWS, states, ctx_for(**ok)) == "🟢 Healthy"


@pytest.mark.parametrize("mergeable,state,reason", [
    (None, "unknown", "mergeability still computing"),
    (True, "blocked", "merge blocked by required checks or label"),
    (True, "behind", "merge behind base"),
    (True, "dirty", "merge has conflicts"),
    (True, "draft", "merge waits on draft status"),
    (True, "odd<b>", "merge state unknown"),
])
def test_headline_not_green_unless_github_says_mergeable(mergeable, state, reason):
    # Round 2: mergeable=None or a blocking state must not read as healthy.
    hv = vals()
    ctx = ctx_for(files=FILES[:1], mergeable=mergeable, state=state)
    assert pri.headline("success", hv, ROWS, states_for(hv, hv), ctx) == \
        f"🟡 Healthy, needs attention: {reason}"


@pytest.mark.parametrize("state", ["clean", "has_hooks", "unstable"])
def test_headline_green_states(state):
    hv = vals()
    ctx = ctx_for(files=FILES[:1], state=state)
    assert pri.headline("success", hv, ROWS, states_for(hv, hv), ctx) == "🟢 Healthy"


def test_notice_with_failed_lookup_says_merge_state_unavailable(tmp_path, monkeypatch):
    # Round 2: no metrics AND a failed PR lookup. The flag must reach render_notice.
    monkeypatch.setattr(pri, "gh_api", fake_api([]))
    assert pri.main(report_args(tmp_path, tmp_path / "missing.json")) == 0
    body = (tmp_path / "c.md").read_text()
    assert body.splitlines()[3] == \
        "**🟡 Healthy, needs attention: metrics not evaluated; merge state unavailable**"
    assert "### Change footprint\n\nUnavailable" in body


def test_too_deep_pattern_fails_closed():
    # Round 2: a pattern nested deeper than Python's recursion limit, but
    # under the length cap, cannot be evaluated, so the path counts as safety.
    depth = 340
    pattern = "@(" * depth + "a" + ")" * depth
    assert len(pattern) <= pri.MAX_MATCH_LEN
    with pytest.raises(pri.GlobUnevaluable):
        pri.glob_match("a", pattern)
    assert pri.matches_safety("docs/readme.md", [pattern]) is True
    assert pri.matches_safety("docs/readme.md", ["backend/*", pattern]) is True


@pytest.mark.parametrize("ci", ["skipped", "neutral", "cancelled"])
def test_headline_never_green_unless_ci_succeeded(ci):
    hv = vals()
    got = pri.headline(ci, hv, ROWS, states_for(hv, hv))
    assert not got.startswith("🟢")


def test_notice_keeps_footprint_and_never_goes_green(tmp_path, monkeypatch):
    pull = {"state": "open", "draft": True, "mergeable": True, "mergeable_state": "draft",
            "base": {"ref": "main", "repo": {"default_branch": "main"}}, "labels": []}
    monkeypatch.setattr(pri, "gh_api", fake_api(
        [("repos/owner/repo/pulls/7/files", FILES[:1]), ("repos/owner/repo/pulls/7", pull)]))
    assert pri.main(report_args(tmp_path, tmp_path / "missing.json", ci="failure")) == 0
    body = (tmp_path / "c.md").read_text()
    assert "**🔴 CI failed · Draft**" in body and "🟢" not in body
    assert "Metrics not evaluated for this commit" in body
    assert "### Change footprint" in body and "Draft · base `main`" in body


def test_report_workflow_passes_pr_context_from_the_api():
    report = (REPO_ROOT / ".github/workflows/pr-impact.yml").read_text()
    assert '--repo "$REPO" --pr "$PR"' in report
    assert "PR: ${{ steps.pr.outputs.number }}" in report
