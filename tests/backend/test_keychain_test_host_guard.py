"""Source scan for the production Keychain guard (issue #66, follow-ups 1 and 2 of PR #573).

GrowinTests is app-hosted: `xcodebuild test` runs the real Growin app with the operator's real
Keychain. The guard in `Growin/Security/ProductionKeychainGuard.swift` makes every Keychain call
on the production service fail closed when the process is a test host. Swift is CI-only here, so
this scan reads the source and proves four things:

1. Every `SecItem*` call in `KeychainStore.swift` and `LegacyT212KeychainCleanup.swift` sits in a
   function that runs the guard first, at the top level of the function body (not inside an `if`,
   a closure or after the call), and the guard is never swallowed with `try?` or `try!`.
2. The guard itself is intact: the predicate is `isProductionStore && testHost`, the store checks
   the real production service and the real process, and the test-host signals are all present.
3. No other app file calls `SecItem*`, and no other app file passes the `protectedService:` or
   `testHost:` labels, so the app cannot add a second Keychain path or weaken the guard's inputs.
4. The production service literal is defined once, in the guard file.

Each rule has planted-sample break-proofs further down: a copy of the real source with the guard
removed, moved, swallowed or weakened must produce offences. Source scans read the comment- and
string-free output of `reduce_swift` from `test_swift_scan_bypasses`.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from test_swift_scan_bypasses import reduce_swift

REPO = Path(__file__).resolve().parents[2]
APP = REPO / "Growin"
SECURITY = APP / "Security"

KEYCHAIN_STORE = SECURITY / "KeychainStore.swift"
LEGACY_CLEANUP = SECURITY / "LegacyT212KeychainCleanup.swift"
GUARD = SECURITY / "ProductionKeychainGuard.swift"
LAUNCH_MIGRATIONS = SECURITY / "LaunchMigrations.swift"

# The only app files that may call SecItem, and the only ones that may pass the guard's inputs.
SEC_ITEM_FILES = frozenset({KEYCHAIN_STORE.relative_to(APP), LEGACY_CLEANUP.relative_to(APP)})
GUARD_INPUT_FILES = frozenset(
    {KEYCHAIN_STORE.relative_to(APP), LEGACY_CLEANUP.relative_to(APP), GUARD.relative_to(APP)}
)

SEC_ITEM_CALL = re.compile(r"(?<![A-Za-z0-9_])SecItem[A-Za-z]*\s*\(")
FUNC = re.compile(r"(?<![A-Za-z0-9_])func\s+(?P<name>[A-Za-z_]\w*)")

PRODUCTION_LITERAL = 'static let productionService = "san.Growin.credentials.v1"'


# --------------------------------------------------------------------------------------------
# Reading functions out of reduced Swift
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Func:
    name: str
    params: str
    start: int  # the `func` keyword
    body_start: int  # just after the opening brace
    body_end: int  # the closing brace

    def body(self, bare: str) -> str:
        return bare[self.body_start : self.body_end]


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


def functions(bare: str) -> list[Func]:
    found = []
    for hit in FUNC.finditer(bare):
        open_paren = bare.find("(", hit.end())
        if open_paren < 0:
            continue
        close_paren = _match(bare, open_paren, "(", ")")
        if close_paren is None:
            continue
        open_brace = bare.find("{", close_paren)
        if open_brace < 0:
            continue
        close_brace = _match(bare, open_brace, "{", "}")
        if close_brace is None:
            continue
        found.append(
            Func(hit.group("name"), bare[open_paren + 1 : close_paren], hit.start(), open_brace + 1, close_brace)
        )
    return found


def enclosing(funcs: list[Func], position: int) -> Func | None:
    inside = [f for f in funcs if f.body_start <= position < f.body_end]
    return max(inside, key=lambda f: f.start) if inside else None


def top_level(bare: str, func: Func, position: int) -> bool:
    """True when `position` is in the function's own body, not inside a nested block or closure."""
    chunk = bare[func.body_start : position]
    return chunk.count("{") == chunk.count("}")


def by_name(funcs: list[Func], name: str, *, params_have: str | None = None) -> list[Func]:
    return [f for f in funcs if f.name == name and (params_have is None or params_have in f.params)]


