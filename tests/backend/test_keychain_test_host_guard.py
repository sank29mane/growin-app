"""Source scan for the production Keychain guard (issue #66, follow-ups 1 and 2 of PR #573).

GrowinTests is app-hosted: `xcodebuild test` runs the real Growin app with the operator's real
Keychain. The guard in `Growin/Security/ProductionKeychainGuard.swift` makes every Keychain call
on the production service fail closed when the process is a test host. Swift is CI-only here, so
this scan reads the source and proves:

1. Every `SecItem*` call in `KeychainStore.swift` and `LegacyT212KeychainCleanup.swift` sits in a
   function whose first statement is the guard, and the guard is never swallowed with `try?` or
   `try!`. Every `SecItem*` mention is a call, so a function reference cannot dodge the scan.
2. The guard functions are pinned to their exact bodies (whitespace aside): the predicate, the
   store's check, the cleanup's two entry points and the refusal. Presence of a substring is not
   enough, because `if false { guard() }` contains the substring and never runs it.
3. No `#if`, no environment or `ProcessInfo` opt-out, no `&& false` or `|| true` in the guarded
   files. The real-process refusal is unconditional and runs first. Injected inputs may add a
   refusal, never remove one.
4. The test-host signals are read from code only. A signal that survives only in a comment, or a
   typo'd key whose correct name sits in a comment, fails.
5. No other app file mentions `SecItem*` or `SecKeychain*`, or passes `protectedService:` or
   `testHost:`. The production service literal is defined once, in the guard file.
6. `RawKeychain` (the test helper that issues raw SecItem calls) refuses any service that is not
   test-prefixed with a non-empty suffix, before it stores the service.

Each rule has planted-sample break-proofs further down: a copy of the real source with the guard
removed, moved, wrapped, bypassed or weakened must produce offences. Reading uses `reduce_swift`
from `test_swift_scan_bypasses` (comments and strings gone) for call scans, and comment-stripped
code with strings kept for the pinned bodies and literals.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from test_app_does_not_push_t212_keys import strip_swift_comments_and_strings
from test_swift_scan_bypasses import reduce_swift

REPO = Path(__file__).resolve().parents[2]
APP = REPO / "Growin"
SECURITY = APP / "Security"
APP_TESTS = REPO / "GrowinTests"

KEYCHAIN_STORE = SECURITY / "KeychainStore.swift"
LEGACY_CLEANUP = SECURITY / "LegacyT212KeychainCleanup.swift"
GUARD = SECURITY / "ProductionKeychainGuard.swift"
LAUNCH_MIGRATIONS = SECURITY / "LaunchMigrations.swift"
RAW_KEYCHAIN = APP_TESTS / "WorkspaceIsolation" / "RawKeychain.swift"

# The only app files that may mention SecItem, and the only ones that may pass the guard's inputs.
SEC_ITEM_FILES = frozenset({KEYCHAIN_STORE.relative_to(APP), LEGACY_CLEANUP.relative_to(APP)})
GUARD_INPUT_FILES = frozenset(
    {KEYCHAIN_STORE.relative_to(APP), LEGACY_CLEANUP.relative_to(APP), GUARD.relative_to(APP)}
)

SEC_ITEM_CALL = re.compile(r"(?<![A-Za-z0-9_])SecItem[A-Za-z]*\s*\(")
SEC_ITEM_ANY = re.compile(r"(?<![A-Za-z0-9_])SecItem[A-Za-z]*")
SEC_KEYCHAIN_ANY = re.compile(r"(?<![A-Za-z0-9_])SecKeychain[A-Za-z]*")
FUNC = re.compile(r"(?<![A-Za-z0-9_])func\s+(?P<name>[A-Za-z_]\w*)")

# Constructs that can switch the guard off without removing its text.
CONDITIONAL_COMPILATION = re.compile(r"(?m)^\s*#(?:if|elseif|else|endif)\b")
FORCED_CONDITION = re.compile(r"&&\s*false\b|\|\|\s*true\b|\bfalse\s*&&|\btrue\s*\|\|")
ENVIRONMENT_READ = re.compile(r"(?<![A-Za-z0-9_])(?:ProcessInfo|getenv|NSClassFromString)(?![A-Za-z0-9_])|\benvironment\b")
GETENV = re.compile(r"(?<![A-Za-z0-9_])getenv(?![A-Za-z0-9_])")

PRODUCTION_LITERAL = 'static let productionService = "san.Growin.credentials.v1"'


# --------------------------------------------------------------------------------------------
# Reading functions out of Swift
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Func:
    name: str
    params: str
    start: int  # the `func` keyword
    body_start: int  # just after the opening brace
    body_end: int  # the closing brace

    def body(self, text: str) -> str:
        return text[self.body_start : self.body_end]


def _match(text: str, start: int, opener: str, closer: str) -> int | None:
    """Index of the closer that matches the opener at `start`, or None."""
    depth = 0
    for index in range(start, len(text)):
        if text[index] == opener:
            depth += 1
        elif text[index] == closer:
            depth -= 1
            if depth == 0:
                return index
    return None


def functions(text: str) -> list[Func]:
    found = []
    for hit in FUNC.finditer(text):
        open_paren = text.find("(", hit.end())
        if open_paren < 0:
            continue
        close_paren = _match(text, open_paren, "(", ")")
        if close_paren is None:
            continue
        open_brace = text.find("{", close_paren)
        if open_brace < 0:
            continue
        close_brace = _match(text, open_brace, "{", "}")
        if close_brace is None:
            continue
        found.append(
            Func(hit.group("name"), text[open_paren + 1 : close_paren], hit.start(), open_brace + 1, close_brace)
        )
    return found


def enclosing(funcs: list[Func], position: int) -> Func | None:
    inside = [f for f in funcs if f.body_start <= position < f.body_end]
    return max(inside, key=lambda f: f.start) if inside else None


def top_level(bare: str, func: Func, position: int) -> bool:
    """True when `position` is in the function's own body, not inside a nested block or closure."""
    chunk = bare[func.body_start : position]
    return chunk.count("{") == chunk.count("}")


