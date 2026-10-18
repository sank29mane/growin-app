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
import tempfile
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
    ".github/required-swift-tests.txt",
    # RelayVectors in GrowinTests reads signing_vectors.json and
    # swift_signature.json from this directory.
    "tests/backend/fixtures/relay_orders/**",
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


def detection_globs(wf: dict) -> list[str]:
    """The pathspecs the detection step passes to git diff, minus the :(icase) magic."""
    script = detect_step(wf)["run"]
    return re.findall(r"'(?::\(icase\))?([^'\s]+)'", script[script.index("git diff"):])


def find_test_step(wf: dict) -> dict:
    return next(s for s in wf["jobs"][SWIFT_JOB]["steps"] if s.get("id") == "test")


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
        assert f"':(icase){glob}'" in script, f"detection is missing a case-insensitive {glob}"
    assert detection_globs(wf) == list(SWIFT_GLOBS)
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
    # The push filter is case-sensitive and the detection is not, so compare
    # the lists with the :(icase) magic removed. Equal means neither can drift.
    push = triggers(wf)["push"]["paths"]
    assert push == list(SWIFT_GLOBS)
    assert push == detection_globs(wf)


def check_pull_request_types_default(wf):
    # `edited` would cancel a running macOS job on every title or body edit.
    assert "types" not in (triggers(wf)["pull_request"] or {})


# ---- the test step (build-for-testing is covered in test_pr_dashboard.py)


def run_test_step(wf: dict, status: int) -> tuple[int, list[str], str]:
    """Run the real test-step script against a fake xcodebuild.

    Returns (exit code of the step, argv xcodebuild received, what it saw in
    TEST_RUNNER_GROWIN_SE_UAT).
    """
    step = find_test_step(wf)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        fake = tmp_path / "bin" / "xcodebuild"
        fake.parent.mkdir()
        fake.write_text(
            '#!/bin/bash\n'
            'printf "%s\\n" "$@" > "$FAKE_ARGV"\n'
            'printf "%s\\n" "${TEST_RUNNER_GROWIN_SE_UAT-UNSET}" > "$FAKE_ENV"\n'
            'echo "Test case \'-[X y]\' passed"\n'
            'exit "$FAKE_STATUS"\n')
        fake.chmod(0o755)
        env = dict(os.environ, PATH=f"{fake.parent}:{os.environ['PATH']}", RUNNER_TEMP=str(tmp_path),
                   FAKE_ARGV=str(tmp_path / "argv"), FAKE_ENV=str(tmp_path / "env"),
                   FAKE_STATUS=str(status), TEST_RUNNER_GROWIN_SE_UAT="1")
        proc = subprocess.run(["bash", "-e", "-c", step["run"]], env=env, capture_output=True, text=True)
        argv = (tmp_path / "argv").read_text().splitlines() if (tmp_path / "argv").exists() else []
        seen = (tmp_path / "env").read_text().strip() if (tmp_path / "env").exists() else ""
        return proc.returncode, argv, seen


def check_test_step_propagates_xcodebuild_status(wf):
    for status in (0, 1, 65):
        code, argv, _ = run_test_step(wf, status)
        assert argv, "the step never called xcodebuild"
        assert code == status, f"xcodebuild exited {status} but the step exited {code}"


def check_test_step_runs_whenever_the_build_succeeded(wf):
    # Exactly this, nothing narrower: any extra `&&` could skip tests after a good build.
    assert find_test_step(wf).get("if") == "steps.build.outputs.built == 'true'"
    steps = wf["jobs"][SWIFT_JOB]["steps"]
    assert [s.get("id") for s in steps].index("build") < [s.get("id") for s in steps].index("test")
    assert find_test_step(wf).get("continue-on-error") in (None, False)


