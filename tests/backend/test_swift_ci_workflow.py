"""Swift CI is a required check, so it must report on every pull request.

A workflow that never starts leaves a required check "Expected" forever, so
swift-ci.yml has no `paths:` filter on pull_request. A cheap job detects Swift
changes and the macOS job skips itself when there are none. A job skipped by
`if:` reports success, which satisfies the required check:
https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks

Every rule is a `check_*` function over the parsed workflow. The break-proof
tests plant the opposite fault in a copy and demand the check fail, so a check
that cannot fail does not slip through. Offline: no network, no Xcode. The
detection step is run for real against a throwaway repo.
"""

from __future__ import annotations

import copy
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "swift-ci.yml"

SWIFT_GLOBS = (
    "Growin/**",
    "GrowinTests/**",
    "GrowinUITests/**",
    "Growin.xcodeproj/**",
    ".github/workflows/swift-ci.yml",
)
SWIFT_JOB = "unit-tests"
DETECT_JOB = "changes"
REQUIRED_NAME = "Swift unit tests"


def load() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def triggers(wf: dict) -> dict:
    # PyYAML reads the bare key `on` as boolean True.
    return wf[True] if True in wf else wf["on"]


def detect_step(wf: dict) -> dict:
    return next(s for s in wf["jobs"][DETECT_JOB]["steps"] if s.get("id") == "detect")


def _if_text(job: dict) -> str:
    cond = str(job.get("if", ""))
    m = re.fullmatch(r"\s*\$\{\{(.*)\}\}\s*", cond, re.S)
    return (m.group(1) if m else cond).strip()


TOKEN = re.compile(r"""
    (?P<call>!?cancelled\(\)|always\(\)|success\(\)|failure\(\))
  | (?P<op>&&|\|\||==|!=|!|\(|\))
  | (?P<ctx>needs\.changes\.(?:result|outputs\.swift))
  | (?P<lit>'[^']*')
  | (?P<ws>\s+)
""", re.X)


def eval_if(expr: str, *, cancelled: bool, result: str, output: str) -> bool:
    """Evaluate the small subset of GitHub expressions the Swift job uses.

    `!` is only accepted before a call or a parenthesis, so Python's
    `not a == b` precedence can never differ from GitHub's. Unknown tokens
    raise instead of guessing.
    """
    py, pos, prev = [], 0, None
    while pos < len(expr):
        m = TOKEN.match(expr, pos)
        if not m:
            raise ValueError(f"unsupported expression text at {expr[pos:pos + 20]!r}")
        pos, kind, text = m.end(), m.lastgroup, m.group()
        if kind == "ws":
            continue
        if prev == "!" and not (kind == "call" or text == "("):
            raise ValueError("`!` must precede a call or a parenthesis")
        if kind == "call":
            name = text.lstrip("!")
            val = {"cancelled()": "CANCELLED", "always()": "True",
                   "success()": "(RESULT == 'success')", "failure()": "(RESULT == 'failure')"}[name]
            py.append(f"(not {val})" if text.startswith("!") else val)
        elif kind == "op":
            py.append({"&&": " and ", "||": " or ", "!": " not "}.get(text, text))
        elif kind == "ctx":
            py.append("RESULT" if text.endswith("result") else "OUT")
        else:
            py.append(text)
        prev = text
    # Only whitelisted tokens reach eval, and builtins are empty.
    return bool(eval("".join(py), {"__builtins__": {}},  # noqa: S307
                     {"CANCELLED": cancelled, "RESULT": result, "OUT": output}))


# ---------------------------------------------------------------- the checks


def check_no_pr_paths_filter(wf):
    pr = triggers(wf)["pull_request"] or {}
    assert "paths" not in pr, "pull_request must not filter by paths"
    assert "paths-ignore" not in pr, "pull_request must not filter by paths-ignore"
    assert pr.get("branches") == ["main"]