def by_name(funcs: list[Func], name: str, *, params_have: str | None = None, params_lack: str | None = None) -> list[Func]:
    return [
        f
        for f in funcs
        if f.name == name
        and (params_have is None or params_have in f.params)
        and (params_lack is None or params_lack not in f.params)
    ]


def squash(text: str) -> str:
    return " ".join(text.split())


def norm(text: str) -> str:
    """All whitespace removed, so a pinned body ignores layout and nothing else."""
    return re.sub(r"\s+", "", text)


@dataclass(frozen=True)
class Source:
    """One Swift file, read two ways.

    `bare`: comments and string contents gone (call scans cannot be fooled by text).
    `code`: comments gone, strings kept (pinned bodies and literals; a comment is never evidence).
    """

    name: str
    bare: str
    code: str
    errors: tuple[str, ...]

    @property
    def bare_funcs(self) -> list[Func]:
        return functions(self.bare)

    @property
    def code_funcs(self) -> list[Func]:
        return functions(self.code)


def read(name: str, text: str) -> Source:
    reduced = reduce_swift(text)
    return Source(
        name,
        reduced.bare,
        strip_swift_comments_and_strings(text, keep_strings=True),
        tuple(f"{name}: cannot be read to the end: {e}" for e in reduced.errors),
    )


def pinned(source: Source, name: str, expected: str, *, params_have: str | None = None, params_lack: str | None = None) -> list[str]:
    """The one function `name` must exist and its body must equal `expected` (whitespace aside)."""
    found = by_name(source.code_funcs, name, params_have=params_have, params_lack=params_lack)
    label = f"{name}({params_have or ''})"
    if len(found) != 1:
        return [f"{source.name}: {label} must exist exactly once, found {len(found)}"]
    if norm(found[0].body(source.code)) != norm(expected):
        return [f"{source.name}: {label} is not the pinned body: {squash(found[0].body(source.code))[:120]}"]
    return []


def bypass_offences(source: Source, *, environment_reads: int = 0) -> list[str]:
    """`#if`, an environment read or a forced condition anywhere in a guarded file."""
    found = []
    for hit in CONDITIONAL_COMPILATION.finditer(source.bare):
        found.append(f"{source.name}: conditional compilation ({squash(hit.group(0))}) can switch the guard off")
    for hit in FORCED_CONDITION.finditer(source.bare):
        found.append(f"{source.name}: forced condition ({squash(hit.group(0))})")
    reads = ENVIRONMENT_READ.findall(source.bare)
    if len(reads) > environment_reads:
        found.append(f"{source.name}: reads the environment or process info ({len(reads)}x, allowed {environment_reads})")
    return found


# --------------------------------------------------------------------------------------------
# 1. The guard runs first in every function that reaches SecItem
# --------------------------------------------------------------------------------------------

STORE_GUARD = re.compile(r"(?<![A-Za-z0-9_?!])try\s+assertAccessAllowed\s*\(\s*\)")
STORE_GUARD_STATEMENT = "try assertAccessAllowed()"
SWALLOWED_STORE_GUARD = re.compile(r"try\s*[?!]\s*assertAccessAllowed|try\s*[?!]\s*Self\s*\.\s*checkAccess")


def sec_item_offences(source: Source, guard: re.Pattern[str]) -> list[str]:
    """Each SecItem call not in a function whose top level runs `guard` first, and any non-call mention."""
    bare, funcs = source.bare, source.bare_funcs
    found = []
    calls = list(SEC_ITEM_CALL.finditer(bare))
    if not calls:
        found.append(f"{source.name}: no SecItem call found, so the scan is not looking at the real file")
    mentions = list(SEC_ITEM_ANY.finditer(bare))
    if len(mentions) != len(calls):
        found.append(f"{source.name}: SecItem is mentioned {len(mentions)}x but called {len(calls)}x (a function reference?)")
    for call in calls:
        owner = enclosing(funcs, call.start())
        what = squash(call.group(0))
        if owner is None:
            found.append(f"{source.name}: {what} is outside any function")
            continue
        hit = guard.search(bare, owner.body_start, call.start())
        if hit is None:
            found.append(f"{source.name}: {what} in {owner.name}() has no guard before it")
        elif not top_level(bare, owner, hit.start()):
            found.append(f"{source.name}: the guard in {owner.name}() is inside a nested block, not at the top")
    if SEC_KEYCHAIN_ANY.search(bare):
        found.append(f"{source.name}: uses the legacy SecKeychain API")
    return found


def swallowed_guard_offences(source: Source) -> list[str]:
    return [
        f"{source.name}: the guard's error is swallowed ({squash(m.group(0))})"
        for m in SWALLOWED_STORE_GUARD.finditer(source.bare)
    ]


STORE_ASSERT_BODY = """
try Self.checkAccess(
    service: service,
    protectedService: ProductionKeychainGuard.productionService,
    testHost: TestHostDetection.isTestHost())
"""
STORE_CHECK_BODY = """
if ProductionKeychainGuard.refuses(service: service, protectedService: protectedService, testHost: testHost) {
    throw KeychainStoreError.productionKeychainRefusedUnderTests
}
"""
STORE_ALLOWED_BODY = """
do {
    try assertAccessAllowed()
    return true
} catch {
    return false
}
"""


