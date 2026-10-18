"""The required-Swift-tests list must match the Swift sources.

.github/required-swift-tests.txt names the production-Keychain tripwires and
canaries that the "Swift unit tests" job must see run and pass (see the step
"Require the Keychain tripwire and canary tests" in swift-ci.yml). CI can only
check names it is given, so a rename in Swift must fail here instead of
silently turning a required test into a "did not run" in CI.

Three layers, each with break-proofs: the list matches the Swift sources, the
list is exactly the 8 pinned names, and the workflow step that enforces the
list is present, ordered after the tests, unconditional and able to fail.
Offline: no Xcode, no Swift.
"""

from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
LIST = REPO_ROOT / ".github" / "required-swift-tests.txt"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "swift-ci.yml"
ISOLATION = REPO_ROOT / "GrowinTests" / "WorkspaceIsolation"
TRIPWIRE = ISOLATION / "ProductionTripwireTests.swift"
SWIFT_FILES = {
    "ProductionTripwireTests": TRIPWIRE,
    "KeychainScopeTests": ISOLATION / "KeychainScopeTests.swift",
    "LegacyT212KeychainCleanupTests": ISOLATION / "LegacyT212KeychainCleanupTests.swift",
}
EXPECTED_COUNT = 8  # 4 tripwires + 3 KeychainScopeTests canaries + 1 legacy canary
TRIPWIRE_COUNT = 4
# The exact names CI requires. Swapping one for another real test in the same
# suite still matches the Swift sources, so the names are pinned here too.
PINNED = frozenset({
    "ProductionTripwireTests/thisProcessIsATestHost",
    "ProductionTripwireTests/theSharedStoreRefusesARead",
    "ProductionTripwireTests/theSignersReportNotConfigured",
    "ProductionTripwireTests/theCleanupRefusesADeleteOnTheProductionService",
    "KeychainScopeTests/aStoreNeverSeesOrChangesAnotherServicesItem",
    "KeychainScopeTests/aStoreCleanUpAndLegacyCallsLeaveAnotherServicesItemsAlone",
    "KeychainScopeTests/rawKeychainOnOneServiceCannotSeeOrRemoveAnother",
    "LegacyT212KeychainCleanupTests/theRealDeleteNeverTouchesAnotherServicesItem",
})
STEP_NAME = "Require the Keychain tripwire and canary tests"
STEP_IF = "${{ !cancelled() && steps.build.outputs.built == 'true' }}"

SUITE_RE = re.compile(r"^\s*(?:final\s+)?(?:struct|class)\s+(\w+)", re.MULTILINE)
TEST_RE = re.compile(r"^\s*@Test\b[^\n]*?\bfunc\s+(\w+)\s*\(", re.MULTILINE)