def squash(text: str) -> str:
    return " ".join(text.split())


# --------------------------------------------------------------------------------------------
# 1. The guard runs before every SecItem call
# --------------------------------------------------------------------------------------------

STORE_GUARD = re.compile(r"(?<![A-Za-z0-9_?!])try\s+assertAccessAllowed\s*\(\s*\)")
CLEANUP_DELETE_GUARD = re.compile(
    r"guard\s+!\s*ProductionKeychainGuard\s*\.\s*refusesCurrentProcess\s*\(\s*service\s*:\s*service\s*\)"
    r"\s*else\s*\{\s*return\s+refusedUnderTestsStatus\s*\}"
)
CLEANUP_RUN_GUARD = re.compile(
    r"if\s+ProductionKeychainGuard\s*\.\s*refuses\s*\(\s*service\s*:\s*service\s*,\s*"
    r"protectedService\s*:\s*protectedService\s*,\s*testHost\s*:\s*testHost\s*\)\s*\{"
    r"[^{}]*\breturn\s+Outcome\s*\([^()]*\brefused\s*:\s*true[^()]*\)[^{}]*\}"
)
SWALLOWED_STORE_GUARD = re.compile(r"try\s*[?!]\s*assertAccessAllowed|try\s*[?!]\s*Self\s*\.\s*checkAccess")


def sec_item_offences(bare: str, guard: re.Pattern[str], file_name: str) -> list[str]:
    """Each SecItem call that is not in a function whose top level runs `guard` first."""
    funcs = functions(bare)
    found = []
    calls = list(SEC_ITEM_CALL.finditer(bare))
    if not calls:
        found.append(f"{file_name}: no SecItem call found, so the scan is not looking at the real file")
    for call in calls:
        owner = enclosing(funcs, call.start())
        what = squash(call.group(0))
        if owner is None:
            found.append(f"{file_name}: {what} is outside any function")
            continue
        hit = guard.search(bare, owner.body_start, call.start())
        if hit is None:
            found.append(f"{file_name}: {what} in {owner.name}() has no guard before it")
        elif not top_level(bare, owner, hit.start()):
            found.append(f"{file_name}: the guard in {owner.name}() is inside a nested block, not at the top")
    return found


def swallowed_guard_offences(bare: str, file_name: str) -> list[str]:
    return [f"{file_name}: the guard's error is swallowed ({squash(m.group(0))})" for m in SWALLOWED_STORE_GUARD.finditer(bare)]


def keychain_store_offences(text: str) -> list[str]:
    reduced = reduce_swift(text)
    bare = reduced.bare
    found = [f"KeychainStore.swift: cannot be read to the end: {e}" for e in reduced.errors]
    found += sec_item_offences(bare, STORE_GUARD, "KeychainStore.swift")
    found += swallowed_guard_offences(bare, "KeychainStore.swift")
    funcs = functions(bare)

    guarded = {f.name for f in (enclosing(funcs, c.start()) for c in SEC_ITEM_CALL.finditer(bare)) if f}
    for required in ("rawData", "rawSet", "rawRemove"):
        if required not in guarded:
            found.append(f"KeychainStore.swift: {required}() is not a guarded SecItem function")

    # The guard function reads the real production service and the real process, and calls the check.
    allowed = by_name(funcs, "assertAccessAllowed")
    if len(allowed) != 1:
        found.append("KeychainStore.swift: assertAccessAllowed() must exist exactly once")
    else:
        body = squash(allowed[0].body(bare))
        expected = (
            "try Self.checkAccess( service: service, protectedService: ProductionKeychainGuard.productionService, "
            "testHost: TestHostDetection.isTestHost())"
        )
        if squash(expected).replace("( ", "(") not in body.replace("( ", "("):
            found.append("KeychainStore.swift: assertAccessAllowed() does not check the real service and process")
    check = by_name(funcs, "checkAccess")
    if len(check) != 1:
        found.append("KeychainStore.swift: checkAccess() must exist exactly once")
    else:
        body = squash(check[0].body(bare))
        refuses = re.search(
            r"if ProductionKeychainGuard\.refuses\(service: service, protectedService: protectedService, "
            r"testHost: testHost\) \{ throw KeychainStoreError\.productionKeychainRefusedUnderTests \}$",
            body,
        )
        if refuses is None:
            found.append("KeychainStore.swift: checkAccess() does not throw the typed refusal on the guard's decision")

    # The two migrations bail out before touching UserDefaults or the Keychain.
    for name in ("migrateLegacyUserDefaults", "migrateFlatItemsToScoped"):
        migrations = by_name(funcs, name)
        if len(migrations) != 1:
            found.append(f"KeychainStore.swift: {name}() must exist exactly once")
            continue
        body = migrations[0].body(bare)
        early = re.search(r"guard\s+accessIsAllowed\s*\(\s*\)\s*else\s*\{\s*return\s+migratableNames\s*\}", body)
        loop = re.search(r"\bfor\b", body)
        if early is None or loop is None or early.start() > loop.start() or not top_level(bare, migrations[0], migrations[0].body_start + early.start()):
            found.append(f"KeychainStore.swift: {name}() does not refuse before its loop")
    helper = by_name(funcs, "accessIsAllowed")
    if len(helper) != 1 or not STORE_GUARD.search(helper[0].body(bare)) or "return false" not in helper[0].body(bare):
        found.append("KeychainStore.swift: accessIsAllowed() must run the guard and return false on a refusal")

    # The production service is an alias of the one definition in the guard file.
    if not re.search(
        r"nonisolated\s+static\s+let\s+productionService\s*=\s*ProductionKeychainGuard\s*\.\s*productionService\b", bare
    ):
        found.append("KeychainStore.swift: productionService is not an alias of the guard's definition")
    return found