def check_detection_job_has_all_globs(wf):
    job = wf["jobs"][DETECT_JOB]
    assert job["runs-on"] == "ubuntu-latest"
    script = detect_step(wf)["run"]
    for glob in SWIFT_GLOBS:
        assert f"'{glob}'" in script, f"detection is missing {glob}"
    assert "git diff --name-only" in script


def check_swift_job_name(wf):
    assert wf["jobs"][SWIFT_JOB]["name"] == REQUIRED_NAME
    names = [j.get("name") for j in wf["jobs"].values()]
    assert names.count(REQUIRED_NAME) == 1, "the required name must belong to exactly one job"


def check_swift_job_needs_detection(wf):
    needs = wf["jobs"][SWIFT_JOB].get("needs")
    assert needs, "the Swift job must declare needs"
    assert (needs if isinstance(needs, list) else [needs]) == [DETECT_JOB]


def check_fail_closed(wf):
    """The Swift job skips only for (not cancelled, detection success, swift=false)."""
    expr = _if_text(wf["jobs"][SWIFT_JOB])
    assert expr, "the Swift job needs an `if:` so it can skip"
    try:
        for cancelled in (False, True):
            for result in ("success", "failure", "skipped", "cancelled", ""):
                for output in ("true", "false", ""):
                    runs = eval_if(expr, cancelled=cancelled, result=result, output=output)
                    must_skip = cancelled or (result == "success" and output == "false")
                    assert runs is not must_skip, (
                        f"cancelled={cancelled} result={result!r} swift={output!r}: "
                        f"runs={runs}, must_skip={must_skip}")
    except ValueError as exc:
        raise AssertionError(f"cannot evaluate the Swift job `if:`: {exc}") from exc


def check_detection_script_fails_closed(wf):
    script = detect_step(wf)["run"]
    assert script.count("swift=false") == 1, "swift=false is only for a clean, empty diff"
    assert 'run_swift() { echo "swift=true" >> "$GITHUB_OUTPUT"; exit 0; }' in script
    # The diff-failure branch must call run_swift (which answers swift=true).
    assert re.search(r"if ! files=\$\(.*?\); then\n[^\n]*\n\s*run_swift\n\s*fi", script, re.S), \
        "a failing git diff must run the Swift job"


def check_no_continue_on_error(wf):
    for jid in (SWIFT_JOB, DETECT_JOB):
        job = wf["jobs"][jid]
        assert job.get("continue-on-error") in (None, False), f"{jid} continue-on-error"
        for step in job["steps"]:
            assert step.get("continue-on-error") in (None, False), f"{jid} step continue-on-error"


def check_timeout_and_concurrency_kept(wf):
    assert wf["jobs"][SWIFT_JOB]["timeout-minutes"] == 45
    assert wf["concurrency"] == {
        "group": "swift-ci-${{ github.event.pull_request.number || github.ref }}",
        "cancel-in-progress": True,
    }


def check_push_paths_match_detection(wf):
    assert triggers(wf)["push"]["paths"] == list(SWIFT_GLOBS)


CHECKS = [
    check_no_pr_paths_filter,
    check_detection_job_has_all_globs,
    check_swift_job_name,
    check_swift_job_needs_detection,
    check_fail_closed,
    check_detection_script_fails_closed,
    check_no_continue_on_error,
    check_timeout_and_concurrency_kept,
    check_push_paths_match_detection,
]


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__)
def test_workflow_satisfies_check(check):
    check(load())


# ------------------------------------------------------------- break-proofs


def _plant_pr_paths(wf):
    triggers(wf)["pull_request"]["paths"] = ["Growin/**"]


def drop_ui_glob(script: str) -> str:
    return re.sub(r"\n\s*'GrowinUITests/\*\*' \\", "", script)


def fail_open_on_diff_error(script: str) -> str:
    """The opposite of fail-closed: a diff error answers swift=false."""
    return re.sub(r"(Could not diff[^\n]*\n\s*)run_swift",
                  r'\1echo "swift=false" >> "$GITHUB_OUTPUT"; exit 0', script)