def keychain_store_offences(text: str) -> list[str]:
    source = read("KeychainStore.swift", text)
    found = list(source.errors)
    found += sec_item_offences(source, STORE_GUARD)
    found += swallowed_guard_offences(source)
    found += bypass_offences(source)

    # Each function that reaches SecItem runs the guard as its very first statement.
    guarded = {f.name for f in (enclosing(source.bare_funcs, c.start()) for c in SEC_ITEM_CALL.finditer(source.bare)) if f}
    for required in ("rawData", "rawSet", "rawRemove"):
        if required not in guarded:
            found.append(f"KeychainStore.swift: {required}() is not a guarded SecItem function")
        for func in by_name(source.code_funcs, required):
            if not norm(func.body(source.code)).startswith(norm(STORE_GUARD_STATEMENT)):
                found.append(f"KeychainStore.swift: {required}() does not start with the guard")

    found += pinned(source, "assertAccessAllowed", STORE_ASSERT_BODY)
    found += pinned(source, "checkAccess", STORE_CHECK_BODY)
    found += pinned(source, "accessIsAllowed", STORE_ALLOWED_BODY)

    # The two migrations bail out before touching UserDefaults or the Keychain.
    for name in ("migrateLegacyUserDefaults", "migrateFlatItemsToScoped"):
        migrations = by_name(source.code_funcs, name)
        if len(migrations) != 1:
            found.append(f"KeychainStore.swift: {name}() must exist exactly once")
        elif not norm(migrations[0].body(source.code)).startswith(norm("guard accessIsAllowed() else { return migratableNames }")):
            found.append(f"KeychainStore.swift: {name}() does not refuse first")

    # The production service is an alias of the one definition in the guard file.
    if not re.search(
        r"nonisolated\s+static\s+let\s+productionService\s*=\s*ProductionKeychainGuard\s*\.\s*productionService\b",
        source.code,
    ):
        found.append("KeychainStore.swift: productionService is not an alias of the guard's definition")
    return found


# --- LegacyT212KeychainCleanup ---

CLEANUP_DELETE_GUARD = re.compile(
    r"guard\s+!\s*ProductionKeychainGuard\s*\.\s*refusesCurrentProcess\s*\(\s*service\s*:\s*service\s*\)"
    r"\s*else\s*\{\s*return\s+refusedUnderTestsStatus\s*\}"
)
CLEANUP_DELETE_BODY = """
guard !ProductionKeychainGuard.refusesCurrentProcess(service: service) else {
    return refusedUnderTestsStatus
}
return SecItemDelete(exactQuery(service: service, account: account) as CFDictionary)
"""
CLEANUP_PUBLIC_RUN_BODY = """
if ProductionKeychainGuard.refusesCurrentProcess(service: service) {
    return refusedOutcome(log: log)
}
return performRun(service: service, defaults: defaults, delete: delete, log: log)
"""
CLEANUP_EXPLICIT_RUN_BODY = """
if ProductionKeychainGuard.refusesCurrentProcess(service: service)
    || ProductionKeychainGuard.refuses(service: service, protectedService: protectedService, testHost: testHost) {
    return refusedOutcome(log: log)
}
return performRun(service: service, defaults: defaults, delete: delete, log: log)
"""
CLEANUP_REFUSED_BODY = """
log("legacy cleanup refused: \\(ProductionKeychainGuard.refusalCode)")
return Outcome(skipped: true, removed: 0, failedStatuses: [], refused: true)
"""


def legacy_cleanup_offences(text: str) -> list[str]:
    source = read("LegacyT212KeychainCleanup.swift", text)
    name = source.name
    found = list(source.errors)
    found += bypass_offences(source)
    found += swallowed_guard_offences(source)

    # deleteExactItem owns the one SecItem call and refuses before it.
    found += sec_item_offences(source, CLEANUP_DELETE_GUARD)
    owners = {f.name for f in (enclosing(source.bare_funcs, c.start()) for c in SEC_ITEM_CALL.finditer(source.bare)) if f}
    if owners != {"deleteExactItem"}:
        found.append(f"{name}: SecItem calls live in {sorted(owners)}, expected only deleteExactItem")
    found += pinned(source, "deleteExactItem", CLEANUP_DELETE_BODY)

    # Both entry points run the real-process refusal first and unconditionally. The injected pair may
    # only add a refusal.
    found += pinned(source, "runOnce", CLEANUP_PUBLIC_RUN_BODY, params_lack="protectedService")
    found += pinned(source, "runOnce", CLEANUP_EXPLICIT_RUN_BODY, params_have="protectedService")
    found += pinned(source, "refusedOutcome", CLEANUP_REFUSED_BODY)

    # The work is reachable only from the two entry points, and drives the match loop alone.
    for call in re.finditer(r"(?<![A-Za-z0-9_])performRun\s*\(", source.bare):
        if source.bare[: call.start()].rstrip().endswith("func"):
            continue  # the definition
        owner = enclosing(source.bare_funcs, call.start())
        if owner is None or owner.name != "runOnce":
            found.append(f"{name}: performRun is called from {owner.name if owner else 'outside a function'}")
    for call in re.finditer(r"(?<![A-Za-z0-9_])deleteAllMatches\s*\(", source.bare):
        owner = enclosing(source.bare_funcs, call.start())
        if owner is not None and owner.name != "performRun":
            found.append(f"{name}: deleteAllMatches is called from {owner.name}()")
    return found


# --------------------------------------------------------------------------------------------
# 2. The guard file
# --------------------------------------------------------------------------------------------

GUARD_PURE_BODY = "isProductionStore && testHost"
GUARD_BY_SERVICE_BODY = "refuses(isProductionStore: service == protectedService, testHost: testHost)"
GUARD_CURRENT_BODY = """
refuses(
    service: service,
    protectedService: productionService,
    testHost: TestHostDetection.isTestHost())
"""
GUARD_DETECT_BODY = "xctestCaseClassLoaded || environmentKeys.contains { environment[$0] != nil }"
GUARD_DETECT_SIGNATURE = (
    "static func isTestHost("
    "environment: [String: String] = ProcessInfo.processInfo.environment,"
    'xctestCaseClassLoaded: Bool = NSClassFromString("XCTestCase") != nil'
    ") -> Bool {"
)
GUARD_KEYS = (
    "static let environmentKeys = ["
    '"XCTestConfigurationFilePath","XCTestBundlePath","XCTestSessionIdentifier",'
    "]"
)