def legacy_cleanup_offences(text: str) -> list[str]:
    reduced = reduce_swift(text)
    bare = reduced.bare
    name = "LegacyT212KeychainCleanup.swift"
    found = [f"{name}: cannot be read to the end: {e}" for e in reduced.errors]
    funcs = functions(bare)

    # deleteExactItem owns the one SecItem call and refuses before it.
    found += sec_item_offences(bare, CLEANUP_DELETE_GUARD, name)
    owner_names = {f.name for f in (enclosing(funcs, c.start()) for c in SEC_ITEM_CALL.finditer(bare)) if f}
    if owner_names != {"deleteExactItem"}:
        found.append(f"{name}: SecItem calls live in {sorted(owner_names)}, expected only deleteExactItem")

    # The explicit runOnce refuses before the injected delete, any match loop or any defaults access.
    explicit = by_name(funcs, "runOnce", params_have="protectedService")
    if len(explicit) != 1:
        found.append(f"{name}: the explicit runOnce(service:protectedService:testHost:...) must exist exactly once")
    else:
        run = explicit[0]
        body = run.body(bare)
        refusal = CLEANUP_RUN_GUARD.search(body)
        if refusal is None:
            found.append(f"{name}: runOnce has no refusal that returns Outcome(... refused: true)")
        else:
            if not top_level(bare, run, run.body_start + refusal.start()):
                found.append(f"{name}: the runOnce refusal is inside a nested block")
            work = re.search(r"deleteAllMatches\s*\(|(?<![A-Za-z0-9_])defaults\s*\.|(?<![A-Za-z0-9_])delete\s*\(", body)
            if work is not None and work.start() < refusal.start():
                found.append(f"{name}: runOnce does work ({squash(work.group(0))}) before it refuses")
        # No other function may drive the match loop or the injected delete.
        for call in re.finditer(r"deleteAllMatches\s*\(", bare):
            owner = enclosing(funcs, call.start())
            if owner is not None and owner.name not in ("runOnce", "deleteAllMatches"):
                found.append(f"{name}: deleteAllMatches is called from {owner.name}()")
    # The public runOnce hands the real production service and the real process to the explicit one.
    public = [f for f in by_name(funcs, "runOnce") if "protectedService" not in f.params]
    if len(public) != 1:
        found.append(f"{name}: the public runOnce(service:...) must exist exactly once")
    else:
        body = squash(public[0].body(bare))
        for needle in (
            "protectedService: ProductionKeychainGuard.productionService",
            "testHost: TestHostDetection.isTestHost()",
        ):
            if needle not in body:
                found.append(f"{name}: the public runOnce does not pass `{needle}`")
    found += swallowed_guard_offences(bare, name)
    return found