def check_test_target_selection(wf):
    _, argv, seen_uat = run_test_step(wf, 0)
    assert argv[0] == "test-without-building"
    assert argv[argv.index("-scheme") + 1] == "Growin"
    assert argv[argv.index("-project") + 1] == "Growin.xcodeproj"
    assert [a for a in argv if a.startswith("-only-testing")] == ["-only-testing:GrowinTests"]
    assert [a for a in argv if a.startswith("-skip-testing")] == ["-skip-testing:GrowinUITests"]
    assert seen_uat == "UNSET", "the Secure Enclave UAT switch must be unset for the test run"


# ---- the SDK degrade override (operator-only repository variable)

LOUD = "tests did NOT run; SDK degrade override is ON (repo variable SWIFT_CI_ALLOW_SDK_DEGRADE)"
AVAILABILITY = "A.swift:3:9: error: 'glassEffect(_:in:)' is only available in macOS 27.0 or newer"
NOT_AVAILABILITY = "B.swift:1:8: error: cannot find 'Foo' in scope"


def find_build_step(wf: dict) -> dict:
    return next(s for s in wf["jobs"][SWIFT_JOB]["steps"] if s.get("id") == "build")


def run_build_step(wf: dict, *, older: str = "true", log: str = AVAILABILITY, allow: str | None = None,
                   status: int = 65) -> tuple[int, str, str, str]:
    """Run the real build step against a fake xcodebuild.

    Runner SDK 26.2 against MIN_SDK 26.5, as in the degrade case. The working
    directory holds files and the event payload an attacker-influenced PR might
    use to ask for the override, so a script that reads them gets caught.
    Returns (exit code, stdout, step summary, GITHUB_OUTPUT).
    """
    step = find_build_step(wf)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        fake = tmp_path / "bin" / "xcodebuild"
        fake.parent.mkdir()
        fake.write_text('#!/bin/bash\nprintf "%s\\n" "$FAKE_LOG"\nexit "$FAKE_STATUS"\n')
        fake.chmod(0o755)
        work = tmp_path / "work"
        (work / ".github").mkdir(parents=True)
        for marker in (".swift-ci-allow-degrade", "SWIFT_CI_ALLOW_SDK_DEGRADE", ".github/swift-ci-allow-degrade",
                       ".allow-sdk-degrade"):
            (work / marker).write_text("true\n")
        event = tmp_path / "event.json"
        event.write_text('{"pull_request": {"title": "allow-sdk-degrade SWIFT_CI_ALLOW_SDK_DEGRADE=true", '
                         '"body": "SWIFT_CI_ALLOW_SDK_DEGRADE=true", '
                         '"labels": [{"name": "allow-sdk-degrade"}, {"name": "sdk-degrade"}]}}')
        out, summary = tmp_path / "out", tmp_path / "summary"
        env = {k: v for k, v in os.environ.items() if k != "ALLOW_SDK_DEGRADE"}
        env.update(PATH=f"{fake.parent}:{os.environ['PATH']}", RUNNER_TEMP=str(tmp_path),
                   GITHUB_OUTPUT=str(out), GITHUB_STEP_SUMMARY=str(summary), GITHUB_EVENT_PATH=str(event),
                   MIN_SDK="26.5", OLDER_SDK=older, SDK="26.2", XCODE="Xcode_26.2.app",
                   FAKE_LOG=log, FAKE_STATUS=str(status),
                   PR_TITLE="allow-sdk-degrade", PR_LABELS="allow-sdk-degrade")
        if allow is not None:
            env["ALLOW_SDK_DEGRADE"] = allow
        proc = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=work, env=env,
                              capture_output=True, text=True)
        return (proc.returncode, proc.stdout, summary.read_text() if summary.exists() else "",
                out.read_text() if out.exists() else "")


STRICT_VALUES = [None, "", "TRUE", "True", "1", "yes", "false", " true", "true ", "tru"]