def guard_file_offences(text: str) -> list[str]:
    # Signals are read from `code`: a comment is never evidence.
    source = read("ProductionKeychainGuard.swift", text)
    name = source.name
    found = list(source.errors)
    # ProcessInfo and NSClassFromString appear once each, in isTestHost's default arguments.
    found += bypass_offences(source, environment_reads=ENVIRONMENT_READS_IN_GUARD_FILE)
    if GETENV.search(source.bare):
        found.append(f"{name}: calls getenv")

    if source.code.count(PRODUCTION_LITERAL) != 1:
        found.append(f"{name}: the production service literal must be defined here exactly once")
    if not re.search(r'static let refusalCode = "PRODUCTION_KEYCHAIN_REFUSED_UNDER_TESTS"', source.code):
        found.append(f"{name}: the stable refusal code is missing or changed")

    found += pinned(source, "refuses", GUARD_PURE_BODY, params_have="isProductionStore")
    found += pinned(source, "refuses", GUARD_BY_SERVICE_BODY, params_have="protectedService")
    found += pinned(source, "refusesCurrentProcess", GUARD_CURRENT_BODY)
    found += pinned(source, "isTestHost", GUARD_DETECT_BODY)
    if norm(GUARD_DETECT_SIGNATURE) not in norm(source.code):
        found.append(f"{name}: isTestHost() does not default to the real environment and the XCTestCase class")
    if norm(GUARD_KEYS) not in norm(source.code):
        found.append(f"{name}: environmentKeys is not exactly the three XCTest signals")
    return found


# ProcessInfo, environment (parameter name, twice in the pinned signature and body) and NSClassFromString.
ENVIRONMENT_READS_IN_GUARD_FILE = 5


# --------------------------------------------------------------------------------------------
# 3. Nothing else in the app reaches the Keychain or the guard's inputs
# --------------------------------------------------------------------------------------------

GUARD_INPUT_LABEL = re.compile(r"(?<![A-Za-z0-9_])(?:protectedService|testHost)\s*:")


def app_wide_offences(files: dict[Path, str]) -> list[str]:
    found = []
    for relative, text in sorted(files.items()):
        bare = reduce_swift(text).bare
        if relative not in SEC_ITEM_FILES and SEC_ITEM_ANY.search(bare):
            found.append(f"{relative}: mentions SecItem outside the two guarded files")
        if SEC_KEYCHAIN_ANY.search(bare):
            found.append(f"{relative}: uses the legacy SecKeychain API")
        if relative not in GUARD_INPUT_FILES and GUARD_INPUT_LABEL.search(bare):
            found.append(f"{relative}: passes protectedService: or testHost:, the guard's inputs")
    return found


def app_files() -> dict[Path, str]:
    return {p.relative_to(APP): p.read_text(encoding="utf-8") for p in sorted(APP.rglob("*.swift"))}


# --------------------------------------------------------------------------------------------
# 4. RawKeychain (the test helper with raw SecItem calls) refuses a non-test service
# --------------------------------------------------------------------------------------------

RAW_PREFIX_DECL = 'static let testServicePrefix = "san.Growin.credentials.v1.test."'
RAW_INIT = (
    "init(service candidate: String) {"
    "precondition("
    "candidate.hasPrefix(Self.testServicePrefix) && candidate.count > Self.testServicePrefix.count,"
    '"RawKeychain only works on a test-prefixed service")'
    "self.service = candidate"
    "}"
)


def raw_keychain_offences(text: str) -> list[str]:
    source = read("RawKeychain.swift", text)
    name = source.name
    found = list(source.errors)
    code = norm(source.code)
    if norm(RAW_PREFIX_DECL) not in code:
        found.append(f"{name}: the test service prefix is not exactly the pinned literal")
    if norm(RAW_INIT) not in code:
        found.append(f"{name}: init(service:) is not the pinned precondition")
    if "letservice:String" not in code or re.search(r"\bvar\s+service\b", source.code):
        found.append(f"{name}: service must be an immutable let")
    for hit in CONDITIONAL_COMPILATION.finditer(source.bare):
        found.append(f"{name}: conditional compilation ({squash(hit.group(0))}) can switch the check off")
    if len(re.findall(r"(?<![A-Za-z0-9_])init\s*\(", source.bare)) != 1:
        found.append(f"{name}: must have exactly one initializer")
    return found


# --------------------------------------------------------------------------------------------
# Real source
# --------------------------------------------------------------------------------------------

STORE_TEXT = KEYCHAIN_STORE.read_text(encoding="utf-8")
CLEANUP_TEXT = LEGACY_CLEANUP.read_text(encoding="utf-8")
GUARD_TEXT = GUARD.read_text(encoding="utf-8")
RAW_TEXT = RAW_KEYCHAIN.read_text(encoding="utf-8")


def test_the_keychain_store_runs_the_guard_first_in_every_sec_item_function():
    assert keychain_store_offences(STORE_TEXT) == []


def test_the_legacy_cleanup_refuses_unconditionally_before_any_sec_item_call_or_defaults_write():
    assert legacy_cleanup_offences(CLEANUP_TEXT) == []


def test_the_guard_is_the_pinned_predicate_with_every_test_host_signal():
    assert guard_file_offences(GUARD_TEXT) == []


def test_raw_keychain_refuses_a_service_that_is_not_test_prefixed():
    assert raw_keychain_offences(RAW_TEXT) == []


def test_no_other_app_file_mentions_sec_item_or_passes_the_guards_inputs():
    files = app_files()
    assert KEYCHAIN_STORE.relative_to(APP) in files and GUARD.relative_to(APP) in files  # positive control
    assert app_wide_offences(files) == []