# --------------------------------------------------------------------------------------------
# 2. The guard file
# --------------------------------------------------------------------------------------------


def guard_file_offences(text: str) -> list[str]:
    reduced = reduce_swift(text)
    bare = reduced.bare
    name = "ProductionKeychainGuard.swift"
    found = [f"{name}: cannot be read to the end: {e}" for e in reduced.errors]
    funcs = functions(bare)

    if text.count(PRODUCTION_LITERAL) != 1:
        found.append(f"{name}: the production service literal must be defined here exactly once")
    if not re.search(r'static let refusalCode = "PRODUCTION_KEYCHAIN_REFUSED_UNDER_TESTS"', text):
        found.append(f"{name}: the stable refusal code is missing or changed")

    pure = by_name(funcs, "refuses", params_have="isProductionStore")
    if len(pure) != 1 or not re.fullmatch(r"\s*isProductionStore\s*&&\s*testHost\s*", pure[0].body(bare)):
        found.append(f"{name}: refuses(isProductionStore:testHost:) must be exactly `isProductionStore && testHost`")

    by_service = by_name(funcs, "refuses", params_have="protectedService")
    if len(by_service) != 1 or not re.fullmatch(
        r"\s*refuses\s*\(\s*isProductionStore\s*:\s*service\s*==\s*protectedService\s*,\s*testHost\s*:\s*testHost\s*\)\s*",
        by_service[0].body(bare),
    ):
        found.append(f"{name}: refuses(service:protectedService:testHost:) must compare service == protectedService")

    current = by_name(funcs, "refusesCurrentProcess")
    if len(current) != 1:
        found.append(f"{name}: refusesCurrentProcess() must exist exactly once")
    else:
        body = squash(current[0].body(bare)).replace("( ", "(")
        for needle in ("protectedService: productionService", "testHost: TestHostDetection.isTestHost()"):
            if needle not in body:
                found.append(f"{name}: refusesCurrentProcess() does not use `{needle}`")

    detect = by_name(funcs, "isTestHost")
    if len(detect) != 1 or not re.fullmatch(
        r"\s*xctestCaseClassLoaded\s*\|\|\s*environmentKeys\s*\.\s*contains\s*\{\s*environment\s*\[\s*\$0\s*\]\s*!=\s*nil\s*\}\s*",
        detect[0].body(bare),
    ):
        found.append(f"{name}: isTestHost() must be `xctestCaseClassLoaded || any signal key is set`")
    if 'NSClassFromString("XCTestCase") != nil' not in text:
        found.append(f"{name}: the XCTestCase class signal is missing")
    for key in ("XCTestConfigurationFilePath", "XCTestBundlePath", "XCTestSessionIdentifier"):
        if f'"{key}"' not in text:
            found.append(f"{name}: the {key} signal is missing")
    if "ProcessInfo.processInfo.environment" not in text:
        found.append(f"{name}: isTestHost() does not default to the real process environment")
    return found


# --------------------------------------------------------------------------------------------
# 3. Nothing else in the app reaches the Keychain or the guard's inputs
# --------------------------------------------------------------------------------------------

GUARD_INPUT_LABEL = re.compile(r"(?<![A-Za-z0-9_])(?:protectedService|testHost)\s*:")


def app_wide_offences(files: dict[Path, str]) -> list[str]:
    found = []
    for relative, text in sorted(files.items()):
        reduced = reduce_swift(text)
        bare = reduced.bare
        if relative not in SEC_ITEM_FILES and SEC_ITEM_CALL.search(bare):
            found.append(f"{relative}: calls SecItem outside the two guarded files")
        if relative not in GUARD_INPUT_FILES and GUARD_INPUT_LABEL.search(bare):
            found.append(f"{relative}: passes protectedService: or testHost:, the guard's inputs")
    return found


def app_files() -> dict[Path, str]:
    return {p.relative_to(APP): p.read_text(encoding="utf-8") for p in sorted(APP.rglob("*.swift"))}


# --------------------------------------------------------------------------------------------
# Real source
# --------------------------------------------------------------------------------------------