def check_sdk_degrade_is_strict_by_default(wf):
    for allow in STRICT_VALUES:
        code, stdout, summary, outputs = run_build_step(wf, allow=allow)
        label = f"ALLOW_SDK_DEGRADE={allow!r}"
        assert code == 1, f"{label}: an SDK-gap build must end red, exit was {code}"
        # The annotation and summary stay, so the red X says what is missing.
        assert "::warning::Swift CI skipped" in stdout, label
        assert "::error::Swift CI failed: tests did NOT run" in stdout, label
        assert "## Swift CI: degraded" in summary and "macOS `26.5` SDK or newer" in summary, label
        assert "only available in macOS 27.0 or newer" in summary, label
        assert "tests did NOT run" in stdout and "override is ON" not in stdout + summary, label
        assert "built=false" in outputs, label


def check_sdk_degrade_override_true_is_green_and_loud(wf):
    code, stdout, summary, outputs = run_build_step(wf, allow="true")
    assert code == 0, stdout
    warning = next((ln for ln in stdout.splitlines() if ln.startswith("::warning::")), "")
    assert LOUD in warning, "the annotation must say the override is ON and tests did NOT run"
    assert LOUD in summary, "the step summary must say the override is ON and tests did NOT run"
    assert "## Swift CI: degraded" in summary and "macOS `26.5` SDK or newer" in summary
    assert "built=false" in outputs and "::error::" not in stdout


def check_sdk_degrade_override_only_rescues_sdk_gaps(wf):
    for kwargs in (
        dict(log=NOT_AVAILABILITY),
        dict(log=f"{AVAILABILITY}\n{NOT_AVAILABILITY}"),
        dict(log="** BUILD FAILED **"),
        dict(older="false", log=AVAILABILITY),        # SDK >= MIN_SDK: real failure
    ):
        code, stdout, _, _ = run_build_step(wf, allow="true", **kwargs)
        assert code == 1, f"override must not rescue {kwargs}"
        assert "::error::Swift build failed" in stdout


def check_sdk_degrade_switch_is_operator_only(wf):
    step = find_build_step(wf)
    # Exactly one source: the repository variable, read through env.
    assert step["env"]["ALLOW_SDK_DEGRADE"] == "${{ vars.SWIFT_CI_ALLOW_SDK_DEGRADE }}"
    assert set(step["env"]) == {"OLDER_SDK", "SDK", "XCODE", "ALLOW_SDK_DEGRADE"}
    assert "${{" not in step["run"], "no inline expressions in run"
    text = WORKFLOW.read_text()
    assert text.count("SWIFT_CI_ALLOW_SDK_DEGRADE }}") == 1
    for pr_controlled in ("labels", "pull_request.title", "pull_request.body", "head_commit", "commits"):
        assert pr_controlled not in step["env"]["ALLOW_SDK_DEGRADE"]
    # Behavior: with the variable unset, no marker file, label, title or event
    # payload the PR can influence flips the result.
    code, _, _, _ = run_build_step(wf, allow=None)
    assert code == 1, "something other than the repository variable turned the override on"


CHECKS = [
    check_sdk_degrade_is_strict_by_default,
    check_sdk_degrade_override_true_is_green_and_loud,
    check_sdk_degrade_override_only_rescues_sdk_gaps,
    check_sdk_degrade_switch_is_operator_only,
    check_no_pr_paths_filter,
    check_detection_job_has_all_globs,
    check_swift_job_name,
    check_swift_job_needs_detection,
    check_fail_closed,
    check_detection_script_fails_closed,
    check_no_continue_on_error,
    check_timeout_and_concurrency_kept,
    check_push_paths_match_detection,
    check_pull_request_types_default,
    check_test_step_propagates_xcodebuild_status,
    check_test_step_runs_whenever_the_build_succeeded,
    check_test_target_selection,
]


@pytest.mark.parametrize("check", CHECKS, ids=lambda c: c.__name__)
def test_workflow_satisfies_check(check):
    check(load())


# ------------------------------------------------------------- break-proofs