def test_the_production_service_literal_is_defined_once_in_the_app():
    literal = re.compile(r'"san\.Growin\.credentials\.v1"')
    owners = [
        str(path)
        for path, text in app_files().items()
        if literal.search(strip_swift_comments_and_strings(text, keep_strings=True))
    ]
    assert owners == [str(GUARD.relative_to(APP))]


def test_launch_migrations_still_names_the_service_through_the_store_constant():
    # The T212 key scan and the launch guard both pin this exact call.
    text = LAUNCH_MIGRATIONS.read_text(encoding="utf-8")
    assert "LegacyT212KeychainCleanup.runOnce(service: KeychainStore.productionService)" in text
    assert "TestHostDetection.isTestHost(" in text


def test_the_cleanup_never_names_the_constant_the_swift_call_scan_bans():
    # LegacyT212KeychainCleanupTests.everyMigrationCallStatesItsService bans this needle in the file.
    assert "production" + "Service" not in CLEANUP_TEXT


def test_the_other_banned_needles_stay_out_of_the_files_this_change_touches():
    # GrowinTests scans: no production-service name or quoted literal, no legacy account names
    # (case-insensitive) outside the cleanup file, no shared-store reach, no process launcher.
    test_files = [
        APP_TESTS / "WorkspaceIsolation" / "ProductionKeychainGuardTests.swift",
        APP_TESTS / "WorkspaceIsolation" / "RawKeychain.swift",
        APP_TESTS / "WorkspaceIsolation" / "KeychainScopeTests.swift",
        APP_TESTS / "WorkspaceIsolation" / "LegacyT212KeychainCleanupTests.swift",
    ]
    quoted_literal = '"' + "san.Growin.credentials." + 'v1"'
    for path in test_files:
        text = path.read_text(encoding="utf-8")
        assert "production" + "Service" not in text, path.name
        assert quoted_literal not in text, path.name
        bare = reduce_swift(text).bare  # the scans read code, not strings or comments
        assert not re.search(r"KeychainStore\s*\.\s*(?:shared|self|init)\b", bare), path.name
        assert not re.search(r"(?<![A-Za-z0-9_])(?:Process|NSTask|NSWorkspace|NSClassFromString)(?![A-Za-z0-9_])", bare), path.name
    for path in (GUARD, RAW_KEYCHAIN, *test_files[:3]):
        lowered = path.read_text(encoding="utf-8").lower()
        assert "trading212apikey" not in lowered and "trading212apisecret" not in lowered, path.name


# --------------------------------------------------------------------------------------------
# Break-proofs: planted copies of the real source
# --------------------------------------------------------------------------------------------


def replaced(text: str, old: str, new: str, count: int = 1) -> str:
    assert text.count(old) >= 1, f"the plant did not apply: {old!r}"
    return text.replace(old, new, count)


STORE_GUARD_LINE = "        try assertAccessAllowed()\n"
RAW_DATA_HEAD = "private func rawData(account: String) throws -> Data? {\n"
RAW_SET_HEAD = "private func rawSet(_ data: Data, account: String) throws {\n"
RAW_REMOVE_HEAD = "private func rawRemove(account: String) throws {\n"
STORE_ASSERT_TEXT = (
    "        try Self.checkAccess(\n"
    "            service: service,\n"
    "            protectedService: ProductionKeychainGuard.productionService,\n"
    "            testHost: TestHostDetection.isTestHost())\n"
)
STORE_CHECK_TEXT = (
    "        if ProductionKeychainGuard.refuses(service: service, protectedService: protectedService, testHost: testHost) {\n"
    "            throw KeychainStoreError.productionKeychainRefusedUnderTests\n"
    "        }\n"
)
CLEANUP_DELETE_TEXT = (
    "        guard !ProductionKeychainGuard.refusesCurrentProcess(service: service) else {\n"
    "            return refusedUnderTestsStatus\n"
    "        }\n"
)
CLEANUP_PUBLIC_TEXT = (
    "        if ProductionKeychainGuard.refusesCurrentProcess(service: service) {\n"
    "            return refusedOutcome(log: log)\n"
    "        }\n"
)
CLEANUP_EXPLICIT_COND = (
    "        if ProductionKeychainGuard.refusesCurrentProcess(service: service)\n"
    "            || ProductionKeychainGuard.refuses(service: service, protectedService: protectedService, testHost: testHost) {\n"
)