STORE_TEXT = KEYCHAIN_STORE.read_text(encoding="utf-8")
CLEANUP_TEXT = LEGACY_CLEANUP.read_text(encoding="utf-8")
GUARD_TEXT = GUARD.read_text(encoding="utf-8")


def test_the_keychain_store_runs_the_guard_before_every_sec_item_call():
    assert keychain_store_offences(STORE_TEXT) == []


def test_the_legacy_cleanup_refuses_before_any_sec_item_call_or_defaults_write():
    assert legacy_cleanup_offences(CLEANUP_TEXT) == []


def test_the_guard_is_the_pinned_predicate_with_every_test_host_signal():
    assert guard_file_offences(GUARD_TEXT) == []


def test_no_other_app_file_calls_sec_item_or_passes_the_guards_inputs():
    files = app_files()
    assert KEYCHAIN_STORE.relative_to(APP) in files and GUARD.relative_to(APP) in files  # positive control
    assert app_wide_offences(files) == []


def test_the_production_service_literal_is_defined_once_in_the_app():
    literal = re.compile(r'"san\.Growin\.credentials\.v1"')
    owners = [str(path) for path, text in app_files().items() if literal.search(text)]
    assert owners == [str(GUARD.relative_to(APP))]


def test_launch_migrations_still_names_the_service_through_the_store_constant():
    # The T212 key scan and the launch guard both pin this exact call.
    text = LAUNCH_MIGRATIONS.read_text(encoding="utf-8")
    assert "LegacyT212KeychainCleanup.runOnce(service: KeychainStore.productionService)" in text
    assert "TestHostDetection.isTestHost(" in text


# --------------------------------------------------------------------------------------------
# Break-proofs: planted copies of the real source
# --------------------------------------------------------------------------------------------


def replaced(text: str, old: str, new: str, count: int = 1) -> str:
    assert text.count(old) >= count, f"the plant did not apply: {old!r}"
    return text.replace(old, new, count)


STORE_GUARD_LINE = "        try assertAccessAllowed()\n"