def _plant_pr_paths(wf):
    triggers(wf)["pull_request"]["paths"] = ["Growin/**"]


def drop_ui_glob(script: str) -> str:
    return re.sub(r"\n\s*':\(icase\)GrowinUITests/\*\*' \\", "", script)


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


def _plant_push_missing_fixtures(wf):
    triggers(wf)["push"]["paths"].remove("tests/backend/fixtures/relay_orders/**")


def _plant_detection_missing_fixtures(wf):
    step = detect_step(wf)
    step["run"] = re.sub(r" \\\n\s*':\(icase\)tests/backend/fixtures/relay_orders/\*\*'", "", step["run"])


def _plant_case_sensitive_detection(wf):
    step = detect_step(wf)
    step["run"] = step["run"].replace(":(icase)", "")


def _plant_edited_type(wf):
    triggers(wf)["pull_request"]["types"] = ["opened", "synchronize", "reopened", "edited"]


def _plant_test_step_exit_zero(wf):
    step = find_test_step(wf)
    step["run"] = step["run"].replace('exit "$status"', "exit 0")


def _plant_test_step_masked_status(wf):
    # Reads tee's status (index 1) instead of xcodebuild's, so a failing run exits 0.
    step = find_test_step(wf)
    step["run"] = step["run"].replace("status=${PIPESTATUS[0]}", "status=${PIPESTATUS[1]}")


def _plant_test_step_never_runs(wf):
    find_test_step(wf)["if"] = "steps.build.outputs.built == 'never'"


def _plant_test_step_if_false(wf):
    find_test_step(wf)["if"] = "false"


def _plant_test_step_narrowed_if(wf):
    find_test_step(wf)["if"] = "steps.build.outputs.built == 'true' && steps.xcode.outputs.older_sdk == 'false'"


def _plant_test_step_continue_on_error(wf):
    find_test_step(wf)["continue-on-error"] = True


def _plant_narrowed_only_testing(wf):
    step = find_test_step(wf)
    step["run"] = step["run"].replace("-only-testing:GrowinTests ", "-only-testing:GrowinTests/OneSuite ", 1)


def _plant_widened_only_testing(wf):
    step = find_test_step(wf)
    step["run"] = step["run"].replace("-only-testing:GrowinTests ",
                                      "-only-testing:GrowinTests -only-testing:GrowinUITests ", 1)


def _plant_dropped_skip_testing(wf):
    step = find_test_step(wf)
    step["run"] = re.sub(r"\n\s*-skip-testing:GrowinUITests \\", "", step["run"])


def _plant_se_uat_left_set(wf):
    step = find_test_step(wf)
    step["run"] = step["run"].replace("unset TEST_RUNNER_GROWIN_SE_UAT", "true")


OVERRIDE_TEST = 'if [[ "${ALLOW_SDK_DEGRADE:-}" == "true" ]]; then override=true; fi'


def _edit_build_run(wf, old: str, new: str):
    step = find_build_step(wf)
    assert old in step["run"], f"plant target not found: {old!r}"
    step["run"] = step["run"].replace(old, new, 1)


def _edit_build_run_re(wf, pattern: str, repl: str):
    step = find_build_step(wf)
    new, count = re.subn(pattern, repl, step["run"], count=1)
    assert count == 1, f"plant target not found: {pattern!r}"
    step["run"] = new


def _plant_degrade_default_permissive(wf):
    # Unset or empty now means "allow"; only an explicit "false" is strict.
    _edit_build_run(wf, OVERRIDE_TEST,
                    'if [[ "${ALLOW_SDK_DEGRADE:-}" != "false" ]]; then override=true; fi')


def _plant_degrade_case_insensitive(wf):
    _edit_build_run(wf, OVERRIDE_TEST,
                    'if [[ "${ALLOW_SDK_DEGRADE:-}" == [Tt][Rr][Uu][Ee] ]]; then override=true; fi')