# label -> (scan kind, planted text). With the guard broken, the scan must report something.
PLANTS: dict[str, tuple[str, str]] = {
    # --- KeychainStore: removing, moving or nesting the guard ---
    "store: guard removed from rawData": ("store", replaced(STORE_TEXT, RAW_DATA_HEAD + STORE_GUARD_LINE, RAW_DATA_HEAD)),
    "store: guard removed from rawSet": ("store", replaced(STORE_TEXT, RAW_SET_HEAD + STORE_GUARD_LINE, RAW_SET_HEAD)),
    "store: guard removed from rawRemove": (
        "store",
        replaced(STORE_TEXT, RAW_REMOVE_HEAD + STORE_GUARD_LINE, RAW_REMOVE_HEAD),
    ),
    "store: guard moved after the SecItem call": (
        "store",
        replaced(
            replaced(STORE_TEXT, RAW_REMOVE_HEAD + STORE_GUARD_LINE, RAW_REMOVE_HEAD),
            "SecItemDelete(baseQuery(account: account) as CFDictionary)\n",
            "SecItemDelete(baseQuery(account: account) as CFDictionary)\n        try assertAccessAllowed()\n",
        ),
    ),
    "store: guard nested in an if": (
        "store",
        replaced(
            STORE_TEXT,
            RAW_DATA_HEAD + STORE_GUARD_LINE,
            RAW_DATA_HEAD + "        if true { try assertAccessAllowed() }\n",
        ),
    ),
    "store: guard swallowed with try?": (
        "store",
        replaced(STORE_TEXT, RAW_DATA_HEAD + STORE_GUARD_LINE, RAW_DATA_HEAD + "        try? assertAccessAllowed()\n"),
    ),
    "store: guard forced with try!": (
        "store",
        replaced(STORE_TEXT, RAW_SET_HEAD + STORE_GUARD_LINE, RAW_SET_HEAD + "        try! assertAccessAllowed()\n"),
    ),
    "store: an early return before the guard": (
        "store",
        replaced(
            STORE_TEXT,
            RAW_DATA_HEAD + STORE_GUARD_LINE,
            RAW_DATA_HEAD + "        if account.isEmpty { return nil }\n" + STORE_GUARD_LINE,
        ),
    ),
    "store: a new unguarded raw read": (
        "store",
        replaced(
            STORE_TEXT,
            "    private func baseQuery(account: String)",
            "    func peek(account: String) -> OSStatus {\n"
            "        SecItemCopyMatching(baseQuery(account: account) as CFDictionary, nil)\n"
            "    }\n\n    private func baseQuery(account: String)",
        ),
    ),
    "store: SecItem in a property initializer": (
        "store",
        replaced(
            STORE_TEXT,
            "    private let service: String\n",
            "    private let service: String\n    private let probe = SecItemDelete([:] as CFDictionary)\n",
        ),
    ),
    "store: SecItem function reference, not a call (finding 6)": (
        "store",
        replaced(
            STORE_TEXT,
            "    private func baseQuery(account: String)",
            "    private let zap = SecItemDelete\n\n    private func baseQuery(account: String)",
        ),
    ),
    "store: legacy SecKeychain API used (finding 6)": (
        "store",
        replaced(
            STORE_TEXT,
            "    private func baseQuery(account: String)",
            "    private func old() { SecKeychainCopyDefault(nil) }\n\n    private func baseQuery(account: String)",
        ),
    ),
    # --- KeychainStore: the check itself ---
    "store: the check compares against the wrong service": (
        "store",
        replaced(
            STORE_TEXT,
            STORE_ASSERT_TEXT,
            "        try Self.checkAccess(\n            service: service,\n            protectedService: service,\n            testHost: false)\n",
        ),
    ),
    "store: the check no longer throws": (
        "store",
        replaced(STORE_TEXT, "throw KeychainStoreError.productionKeychainRefusedUnderTests", "return"),
    ),
    "store: checkAccess condition gets && false (finding 4)": (
        "store",
        replaced(
            STORE_TEXT,
            "testHost: testHost) {\n            throw KeychainStoreError",
            "testHost: testHost) && false {\n            throw KeychainStoreError",
        ),
    ),
    "store: the guard call inside if false (finding 4, Codex)": (
        "store",
        replaced(
            STORE_TEXT,
            STORE_ASSERT_TEXT,
            "        if false {\n" + STORE_ASSERT_TEXT.replace("\n            ", "\n                ") + "        }\n",
        ),
    ),
    "store: env-var early return in assertAccessAllowed (finding 4)": (
        "store",
        replaced(
            STORE_TEXT,
            STORE_ASSERT_TEXT,
            '        if ProcessInfo.processInfo.environment["GROWIN_ALLOW_REAL_KEYCHAIN"] != nil { return }\n'
            + STORE_ASSERT_TEXT,
        ),
    ),
    "store: the guard line inside #if !DEBUG (finding 4)": (
        "store",
        replaced(
            STORE_TEXT,
            RAW_DATA_HEAD + STORE_GUARD_LINE,
            RAW_DATA_HEAD + "        #if !DEBUG\n" + STORE_GUARD_LINE + "        #endif\n",
        ),
    ),
    "store: migration no longer refuses early": (
        "store",
        replaced(STORE_TEXT, "        guard accessIsAllowed() else { return migratableNames }\n", ""),
    ),
    "store: accessIsAllowed always true": (
        "store",
        replaced(STORE_TEXT, "        } catch {\n            return false\n", "        } catch {\n            return true\n"),
    ),
    "store: production service no longer the shared definition": (
        "store",
        replaced(
            STORE_TEXT,
            "nonisolated static let productionService = ProductionKeychainGuard.productionService",
            'nonisolated static let productionService = "san.Growin.credentials.v1"',
        ),
    ),
    # --- LegacyT212KeychainCleanup ---
    "cleanup: guard removed from deleteExactItem": ("cleanup", replaced(CLEANUP_TEXT, CLEANUP_DELETE_TEXT, "")),
    "cleanup: deleteExactItem guard returns success instead of refusing": (
        "cleanup",
        replaced(CLEANUP_TEXT, "            return refusedUnderTestsStatus\n", "            return errSecSuccess\n"),
    ),
    "cleanup: deleteExactItem guard inside if false (finding 4)": (
        "cleanup",
        replaced(CLEANUP_TEXT, CLEANUP_DELETE_TEXT, "        if false {\n" + CLEANUP_DELETE_TEXT + "        }\n"),
    ),
    "cleanup: deleteExactItem guard inside #if !DEBUG (finding 4)": (
        "cleanup",
        replaced(CLEANUP_TEXT, CLEANUP_DELETE_TEXT, "        #if !DEBUG\n" + CLEANUP_DELETE_TEXT + "        #endif\n"),
    ),
    "cleanup: public runOnce refusal removed": ("cleanup", replaced(CLEANUP_TEXT, CLEANUP_PUBLIC_TEXT, "")),
    "cleanup: public runOnce gets && false (finding 4)": (
        "cleanup",
        replaced(
            CLEANUP_TEXT,
            "        if ProductionKeychainGuard.refusesCurrentProcess(service: service) {\n            return refusedOutcome",
            "        if ProductionKeychainGuard.refusesCurrentProcess(service: service) && false {\n            return refusedOutcome",
        ),
    ),
    "cleanup: explicit overload drops the real-process refusal, trusts injected inputs (finding 2)": (
        "cleanup",
        replaced(
            CLEANUP_TEXT,
            CLEANUP_EXPLICIT_COND,
            "        if ProductionKeychainGuard.refuses(service: service, protectedService: protectedService, testHost: testHost) {\n",
        ),
    ),
    "cleanup: explicit overload needs the injected refusal too (finding 2)": (
        "cleanup",
        replaced(
            CLEANUP_TEXT,
            "service: service)\n            || ProductionKeychainGuard.refuses(",
            "service: service)\n            && ProductionKeychainGuard.refuses(",
        ),
    ),
    "cleanup: env-var opt-out before the refusal (finding 4)": (
        "cleanup",
        replaced(
            CLEANUP_TEXT,
            CLEANUP_PUBLIC_TEXT,
            '        if ProcessInfo.processInfo.environment["GROWIN_ALLOW_REAL_KEYCHAIN"] != nil {\n'
            "            return performRun(service: service, defaults: defaults, delete: delete, log: log)\n"
            "        }\n" + CLEANUP_PUBLIC_TEXT,
        ),
    ),
    "cleanup: refusal does not report refused": (
        "cleanup",
        replaced(CLEANUP_TEXT, "failedStatuses: [], refused: true)", "failedStatuses: [])"),
    ),
    "cleanup: a second function reaches the work": (
        "cleanup",
        replaced(
            CLEANUP_TEXT,
            "    private static let logger",
            "    static func sneaky(service: String, defaults: UserDefaults, delete: Deleter, log: (String) -> Void) -> Outcome {\n"
            "        performRun(service: service, defaults: defaults, delete: delete, log: log)\n"
            "    }\n\n    private static let logger",
        ),
    ),
    "cleanup: a second function reaches SecItem": (
        "cleanup",
        replaced(
            CLEANUP_TEXT,
            "    private static let logger",
            "    static func sneaky(service: String) -> OSStatus {\n"
            '        SecItemDelete(exactQuery(service: service, account: "x") as CFDictionary)\n'
            "    }\n\n    private static let logger",
        ),
    ),
    "cleanup: SecItem function reference, not a call (finding 6)": (
        "cleanup",
        replaced(CLEANUP_TEXT, "    private static let logger", "    static let zap = SecItemDelete\n\n    private static let logger"),
    ),
    # --- ProductionKeychainGuard ---
    "guard: predicate weakened to ||": ("guard", replaced(GUARD_TEXT, "isProductionStore && testHost", "isProductionStore || testHost")),
    "guard: predicate always false": ("guard", replaced(GUARD_TEXT, "isProductionStore && testHost", "false")),
    "guard: predicate gets || true (finding 4)": (
        "guard",
        replaced(GUARD_TEXT, "isProductionStore && testHost", "(isProductionStore && testHost) || true"),
    ),
    "guard: predicate wrapped in #if DEBUG (finding 4)": (
        "guard",
        replaced(
            GUARD_TEXT,
            "        isProductionStore && testHost\n",
            "        #if DEBUG\n        isProductionStore && testHost\n        #else\n        false\n        #endif\n",
        ),
    ),
    "guard: predicate gets an env-var opt-out (finding 4)": (
        "guard",
        replaced(
            GUARD_TEXT,
            "isProductionStore && testHost",
            'isProductionStore && testHost && ProcessInfo.processInfo.environment["GROWIN_ALLOW_REAL_KEYCHAIN"] == nil',
        ),
    ),
    "guard: service comparison inverted": (
        "guard",
        replaced(GUARD_TEXT, "service == protectedService", "service != protectedService"),
    ),
    "guard: current process ignores the real test host": (
        "guard",
        replaced(GUARD_TEXT, "testHost: TestHostDetection.isTestHost())", "testHost: false)"),
    ),
    "guard: refusesCurrentProcess uses isTestHost() && false (finding 4)": (
        "guard",
        replaced(GUARD_TEXT, "testHost: TestHostDetection.isTestHost())", "testHost: TestHostDetection.isTestHost() && false)"),
    ),
    "guard: a test-host signal dropped": ("guard", replaced(GUARD_TEXT, '        "XCTestSessionIdentifier",\n', "")),
    "guard: XCTestCase class signal dropped": (
        "guard",
        replaced(
            GUARD_TEXT,
            'xctestCaseClassLoaded: Bool = NSClassFromString("XCTestCase") != nil',
            "xctestCaseClassLoaded: Bool = false",
        ),
    ),
    "guard: XCTestCase signal only in a comment (finding 5)": (
        "guard",
        replaced(
            GUARD_TEXT,
            'xctestCaseClassLoaded: Bool = NSClassFromString("XCTestCase") != nil',
            'xctestCaseClassLoaded: Bool = false // NSClassFromString("XCTestCase") != nil',
        ),
    ),
    "guard: typo in an env key, correct name in a comment (finding 5)": (
        "guard",
        replaced(GUARD_TEXT, '"XCTestBundlePath",', '"XCTestBundlePth", // "XCTestBundlePath"'),
    ),
    "guard: detection ignores the class signal": (
        "guard",
        replaced(GUARD_TEXT, "xctestCaseClassLoaded || environmentKeys", "environmentKeys"),
    ),
    "guard: refusal code changed": (
        "guard",
        replaced(GUARD_TEXT, "PRODUCTION_KEYCHAIN_REFUSED_UNDER_TESTS", "REFUSED"),
    ),
    # --- RawKeychain (finding 3) ---
    "raw: precondition removed": (
        "raw",
        replaced(
            RAW_TEXT,
            "        precondition(\n"
            "            candidate.hasPrefix(Self.testServicePrefix) && candidate.count > Self.testServicePrefix.count,\n"
            '            "RawKeychain only works on a test-prefixed service")\n'
            "        self.service = candidate\n",
            "        self.service = candidate\n",
        ),
    ),
    "raw: prefix check only, empty suffix allowed": (
        "raw",
        replaced(RAW_TEXT, " && candidate.count > Self.testServicePrefix.count", ""),
    ),
    "raw: prefix loosened": (
        "raw",
        replaced(RAW_TEXT, '"san.Growin.credentials.v1.test."', '"san.Growin."'),
    ),
    "raw: precondition inside if false": (
        "raw",
        replaced(RAW_TEXT, "        precondition(\n", "        if false { precondition(true) }\n        _ = (\n"),
    ),
    "raw: service made mutable": ("raw", replaced(RAW_TEXT, "    let service: String\n", "    var service: String\n")),
    "raw: a second unchecked initializer": (
        "raw",
        replaced(RAW_TEXT, "    private func query(", "    init(unchecked service: String) { self.service = service }\n\n    private func query("),
    ),
    "raw: check wrapped in #if DEBUG": (
        "raw",
        replaced(
            RAW_TEXT,
            "        precondition(\n",
            "        #if DEBUG\n        precondition(\n",
        ).replace('test-prefixed service")\n', 'test-prefixed service")\n        #endif\n', 1),
    ),
}