# Each plant is (label, scan, planted text). With the guard broken, the scan must report something.
PLANTS: dict[str, tuple[str, str]] = {
    "store: guard removed from rawData": (
        "store",
        replaced(
            STORE_TEXT,
            "private func rawData(account: String) throws -> Data? {\n" + STORE_GUARD_LINE,
            "private func rawData(account: String) throws -> Data? {\n",
        ),
    ),
    "store: guard removed from rawSet": (
        "store",
        replaced(
            STORE_TEXT,
            "private func rawSet(_ data: Data, account: String) throws {\n" + STORE_GUARD_LINE,
            "private func rawSet(_ data: Data, account: String) throws {\n",
        ),
    ),
    "store: guard removed from rawRemove": (
        "store",
        replaced(
            STORE_TEXT,
            "private func rawRemove(account: String) throws {\n" + STORE_GUARD_LINE,
            "private func rawRemove(account: String) throws {\n",
        ),
    ),
    "store: guard moved after the SecItem call": (
        "store",
        replaced(
            replaced(
                STORE_TEXT,
                "private func rawRemove(account: String) throws {\n" + STORE_GUARD_LINE,
                "private func rawRemove(account: String) throws {\n",
            ),
            "SecItemDelete(baseQuery(account: account) as CFDictionary)\n",
            "SecItemDelete(baseQuery(account: account) as CFDictionary)\n        try assertAccessAllowed()\n",
        ),
    ),
    "store: guard nested in an if": (
        "store",
        replaced(
            STORE_TEXT,
            "private func rawData(account: String) throws -> Data? {\n" + STORE_GUARD_LINE,
            "private func rawData(account: String) throws -> Data? {\n        if true { try assertAccessAllowed() }\n",
        ),
    ),
    "store: guard swallowed with try?": (
        "store",
        replaced(
            STORE_TEXT,
            "private func rawData(account: String) throws -> Data? {\n" + STORE_GUARD_LINE,
            "private func rawData(account: String) throws -> Data? {\n        try? assertAccessAllowed()\n",
        ),
    ),
    "store: guard forced with try!": (
        "store",
        replaced(
            STORE_TEXT,
            "private func rawSet(_ data: Data, account: String) throws {\n" + STORE_GUARD_LINE,
            "private func rawSet(_ data: Data, account: String) throws {\n        try! assertAccessAllowed()\n",
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
            "    private let service: String\n"
            "    private let probe = SecItemDelete([:] as CFDictionary)\n",
        ),
    ),
    "store: the check compares against the wrong service": (
        "store",
        replaced(
            STORE_TEXT,
            "protectedService: ProductionKeychainGuard.productionService,\n            testHost: TestHostDetection.isTestHost())",
            "protectedService: service,\n            testHost: false)",
        ),
    ),
    "store: the check no longer throws": (
        "store",
        replaced(STORE_TEXT, "throw KeychainStoreError.productionKeychainRefusedUnderTests", "return"),
    ),
    "store: migration no longer refuses early": (
        "store",
        replaced(
            STORE_TEXT,
            "        guard accessIsAllowed() else { return migratableNames }\n",
            "",
            count=1,
        ),
    ),
    "store: production service no longer the shared definition": (
        "store",
        replaced(
            STORE_TEXT,
            "nonisolated static let productionService = ProductionKeychainGuard.productionService",
            'nonisolated static let productionService = "san.Growin.credentials.v1"',
        ),
    ),
    "cleanup: guard removed from deleteExactItem": (
        "cleanup",
        replaced(
            CLEANUP_TEXT,
            "        guard !ProductionKeychainGuard.refusesCurrentProcess(service: service) else {\n"
            "            return refusedUnderTestsStatus\n"
            "        }\n",
            "",
        ),
    ),
    "cleanup: deleteExactItem guard returns success instead of refusing": (
        "cleanup",
        replaced(CLEANUP_TEXT, "            return refusedUnderTestsStatus\n", "            return errSecSuccess\n"),
    ),
    "cleanup: runOnce refusal removed": (
        "cleanup",
        replaced(
            CLEANUP_TEXT,
            "        if ProductionKeychainGuard.refuses(service: service, protectedService: protectedService, testHost: testHost) {\n"
            '            log("legacy cleanup refused: \\(ProductionKeychainGuard.refusalCode)")\n'
            "            return Outcome(skipped: true, removed: 0, failedStatuses: [], refused: true)\n"
            "        }\n",
            "",
        ),
    ),
    "cleanup: runOnce refusal moved after the defaults read": (
        "cleanup",
        replaced(
            replaced(
                CLEANUP_TEXT,
                "        if ProductionKeychainGuard.refuses(service: service, protectedService: protectedService, testHost: testHost) {\n"
                '            log("legacy cleanup refused: \\(ProductionKeychainGuard.refusalCode)")\n'
                "            return Outcome(skipped: true, removed: 0, failedStatuses: [], refused: true)\n"
                "        }\n",
                "",
            ),
            "        var failed: [OSStatus] = []\n",
            "        var failed: [OSStatus] = []\n"
            "        if ProductionKeychainGuard.refuses(service: service, protectedService: protectedService, testHost: testHost) {\n"
            "            return Outcome(skipped: true, removed: 0, failedStatuses: [], refused: true)\n"
            "        }\n",
        ),
    ),
    "cleanup: runOnce refusal does not report refused": (
        "cleanup",
        replaced(
            CLEANUP_TEXT,
            "failedStatuses: [], refused: true)",
            "failedStatuses: [])",
        ),
    ),
    "cleanup: public runOnce passes a false test host": (
        "cleanup",
        replaced(CLEANUP_TEXT, "testHost: TestHostDetection.isTestHost(),", "testHost: false,"),
    ),
    "cleanup: public runOnce protects a different service": (
        "cleanup",
        replaced(
            CLEANUP_TEXT,
            "protectedService: ProductionKeychainGuard.productionService,\n            testHost",
            'protectedService: "other",\n            testHost',
        ),
    ),
    "cleanup: a second function reaches SecItem": (
        "cleanup",
        replaced(
            CLEANUP_TEXT,
            "    private static let logger",
            "    static func sneaky(service: String) -> OSStatus {\n"
            "        SecItemDelete(exactQuery(service: service, account: \"x\") as CFDictionary)\n"
            "    }\n\n    private static let logger",
        ),
    ),
    "guard: predicate weakened to ||": (
        "guard",
        replaced(GUARD_TEXT, "isProductionStore && testHost", "isProductionStore || testHost"),
    ),
    "guard: predicate always false": (
        "guard",
        replaced(GUARD_TEXT, "isProductionStore && testHost", "false"),
    ),
    "guard: service comparison inverted": (
        "guard",
        replaced(GUARD_TEXT, "service == protectedService", "service != protectedService"),
    ),
    "guard: current process ignores the real test host": (
        "guard",
        replaced(
            GUARD_TEXT,
            "testHost: TestHostDetection.isTestHost())",
            "testHost: false)",
        ),
    ),
    "guard: a test-host signal dropped": (
        "guard",
        replaced(GUARD_TEXT, '        "XCTestSessionIdentifier",\n', ""),
    ),
    "guard: XCTestCase class signal dropped": (
        "guard",
        replaced(GUARD_TEXT, 'xctestCaseClassLoaded: Bool = NSClassFromString("XCTestCase") != nil', "xctestCaseClassLoaded: Bool = false"),
    ),
    "guard: detection ignores the class signal": (
        "guard",
        replaced(GUARD_TEXT, "xctestCaseClassLoaded || environmentKeys", "environmentKeys"),
    ),
    "guard: refusal code changed": (
        "guard",
        replaced(GUARD_TEXT, "PRODUCTION_KEYCHAIN_REFUSED_UNDER_TESTS", "REFUSED"),
    ),
}