def read_list(text: str) -> list[str]:
    return [
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def swift_tests(text: str) -> dict[str, set[str]]:
    """Suite name -> @Test func names, for the one suite declared in a file."""
    suites = SUITE_RE.findall(text)
    assert len(suites) >= 1, "no struct or class found"
    return {suites[0]: set(TEST_RE.findall(text))}


def check_list(names: list[str], on_disk: dict[str, set[str]]) -> list[str]:
    """Problems found; empty means the list matches the Swift sources."""
    problems: list[str] = []
    if len(names) != len(set(names)):
        problems.append("duplicate lines in the list")
    if len(names) != EXPECTED_COUNT:
        problems.append(f"expected {EXPECTED_COUNT} names, found {len(names)}")
    listed: dict[str, set[str]] = {}
    for line in names:
        suite, sep, name = line.partition("/")
        if not sep or not suite or not name or "/" in name:
            problems.append(f"bad line {line!r}, want Suite/testFunction")
            continue
        listed.setdefault(suite, set()).add(name)
    for suite, wanted in listed.items():
        if suite not in on_disk:
            problems.append(f"suite {suite} is not one of {sorted(on_disk)}")
            continue
        for name in sorted(wanted - on_disk[suite]):
            problems.append(f"{suite}/{name} is listed but has no @Test func in Swift")
    # Every test in the tripwire file is a tripwire: none may be left unlisted.
    trip = on_disk.get("ProductionTripwireTests", set())
    for name in sorted(trip - listed.get("ProductionTripwireTests", set())):
        problems.append(f"ProductionTripwireTests/{name} is a tripwire in Swift but not in the list")
    if len(trip) != TRIPWIRE_COUNT:
        problems.append(f"expected {TRIPWIRE_COUNT} tripwires in Swift, found {len(trip)}")
    return problems


def on_disk_tests(files: dict[str, Path] = SWIFT_FILES) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for suite, path in files.items():
        assert path.exists(), f"{path} is missing: the tripwires and canaries must exist in Swift"
        parsed = swift_tests(path.read_text())
        assert list(parsed) == [suite], f"{path.name} declares {list(parsed)}, expected {suite}"
        found.update(parsed)
    return found


def test_list_file_shape():
    names = read_list(LIST.read_text())
    assert len(names) == EXPECTED_COUNT
    assert len(set(names)) == EXPECTED_COUNT
    suites = [n.partition("/")[0] for n in names]
    assert suites.count("ProductionTripwireTests") == TRIPWIRE_COUNT
    assert suites.count("KeychainScopeTests") == 3
    assert suites.count("LegacyT212KeychainCleanupTests") == 1
    assert all(re.fullmatch(r"\w+/\w+", n) for n in names)


def test_list_matches_the_swift_sources():
    problems = check_list(read_list(LIST.read_text()), on_disk_tests())
    assert not problems, "\n".join(problems)


def check_pinned(names: list[str], pinned: frozenset[str] = PINNED) -> list[str]:
    """Problems found; empty means the list is exactly the pinned names."""
    problems = [f"{n} is pinned but missing from the list" for n in sorted(pinned - set(names))]
    problems += [f"{n} is in the list but not pinned" for n in sorted(set(names) - pinned)]
    if len(names) != len(set(names)):
        problems.append("duplicate lines in the list")
    return problems


def test_list_is_exactly_the_pinned_names():
    assert check_pinned(read_list(LIST.read_text())) == []


def test_pinned_names_exist_in_swift():
    # Guards the constant itself: a rename in Swift must update PINNED too.
    problems = check_list(sorted(PINNED), on_disk_tests())
    assert not problems, "\n".join(problems)


# Break-proofs: the checker itself must fail on the faults it exists to catch.

GOOD_DISK = {
    "ProductionTripwireTests": {"a1", "a2", "a3", "a4"},
    "KeychainScopeTests": {"b1", "b2", "b3", "extra"},
    "LegacyT212KeychainCleanupTests": {"c1", "other"},
}
GOOD_LIST = [
    "ProductionTripwireTests/a1",
    "ProductionTripwireTests/a2",
    "ProductionTripwireTests/a3",
    "ProductionTripwireTests/a4",
    "KeychainScopeTests/b1",
    "KeychainScopeTests/b2",
    "KeychainScopeTests/b3",
    "LegacyT212KeychainCleanupTests/c1",
]


def test_checker_accepts_a_matching_list():
    assert check_list(GOOD_LIST, GOOD_DISK) == []


def test_checker_catches_a_rename_in_swift():
    disk = {**GOOD_DISK, "KeychainScopeTests": {"b1", "b2", "b3_renamed"}}
    assert any("KeychainScopeTests/b3" in p for p in check_list(GOOD_LIST, disk))


def test_checker_catches_an_unlisted_tripwire():
    disk = {**GOOD_DISK, "ProductionTripwireTests": GOOD_DISK["ProductionTripwireTests"] | {"a5"}}
    assert any("a5" in p for p in check_list(GOOD_LIST, disk))


def test_checker_catches_a_dropped_or_duplicated_line():
    assert check_list(GOOD_LIST[:-1], GOOD_DISK)
    assert check_list(GOOD_LIST[:-1] + [GOOD_LIST[0]], GOOD_DISK)


def test_checker_catches_a_wrong_suite():
    wrong = GOOD_LIST[:-1] + ["KeychainScopeTests/c1"]
    assert any("c1" in p for p in check_list(wrong, GOOD_DISK))


def test_swift_parser_reads_test_funcs():
    src = (
        "@Suite(.serialized)\nstruct Demo {\n"
        "    @Test func one() {}\n"
        "    @Test func two() throws {}\n"
        "    @Test(.disabled) func three() {}\n"
        "    private static func helper() {}\n}\n"
    )
    assert swift_tests(src) == {"Demo": {"one", "two", "three"}}


def test_checker_catches_a_missing_swift_file():
    gone = {**SWIFT_FILES, "ProductionTripwireTests": REPO_ROOT / "GrowinTests" / "nowhere.swift"}
    with pytest.raises(AssertionError, match="nowhere.swift"):
        on_disk_tests(gone)


def test_pin_catches_a_canary_swapped_for_another_test_in_the_same_suite():
    real = read_list(LIST.read_text())
    swapped = [
        "KeychainScopeTests/policyTableMatchesOperatorDecision"
        if n == "KeychainScopeTests/rawKeychainOnOneServiceCannotSeeOrRemoveAnother" else n
        for n in real
    ]
    assert swapped != real
    # The swap is a real @Test in the same suite, so the Swift match passes ...
    assert check_list(swapped, on_disk_tests()) == []
    # ... and only the pin notices.
    problems = check_pinned(swapped)
    assert any("rawKeychainOnOneServiceCannotSeeOrRemoveAnother is pinned" in p for p in problems)
    assert any("policyTableMatchesOperatorDecision is in the list" in p for p in problems)


def test_pin_catches_a_dropped_added_or_duplicated_line():
    real = read_list(LIST.read_text())
    assert check_pinned(real[:-1])
    assert check_pinned(real + ["KeychainScopeTests/policyTableMatchesOperatorDecision"])
    assert check_pinned(real[:-1] + [real[0]])


# ---------------------------------------------------- the enforcing workflow step


def load_workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())