def run_scan(kind: str, text: str) -> list[str]:
    scans = {
        "store": keychain_store_offences,
        "cleanup": legacy_cleanup_offences,
        "guard": guard_file_offences,
        "raw": raw_keychain_offences,
    }
    return scans[kind](text)


REAL = {"store": STORE_TEXT, "cleanup": CLEANUP_TEXT, "guard": GUARD_TEXT, "raw": RAW_TEXT}


@pytest.mark.parametrize("label", sorted(PLANTS))
def test_each_planted_guard_break_is_caught(label):
    kind, planted = PLANTS[label]
    assert planted != REAL[kind], "the plant changed nothing"
    assert run_scan(kind, planted), f"the scan missed: {label}"


def test_no_planted_break_goes_undetected():
    undetected = [label for label, (kind, text) in PLANTS.items() if not run_scan(kind, text)]
    assert undetected == [], f"{len(undetected)} planted breaks go undetected"
    assert len(PLANTS) >= 45


def test_a_new_app_file_that_mentions_sec_item_is_caught():
    files = app_files()
    for sample in (
        "func peek() { SecItemCopyMatching(q as CFDictionary, nil) }\n",
        "let zap = SecItemDelete\n",
        "func old() { SecKeychainCopyDefault(nil) }\n",
    ):
        files[Path("Services/Sneaky.swift")] = sample
        assert any("Sneaky.swift" in item for item in app_wide_offences(files)), sample


