"""The required-Swift-tests list must match the Swift sources.

.github/required-swift-tests.txt names the production-Keychain tripwires and
canaries that the "Swift unit tests" job must see run and pass (see the step
"Require the Keychain tripwire and canary tests" in swift-ci.yml). CI can only
check names it is given, so a rename in Swift must fail here instead of
silently turning a required test into a "did not run" in CI.

The tests arrive with PR #575. Until it is merged the Swift files do not hold
them, and the match tests skip. Offline: no Xcode, no Swift.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

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

SUITE_RE = re.compile(r"^\s*(?:final\s+)?(?:struct|class)\s+(\w+)", re.MULTILINE)
TEST_RE = re.compile(r"^\s*@Test\b[^\n]*?\bfunc\s+(\w+)\s*\(", re.MULTILINE)

needs_575 = pytest.mark.skipif(
    not TRIPWIRE.exists(),
    reason=(
        "GrowinTests/WorkspaceIsolation/ProductionTripwireTests.swift is absent: "
        "the tripwires and canaries arrive with PR #575 (fix/66-keychain-test-guard). "
        "This check runs for real once it is merged."
    ),
)


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


def on_disk_tests() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for suite, path in SWIFT_FILES.items():
        assert path.exists(), f"{path.relative_to(REPO_ROOT)} is missing (ProductionTripwireTests exists, so #575 landed)"
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


def test_workflow_reads_the_list():
    # The step must read the list file and the list must trigger the job.
    text = WORKFLOW.read_text()
    assert ".github/required-swift-tests.txt" in text
    assert "Require the Keychain tripwire and canary tests" in text


@needs_575
def test_list_matches_the_swift_sources():
    problems = check_list(read_list(LIST.read_text()), on_disk_tests())
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