def steps_of(wf: dict) -> list[dict]:
    return wf["jobs"]["unit-tests"]["steps"]


def required_step(wf: dict) -> dict:
    found = [s for s in steps_of(wf) if s.get("id") == "required"]
    assert len(found) == 1, f"want exactly one step with id 'required', found {len(found)}"
    return found[0]


def check_required_step(wf: dict) -> list[str]:
    """Problems found; empty means the step exists, runs after the tests and can fail the job."""
    problems: list[str] = []
    try:
        step = required_step(wf)
    except AssertionError as e:
        return [str(e)]
    ids = [s.get("id") for s in steps_of(wf)]
    if "test" not in ids or ids.index("required") < ids.index("test"):
        problems.append("the required-tests step must come after the 'Run GrowinTests' step (id test)")
    if step.get("name") != STEP_NAME:
        problems.append(f"step name is {step.get('name')!r}, want {STEP_NAME!r}")
    if step.get("if") != STEP_IF:
        problems.append(f"if is {step.get('if')!r}, want exactly {STEP_IF!r}")
    if step.get("continue-on-error") not in (None, False):
        problems.append("continue-on-error must not be set")
    if "uses" in step:
        problems.append("the step must run a script, not an action")
    run = step.get("run") or ""
    if 'list=".github/required-swift-tests.txt"' not in run or 'done < "$list"' not in run:
        problems.append("run must read .github/required-swift-tests.txt")
    if "xcresulttool get test-results tests" not in run:
        problems.append("run must read the xcresult through xcresulttool")
    lines = [ln.strip() for ln in run.splitlines() if ln.strip()]
    if not lines or lines[-1] != '[[ "$bad" -eq 0 ]] || exit 1':
        problems.append("run must end by failing the step when any required test is bad")
    return problems


def test_real_workflow_has_the_required_step():
    assert check_required_step(load_workflow()) == []


def _wf(mutate) -> dict:
    wf = copy.deepcopy(load_workflow())
    mutate(wf)
    return wf


def _delete_step(wf):
    wf["jobs"]["unit-tests"]["steps"] = [s for s in steps_of(wf) if s.get("id") != "required"]


def _set(key, value):
    def mutate(wf):
        required_step(wf)[key] = value
    return mutate


def _move_before_tests(wf):
    steps = steps_of(wf)
    step = required_step(wf)
    steps.remove(step)
    steps.insert([s.get("id") for s in steps].index("test"), step)


def _replace_run(text):
    def mutate(wf):
        required_step(wf)["run"] = text
    return mutate