def _plant_missing_ui_glob(wf):
    step = detect_step(wf)
    step["run"] = drop_ui_glob(step["run"])


def _plant_advisory_name(wf):
    wf["jobs"][SWIFT_JOB]["name"] = "Swift unit tests (advisory)"


def _plant_duplicate_name(wf):
    wf["jobs"][DETECT_JOB]["name"] = REQUIRED_NAME


def _plant_no_needs(wf):
    del wf["jobs"][SWIFT_JOB]["needs"]


def _plant_fail_open_if(wf):
    # Detection failure leaves the output empty, so this skips instead of running.
    wf["jobs"][SWIFT_JOB]["if"] = "${{ needs.changes.outputs.swift == 'true' }}"


def _plant_always_if(wf):
    # always() also runs after cancellation and ignores a swift=false answer.
    wf["jobs"][SWIFT_JOB]["if"] = "${{ always() }}"


def _plant_bare_needs_no_if(wf):
    # Without !cancelled() a failed detection job skips the Swift job.
    wf["jobs"][SWIFT_JOB]["if"] = "${{ needs.changes.outputs.swift != 'false' }}"


def _plant_skip_on_failed_detection(wf):
    wf["jobs"][SWIFT_JOB]["if"] = "${{ !cancelled() && needs.changes.outputs.swift == 'true' }}"


def _plant_diff_failure_skips(wf):
    step = detect_step(wf)
    step["run"] = fail_open_on_diff_error(step["run"])


def _plant_job_continue_on_error(wf):
    wf["jobs"][SWIFT_JOB]["continue-on-error"] = True


def _plant_step_continue_on_error(wf):
    wf["jobs"][SWIFT_JOB]["steps"][1]["continue-on-error"] = True


def _plant_timeout(wf):
    wf["jobs"][SWIFT_JOB]["timeout-minutes"] = 60


def _plant_concurrency(wf):
    wf["concurrency"]["cancel-in-progress"] = False


def _plant_push_drift(wf):
    triggers(wf)["push"]["paths"].remove("GrowinUITests/**")


PLANTS = [
    (_plant_pr_paths, check_no_pr_paths_filter),
    (_plant_missing_ui_glob, check_detection_job_has_all_globs),
    (_plant_advisory_name, check_swift_job_name),
    (_plant_duplicate_name, check_swift_job_name),
    (_plant_no_needs, check_swift_job_needs_detection),
    (_plant_fail_open_if, check_fail_closed),
    (_plant_always_if, check_fail_closed),
    (_plant_bare_needs_no_if, check_fail_closed),
    (_plant_skip_on_failed_detection, check_fail_closed),
    (_plant_diff_failure_skips, check_detection_script_fails_closed),
    (_plant_job_continue_on_error, check_no_continue_on_error),
    (_plant_step_continue_on_error, check_no_continue_on_error),
    (_plant_timeout, check_timeout_and_concurrency_kept),
    (_plant_concurrency, check_timeout_and_concurrency_kept),
    (_plant_push_drift, check_push_paths_match_detection),
]


@pytest.mark.parametrize("plant,check", PLANTS, ids=lambda f: f.__name__)
def test_check_catches_planted_fault(plant, check):
    wf = copy.deepcopy(load())
    before = copy.deepcopy(wf)
    plant(wf)
    assert wf != before, "the plant changed nothing, so the break-proof proves nothing"
    with pytest.raises(AssertionError):
        check(wf)


def test_fail_closed_check_accepts_the_documented_expression():
    wf = copy.deepcopy(load())
    wf["jobs"][SWIFT_JOB]["if"] = (
        "${{ !cancelled() && !(needs.changes.result == 'success' "
        "&& needs.changes.outputs.swift == 'false') }}")
    check_fail_closed(wf)