def _plant_degrade_one_accepted(wf):
    _edit_build_run(wf, OVERRIDE_TEST,
                    'if [[ "${ALLOW_SDK_DEGRADE:-}" == "true" || "${ALLOW_SDK_DEGRADE:-}" == "1" ]]; '
                    'then override=true; fi')


def _plant_degrade_from_label(wf):
    # The override read from a PR-controlled source: a label.
    find_build_step(wf)["env"]["ALLOW_SDK_DEGRADE"] = (
        "${{ contains(github.event.pull_request.labels.*.name, 'allow-sdk-degrade') && 'true' || '' }}")


def _plant_degrade_from_repo_file(wf):
    # ...or a file in the repo checkout.
    _edit_build_run(wf, OVERRIDE_TEST,
                    'if [[ "${ALLOW_SDK_DEGRADE:-}" == "true" || -f .swift-ci-allow-degrade ]]; '
                    'then override=true; fi')


def _plant_degrade_from_event_payload(wf):
    # ...or the event payload (title, body or labels).
    _edit_build_run(wf, OVERRIDE_TEST,
                    'if [[ "${ALLOW_SDK_DEGRADE:-}" == "true" ]] || grep -q "SWIFT_CI_ALLOW_SDK_DEGRADE=true" '
                    '"${GITHUB_EVENT_PATH:-/dev/null}"; then override=true; fi')


def _plant_degrade_warning_dropped_in_override(wf):
    _edit_build_run(wf, 'echo "::warning::Swift CI skipped: $loud. ', 'echo "::warning::Swift CI skipped: ')


def _plant_degrade_summary_not_loud(wf):
    _edit_build_run(wf, 'echo "**Warning: $loud.** This job is green only because of that override."',
                    'echo "This job is green."')


def _plant_degrade_strict_exits_zero(wf):
    _edit_build_run_re(wf, r"(to allow this temporarily\.\"\n\s*)exit 1", r"\1exit 0")


def _plant_degrade_strict_loses_summary(wf):
    _edit_build_run_re(wf, r"\n\s*echo \"## Swift CI: degraded\"", "")


def _plant_degrade_override_rescues_any_error(wf):
    _edit_build_run(wf, 'if [[ "$OLDER_SDK" == "true" && "$all_gap" == "true" ]]; then',
                    'if [[ "$OLDER_SDK" == "true" && ( "$all_gap" == "true" || "${ALLOW_SDK_DEGRADE:-}" == "true" ) ]]; then')


def _plant_degrade_override_rescues_new_sdk(wf):
    _edit_build_run(wf, 'if [[ "$OLDER_SDK" == "true" && "$all_gap" == "true" ]]; then',
                    'if [[ ( "$OLDER_SDK" == "true" || "${ALLOW_SDK_DEGRADE:-}" == "true" ) && "$all_gap" == "true" ]]; then')