def _change_list_path(wf):
    step = required_step(wf)
    step["run"] = step["run"].replace(".github/required-swift-tests.txt", ".github/other.txt")


def _drop_final_exit(wf):
    step = required_step(wf)
    step["run"] = step["run"].replace('[[ "$bad" -eq 0 ]] || exit 1', '[[ "$bad" -eq 0 ]] || true')


def _drop_xcresulttool(wf):
    step = required_step(wf)
    step["run"] = step["run"].replace("xcresulttool get test-results tests", "echo get test-results tests")


STEP_MUTATIONS = {
    "step deleted": _delete_step,
    "if false": _set("if", False),
    "if string false": _set("if", "false"),
    "if expression false": _set("if", "${{ false }}"),
    "if narrowed": _set("if", "${{ !cancelled() && steps.build.outputs.built == 'true' && steps.test.outcome == 'success' }}"),
    "if loses cancelled guard": _set("if", "${{ steps.build.outputs.built == 'true' }}"),
    "continue-on-error": _set("continue-on-error", True),
    "step moved before the tests": _move_before_tests,
    "run is true": _replace_run("true"),
    "run is exit 0": _replace_run("exit 0"),
    "list path changed": _change_list_path,
    "final exit dropped": _drop_final_exit,
    "xcresulttool dropped": _drop_xcresulttool,
}


@pytest.mark.parametrize("name", STEP_MUTATIONS)
def test_step_check_fails_on_mutation(name):
    assert check_required_step(_wf(STEP_MUTATIONS[name])), f"{name}: the check still passes"


# Behaviour: run the real step script against a fake xcrun and the real jq.

GOOD_LINES = sorted(PINNED)


def xcresult_json(passed: list[str], results: dict[str, str] | None = None) -> str:
    suites: dict[str, list[dict]] = {}
    for line in passed:
        suite, _, name = line.partition("/")
        result = (results or {}).get(line, "Passed")
        suites.setdefault(suite, []).append({"name": f"{name}()", "nodeType": "Test Case", "result": result})
    return json.dumps({"testNodes": [{"name": "GrowinTests", "nodeType": "Unit test bundle", "children": [
        {"name": suite, "nodeType": "Test Suite", "children": cases} for suite, cases in suites.items()
    ]}]})


def run_required_step(list_lines: list[str], tests_json: str) -> tuple[int, str]:
    step = required_step(load_workflow())
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / ".github").mkdir()
        (tmp_path / ".github" / "required-swift-tests.txt").write_text("\n".join(list_lines) + "\n")
        (tmp_path / "GrowinTests.xcresult").mkdir()
        (tmp_path / "tests.json").write_text(tests_json)
        fake = tmp_path / "bin" / "xcrun"
        fake.parent.mkdir()
        fake.write_text('#!/bin/bash\ncat "$FAKE_JSON"\n')
        fake.chmod(0o755)
        summary = tmp_path / "summary.md"
        env = dict(os.environ, PATH=f"{fake.parent}:{os.environ['PATH']}", RUNNER_TEMP=str(tmp_path),
                   GITHUB_STEP_SUMMARY=str(summary), FAKE_JSON=str(tmp_path / "tests.json"))
        proc = subprocess.run(["bash", "-e", "-c", step["run"]], env=env, cwd=tmp_path,
                              capture_output=True, text=True)
        return proc.returncode, summary.read_text() if summary.exists() else ""


def test_step_passes_when_all_required_tests_passed():
    code, summary = run_required_step(GOOD_LINES, xcresult_json(GOOD_LINES))
    assert code == 0
    assert "8 of 8 ran and passed" in summary


def test_step_fails_when_a_required_test_did_not_run():
    code, _ = run_required_step(GOOD_LINES, xcresult_json(GOOD_LINES[1:]))
    assert code == 1


def test_step_fails_when_a_required_test_did_not_pass():
    code, _ = run_required_step(GOOD_LINES, xcresult_json(GOOD_LINES, {GOOD_LINES[0]: "Skipped"}))
    assert code == 1


def test_step_fails_on_a_bad_list_line_and_counts_it():
    code, summary = run_required_step(GOOD_LINES + ["NoSlashHere"], xcresult_json(GOOD_LINES))
    assert code == 1
    assert "8 of 9 ran and passed" in summary