def test_expression_evaluator_rejects_unknown_syntax():
    with pytest.raises(ValueError):
        eval_if("contains(needs.changes.outputs.swift, 'x')", cancelled=False, result="", output="")
    with pytest.raises(ValueError):
        eval_if("!needs.changes.result == 'x'", cancelled=False, result="", output="")


# ------------------------------------------- the detection step, run for real


def _git(cwd, *args):
    env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
               GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.com")
    out = subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True)
    return out.stdout.strip()


def _write(root: Path, rel: str, text: str):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


BASE_FILES = {
    "Growin/App.swift": "a",
    "Growin/Views/Deep/Panel.swift": "a",
    "GrowinTests/AppTests.swift": "a",
    "GrowinUITests/LaunchUITests.swift": "a",
    "Growin.xcodeproj/project.pbxproj": "a",
    ".github/workflows/swift-ci.yml": "a",
    ".github/workflows/ci.yml": "a",
    "GrowinOther/Thing.txt": "a",
    "backend/app.py": "a",
    "README.md": "a",
}


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    for rel, text in BASE_FILES.items():
        _write(root, rel, text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    return root


def run_detect(root: Path, tmp_path: Path, *, script: str | None = None, event: str = "pull_request",
               base: str | None = None, head: str | None = None) -> str:
    """Run the workflow's detection step and return what it wrote to GITHUB_OUTPUT."""
    script = script if script is not None else detect_step(load())["run"]
    out = tmp_path / "github_output"
    out.write_text("")
    env = dict(os.environ, GITHUB_OUTPUT=str(out), EVENT_NAME=event,
               BASE_SHA=base or "", HEAD_SHA=head or "")
    # The runner's default shell for `run:` is `bash -e {0}`.
    proc = subprocess.run(["bash", "-e", "-c", script], cwd=root, env=env, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = [ln for ln in out.read_text().splitlines() if ln.startswith("swift=")]
    assert len(lines) == 1, f"exactly one swift= line expected, got {lines}"
    return lines[0].removeprefix("swift=")


def make_pr(root: Path, changes: dict[str, str | None]) -> tuple[str, str]:
    base = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-q", "-b", "pr")
    for rel, text in changes.items():
        if text is None:
            (root / rel).unlink()
        else:
            _write(root, rel, text)
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "pr")
    return base, _git(root, "rev-parse", "HEAD")


@pytest.mark.parametrize("path,expected", [
    ("Growin/App.swift", "true"),
    ("Growin/Views/Deep/New.swift", "true"),
    ("GrowinTests/AppTests.swift", "true"),
    ("GrowinUITests/LaunchUITests.swift", "true"),
    ("GrowinUITests/Brand/New.swift", "true"),
    ("Growin.xcodeproj/project.pbxproj", "true"),
    (".github/workflows/swift-ci.yml", "true"),
    (".github/workflows/ci.yml", "false"),
    ("GrowinOther/Thing.txt", "false"),
    ("backend/app.py", "false"),
    ("README.md", "false"),
])
def test_detection_by_changed_path(repo, tmp_path, path, expected):
    base, head = make_pr(repo, {path: "changed"})
    assert run_detect(repo, tmp_path, base=base, head=head) == expected


def test_detection_sees_a_deleted_swift_file(repo, tmp_path):
    base, head = make_pr(repo, {"GrowinUITests/LaunchUITests.swift": None})
    assert run_detect(repo, tmp_path, base=base, head=head) == "true"


def test_detection_sees_a_rename_out_of_a_swift_folder(repo, tmp_path):
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "pr")
    _git(repo, "mv", "Growin/App.swift", "backend/App.swift")
    _git(repo, "commit", "-q", "-m", "move out")
    head = _git(repo, "rev-parse", "HEAD")
    assert run_detect(repo, tmp_path, base=base, head=head) == "true"


def test_detection_ignores_swift_changes_that_landed_on_main_after_the_branch(repo, tmp_path):
    base, head = make_pr(repo, {"backend/app.py": "changed"})
    _git(repo, "checkout", "-q", "main")
    _write(repo, "Growin/App.swift", "main moved")
    _git(repo, "commit", "-q", "-am", "main moves on")
    main_tip = _git(repo, "rev-parse", "HEAD")
    # Three-dot diff: only what the PR branch changed since it left main.
    assert run_detect(repo, tmp_path, base=main_tip, head=head) == "false"