def run_scan(kind: str, text: str) -> list[str]:
    if kind == "store":
        return keychain_store_offences(text)
    if kind == "cleanup":
        return legacy_cleanup_offences(text)
    return guard_file_offences(text)


@pytest.mark.parametrize("label", sorted(PLANTS))
def test_each_planted_guard_break_is_caught(label):
    kind, planted = PLANTS[label]
    assert planted != {"store": STORE_TEXT, "cleanup": CLEANUP_TEXT, "guard": GUARD_TEXT}[kind]
    assert run_scan(kind, planted), f"the scan missed: {label}"


def test_no_planted_break_goes_undetected():
    undetected = [label for label, (kind, text) in PLANTS.items() if not run_scan(kind, text)]
    assert undetected == [], f"{len(undetected)} planted breaks go undetected"
    assert len(PLANTS) >= 28


def test_a_new_app_file_that_calls_sec_item_is_caught():
    files = app_files()
    files[Path("Services/Sneaky.swift")] = "func peek() { SecItemCopyMatching(q as CFDictionary, nil) }\n"
    assert any("Sneaky.swift" in item for item in app_wide_offences(files))


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
        '// protectedService: and testHost: are guard inputs\nlet note = "testHost: SecItemDelete("\n'
    )
    assert app_wide_offences(files) == []


def test_a_second_definition_of_the_production_literal_is_caught():
    literal = re.compile(r'"san\.Growin\.credentials\.v1"')
    files = app_files()
    files[Path("Views/Dup.swift")] = 'let s = "san.Growin.credentials.v1"\n'
    owners = sorted(str(path) for path, text in files.items() if literal.search(text))
    assert owners != [str(GUARD.relative_to(APP))]


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
    found = sec_item_offences(reduce_swift(text).bare, STORE_GUARD, "x.swift")
    # The comment and the string are not calls. The nested function has no guard of its own.
    assert len(found) == 1 and "inner()" in found[0]


def test_a_guard_after_the_call_or_inside_a_closure_is_not_a_guard():
    after = "func f() throws {\n    SecItemDelete(q as CFDictionary)\n    try assertAccessAllowed()\n}\n"
    nested = "func f() throws {\n    run { try assertAccessAllowed() }\n    SecItemDelete(q as CFDictionary)\n}\n"
    ok = "func f() throws {\n    try assertAccessAllowed()\n    SecItemDelete(q as CFDictionary)\n}\n"
    assert sec_item_offences(reduce_swift(after).bare, STORE_GUARD, "x") != []
    assert sec_item_offences(reduce_swift(nested).bare, STORE_GUARD, "x") != []
    assert sec_item_offences(reduce_swift(ok).bare, STORE_GUARD, "x") == []