PLANTS = [
    (_plant_degrade_default_permissive, check_sdk_degrade_is_strict_by_default),
    (_plant_degrade_case_insensitive, check_sdk_degrade_is_strict_by_default),
    (_plant_degrade_one_accepted, check_sdk_degrade_is_strict_by_default),
    (_plant_degrade_from_label, check_sdk_degrade_switch_is_operator_only),
    (_plant_degrade_from_repo_file, check_sdk_degrade_switch_is_operator_only),
    (_plant_degrade_from_event_payload, check_sdk_degrade_switch_is_operator_only),
    (_plant_degrade_warning_dropped_in_override, check_sdk_degrade_override_true_is_green_and_loud),
    (_plant_degrade_summary_not_loud, check_sdk_degrade_override_true_is_green_and_loud),
    (_plant_degrade_strict_exits_zero, check_sdk_degrade_is_strict_by_default),
    (_plant_degrade_strict_loses_summary, check_sdk_degrade_is_strict_by_default),
    (_plant_degrade_override_rescues_any_error, check_sdk_degrade_override_only_rescues_sdk_gaps),
    (_plant_degrade_override_rescues_new_sdk, check_sdk_degrade_override_only_rescues_sdk_gaps),
    (_plant_push_missing_fixtures, check_push_paths_match_detection),
    (_plant_detection_missing_fixtures, check_detection_job_has_all_globs),
    (_plant_detection_missing_fixtures, check_push_paths_match_detection),
    (_plant_case_sensitive_detection, check_detection_job_has_all_globs),
    (_plant_edited_type, check_pull_request_types_default),
    (_plant_test_step_exit_zero, check_test_step_propagates_xcodebuild_status),
    (_plant_test_step_masked_status, check_test_step_propagates_xcodebuild_status),
    (_plant_test_step_never_runs, check_test_step_runs_whenever_the_build_succeeded),
    (_plant_test_step_if_false, check_test_step_runs_whenever_the_build_succeeded),
    (_plant_test_step_narrowed_if, check_test_step_runs_whenever_the_build_succeeded),
    (_plant_test_step_continue_on_error, check_test_step_runs_whenever_the_build_succeeded),
    (_plant_test_step_continue_on_error, check_no_continue_on_error),
    (_plant_narrowed_only_testing, check_test_target_selection),
    (_plant_widened_only_testing, check_test_target_selection),
    (_plant_dropped_skip_testing, check_test_target_selection),
    (_plant_se_uat_left_set, check_test_target_selection),
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
    "tests/backend/fixtures/relay_orders/signing_vectors.json": "{}",
    "tests/backend/fixtures/relay_orders/swift_signature.json": "{}",
    "tests/backend/fixtures/relay_orders/limits_vectors.json": "{}",
    "tests/backend/fixtures/other/data.json": "{}",
    "README.md": "a",
}


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    # macOS defaults to core.ignorecase=true, which would blur the icase tests.
    _git(root, "config", "core.ignorecase", "false")
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
    # GrowinTests read these through RelayVectors.
    ("tests/backend/fixtures/relay_orders/signing_vectors.json", "true"),
    ("tests/backend/fixtures/relay_orders/swift_signature.json", "true"),
    ("tests/backend/fixtures/relay_orders/new_vectors.json", "true"),
    ("tests/backend/fixtures/other/data.json", "false"),
    ("tests/backend/test_pr_dashboard.py", "false"),
    (".github/workflows/ci.yml", "false"),
    ("GrowinOther/Thing.txt", "false"),
    ("backend/app.py", "false"),
    ("README.md", "false"),
])
def test_detection_by_changed_path(repo, tmp_path, path, expected):
    base, head = make_pr(repo, {path: "changed"})
    assert run_detect(repo, tmp_path, base=base, head=head) == expected


@pytest.mark.parametrize("path", [
    "growin/X.swift",
    "GROWIN/X.swift",
    "growintests/X.swift",
    "GROWINUITESTS/Deep/X.swift",
    "growin.xcodeproj/project.pbxproj",
    ".GitHub/Workflows/Swift-CI.yml",
    "Tests/Backend/Fixtures/Relay_Orders/signing_vectors.json",
])
def test_detection_ignores_case_in_the_path(tmp_path, path):
    # A bare repo with none of the mixed-case folders, so the file system's own
    # case folding cannot hide a case-sensitive pathspec.
    root = tmp_path / "bare"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "core.ignorecase", "false")
    _write(root, "README.md", "a")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    base, head = make_pr(root, {path: "changed"})
    assert run_detect(root, tmp_path, base=base, head=head) == "true"
    # Break-proof: the same script without :(icase) misses it.
    script = detect_step(load())["run"]
    case_sensitive = script.replace(":(icase)", "")
    assert case_sensitive != script
    assert run_detect(root, tmp_path, script=case_sensitive, base=base, head=head) == "false"


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


# ------------------------------- Swift tests reading files outside the globs