def test_detection_is_false_for_a_mixed_non_swift_pr(repo, tmp_path):
    base, head = make_pr(repo, {"backend/app.py": "x", "README.md": "y", ".github/workflows/ci.yml": "z"})
    assert run_detect(repo, tmp_path, base=base, head=head) == "false"


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "merge_group", ""])
def test_detection_runs_swift_for_events_without_a_pr_diff(repo, tmp_path, event):
    assert run_detect(repo, tmp_path, event=event) == "true"


def test_detection_fails_closed_when_the_base_cannot_be_fetched(repo, tmp_path):
    _, head = make_pr(repo, {"backend/app.py": "changed"})
    missing_base = "0123456789abcdef0123456789abcdef01234567"
    assert run_detect(repo, tmp_path, base=missing_base, head=head) == "true"


def test_detection_fails_closed_when_the_head_is_unknown(repo, tmp_path):
    base, _ = make_pr(repo, {"backend/app.py": "changed"})
    assert run_detect(repo, tmp_path, base=base, head="deadbeef" * 5) == "true"


@pytest.mark.parametrize("base,head", [("", "x"), ("x", ""), ("", "")])
def test_detection_fails_closed_when_a_sha_is_missing(repo, tmp_path, base, head):
    assert run_detect(repo, tmp_path, base=base, head=head) == "true"


def test_detection_fails_closed_outside_a_git_repo(tmp_path):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    sha = "0123456789abcdef0123456789abcdef01234567"
    out = run_detect(not_a_repo, tmp_path, base=sha, head=sha)
    assert out == "true"


# Break-proofs for the behavioral tests: a broken script must be caught.


def test_break_proof_detection_without_ui_glob_misses_ui_only_change(repo, tmp_path):
    base, head = make_pr(repo, {"GrowinUITests/LaunchUITests.swift": "changed"})
    broken = drop_ui_glob(detect_step(load())["run"])
    assert broken != detect_step(load())["run"]
    assert run_detect(repo, tmp_path, script=broken, base=base, head=head) == "false"
    assert run_detect(repo, tmp_path, base=base, head=head) == "true"


def test_break_proof_fail_open_script_skips_on_unknown_base(repo, tmp_path):
    _, head = make_pr(repo, {"backend/app.py": "changed"})
    good = detect_step(load())["run"]
    broken = fail_open_on_diff_error(good)
    assert broken != good
    missing_base = "0123456789abcdef0123456789abcdef01234567"
    assert run_detect(repo, tmp_path, script=broken, base=missing_base, head=head) == "false"
    assert run_detect(repo, tmp_path, base=missing_base, head=head) == "true"


# ------------------------------------------------------- repo-wide hygiene


def test_workflow_text_is_required_not_advisory():
    text = WORKFLOW.read_text()
    assert "REQUIRED check" in text
    assert "advisory" not in text.lower()


def test_no_stale_references_to_the_old_check_name():
    for path in (REPO_ROOT / ".github").rglob("*"):
        if path.is_file():
            assert "Swift unit tests (advisory)" not in path.read_text(errors="ignore"), path
    for path in (REPO_ROOT / "tests").rglob("*.py"):
        if path.name == Path(__file__).name:
            continue
        assert "Swift unit tests (advisory)" not in path.read_text(errors="ignore"), path


def test_every_action_is_pinned_by_full_sha_and_no_inline_expressions_in_run():
    wf = load()
    for job in wf["jobs"].values():
        for step in job["steps"]:
            if "uses" in step:
                assert re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", step["uses"]), step["uses"]
            assert "${{" not in step.get("run", ""), "pass event data through env, not inline"
    assert wf["permissions"] == {"contents": "read"}