def test_an_app_file_that_passes_the_guards_inputs_is_caught():
    for sample in (
        "let a = LegacyT212KeychainCleanup.runOnce(service: s, protectedService: p, testHost: false, defaults: d, delete: x, log: l)\n",
        "try KeychainStore.checkAccess(service: s, protectedService: s, testHost: false)\n",
        "let flag = ProductionKeychainGuard.refuses(isProductionStore: true, testHost: false)\n",
    ):
        files = app_files()
        files[Path("Views/Sneaky.swift")] = sample
        assert any("Sneaky.swift" in item for item in app_wide_offences(files)), sample


def test_the_guard_inputs_named_only_in_comments_or_strings_are_not_findings():
    files = app_files()
    files[Path("Views/Fine.swift")] = (
        '// protectedService: and testHost: are guard inputs, SecItemDelete(x), SecKeychainFoo\n'
        'let note = "testHost: SecItemDelete( SecKeychainFoo"\n'
    )
    assert app_wide_offences(files) == []


def test_a_second_definition_of_the_production_literal_is_caught():
    literal = re.compile(r'"san\.Growin\.credentials\.v1"')
    files = app_files()
    files[Path("Views/Dup.swift")] = 'let s = "san.Growin.credentials.v1"\n'
    owners = sorted(
        str(path)
        for path, text in files.items()
        if literal.search(strip_swift_comments_and_strings(text, keep_strings=True))
    )
    assert owners != [str(GUARD.relative_to(APP))]


def test_a_literal_named_only_in_a_comment_is_not_a_definition():
    assert not re.search(
        r'"san\.Growin\.credentials\.v1"',
        strip_swift_comments_and_strings('// the service is "san.Growin.credentials.v1"\n', keep_strings=True),
    )


def test_the_function_reader_ignores_comments_and_strings_and_finds_nested_calls():
    text = (
        "// SecItemDelete(q)\n"
        'let note = "SecItemDelete("\n'
        "func outer() throws {\n"
        "    try assertAccessAllowed()\n"
        "    func inner() { SecItemDelete(q as CFDictionary) }\n"
        "    SecItemAdd(q as CFDictionary, nil)\n"
        "}\n"
    )
    found = sec_item_offences(read("x.swift", text), STORE_GUARD)
    # The comment and the string are not calls. The nested function has no guard of its own.
    assert len(found) == 1 and "inner()" in found[0]


def test_a_guard_after_the_call_or_inside_a_closure_is_not_a_guard():
    after = "func f() throws {\n    SecItemDelete(q as CFDictionary)\n    try assertAccessAllowed()\n}\n"
    nested = "func f() throws {\n    run { try assertAccessAllowed() }\n    SecItemDelete(q as CFDictionary)\n}\n"
    ok = "func f() throws {\n    try assertAccessAllowed()\n    SecItemDelete(q as CFDictionary)\n}\n"
    assert sec_item_offences(read("x", after), STORE_GUARD) != []
    assert sec_item_offences(read("x", nested), STORE_GUARD) != []
    assert sec_item_offences(read("x", ok), STORE_GUARD) == []


def test_a_pinned_body_ignores_layout_and_comments_but_nothing_else():
    source = read("x.swift", "func f() -> Bool {\n    // note\n    a  &&\n        b\n}\n")
    assert pinned(source, "f", "a && b") == []
    assert pinned(source, "f", "a || b") != []
    assert pinned(source, "g", "a && b") != []