SWIFT_DIRS = ("Growin", "GrowinTests", "GrowinUITests")
STRING_LITERAL = re.compile(r'"((?:[^"\\\n]|\\.)*)"')
LINE_COMMENT = re.compile(r'^((?:[^"\\]|\\.|"(?:[^"\\]|\\.)*")*?)//.*$')
TOP_LEVEL_FILE = re.compile(r"^[\w.-]+\.(?:md|json|ya?ml|plist|txt|py|sh|toml|xcconfig|entitlements|swift)$")


def out_of_tree_reads(swift_root: Path, repo_top: set[str]) -> dict[str, set[str]]:
    """Repo paths named in Swift string literals that sit outside the three Swift folders.

    A literal counts when it starts with a real top-level repo entry other than
    Growin, GrowinTests or GrowinUITests (`tests/backend/...`, `.github/...`),
    or is a bare top-level file name (`README.md`). Line comments are ignored.
    """
    found: dict[str, set[str]] = {}
    for swift in sorted(swift_root.rglob("*.swift")):
        for line in swift.read_text().splitlines():
            code = LINE_COMMENT.sub(r"\1", line)
            for literal in STRING_LITERAL.findall(code):
                first = literal.split("/", 1)[0]
                outside = "/" in literal and first in repo_top and first not in SWIFT_DIRS
                bare_file = "/" not in literal and TOP_LEVEL_FILE.match(literal) and literal in repo_top
                if outside or bare_file:
                    found.setdefault(literal, set()).add(swift.name)
    return found


def is_covered(path: str, globs: list[str]) -> bool:
    probe = path.rstrip("/") + "/"
    return path in globs or any(g.endswith("/**") and probe.startswith(g[:-2]) for g in globs)


def repo_top_level() -> set[str]:
    return {p.name for p in REPO_ROOT.iterdir() if p.name != ".git"}


def test_swift_tests_read_no_path_outside_the_detection_globs():
    found = {}
    for folder in SWIFT_DIRS:
        for lit, files in out_of_tree_reads(REPO_ROOT / folder, repo_top_level()).items():
            found.setdefault(lit, set()).update(files)
    # The one known dependency. If this set grows, add the path to BOTH lists in swift-ci.yml.
    assert "tests/backend/fixtures/relay_orders" in found, "scanner no longer sees the RelayVectors fixture dir"
    globs = detection_globs(load())
    uncovered = {lit: sorted(files) for lit, files in found.items() if not is_covered(lit, globs)}
    assert not uncovered, f"Swift tests read paths the Swift job is not triggered by: {uncovered}"
    assert (REPO_ROOT / "tests/backend/fixtures/relay_orders/signing_vectors.json").is_file()


def test_break_proof_scanner_flags_an_uncovered_read(tmp_path):
    swift = tmp_path / "T.swift"
    swift.write_text(
        'let a = root.appendingPathComponent("scripts/vectors.json")\n'
        'let b = "tests/backend/fixtures/relay_orders"   // trailing comment\n'
        '// let c = "docs/commented-out.md"\n'
        'let d = "Growin/App.swift"\n'
        'let e = "README.md"\n'
        'let f = "not/a/top/level/entry.json"\n')
    top = {"scripts", "tests", "docs", "Growin", "README.md"}
    found = out_of_tree_reads(tmp_path, top)
    assert set(found) == {"scripts/vectors.json", "tests/backend/fixtures/relay_orders", "README.md"}
    uncovered = [lit for lit in found if not is_covered(lit, detection_globs(load()))]
    assert sorted(uncovered) == ["README.md", "scripts/vectors.json"]


def test_break_proof_dropping_the_fixture_glob_leaves_the_scanned_read_uncovered():
    wf = copy.deepcopy(load())
    _plant_detection_missing_fixtures(wf)
    assert not is_covered("tests/backend/fixtures/relay_orders", detection_globs(wf))


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
