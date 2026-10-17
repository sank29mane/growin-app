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
7. Only allow-listed functions may mention SecItem, and each opens with the exact unconditional guard
   statement. The query builders are pinned so `kSecAttrService` carries the same `service` the guard
   checked (not a constant, not a literal, not absent, not doubled).
8. Nothing in `Growin/` or `GrowinTests/` declares a type, typealias, extension or member that shadows
   `TestHostDetection` or `ProductionKeychainGuard`, or re-declares the guard functions.
9. The tripwire test (`ProductionTripwireTests.swift`) runs the real production wiring and only expects
   refusals: read-only, every call inside a refusal assertion, no output. The #573 scans exempt it by
   exact path and for one exact expression, in Python and in the Swift mirror.
10. The refusal log line is the code and the call-site kind only, and changes no control flow. The
   UI-test app process carries `GROWIN_UI_TEST_HOST`, which `TestHostDetection` reads.
11. Every SecItem owner is pinned WHOLE (not only its first statement and its query builder), so the
   service cannot be stripped, swapped or merged over after the builder runs. The runtime canaries in
   `KeychainScopeTests` and `LegacyT212KeychainCleanupTests` prove the same thing without any scan.
12. The tripwire cannot be compiled out (`#if`), or disabled (traits), or moved out of a `@Test`, and it
   cannot write through the `KeychainStorage` property wrapper.
13. The guard is not shadowed one layer down: `TestHostDetection` calls `Foundation.`-qualified names and
   a plain loop, and `Growin/` may not extend the standard types (sugared or aliased forms included),
   overload `==`, `&&`, `||` or `!` on them, or declare names that stand in for `NSClassFromString`,
   `ProcessInfo` or a system module.
14. The four tripwire tests are pinned WHOLE, byte for byte: the attribute line, the signature, every
   statement in order and the closing brace. `#expect` being present is not enough: a `return`, a closure
   that is never called, `if false`, `guard false else { return }`, an unreachable `defer`, a `try?`, a
   no-op local `expect`, inverted arguments or a changed account all keep an `#expect` in the text and
   skip or weaken it. No function, macro or type named `expect`, `require`, `Issue` or `withKnownIssue`
   may be declared anywhere in GrowinTests.
15. The four service-isolation canaries and the helper that runs their body are pinned WHOLE too, so an
   assertion cannot be removed, the test disabled, or the body skipped. No `#expect` or `#require`
   argument in a canary or the tripwire may read a credential (`.string(`, `.data(`, `rawData`,
   `legacyFlatData`): a failure message must only ever show a Bool.
16. The Security symbols that build and send a query (`kSec*`, `SecItem*`) are `Security.`-qualified in
   every pinned owner and query builder, nothing in `Growin/` or `GrowinTests/` may declare a name that
   starts `kSec`, `errSec`, `SecItem` or `SecKeychain`, and in GrowinTests only `RawKeychain.swift` may
   mention `SecItem*` or `SecKeychain*`. `dlsym`, `dlopen` and `NSSelectorFromString` are banned in both.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from test_app_does_not_push_t212_keys import strip_swift_comments_and_strings
from test_swift_scan_bypasses import TRIPWIRE_PATH, TRIPWIRE_SHARED_CALL, reduce_swift, tripwire_exempted

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


def pinned(
    source: Source,
    name: str,
    expected: str,
    *,
    params_have: str | None = None,
    params_lack: str | None = None,
    params_norm: str | None = None,
) -> list[str]:
    """The one function `name` must exist and its body must equal `expected` (whitespace aside)."""
    found = by_name(source.code_funcs, name, params_have=params_have, params_lack=params_lack)
    if params_norm is not None:
        found = [f for f in found if norm(f.params) == params_norm]
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

STORE_GUARD = re.compile(r"(?<![A-Za-z0-9_?!])try\s+assertAccessAllowed\s*\(\s*for\s*:\s*\.\w+\s*\)")
SWALLOWED_STORE_GUARD = re.compile(r"try\s*[?!]\s*assertAccessAllowed|try\s*[?!]\s*Self\s*\.\s*checkAccess")

# The ONLY functions that may mention SecItem in each file, and the exact statement each one opens with.
STORE_OWNERS = {
    "rawData": "try assertAccessAllowed(for: .read)",
    "rawSet": "try assertAccessAllowed(for: .write)",
    "rawRemove": "try assertAccessAllowed(for: .delete)",
}
CLEANUP_OWNERS = frozenset({"deleteExactItem"})
RAW_OWNERS = frozenset({"set", "remove", "data"})  # RawKeychain: its init is the guard

SERVICE_KEY = re.compile(r"(?<![A-Za-z0-9_])kSecAttrService(?![A-Za-z0-9_])")


def opens_with(body: str, statement: str) -> bool:
    """The body's first statement is exactly `statement` (layout aside), ended by a newline or `;`.

    A ternary, an `if`, a `#if`, `try?` or any other prefix fails, because the next token must start a
    new statement and nothing may come before the guard.
    """
    want = norm(statement)
    i, n = 0, len(body)
    for char in want:
        while i < n and body[i].isspace():
            i += 1
        if i >= n or body[i] != char:
            return False
        i += 1
    return re.match(r"[ \t]*(?:;|\n)", body[i:]) is not None


def sec_item_offences(source: Source, guard: re.Pattern[str] | None, owners: frozenset[str]) -> list[str]:
    """SecItem may be mentioned only as a call, only in an allow-listed function, guarded at its top level."""
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
        if owner.name not in owners:
            found.append(f"{source.name}: {what} in {owner.name}() is not on the allow-list {sorted(owners)}")
            continue
        if guard is None:
            continue
        hit = guard.search(bare, owner.body_start, call.start())
        if hit is None:
            found.append(f"{source.name}: {what} in {owner.name}() has no guard before it")
        elif not top_level(bare, owner, hit.start()):
            found.append(f"{source.name}: the guard in {owner.name}() is inside a nested block, not at the top")
    for name in sorted(owners):
        if len(by_name(funcs, name)) != 1:
            found.append(f"{source.name}: the allow-listed {name}() must exist exactly once")
    if SEC_KEYCHAIN_ANY.search(bare):
        found.append(f"{source.name}: uses the legacy SecKeychain API")
    return found


def swallowed_guard_offences(source: Source) -> list[str]:
    return [
        f"{source.name}: the guard's error is swallowed ({squash(m.group(0))})"
        for m in SWALLOWED_STORE_GUARD.finditer(source.bare)
    ]


def query_binding_offences(source: Source, builder: str, builder_params: str, expected_body: str, *, users: frozenset[str], user_call: str) -> list[str]:
    """The service the guard checked is the service in the real query.

    The query builder's body is pinned: exactly one `kSecAttrService as String: service`, where `service` is
    the stored property (store, RawKeychain) or the parameter (cleanup) the guard checked. The key appears
    once in the file, and the builder is called only from the allow-listed functions.
    """
    found = pinned(source, builder, expected_body, params_norm=norm(builder_params))
    if len(SERVICE_KEY.findall(source.bare)) != 1:
        found.append(f"{source.name}: kSecAttrService must appear exactly once, in {builder}()")
    funcs = source.bare_funcs
    for call in re.finditer(r"(?<![A-Za-z0-9_])" + re.escape(builder) + r"\s*\(", source.bare):
        if source.bare[: call.start()].rstrip().endswith("func"):
            continue  # the definition
        owner = enclosing(funcs, call.start())
        if owner is None or owner.name not in users:
            found.append(f"{source.name}: {builder}() is called from {owner.name if owner else 'outside a function'}")
    for func in (f for name in users for f in by_name(source.code_funcs, name)):
        if norm(user_call) not in norm(func.body(source.code)):
            found.append(f"{source.name}: {func.name}() does not build its query with {user_call}")
    return found


QUERY_BODY = """
[
    Security.kSecClass as String: Security.kSecClassGenericPassword,
    Security.kSecAttrService as String: service,
    Security.kSecAttrAccount as String: account,
]
"""
CLEANUP_QUERY_BODY = """
[
    Security.kSecClass as String: Security.kSecClassGenericPassword,
    Security.kSecAttrService as String: service,
    Security.kSecAttrAccount as String: account,
    Security.kSecUseAuthenticationUI as String: Security.kSecUseAuthenticationUIFail,
]
"""

STORE_RAW_DATA_BODY = """
try assertAccessAllowed(for: .read)
var query = baseQuery(account: account)
query[Security.kSecReturnData as String] = true
query[Security.kSecMatchLimit as String] = Security.kSecMatchLimitOne

var item: CFTypeRef?
let status = Security.SecItemCopyMatching(query as CFDictionary, &item)
if status == errSecItemNotFound {
    return nil
}
guard status == errSecSuccess else {
    throw KeychainStoreError.status(status)
}
guard let data = item as? Data else {
    throw KeychainStoreError.unexpectedData
}
return data
"""
STORE_RAW_SET_BODY = """
try assertAccessAllowed(for: .write)
if data.isEmpty {
    try rawRemove(account: account)
    return
}

let query = baseQuery(account: account)
let attributes: [String: Any] = [Security.kSecValueData as String: data]
let updateStatus = Security.SecItemUpdate(query as CFDictionary, attributes as CFDictionary)

if updateStatus == errSecItemNotFound {
    var item = query
    item[Security.kSecValueData as String] = data
    item[Security.kSecAttrAccessible as String] = Security.kSecAttrAccessibleWhenUnlockedThisDeviceOnly
    let addStatus = Security.SecItemAdd(item as CFDictionary, nil)
    guard addStatus == errSecSuccess else {
        throw KeychainStoreError.status(addStatus)
    }
    return
}

guard updateStatus == errSecSuccess else {
    throw KeychainStoreError.status(updateStatus)
}
"""
STORE_RAW_REMOVE_BODY = """
try assertAccessAllowed(for: .delete)
let status = Security.SecItemDelete(baseQuery(account: account) as CFDictionary)
guard status == errSecSuccess || status == errSecItemNotFound else {
    throw KeychainStoreError.status(status)
}
"""
CLEANUP_DELETE_ALL_BODY = """
var removed = 0
for _ in 0..<maxDeletesPerAccount {
    let status = delete(service, account)
    switch status {
    case errSecSuccess:
        removed += 1
    case errSecItemNotFound:
        return (removed, nil)
    default:
        return (removed, status)
    }
}
return (removed, tooManyMatchesStatus)
"""
RAW_SET_BODY = """
var item = query(account)
item[Security.kSecValueData as String] = data
item[Security.kSecAttrAccessible as String] = Security.kSecAttrAccessibleWhenUnlockedThisDeviceOnly
let status = Security.SecItemAdd(item as CFDictionary, nil)
guard status == errSecSuccess else { throw KeychainStoreError.status(status) }
"""
RAW_REMOVE_BODY = "Security.SecItemDelete(query(account) as CFDictionary)"
RAW_DATA_BODY = """
var item = query(account)
item[Security.kSecReturnData as String] = true
item[Security.kSecMatchLimit as String] = Security.kSecMatchLimitOne
var result: CFTypeRef?
let status = Security.SecItemCopyMatching(item as CFDictionary, &result)
if status == errSecItemNotFound { return nil }
guard status == errSecSuccess else { throw KeychainStoreError.status(status) }
return result as? Data
"""

STORE_ASSERT_BODY = """
try Self.checkAccess(
    service: service,
    protectedService: ProductionKeychainGuard.productionService,
    testHost: TestHostDetection.isTestHost(),
    site: site)
"""
STORE_CHECK_BODY = """
if ProductionKeychainGuard.refuses(service: service, protectedService: protectedService, testHost: testHost) {
    ProductionKeychainGuard.recordRefusal(site: site)
    throw KeychainStoreError.productionKeychainRefusedUnderTests
}
"""
STORE_ALLOWED_BODY = """
do {
    try assertAccessAllowed(for: .write)
    return true
} catch {
    return false
}
"""
STORE_INIT = "init(service: String) { self.service = service }"


def keychain_store_offences(text: str) -> list[str]:
    source = read("KeychainStore.swift", text)
    found = list(source.errors)
    found += sec_item_offences(source, STORE_GUARD, frozenset(STORE_OWNERS))
    found += swallowed_guard_offences(source)
    found += bypass_offences(source)

    # Each allow-listed function opens with its exact, unconditional guard statement.
    for name, statement in STORE_OWNERS.items():
        for func in by_name(source.code_funcs, name):
            if not opens_with(func.body(source.code), statement):
                found.append(f"KeychainStore.swift: {name}() does not open with `{statement}`")

    # The WHOLE body of every SecItem owner is pinned, so nothing can strip, swap or merge over the
    # service between the query builder and the SecItem call.
    found += pinned(source, "rawData", STORE_RAW_DATA_BODY, params_norm=norm("account: String"))
    found += pinned(source, "rawSet", STORE_RAW_SET_BODY, params_norm=norm("_ data: Data, account: String"))
    found += pinned(source, "rawRemove", STORE_RAW_REMOVE_BODY, params_norm=norm("account: String"))
    found += pinned(source, "assertAccessAllowed", STORE_ASSERT_BODY, params_norm=norm("for site: RefusalSite"))
    found += pinned(source, "checkAccess", STORE_CHECK_BODY)
    found += pinned(source, "accessIsAllowed", STORE_ALLOWED_BODY)

    # The two migrations bail out before touching UserDefaults or the Keychain.
    for name in ("migrateLegacyUserDefaults", "migrateFlatItemsToScoped"):
        migrations = by_name(source.code_funcs, name)
        if len(migrations) != 1:
            found.append(f"KeychainStore.swift: {name}() must exist exactly once")
        elif not norm(migrations[0].body(source.code)).startswith(norm("guard accessIsAllowed() else { return migratableNames }")):
            found.append(f"KeychainStore.swift: {name}() does not refuse first")

    # The service the guard checks is the service the query uses.
    found += query_binding_offences(
        source,
        "baseQuery",
        "account: String",
        QUERY_BODY,
        users=frozenset(STORE_OWNERS),
        user_call="baseQuery(account: account)",
    )
    code = norm(source.code)
    if "privateletservice:String" not in code or re.search(r"\bvar\s+service\b", source.code):
        found.append("KeychainStore.swift: service must be a private immutable let")
    if norm(STORE_INIT) not in code or len(re.findall(r"self\s*\.\s*service\s*=", source.bare)) != 1:
        found.append("KeychainStore.swift: init(service:) must be the only place that stores the service, unchanged")

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
    r"\s*else\s*\{\s*ProductionKeychainGuard\s*\.\s*recordRefusal\s*\(\s*site\s*:\s*\.delete\s*\)\s*"
    r"return\s+refusedUnderTestsStatus\s*\}"
)
CLEANUP_DELETE_BODY = """
guard !ProductionKeychainGuard.refusesCurrentProcess(service: service) else {
    ProductionKeychainGuard.recordRefusal(site: .delete)
    return refusedUnderTestsStatus
}
return Security.SecItemDelete(exactQuery(service: service, account: account) as CFDictionary)
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
# Directly under `if let status = result.error {`, so nothing runs between the error and the return.
CLEANUP_TERMINAL_REFUSAL = """
if let status = result.error {
    if status == refusedUnderTestsStatus { return refusedOutcome(log: log) }
"""
CLEANUP_REFUSED_BODY = """
ProductionKeychainGuard.recordRefusal(site: .cleanup)
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
    found += sec_item_offences(source, CLEANUP_DELETE_GUARD, CLEANUP_OWNERS)
    found += pinned(source, "deleteExactItem", CLEANUP_DELETE_BODY)
    found += pinned(
        source,
        "deleteAllMatches",
        CLEANUP_DELETE_ALL_BODY,
        params_norm=norm("service: String, account: String, delete: Deleter"),
    )
    # The service the guard checks is the service the delete query uses.
    found += query_binding_offences(
        source,
        "exactQuery",
        "service: String, account: String",
        CLEANUP_QUERY_BODY,
        users=CLEANUP_OWNERS,
        user_call="exactQuery(service: service, account: account)",
    )

    # Both entry points run the real-process refusal first and unconditionally. The injected pair may
    # only add a refusal.
    found += pinned(source, "runOnce", CLEANUP_PUBLIC_RUN_BODY, params_lack="protectedService")
    found += pinned(source, "runOnce", CLEANUP_EXPLICIT_RUN_BODY, params_have="protectedService")
    found += pinned(source, "refusedOutcome", CLEANUP_REFUSED_BODY)

    # A refused delete is terminal inside the work: it returns the refusal before any defaults write.
    work = by_name(source.code_funcs, "performRun")
    if len(work) != 1:
        found.append(f"{name}: performRun() must exist exactly once")
    else:
        body = norm(work[0].body(source.code))
        terminal = norm(CLEANUP_TERMINAL_REFUSAL)
        at = body.find(terminal)
        writes = [i for i in (body.find("defaults.removeObject("), body.find("defaults.set(")) if i >= 0]
        if at < 0:
            found.append(f"{name}: performRun() does not treat a refused delete as terminal")
        elif not writes or at > min(writes):
            found.append(f"{name}: performRun() writes defaults before it returns on a refused delete")
        elif body.count(terminal) != 1:
            found.append(f"{name}: performRun() has the terminal refusal more than once")

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
GUARD_DETECT_BODY = """
if xctestCaseClassLoaded { return true }
for key in environmentKeys {
    if environment[key] != nil { return true }
}
return false
"""
GUARD_DETECT_SIGNATURE = (
    "static func isTestHost("
    "environment: [String: String] = Foundation.ProcessInfo.processInfo.environment,"
    'xctestCaseClassLoaded: Bool = Foundation.NSClassFromString("XCTestCase") != nil'
    ") -> Bool {"
)
UI_TEST_HOST_KEY = "GROWIN_UI_TEST_HOST"
SKIP_KEY = "GROWIN_SKIP_LAUNCH_MIGRATIONS"
GUARD_KEYS = (
    "static let environmentKeys = ["
    '"XCTestConfigurationFilePath","XCTestBundlePath","XCTestSessionIdentifier",'
    f'"{UI_TEST_HOST_KEY}",'
    "]"
)
# The refusal log line (os_log): the stable code and the kind of call, nothing about the item.
GUARD_SITE_ENUM = "nonisolated enum RefusalSite: String { case read, write, delete, cleanup }"
GUARD_LOGGER = 'private static let logger = Logger(subsystem: "san.Growin", category: "production-keychain-guard")'
GUARD_RECORD_BODY = 'logger.error("\\(refusalCode, privacy: .public) site=\\(site.rawValue, privacy: .public)")'
LOG_API = re.compile(r"(?<![A-Za-z0-9_])(?:os_log|NSLog|print|debugPrint|dump|OSLog|NSLogv)(?![A-Za-z0-9_])")


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
        found.append(f"{name}: environmentKeys is not exactly the four test-host signals")

    # The refusal log: one pinned line, the code and the call-site kind only, and no other logging.
    found += pinned(source, "recordRefusal", GUARD_RECORD_BODY, params_norm=norm("site: RefusalSite"))
    code = norm(source.code)
    if norm(GUARD_SITE_ENUM) not in code:
        found.append(f"{name}: RefusalSite must be exactly read, write, delete, cleanup")
    if norm(GUARD_LOGGER) not in code:
        found.append(f"{name}: the logger is not the pinned subsystem and category")
    if len(re.findall(r"(?<![A-Za-z0-9_])Logger\s*\(", source.bare)) != 1:
        found.append(f"{name}: exactly one Logger may be created")
    if len(re.findall(r"(?<![A-Za-z0-9_])logger\s*\.", source.bare)) != 1:
        found.append(f"{name}: the logger may be used exactly once, in recordRefusal")
    for hit in LOG_API.finditer(source.bare):
        found.append(f"{name}: other logging or printing ({hit.group(0)})")
    if not re.search(r"(?m)^import os$", source.code):
        found.append(f"{name}: import os is missing")
    return found


# ProcessInfo, environment (parameter name, twice in the pinned signature and body) and NSClassFromString.
ENVIRONMENT_READS_IN_GUARD_FILE = 5


def ui_helper_key_offences(helper_text: str, guard_text: str) -> list[str]:
    """The UI-test app process carries the key the guard reads, and the suppression key is not a signal."""
    helper = strip_swift_comments_and_strings(helper_text, keep_strings=True)
    keys = re.findall(r'launchEnvironment\s*\[\s*"([A-Z0-9_]+)"\s*\]\s*=\s*"1"', helper)
    found = []
    if keys != [SKIP_KEY, UI_TEST_HOST_KEY]:
        found.append(f"LaunchSupport.swift: makeApp sets {keys}, expected [{SKIP_KEY}, {UI_TEST_HOST_KEY}] each to \"1\"")
    guard = norm(strip_swift_comments_and_strings(guard_text, keep_strings=True))
    if f'"{UI_TEST_HOST_KEY}"' not in guard:
        found.append(f"ProductionKeychainGuard.swift: {UI_TEST_HOST_KEY} is not a test-host signal")
    listed = re.search(r"staticletenvironmentKeys=\[(.*?)\]", guard)
    if listed is None or f'"{SKIP_KEY}"' in listed.group(1):
        found.append(f"ProductionKeychainGuard.swift: the suppression key must not be a test-host signal")
    return found


# --------------------------------------------------------------------------------------------
# 3. Nothing else in the app reaches the Keychain or the guard's inputs, and nothing shadows the guard
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


# A nested type, typealias, extension or member with the guard's name resolves ahead of the real one
# inside the type that declares it, so `extension KeychainStore { enum TestHostDetection { ... } }` would
# make the store call a fake. Scanned across Growin/ and GrowinTests/, comments and strings removed.
SHADOWED = r"(?:TestHostDetection|ProductionKeychainGuard)"
TYPE_DECLARATION = re.compile(
    r"(?<![A-Za-z0-9_])(?:class|struct|enum|actor|protocol|typealias|associatedtype|extension)\s+`?"
    + SHADOWED
    + r"`?(?![A-Za-z0-9_])"
)
MEMBER_DECLARATION = re.compile(
    r"(?<![A-Za-z0-9_])(?:let|var|func|case)\s+`?" + SHADOWED + r"`?(?![A-Za-z0-9_])"
)
GENERIC_SHADOW = re.compile(r"<[^<>]*(?<![A-Za-z0-9_.])" + SHADOWED + r"(?![A-Za-z0-9_])[^<>]*>")
LABEL_SHADOW = re.compile(r"(?<![A-Za-z0-9_.])" + SHADOWED + r"\s*:(?!:)")
GUARD_REL = GUARD.relative_to(REPO)
STORE_REL = KEYCHAIN_STORE.relative_to(REPO)
# name -> (the one file that declares it, how many declarations it has there)
FUNCTION_HOMES = {
    "isTestHost": (GUARD_REL, 1),
    "refusesCurrentProcess": (GUARD_REL, 1),
    "refuses": (GUARD_REL, 2),
    "recordRefusal": (GUARD_REL, 1),
    "checkAccess": (STORE_REL, 1),
    "assertAccessAllowed": (STORE_REL, 1),
}


# One layer down: the guard's own calls (`NSClassFromString`, `ProcessInfo`, `environment`, `==`, `&&`, `!`,
# `contains`) must not resolve to something the app module declares. The real tree extends none of the
# standard types and overloads no operator on them, so the rules are strict, not allow-listed.
STANDARD_TYPES = (
    r"(?:Array|ContiguousArray|ArraySlice|Sequence|Collection|MutableCollection|BidirectionalCollection"
    r"|RandomAccessCollection|RangeReplaceableCollection|LazySequenceProtocol|IteratorProtocol|Dictionary|Set"
    r"|ProcessInfo|NSDictionary|NSArray|NSString|NSObject|String|Substring|Character|Optional|Bool|Data|Int|UInt8"
    r"|Equatable|Hashable|Comparable|Never|CFDictionary)"
)
STANDARD_EXTENSION = re.compile(
    r"(?<![A-Za-z0-9_])extension\s+(?:(?:Swift|Foundation)\s*\.\s*)?`?" + STANDARD_TYPES + r"`?(?![A-Za-z0-9_])"
)
OPERATOR_DECLARATION = re.compile(
    r"(?P<static>(?<![A-Za-z0-9_])static\s+)?(?:(?:prefix|postfix|infix)\s+)?func\s+(?P<op>==|!=|&&|\|\||!|~=)\s*"
    r"\((?P<params>[^)]*)\)"
)
OPERATOR_STANDARD_PARAMETER = re.compile(
    r"(?<![A-Za-z0-9_])(?:Bool|String|Optional|Any|AnyClass|AnyObject|Self|T|Element|Key|Value|Substring"
    r"|_OptionalNilComparisonType)(?![A-Za-z0-9_])|\?|<"
)
STAND_IN_DECLARATION = re.compile(
    r"(?<![A-Za-z0-9_])(?:func|var|let|class|struct|enum|actor|protocol|typealias|case|associatedtype)\s+`?"
    r"(?:NSClassFromString|ProcessInfo|processInfo|environment|Foundation|Swift|Darwin|Security|os|Dispatch)`?"
    r"(?![A-Za-z0-9_])"
)


# A declaration named like a Security symbol, at any scope, can replace the symbol a query is built from
# (`let kSecAttrService: CFString = kSecUseOperationPrompt` makes every query service-less). The pinned
# owners also write these symbols `Security.`-qualified, which no app declaration can shadow.
SECURITY_NAME = r"(?:kSec\w*|errSec\w*|SecItem\w*|SecKeychain\w*)"
SECURITY_KEYWORD_DECLARATION = re.compile(
    r"(?<![A-Za-z0-9_.])(?:let|var|func|static|class|struct|enum|actor|protocol|typealias|associatedtype)\s+`?"
    + SECURITY_NAME
    + r"`?(?![A-Za-z0-9_])"
)
SECURITY_CASE_LIST = re.compile(r"(?<![A-Za-z0-9_.])case\s+(?P<names>[^\n:{]*)")
SECURITY_PARAMETER = re.compile(
    r"(?<=[(,])\s*(?:(?:_|[A-Za-z]\w*)\s+)?" + SECURITY_NAME + r"\s*(?:[A-Za-z_]\w*\s*)?:(?!:)"
)
SECURITY_LOOP_OR_TUPLE = re.compile(
    r"(?<![A-Za-z0-9_.])for\s+`?" + SECURITY_NAME + r"`?(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_.])(?:let|var)\s*\([^)]*(?<![A-Za-z0-9_.])" + SECURITY_NAME + r"(?![A-Za-z0-9_])[^)]*\)"
)
# A lookup by built string reaches SecItem without ever spelling it, so the scans cannot see it.
# Module-qualified forms count too (`Darwin.dlsym`, `Foundation.NSSelectorFromString`), so the lookbehind
# only excludes identifier characters. `@_silgen_name` binds a Swift declaration to a C symbol by name.
DYNAMIC_LOOKUP = re.compile(
    r"(?<![A-Za-z0-9_])(?:dlsym|dlopen|NSSelectorFromString|CFBundleGetFunctionPointerForName)(?![A-Za-z0-9_])"
    r"|@_silgen_name(?![A-Za-z0-9_])"
)
TYPEALIAS = re.compile(r"(?<![A-Za-z0-9_])typealias\s+(?P<name>[A-Za-z_]\w*)(?:\s*<[^>]*>)?\s*=\s*(?P<rhs>[^\n;{]+)")
STANDARD_RIGHT_HAND_SIDE = re.compile(r"(?:(?:Swift|Foundation)\s*\.\s*)?" + STANDARD_TYPES + r"(?![A-Za-z0-9_])|\[")
SUGARED_EXTENSION = re.compile(r"(?<![A-Za-z0-9_])extension\s+(?:\[|[A-Za-z_][\w.]*\?)")
RAW_KEYCHAIN_REL = RAW_KEYCHAIN.relative_to(REPO)


def declaration_scope(bare: str, position: int) -> str:
    """Where a declaration sits: `top` (module level), `extension`, `type` or `code` (a function or closure body).

    A member named `environment` inside a model struct only shadows inside that struct, so it is harmless.
    A declaration at module level or in an extension is visible to the type it extends, or to the whole
    module, so it can stand in for the call the guard makes.
    """
    headers: list[str] = []
    boundary = 0
    for index in range(position):
        char = bare[index]
        if char == "{":
            headers.append(bare[boundary:index])
            boundary = index + 1
        elif char == "}":
            if headers:
                headers.pop()
            boundary = index + 1
    if not headers:
        return "top"
    header = headers[-1]
    if re.search(r"(?<![A-Za-z0-9_])extension\b[^{};]*\Z", header):
        return "extension"
    if re.search(r"(?<![A-Za-z0-9_])(?:struct|class|enum|actor|protocol)\b[^{};]*\Z", header):
        return "type"
    return "code"


# The tripwire's assertions are Swift Testing's `#expect`. A function, macro or type called `expect`,
# `require`, `Issue` or `withKnownIssue` declared anywhere in GrowinTests (module level or local) could
# stand in for it and turn an assertion into a no-op, so none may be declared.
TESTING_VOCABULARY_DECLARATION = re.compile(
    r"(?<![A-Za-z0-9_.])(?:func|macro|struct|class|enum|actor|protocol|typealias|associatedtype|let|var|case)"
    r"\s+`?(?:expect|require|Issue|withKnownIssue)`?(?![A-Za-z0-9_])"
)


def swift_files() -> dict[Path, str]:
    files = {}
    for root in (APP, APP_TESTS):
        for path in sorted(root.rglob("*.swift")):
            files[path.relative_to(REPO)] = path.read_text(encoding="utf-8")
    return files


def shadow_offences(files: dict[Path, str]) -> list[str]:
    found = []
    # Typealiases in Growin/ whose right-hand side is a standard type or sugar for one: extending the alias
    # extends the type.
    aliases: set[str] = set()
    for relative, text in files.items():
        if relative.parts[0] != "Growin":
            continue
        for hit in TYPEALIAS.finditer(reduce_swift(text).bare):
            if STANDARD_RIGHT_HAND_SIDE.match(hit.group("rhs").strip()):
                aliases.add(hit.group("name"))
    for relative, text in sorted(files.items()):
        bare = reduce_swift(text).bare
        if relative.parts[0] in ("Growin", "GrowinTests"):
            for hit in DYNAMIC_LOOKUP.finditer(bare):
                found.append(f"{relative}: {hit.group(0)} can reach SecItem through a built string")
            for pattern, label in (
                (SECURITY_KEYWORD_DECLARATION, "declares"),
                (SECURITY_PARAMETER, "takes a parameter or label named"),
                (SECURITY_LOOP_OR_TUPLE, "binds"),
            ):
                for hit in pattern.finditer(bare):
                    found.append(f"{relative}: {label} a name that can replace a Security symbol ({squash(hit.group(0))[:60]})")
            for hit in SECURITY_CASE_LIST.finditer(bare):
                if declaration_scope(bare, hit.start()) != "type":
                    continue  # a switch pattern such as `case errSecSuccess:` is a use, not a declaration
                for part in hit.group("names").split(","):
                    word = re.match(r"\s*`?([A-Za-z_]\w*)", part)
                    if word and re.fullmatch(SECURITY_NAME, word.group(1)):
                        found.append(f"{relative}: an enum case named like a Security symbol ({word.group(1)})")
        if relative.parts[0] == "GrowinTests" and relative != RAW_KEYCHAIN_REL:
            for pattern in (SEC_ITEM_ANY, SEC_KEYCHAIN_ANY):
                for hit in pattern.finditer(bare):
                    found.append(f"{relative}: mentions {hit.group(0)}; only RawKeychain.swift may call SecItem or SecKeychain in tests")
        types = [squash(m.group(0)) for m in TYPE_DECLARATION.finditer(bare)]
        expected = ["enum TestHostDetection", "enum ProductionKeychainGuard"] if relative == GUARD_REL else []
        if sorted(types) != sorted(expected):
            found.append(f"{relative}: declares {types or 'nothing'} named like the guard types, expected {expected}")
        for pattern, label in (
            (MEMBER_DECLARATION, "a member"),
            (GENERIC_SHADOW, "a generic parameter"),
            (LABEL_SHADOW, "a parameter or binding"),
        ):
            for hit in pattern.finditer(bare):
                found.append(f"{relative}: {label} named like a guard type ({squash(hit.group(0))})")
        if relative.parts[0] == "GrowinTests":
            for hit in TESTING_VOCABULARY_DECLARATION.finditer(bare):
                found.append(f"{relative}: declares a name that can stand in for Swift Testing ({squash(hit.group(0))})")
        if relative.parts[0] == "Growin":
            for hit in SUGARED_EXTENSION.finditer(bare):
                found.append(f"{relative}: extends sugared array, dictionary or optional syntax ({squash(hit.group(0))})")
            for alias in sorted(aliases):
                for hit in re.finditer(r"(?<![A-Za-z0-9_])extension\s+`?" + re.escape(alias) + r"`?(?![A-Za-z0-9_])", bare):
                    found.append(f"{relative}: extends {alias}, an alias of a standard type")
            for hit in STANDARD_EXTENSION.finditer(bare):
                found.append(f"{relative}: extends a standard type, which can replace a call the guard makes ({squash(hit.group(0))})")
            for hit in OPERATOR_DECLARATION.finditer(bare):
                if not hit.group("static") or OPERATOR_STANDARD_PARAMETER.search(hit.group("params")):
                    found.append(f"{relative}: an operator {hit.group('op')} that is global or takes a standard type ({squash(hit.group(0))[:70]})")
            for hit in STAND_IN_DECLARATION.finditer(bare):
                if declaration_scope(bare, hit.start()) in ("top", "extension"):
                    found.append(
                        f"{relative}: declares a module-level or extension name that can stand in for a call the guard makes "
                        f"({squash(hit.group(0))})"
                    )
        for fname, (home, count) in FUNCTION_HOMES.items():
            declared = re.findall(
                r"(?<![A-Za-z0-9_])(?:func|var|let|case)\s+`?" + re.escape(fname) + r"`?(?![A-Za-z0-9_])", bare
            )
            wanted = count if relative == home else 0
            if len(declared) != wanted:
                found.append(f"{relative}: declares {fname} {len(declared)}x, expected {wanted}")
    return found


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
RAW_USERS = frozenset({"set", "remove", "data"})


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
    # Only set, remove and data may call SecItem, each pinned whole, and the query they use carries the
    # checked service.
    found += sec_item_offences(source, None, RAW_OWNERS)
    found += pinned(source, "set", RAW_SET_BODY, params_norm=norm("_ data: Data, account: String"))
    found += pinned(source, "remove", RAW_REMOVE_BODY, params_norm=norm("account: String"))
    found += pinned(source, "data", RAW_DATA_BODY, params_norm=norm("account: String"))
    found += query_binding_offences(
        source,
        "query",
        "_ account: String",
        QUERY_BODY,
        users=RAW_USERS,
        user_call="query(account)",
    )
    if len(re.findall(r"self\s*\.\s*service\s*=", source.bare)) != 1:
        found.append(f"{name}: only the init may store the service")
    return found


# --------------------------------------------------------------------------------------------
# 5. The tripwire: the REAL production wiring in the real test host, refusals only
# --------------------------------------------------------------------------------------------

TRIPWIRE_FILE = APP_TESTS / TRIPWIRE_PATH
TOUCH_SHARED = re.compile(
    r"(?<![A-Za-z0-9_])(?P<type>KeychainStore|LocalApprovalSigner|ApprovalSignerRouter)\s*\.\s*shared(?![A-Za-z0-9_])"
)
KEYCHAIN_STORE_WORD = re.compile(r"(?<![A-Za-z0-9_])KeychainStore(?![A-Za-z0-9_])")
CLEANUP_USE = re.compile(r"(?<![A-Za-z0-9_])LegacyT212KeychainCleanup\s*\.\s*(?P<member>\w+)")
GUARD_USE = re.compile(r"(?<![A-Za-z0-9_])ProductionKeychainGuard\s*\.\s*(?P<member>\w+)")
ANY_SHARED = re.compile(r"\.\s*shared(?![A-Za-z0-9_])")
SHARED_MEMBER_ALLOWED = {
    "KeychainStore": r"\s*\.\s*string\s*\(\s*for\s*:",
    "LocalApprovalSigner": r"\s*\.\s*isConfigured\s*\(\s*for\s*:",
    "ApprovalSignerRouter": r"\s*\.\s*isConfigured\s*\(\s*for\s*:",
}
CLEANUP_MEMBERS = frozenset({"deleteExactItem", "refusedUnderTestsStatus"})
GUARD_MEMBERS = frozenset({"refusesCurrentProcess"})
REFUSAL_ENDINGS = (
    r"==\s*Self\s*\.\s*refused\Z",
    r"==\s*false\Z",
    r"==\s*LegacyT212KeychainCleanup\s*\.\s*refusedUnderTestsStatus\Z",
)
TRIPWIRE_BANNED_CALLS = re.compile(
    r"\.\s*(?:set|remove|removeLegacyFlatItem|legacyFlatData|data|createIdentityIfNeeded|adoptLegacyKey|identity"
    r"|prepare|sign\w*|migrate\w*)\s*\("
    r"|(?<![A-Za-z0-9_])(?:rawSet|rawRemove|rawData|migrate\w*|runOnce|performRun|SecItem\w*|RawKeychain"
    r"|UserDefaults|signAuthorizedPractice)(?![A-Za-z0-9_])"
)
TRIPWIRE_OUTPUT = re.compile(
    r"(?<![A-Za-z0-9_])(?:print|debugPrint|dump|NSLog|os_log|Logger|XCTAttachment|FileHandle|Issue)(?![A-Za-z0-9_])"
    r"|\.\s*record\s*\(|attachment|(?<![A-Za-z0-9_])withKnownIssue|\.\s*disabled|\.\s*enabled|XCTSkip|try\s*[?!]",
    re.IGNORECASE,
)
TRIPWIRE_REFUSED_DECL = 'private static let refused = "PRODUCTION_KEYCHAIN_REFUSED_UNDER_TESTS"'
TRIPWIRE_NAME_DECL = 'private static let protectedName = "san.Growin.credentials." + "v1"'
TRIPWIRE_CODE_BODY = """
do {
    try body()
    return "NO_REFUSAL"
} catch let error as KeychainStoreError {
    return error.code
} catch {
    return "UNEXPECTED_ERROR"
}
"""
TRIPWIRE_DELETE_ARGUMENTS = "service: Self.protectedName, account: UUID().uuidString"
# The tripwire's exact structure: one unconditional suite, four plain @Test funcs, each holding its checks.
TRIPWIRE_HEADER = "@MainActor @Suite(.serialized) struct ProductionTripwireTests {"
TRIPWIRE_TESTS = (
    "thisProcessIsATestHost",
    "theSharedStoreRefusesARead",
    "theSignersReportNotConfigured",
    "theCleanupRefusesADeleteOnTheProductionService",
)
# Each test, from its attribute to its closing brace, exactly as it appears in the file (indentation and
# line breaks included, no wildcards). The scan compares the text of the block, found structurally, to this.
TRIPWIRE_TEST_BLOCKS = {
    "thisProcessIsATestHost": (
        "@Test func thisProcessIsATestHost() {\n"
        "        #expect(TestHostDetection.isTestHost())\n"
        "        #expect(ProductionKeychainGuard.refusesCurrentProcess(service: Self.protectedName))\n"
        "    }"
    ),
    "theSharedStoreRefusesARead": (
        "@Test func theSharedStoreRefusesARead() {\n"
        "        let refusal = Self.code(of: { _ = try KeychainStore.shared.string(for: .alpacaApiKey, scope: .workspace(.uk)) })\n"
        "        #expect(refusal == Self.refused)\n"
        "    }"
    ),
    "theSignersReportNotConfigured": (
        "@Test func theSignersReportNotConfigured() {\n"
        "        #expect(LocalApprovalSigner.shared.isConfigured(for: .uk) == false)\n"
        "        #expect(LocalApprovalSigner.shared.isConfigured(for: .india) == false)\n"
        "        #expect(ApprovalSignerRouter.shared.isConfigured(for: .uk) == false)\n"
        "        #expect(ApprovalSignerRouter.shared.isConfigured(for: .india) == false)\n"
        "    }"
    ),
    "theCleanupRefusesADeleteOnTheProductionService": (
        "@Test func theCleanupRefusesADeleteOnTheProductionService() {\n"
        "        #expect(\n"
        "            LegacyT212KeychainCleanup.deleteExactItem(service: Self.protectedName, account: UUID().uuidString)\n"
        "                == LegacyT212KeychainCleanup.refusedUnderTestsStatus)\n"
        "    }"
    ),
}
TRIPWIRE_OWNERS = {
    "KeychainStore": "theSharedStoreRefusesARead",
    "LocalApprovalSigner": "theSignersReportNotConfigured",
    "ApprovalSignerRouter": "theSignersReportNotConfigured",
    "delete": "theCleanupRefusesADeleteOnTheProductionService",
    "guard": "thisProcessIsATestHost",
}
TRIPWIRE_WRAPPER = re.compile(r"(?<![A-Za-z0-9_])(?:KeychainStorage|wrappedValue|projectedValue)(?![A-Za-z0-9_])")
TRIPWIRE_ASSIGNMENT = re.compile(r"\.\s*[A-Za-z_]\w*\s*=(?!=)|\]\s*=(?!=)")
TRIPWIRE_GUARD_EXPECT = "ProductionKeychainGuard.refusesCurrentProcess(service: Self.protectedName)"


def tripwire_offences(text: str, *, pin_blocks: bool = True) -> list[str]:
    source = read("ProductionTripwireTests.swift", text)
    name = source.name
    bare, code = source.bare, source.code
    found = list(source.errors)

    spans = []
    for hit in re.finditer(r"#expect\s*\(", bare):
        close = _match(bare, hit.end() - 1, "(", ")")
        if close is not None:
            spans.append((hit.end() - 1, close))

    def expectation(position: int) -> str | None:
        inner = [span for span in spans if span[0] < position < span[1]]
        if not inner:
            return None
        start, end = max(inner, key=lambda span: span[0])
        return bare[start + 1 : end].strip()

    funcs = source.bare_funcs

    def check_in_assertion(position: int, what: str, required_owner: str) -> None:
        owner = enclosing(funcs, position)
        if owner is None or owner.name != required_owner:
            found.append(
                f"{name}: {what} must sit in the @Test func {required_owner}, not in "
                f"{owner.name + '()' if owner else 'no function'}"
            )
        body = expectation(position)
        if body is None:
            found.append(f"{name}: {what} is outside an #expect, so nothing asserts the refusal")
            return
        plain_guard = norm(body) == norm(TRIPWIRE_GUARD_EXPECT)
        refusal = any(re.search(ending, body) for ending in REFUSAL_ENDINGS)
        if not (plain_guard or (refusal and body.count("==") == 1 and not re.search(r"&&|\|\||!|\?", body))):
            found.append(f"{name}: {what} is not inside an assertion that expects a refusal ({squash(body)[:80]})")

    # The shared singletons: only the allowed read or presence call, only inside a refusal assertion.
    touched = {"KeychainStore": 0, "LocalApprovalSigner": 0, "ApprovalSignerRouter": 0, "delete": 0}
    for hit in TOUCH_SHARED.finditer(bare):
        kind = hit.group("type")
        touched[kind] += 1
        if not re.match(SHARED_MEMBER_ALLOWED[kind], bare[hit.end() :]):
            found.append(f"{name}: {kind}.shared is used for something other than the allowed call")
        if kind == "KeychainStore" and not re.search(r"_\s*=\s*try\s+$", bare[: hit.start()]):
            found.append(f"{name}: the shared store read must be discarded with `_ = try`, never bound")
        if kind == "KeychainStore":
            # The read is discarded inside Self.code(of:), and only its refusal code is asserted.
            owner = enclosing(funcs, hit.start())
            if owner is None or owner.name != TRIPWIRE_OWNERS[kind]:
                found.append(f"{name}: KeychainStore.shared must sit in the @Test func {TRIPWIRE_OWNERS[kind]}")
            if not re.search(r"let\s+refusal\s*=\s*Self\s*\.\s*code\s*\(\s*of\s*:\s*\{\s*_\s*=\s*try\s+$", bare[: hit.start()]):
                found.append(f"{name}: the shared read must be `let refusal = Self.code(of: {{ _ = try ... }})`")
        else:
            check_in_assertion(hit.start(), f"{kind}.shared", TRIPWIRE_OWNERS[kind])
    for hit in KEYCHAIN_STORE_WORD.finditer(bare):
        if not re.match(r"\s*\.\s*shared(?![A-Za-z0-9_])", bare[hit.end() :]):
            found.append(f"{name}: names the Keychain store other than the one allowed shared read")
    for hit in ANY_SHARED.finditer(bare):
        if not re.search(r"(?:KeychainStore|LocalApprovalSigner|ApprovalSignerRouter)\s*$", bare[: hit.start()]):
            found.append(f"{name}: a `.shared` that is not one of the three singletons")
    for hit in CLEANUP_USE.finditer(bare):
        member = hit.group("member")
        if member not in CLEANUP_MEMBERS:
            found.append(f"{name}: LegacyT212KeychainCleanup.{member} is not allowed")
            continue
        check_in_assertion(hit.start(), f"LegacyT212KeychainCleanup.{member}", TRIPWIRE_OWNERS["delete"])
        if member == "deleteExactItem":
            touched["delete"] += 1
            open_paren = bare.find("(", hit.end())
            close = _match(bare, open_paren, "(", ")") if open_paren >= 0 else None
            arguments = bare[open_paren + 1 : close] if close else ""
            if norm(arguments) != norm(TRIPWIRE_DELETE_ARGUMENTS):
                found.append(f"{name}: the delete must be (service: Self.protectedName, account: UUID().uuidString)")
    for hit in GUARD_USE.finditer(bare):
        if hit.group("member") not in GUARD_MEMBERS:
            found.append(f"{name}: ProductionKeychainGuard.{hit.group('member')} is not allowed")
        else:
            check_in_assertion(hit.start(), "ProductionKeychainGuard.refusesCurrentProcess", TRIPWIRE_OWNERS["guard"])
    for kind, count in touched.items():
        if count < 1:
            found.append(f"{name}: no {kind} check is left, so the tripwire no longer covers it")

    # The exact allowed shared-store expression is the one the #573 scans exempt, nothing else.
    shared_reads = len(re.findall(r"KeychainStore\s*\.\s*shared", code))
    if shared_reads != norm(code).count(norm(TRIPWIRE_SHARED_CALL)):
        found.append(f"{name}: a shared store use is not exactly {TRIPWIRE_SHARED_CALL}")

    # No write of any kind, no output of any kind.
    for hit in TRIPWIRE_BANNED_CALLS.finditer(bare):
        found.append(f"{name}: a call that can write, migrate, sign or read a value ({squash(hit.group(0))})")
    for hit in TRIPWIRE_OUTPUT.finditer(bare):
        found.append(f"{name}: could print, log, attach or hide a failure ({squash(hit.group(0))})")
    if re.search(r"\\\(", code):
        found.append(f"{name}: string interpolation could put a value in a message")

    # It cannot be compiled out, disabled, or moved out of a @Test.
    for hit in CONDITIONAL_COMPILATION.finditer(bare):
        found.append(f"{name}: conditional compilation ({squash(hit.group(0))}) can compile the tripwire out")
    if norm(TRIPWIRE_HEADER) not in norm(code):
        found.append(f"{name}: the suite is not exactly `{TRIPWIRE_HEADER}`")
    if len(re.findall(r"@Suite(?![A-Za-z0-9_])", bare)) != 1:
        found.append(f"{name}: there must be exactly one @Suite")
    declared = []
    for hit in re.finditer(r"@Test(?![A-Za-z0-9_])", bare):
        shape = re.match(r"\s+func\s+(\w+)\s*\(\s*\)\s*\{", bare[hit.end() :])
        if shape is None:
            found.append(f"{name}: an @Test with arguments or traits, or not directly on a parameterless func")
        else:
            declared.append(shape.group(1))
    if sorted(declared) != sorted(TRIPWIRE_TESTS):
        found.append(f"{name}: the @Test funcs are {sorted(declared)}, expected exactly {sorted(TRIPWIRE_TESTS)}")
    for func in funcs:
        if func.name not in TRIPWIRE_TESTS and func.name != "code":
            found.append(f"{name}: an extra function {func.name}() could hold the assertions out of a @Test")
        if func.name in TRIPWIRE_TESTS and "#expect(" not in func.body(bare):
            found.append(f"{name}: {func.name}() asserts nothing")
    for test in TRIPWIRE_TESTS:
        if len(by_name(funcs, test)) != 1:
            found.append(f"{name}: the @Test func {test}() must exist exactly once")

    # Every test is pinned WHOLE and exactly: attribute, signature, each statement in order, closing brace.
    # An #expect that is merely present can be skipped (return, an uncalled closure, `if false`, `guard false`),
    # weakened (`try?`, a no-op `expect`, inverted arguments) or aimed elsewhere (a changed account).
    blocks: dict[str, list[str]] = {}
    for hit in re.finditer(r"@Test(?![A-Za-z0-9_])", bare):
        shape = re.match(r"\s+func\s+(\w+)\s*\(\s*\)\s*\{", bare[hit.end() :])
        if shape is None:
            continue
        brace = hit.end() + shape.end() - 1
        close = _match(bare, brace, "{", "}")
        if close is not None:
            blocks.setdefault(shape.group(1), []).append(bare[hit.start() : close + 1])
    for test, expected in TRIPWIRE_TEST_BLOCKS.items() if pin_blocks else ():
        got = blocks.get(test, [])
        if got == [expected]:
            continue
        if len(got) != 1:
            found.append(f"{name}: {test}() is declared {len(got)}x as an @Test, expected exactly once")
            continue
        want_lines, got_lines = expected.split("\n"), got[0].split("\n")
        at = next((i for i, (a, b) in enumerate(zip(want_lines, got_lines)) if a != b), min(len(want_lines), len(got_lines)))
        shown = got_lines[at].strip() if at < len(got_lines) else "(the block ends early)"
        found.append(f"{name}: {test}() is not byte for byte the pinned test (line {at + 1}: {shown[:80]})")

    found += secret_expect_offences(name, text)

    # It cannot write through the KeychainStorage property wrapper, or assign to any member.
    for hit in TRIPWIRE_WRAPPER.finditer(bare):
        found.append(f"{name}: the KeychainStorage property wrapper can write ({hit.group(0)})")
    for hit in TRIPWIRE_ASSIGNMENT.finditer(bare):
        found.append(f"{name}: an assignment to a member or subscript ({squash(hit.group(0))})")

    # The helpers the assertions lean on are pinned too.
    found += pinned(source, "code", TRIPWIRE_CODE_BODY, params_norm=norm("of body: () throws -> Void"))
    if norm(TRIPWIRE_REFUSED_DECL) not in norm(code):
        found.append(f"{name}: the expected refusal code is not the pinned one")
    if norm(TRIPWIRE_NAME_DECL) not in norm(code):
        found.append(f"{name}: the protected service name is not the pinned two-piece literal")
    return found


SECRET_READ_IN_EXPECT = re.compile(
    r"\.\s*(?:string|data)\s*\(|(?<![A-Za-z0-9_])(?:rawData|legacyFlatData)(?![A-Za-z0-9_])"
)


def secret_expect_offences(label: str, text: str) -> list[str]:
    """No `#expect` or `#require` argument may read a credential: a failure message shows its operands.

    A String or Data read from the Keychain inside the expression would be printed on a failing run, and
    under a stripped or swapped service on the operator's Mac that could be a real credential. Compute a
    Bool (or a hash) first and assert only that.
    """
    bare = reduce_swift(text).bare
    found = []
    for hit in re.finditer(r"#(?:expect|require)\s*\(", bare):
        close = _match(bare, hit.end() - 1, "(", ")")
        if close is None:
            found.append(f"{label}: an unterminated assertion")
            continue
        argument = bare[hit.end() : close]
        for read in SECRET_READ_IN_EXPECT.finditer(argument):
            found.append(f"{label}: an assertion reads a credential ({squash(read.group(0))}) and would print it on failure")
    return found


def exact_block(source: Source, header: re.Pattern[str], expected: str, label: str) -> list[str]:
    """The one declaration matched by `header`, from its first character to its closing brace, equals `expected`.

    Located in the string-free text (so a decoy copy inside a multi-line string cannot be mistaken for the
    real one), then compared in the comment-free text with string contents kept, so a changed literal
    counts. No comments may sit inside a pinned block.
    """
    bare_hits = list(header.finditer(source.bare))
    code_hits = list(header.finditer(source.code))
    if len(bare_hits) != 1 or len(code_hits) != 1:
        return [f"{source.name}: {label} must be declared exactly once ({len(bare_hits)} real, {len(code_hits)} in the text)"]
    hit = code_hits[0]
    position = hit.end()
    if source.code[position : position + 1] == "(":
        close_paren = _match(source.code, position, "(", ")")
        if close_paren is None:
            return [f"{source.name}: {label} has unbalanced parameters"]
        position = close_paren + 1
    brace = source.code.find("{", position)
    close = _match(source.code, brace, "{", "}") if brace >= 0 else None
    if close is None:
        return [f"{source.name}: {label} has no readable body"]
    got = source.code[hit.start() : close + 1]
    if got == expected:
        return []
    want_lines, got_lines = expected.split("\n"), got.split("\n")
    at = next((i for i, (a, b) in enumerate(zip(want_lines, got_lines)) if a != b), min(len(want_lines), len(got_lines)))
    shown = got_lines[at].strip() if at < len(got_lines) else "(the block ends early)"
    return [f"{source.name}: {label} is not byte for byte the pinned text (line {at + 1}: {shown[:80]})"]


def at_test_header(name: str) -> re.Pattern[str]:
    return re.compile(r"@Test(?![A-Za-z0-9_])\s+func\s+" + re.escape(name) + r"(?![A-Za-z0-9_])")


CANARY_SCOPE_NEVER_SEES = (
    '@Test func aStoreNeverSeesOrChangesAnotherServicesItem() {\n'
    '        let (a, _) = Self.makeStore()\n'
    '        let (b, serviceB) = Self.makeStore()\n'
    '        defer {\n'
    '            Self.cleanUp(a)\n'
    '            Self.cleanUp(b)\n'
    '        }\n'
    '        Self.requireKeychain {\n'
    '            let planted = Data("CANARY-B".utf8)\n'
    '            try b.set(planted, for: .alpacaApiKey, scope: .workspace(.uk))\n'
    '            let rawB = RawKeychain(service: serviceB)\n'
    '            let plantedReadsBack = try rawB.data(account: Self.canaryScopedAccount) == planted\n'
    '            #expect(plantedReadsBack, "positive control")\n'
    '\n'
    '            let aDataIsNil = try a.data(for: .alpacaApiKey, scope: .workspace(.uk)) == nil\n'
    '            let aStringIsNil = try a.string(for: .alpacaApiKey, scope: .workspace(.uk)) == nil\n'
    '            #expect(aDataIsNil)\n'
    '            #expect(aStringIsNil)\n'
    '\n'
    '            try a.remove(.alpacaApiKey, scope: .workspace(.uk))\n'
    '            let afterRemove = try rawB.data(account: Self.canaryScopedAccount) == planted\n'
    '            #expect(afterRemove, "B survives a remove on A")\n'
    '            try a.set("", for: .alpacaApiKey, scope: .workspace(.uk))\n'
    '            let afterEmptySet = try rawB.data(account: Self.canaryScopedAccount) == planted\n'
    '            #expect(afterEmptySet, "B survives an empty set on A")\n'
    '            try a.set("CANARY-A", for: .alpacaApiKey, scope: .workspace(.uk))\n'
    '            let afterSet = try rawB.data(account: Self.canaryScopedAccount) == planted\n'
    '            #expect(afterSet, "B survives a set on A")\n'
    '            let aReadsItsOwn = try a.string(for: .alpacaApiKey, scope: .workspace(.uk)) == "CANARY-A"\n'
    '            #expect(aReadsItsOwn)\n'
    '            try a.remove(.alpacaApiKey, scope: .workspace(.uk))\n'
    '            let afterSecondRemove = try rawB.data(account: Self.canaryScopedAccount) == planted\n'
    '            #expect(afterSecondRemove, "B survives a second remove on A")\n'
    '            let bStillReadsItsOwn = try b.string(for: .alpacaApiKey, scope: .workspace(.uk)) == "CANARY-B"\n'
    '            #expect(bStillReadsItsOwn)\n'
    '        }\n'
    '    }'
)

CANARY_SCOPE_CLEANUP = (
    '@Test func aStoreCleanUpAndLegacyCallsLeaveAnotherServicesItemsAlone() {\n'
    '        let (a, _) = Self.makeStore()\n'
    '        let (b, serviceB) = Self.makeStore()\n'
    '        defer {\n'
    '            Self.cleanUp(a)\n'
    '            Self.cleanUp(b)\n'
    '        }\n'
    '        Self.requireKeychain {\n'
    '            let rawB = RawKeychain(service: serviceB)\n'
    '            let scoped = Data("CANARY-SCOPED-B".utf8)\n'
    '            let flat = Data("CANARY-FLAT-B".utf8)\n'
    '            let approval = Data("CANARY-APPROVAL-B".utf8)\n'
    '            try b.set(scoped, for: .alpacaApiKey, scope: .workspace(.uk))\n'
    '            try rawB.set(flat, account: Self.canaryFlatAccount)\n'
    '            try b.set(approval, for: .approvalSigningKey, scope: .workspace(.uk))\n'
    '\n'
    '            let aFlatIsNil = try a.legacyFlatData(for: .alpacaApiKey) == nil\n'
    '            #expect(aFlatIsNil)\n'
    '            try a.removeLegacyFlatItem(.alpacaApiKey)\n'
    '            Self.cleanUp(a)\n'
    '            let migrationsMoved = a.migrateFlatItemsToScoped()\n'
    '            #expect(migrationsMoved.isEmpty)\n'
    '\n'
    '            let scopedSurvives = try rawB.data(account: Self.canaryScopedAccount) == scoped\n'
    '            let flatSurvives = try rawB.data(account: Self.canaryFlatAccount) == flat\n'
    '            let approvalSurvives = try rawB.data(account: Self.canaryApprovalAccount) == approval\n'
    '            #expect(scopedSurvives, "B\'s scoped item survives A\'s cleanUp and legacy calls")\n'
    '            #expect(flatSurvives, "B\'s flat item survives A\'s cleanUp and legacy calls")\n'
    '            #expect(approvalSurvives, "B\'s approval item survives A\'s cleanUp and legacy calls")\n'
    '        }\n'
    '    }'
)

CANARY_SCOPE_RAW = (
    '@Test func rawKeychainOnOneServiceCannotSeeOrRemoveAnother() {\n'
    '        let (_, serviceA) = Self.makeStore()\n'
    '        let (_, serviceB) = Self.makeStore()\n'
    '        let rawA = RawKeychain(service: serviceA)\n'
    '        let rawB = RawKeychain(service: serviceB)\n'
    '        let account = "canary-raw"\n'
    '        defer {\n'
    '            rawA.remove(account: account)\n'
    '            rawB.remove(account: account)\n'
    '        }\n'
    '        Self.requireKeychain {\n'
    '            let bytesB = Data("CANARY-RAW-B".utf8)\n'
    '            try rawB.set(bytesB, account: account)\n'
    '            let aSeesNothing = try rawA.data(account: account) == nil\n'
    '            #expect(aSeesNothing, "A cannot see B\'s item")\n'
    '            rawA.remove(account: account)\n'
    '            let bSurvivesARemove = try rawB.data(account: account) == bytesB\n'
    '            #expect(bSurvivesARemove, "B survives a raw remove on A")\n'
    '\n'
    '            let bytesA = Data("CANARY-RAW-A".utf8)\n'
    '            try rawA.set(bytesA, account: account)\n'
    '            let aReadsItsOwn = try rawA.data(account: account) == bytesA\n'
    '            #expect(aReadsItsOwn)\n'
    '            rawB.remove(account: account)\n'
    '            let aSurvivesBRemove = try rawA.data(account: account) == bytesA\n'
    '            let bIsGone = try rawB.data(account: account) == nil\n'
    '            #expect(aSurvivesBRemove, "A survives a raw remove on B")\n'
    '            #expect(bIsGone)\n'
    '        }\n'
    '    }'
)

CANARY_SCOPE_REQUIRE = (
    'private static func requireKeychain(_ body: () throws -> Void, sourceLocation: SourceLocation = #_sourceLocation) {\n'
    '        do {\n'
    '            try body()\n'
    '        } catch KeychainStoreError.status(let status) {\n'
    '            Issue.record("Keychain blocked with OSStatus \\(status)", sourceLocation: sourceLocation)\n'
    '        } catch {\n'
    '            Issue.record("Unexpected error: \\(error)", sourceLocation: sourceLocation)\n'
    '        }\n'
    '    }'
)

CANARY_LEGACY_REAL_DELETE = (
    '@Test func theRealDeleteNeverTouchesAnotherServicesItem() throws {\n'
    '        let a = Rig()\n'
    '        let b = Rig()\n'
    '        defer {\n'
    '            a.tearDown()\n'
    '            b.tearDown()\n'
    '        }\n'
    '        try b.seed(Cleanup.legacyAccounts)\n'
    '\n'
    '        for account in Cleanup.legacyAccounts {\n'
    '            let notFound = Cleanup.deleteExactItem(service: a.service, account: account) == errSecItemNotFound\n'
    '            #expect(notFound, "A holds nothing, so the real delete finds nothing")\n'
    '        }\n'
    '        for account in Cleanup.legacyAccounts {\n'
    '            let untouched = try b.raw.data(account: account) == Self.canary\n'
    '            #expect(untouched, "B untouched after deletes aimed at A")\n'
    '        }\n'
    '\n'
    '        try a.seed(Cleanup.legacyAccounts)\n'
    '        for account in Cleanup.legacyAccounts {\n'
    '            let deleted = Cleanup.deleteExactItem(service: a.service, account: account) == errSecSuccess\n'
    '            let goneFromA = try a.raw.data(account: account) == nil\n'
    '            #expect(deleted, "the real delete removes A\'s own item")\n'
    '            #expect(goneFromA, "A\'s item is gone")\n'
    '        }\n'
    '        for account in Cleanup.legacyAccounts {\n'
    '            let untouched = try b.raw.data(account: account) == Self.canary\n'
    '            #expect(untouched, "B untouched after A\'s items were deleted")\n'
    '        }\n'
    '\n'
    '        try a.seed(Cleanup.legacyAccounts)\n'
    '        let outcome = Cleanup.runOnce(service: a.service, defaults: a.defaults, log: { _ in })\n'
    '        let removedAll = outcome.removed == Cleanup.legacyAccounts.count\n'
    '        #expect(removedAll)\n'
    '        for account in Cleanup.legacyAccounts {\n'
    '            let goneFromA = try a.raw.data(account: account) == nil\n'
    '            let untouched = try b.raw.data(account: account) == Self.canary\n'
    '            #expect(goneFromA, "A\'s item is gone after the run")\n'
    '            #expect(untouched, "B untouched after the run")\n'
    '        }\n'
    '    }'
)

TRIP_READ = (
    '@Test func theSharedStoreRefusesARead() {\n'
    '        let refusal = Self.code(of: { _ = try KeychainStore.shared.string(for: .alpacaApiKey, scope: .workspace(.uk)) })\n'
    '        #expect(refusal == Self.refused)\n'
    '    }'
)


CANARY_SCOPE = {
    "aStoreNeverSeesOrChangesAnotherServicesItem": CANARY_SCOPE_NEVER_SEES,
    "aStoreCleanUpAndLegacyCallsLeaveAnotherServicesItemsAlone": CANARY_SCOPE_CLEANUP,
    "rawKeychainOnOneServiceCannotSeeOrRemoveAnother": CANARY_SCOPE_RAW,
}
CANARY_LEGACY = {"theRealDeleteNeverTouchesAnotherServicesItem": CANARY_LEGACY_REAL_DELETE}
SCOPE_TESTS_PATH = APP_TESTS / "WorkspaceIsolation" / "KeychainScopeTests.swift"
LEGACY_TESTS_PATH = APP_TESTS / "WorkspaceIsolation" / "LegacyT212KeychainCleanupTests.swift"


SCOPE_SUITE_HEADER = "@Suite(.serialized)\nstruct KeychainScopeTests {"
LEGACY_SUITE_HEADER = "@Suite(.serialized)\nstruct LegacyT212KeychainCleanupTests {"


def suite_header_offences(source: Source, expected: str) -> list[str]:
    """The one @Suite attribute is exactly `expected`: `.serialized` and no other trait.

    A `.disabled()` or `.enabled(if: false)` on the suite skips every test in it, the canaries included,
    without touching a line of any test. A second @Suite attribute, or a suite with the attribute removed,
    is refused for the same reason.
    """
    if len(re.findall(r"@Suite(?![A-Za-z0-9_])", source.bare)) != 1:
        return [f"{source.name}: there must be exactly one @Suite attribute"]
    hit = re.search(r"@Suite(?![A-Za-z0-9_])", source.code)
    brace = source.code.find("{", hit.start()) if hit else -1
    got = source.code[hit.start() : brace + 1] if hit and brace >= 0 else ""
    if got != expected:
        return [f"{source.name}: the suite header is not exactly `{squash(expected)}` (found `{squash(got)[:80]}`)"]
    return []


def canary_scope_offences(text: str) -> list[str]:
    source = read("KeychainScopeTests.swift", text)
    found = list(source.errors)
    found += suite_header_offences(source, SCOPE_SUITE_HEADER)
    for name, expected in CANARY_SCOPE.items():
        found += exact_block(source, at_test_header(name), expected, name)
        found += secret_expect_offences(f"KeychainScopeTests.swift: {name}", expected)
    # The helper that runs every canary's body: if it stopped calling the body, each canary would pass untested.
    found += exact_block(
        source, re.compile(r"private\s+static\s+func\s+requireKeychain(?![A-Za-z0-9_])"), CANARY_SCOPE_REQUIRE, "requireKeychain"
    )
    return found


def canary_legacy_offences(text: str) -> list[str]:
    source = read("LegacyT212KeychainCleanupTests.swift", text)
    found = list(source.errors)
    found += suite_header_offences(source, LEGACY_SUITE_HEADER)
    for name, expected in CANARY_LEGACY.items():
        found += exact_block(source, at_test_header(name), expected, name)
        found += secret_expect_offences(f"LegacyT212KeychainCleanupTests.swift: {name}", expected)
    return found


def exemption_offences(exempt) -> list[str]:
    """`exempt(relative_path, text)` removes the one allowed call in the one exact path, nothing else."""
    found = []
    allowed = f"x {TRIPWIRE_SHARED_CALL} y"
    if exempt(TRIPWIRE_PATH, allowed) != "x refusedRead() y":
        found.append("the exact tripwire path is not exempted for the exact call")
    for other in (
        Path("Other.swift"),
        Path("WorkspaceIsolation") / "OtherTests.swift",
        Path("Other") / "ProductionTripwireTests.swift",
        Path("WorkspaceIsolation") / "ProductionTripwireTests2.swift",
    ):
        if exempt(other, allowed) != allowed:
            found.append(f"the exemption is widened to {other}")
    for more in (TRIPWIRE_SHARED_CALL.replace("string(", "set("), "KeychainStore.shared.string(for: .openaiApiKey, scope: .shared)"):
        if exempt(TRIPWIRE_PATH, more) != more:
            found.append(f"the exemption covers more than the one call ({more})")
    return found


MIRROR_PATH = APP_TESTS / "WorkspaceIsolation" / "LaunchMigrationsTests.swift"
MIRROR_EXEMPT_BODY = 'path == tripwirePath ? text.replacingOccurrences(of: tripwireSharedCall, with: "refusedRead()") : text'
MIRROR_PATH_DECL = 'private static let tripwirePath = "GrowinTests/WorkspaceIsolation/ProductionTripwireTests.swift"'
MIRROR_CALL_DECL = 'private static let tripwireSharedCall = "KeychainStore" + ".shared.string(for: .alpacaApiKey, scope: .workspace(.uk))"'


def mirror_exemption_offences(text: str) -> list[str]:
    """The Swift mirror of the exemption: the same exact path, the same exact call, an equality test."""
    source = read("LaunchMigrationsTests.swift", text)
    found = pinned(source, "tripwireExempted", MIRROR_EXEMPT_BODY)
    code = norm(source.code)
    if norm(MIRROR_PATH_DECL) not in code:
        found.append("LaunchMigrationsTests.swift: the mirror's tripwire path is not the exact one")
    if norm(MIRROR_CALL_DECL) not in code:
        found.append("LaunchMigrationsTests.swift: the mirror's exempt call is not the exact one")
    if str(Path("GrowinTests") / TRIPWIRE_PATH) not in MIRROR_PATH_DECL:
        found.append("the Python and Swift exemptions name different paths")
    return found


# --------------------------------------------------------------------------------------------
# Real source
# --------------------------------------------------------------------------------------------

STORE_TEXT = KEYCHAIN_STORE.read_text(encoding="utf-8")
CLEANUP_TEXT = LEGACY_CLEANUP.read_text(encoding="utf-8")
GUARD_TEXT = GUARD.read_text(encoding="utf-8")
RAW_TEXT = RAW_KEYCHAIN.read_text(encoding="utf-8")
TRIPWIRE_TEXT = TRIPWIRE_FILE.read_text(encoding="utf-8")
SCOPE_TESTS_TEXT = SCOPE_TESTS_PATH.read_text(encoding="utf-8")
LEGACY_TESTS_TEXT = LEGACY_TESTS_PATH.read_text(encoding="utf-8")
UI_HELPER_TEXT = (REPO / "GrowinUITests" / "LaunchSupport.swift").read_text(encoding="utf-8")
MIRROR_TEXT = MIRROR_PATH.read_text(encoding="utf-8")


def test_the_keychain_store_runs_the_guard_first_in_every_sec_item_function():
    assert keychain_store_offences(STORE_TEXT) == []


def test_the_legacy_cleanup_refuses_unconditionally_before_any_sec_item_call_or_defaults_write():
    assert legacy_cleanup_offences(CLEANUP_TEXT) == []


def test_the_guard_is_the_pinned_predicate_with_every_test_host_signal_and_one_log_line():
    assert guard_file_offences(GUARD_TEXT) == []


def test_raw_keychain_refuses_a_service_that_is_not_test_prefixed():
    assert raw_keychain_offences(RAW_TEXT) == []


def test_the_ui_test_app_process_carries_the_key_the_guard_reads():
    assert ui_helper_key_offences(UI_HELPER_TEXT, GUARD_TEXT) == []


def test_no_other_app_file_mentions_sec_item_or_passes_the_guards_inputs():
    files = app_files()
    assert KEYCHAIN_STORE.relative_to(APP) in files and GUARD.relative_to(APP) in files  # positive control
    assert app_wide_offences(files) == []


def test_nothing_in_the_app_or_the_tests_shadows_the_guard():
    files = swift_files()
    assert GUARD_REL in files and STORE_REL in files  # positive control
    assert shadow_offences(files) == []


def test_the_canaries_are_pinned_whole_and_read_no_credential_in_an_assertion():
    assert canary_scope_offences(SCOPE_TESTS_TEXT) == []
    assert canary_legacy_offences(LEGACY_TESTS_TEXT) == []


SKIPPING_VARIANTS = (
    "return as the first statement",
    "return after the first assertion",
    "assertions in a closure that is never called",
    "assertions inside if false",
    "assertions inside if 1 == 2",
    "assertions after guard false else { return }",
)


def test_the_whole_body_pin_is_what_catches_a_skipped_tripwire_assertion():
    """With the pin on each skipping variant fails; with it off every other tripwire rule passes it.

    That is the proof the pin is needed: a `return`, an uncalled closure, `if false` or `guard false` keeps
    every #expect in the text, in the right function, in the right shape, and only the pin sees it skipped.
    """
    checked = 0
    for name, block in TRIPWIRE_TEST_BLOCKS.items():
        variants = block_variants(block)
        for label in SKIPPING_VARIANTS:
            planted = replaced(TRIPWIRE_TEXT, block, variants[label])
            assert not isinstance(planted, BrokenPlant), planted.message if isinstance(planted, BrokenPlant) else ""
            assert tripwire_offences(planted), f"{name}: {label} slipped past the pin"
            assert tripwire_offences(planted, pin_blocks=False) == [], f"{name}: {label} is caught without the pin too"
            checked += 1
    assert checked == len(TRIPWIRE_TEST_BLOCKS) * len(SKIPPING_VARIANTS)


def test_the_tripwire_runs_the_real_wiring_and_only_expects_refusals():
    assert TRIPWIRE_FILE.is_file()
    assert tripwire_offences(TRIPWIRE_TEXT) == []


def test_the_tripwire_exemption_is_one_exact_path_and_one_exact_call_in_python_and_swift():
    assert exemption_offences(tripwire_exempted) == []
    assert mirror_exemption_offences(MIRROR_TEXT) == []


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
        TRIPWIRE_FILE,
    ]
    quoted_literal = '"' + "san.Growin.credentials." + 'v1"'
    for path in test_files:
        text = path.read_text(encoding="utf-8")
        assert "production" + "Service" not in text, path.name
        assert quoted_literal not in text, path.name
        # The tripwire is exempt from the shared-store rule for its one exact call, as the #573 scans do.
        bare = reduce_swift(tripwire_exempted(path.relative_to(APP_TESTS), text)).bare
        assert not re.search(r"KeychainStore\s*\.\s*(?:shared|self|init)\b", bare), path.name
        assert not re.search(r"(?<![A-Za-z0-9_])(?:Process|NSTask|NSWorkspace|NSClassFromString)(?![A-Za-z0-9_])", bare), path.name
        # LegacyT212KeychainCleanupTests.everyMigrationCallStatesItsService: every call names `service:` first.
        for call in re.finditer(r"runOnce\(", text):
            assert text[call.end() :].lstrip().startswith("service:"), path.name
        if path == TRIPWIRE_FILE:
            assert "runOnce" not in text
        assert "#" + "filePath" not in text, path.name
    for path in (GUARD, RAW_KEYCHAIN, *test_files[:3], TRIPWIRE_FILE):
        lowered = path.read_text(encoding="utf-8").lower()
        assert "trading212apikey" not in lowered and "trading212apisecret" not in lowered, path.name


# --------------------------------------------------------------------------------------------
# Break-proofs: planted copies of the real source
# --------------------------------------------------------------------------------------------


class BrokenPlant(str):
    """A plant whose anchor text is gone from the source.

    It is returned instead of raising, so editing a line a plant refers to fails ONE named test
    (`test_every_plant_applies_to_the_current_source`, and the plant's own test) with the anchor text,
    not the whole module at collection time. Chained `replaced` calls pass it through unchanged.
    """

    message = ""


def replaced(text: str, old: str, new: str, count: int = 1) -> str:
    if isinstance(text, BrokenPlant):
        return text
    if old not in text:
        broken = BrokenPlant(text)
        broken.message = f"the plant did not apply, its anchor text is gone from the source: {old!r}"
        return broken
    return text.replace(old, new, count)


RAW_DATA_HEAD = "private func rawData(account: String) throws -> Data? {\n"
RAW_SET_HEAD = "private func rawSet(_ data: Data, account: String) throws {\n"
RAW_REMOVE_HEAD = "private func rawRemove(account: String) throws {\n"
GUARD_LINE = {
    "rawData": "        try assertAccessAllowed(for: .read)\n",
    "rawSet": "        try assertAccessAllowed(for: .write)\n",
    "rawRemove": "        try assertAccessAllowed(for: .delete)\n",
}
STORE_ASSERT_TEXT = (
    "        try Self.checkAccess(\n"
    "            service: service,\n"
    "            protectedService: ProductionKeychainGuard.productionService,\n"
    "            testHost: TestHostDetection.isTestHost(),\n"
    "            site: site)\n"
)
STORE_CHECK_HEAD = (
    "        if ProductionKeychainGuard.refuses(service: service, protectedService: protectedService, testHost: testHost) {\n"
)
STORE_RECORD_LINE = "            ProductionKeychainGuard.recordRefusal(site: site)\n"
SERVICE_LINE = "            Security.kSecAttrService as String: service,\n"
CLEANUP_DELETE_TEXT = (
    "        guard !ProductionKeychainGuard.refusesCurrentProcess(service: service) else {\n"
    "            ProductionKeychainGuard.recordRefusal(site: .delete)\n"
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
CLEANUP_TERMINAL_TEXT = (
    "                // A refused delete is terminal, not an ordinary Keychain error: stop before any\n"
    "                // defaults write, failure count or completion mark, exactly like the front check.\n"
    "                if status == refusedUnderTestsStatus { return refusedOutcome(log: log) }\n"
)
CLEANUP_RECORD_LINE = "        ProductionKeychainGuard.recordRefusal(site: .cleanup)\n"
GUARD_RECORD_LINE = (
    '        logger.error("\\(refusalCode, privacy: .public) site=\\(site.rawValue, privacy: .public)")\n'
)
TRIPWIRE_READ = (
    "        let refusal = Self.code(of: { _ = try KeychainStore.shared.string(for: .alpacaApiKey, scope: .workspace(.uk)) })\n"
    "        #expect(refusal == Self.refused)\n"
)
TRIPWIRE_SIGNER = "        #expect(LocalApprovalSigner.shared.isConfigured(for: .uk) == false)\n"
TRIPWIRE_DELETE = (
    "        #expect(\n"
    "            LegacyT212KeychainCleanup.deleteExactItem(service: Self.protectedName, account: UUID().uuidString)\n"
    "                == LegacyT212KeychainCleanup.refusedUnderTestsStatus)\n"
)
UI_KEY_LINE = f'        app.launchEnvironment["{UI_TEST_HOST_KEY}"] = "1"\n'

STORE_DATA_MATCH_LINE = "        query[Security.kSecMatchLimit as String] = Security.kSecMatchLimitOne\n"
STORE_SET_QUERY_LINE = "        let query = baseQuery(account: account)\n"
STORE_SET_ITEM_LINE = "            item[Security.kSecAttrAccessible as String] = Security.kSecAttrAccessibleWhenUnlockedThisDeviceOnly\n"
STORE_REMOVE_LINE = "        let status = Security.SecItemDelete(baseQuery(account: account) as CFDictionary)\n"
CLEANUP_DELETE_QUERY_LINE = "        return Security.SecItemDelete(exactQuery(service: service, account: account) as CFDictionary)\n"
CLEANUP_LOOP_LINE = "            let status = delete(service, account)\n"
RAW_REMOVE_LINE = "        Security.SecItemDelete(query(account) as CFDictionary)\n"
RAW_SET_FIRST = "        var item = query(account)\n        item[Security.kSecValueData as String] = data\n"
RAW_DATA_FIRST = "        var item = query(account)\n        item[Security.kSecReturnData as String] = true\n"
GUARD_DETECT_TEXT = (
    "        if xctestCaseClassLoaded { return true }\n"
    "        for key in environmentKeys {\n"
    "            if environment[key] != nil { return true }\n"
    "        }\n"
    "        return false\n"
)
TRIPWIRE_SIGNER_BLOCK = (
    "    @Test func theSignersReportNotConfigured() {\n"
    "        #expect(LocalApprovalSigner.shared.isConfigured(for: .uk) == false)\n"
    "        #expect(LocalApprovalSigner.shared.isConfigured(for: .india) == false)\n"
    "        #expect(ApprovalSignerRouter.shared.isConfigured(for: .uk) == false)\n"
    "        #expect(ApprovalSignerRouter.shared.isConfigured(for: .india) == false)\n"
    "    }\n"
)
TRIPWIRE_HEADER_TEXT = "@MainActor\n@Suite(.serialized)\nstruct ProductionTripwireTests {\n"
TRIPWIRE_FIRST_TEST = "    @Test func thisProcessIsATestHost() {\n"

# label -> (scan kind, planted text). With the guard broken, the scan must report something.
PLANTS: dict[str, tuple[str, str]] = {
    # === round 3, item 1: the service stripped, swapped or merged over AFTER the query builder ===
    "store: rawData strips the service with removeValue(forKey: svce) (r3 item 1)": (
        "store",
        replaced(STORE_TEXT, STORE_DATA_MATCH_LINE, STORE_DATA_MATCH_LINE + '        query.removeValue(forKey: "svce")\n'),
    ),
    "store: rawData strips the service by key constant (r3 item 1)": (
        "store",
        replaced(
            STORE_TEXT,
            STORE_DATA_MATCH_LINE,
            STORE_DATA_MATCH_LINE + "        query.removeValue(forKey: kSecAttrService as String)\n",
        ),
    ),
    "store: rawRemove deletes a service-less copy (r3 item 1)": (
        "store",
        replaced(
            STORE_TEXT,
            STORE_REMOVE_LINE,
            "        var wide = baseQuery(account: account)\n"
            '        wide.removeValue(forKey: "svce")\n'
            "        _ = SecItemDelete(wide as CFDictionary)\n"
            "        let status = errSecSuccess\n",
        ),
    ),
    "store: rawSet filters the service out of its query (r3 item 1)": (
        "store",
        replaced(
            STORE_TEXT,
            STORE_SET_QUERY_LINE,
            '        let query = baseQuery(account: account).filter { $0.key != "svce" }\n',
        ),
    ),
    "store: rawData merges the production service over the query (r3 item 1)": (
        "store",
        replaced(
            STORE_TEXT,
            STORE_DATA_MATCH_LINE,
            STORE_DATA_MATCH_LINE + '        query.merge(["svce": "san.Growin.credentials." + "v1"]) { _, new in new }\n',
        ),
    ),
    "store: rawSet overwrites the service on the add item (r3 item 1)": (
        "store",
        replaced(
            STORE_TEXT,
            STORE_SET_ITEM_LINE,
            STORE_SET_ITEM_LINE + '            item["svce"] = ProductionKeychainGuard.productionService\n',
        ),
    ),
    "store: rawData builds its query from the shared instance (r3 item 1)": (
        "store",
        replaced(
            STORE_TEXT,
            "        var query = baseQuery(account: account)\n",
            "        var query = Self.shared.baseQuery(account: account)\n",
        ),
    ),
    "cleanup: deleteExactItem strips the service from its query (r3 item 1)": (
        "cleanup",
        replaced(
            CLEANUP_TEXT,
            CLEANUP_DELETE_QUERY_LINE,
            '        return SecItemDelete(exactQuery(service: service, account: account).filter { $0.key != "svce" } as CFDictionary)\n',
        ),
    ),
    "cleanup: deleteAllMatches deletes on another service (r3 item 1)": (
        "cleanup",
        replaced(CLEANUP_TEXT, CLEANUP_LOOP_LINE, '            let status = delete("other", account)\n'),
    ),
    "raw: remove deletes a service-less query (r3 item 1)": (
        "raw",
        replaced(
            RAW_TEXT,
            RAW_REMOVE_LINE,
            "        var wide = query(account)\n"
            '        wide.removeValue(forKey: "svce")\n'
            "        SecItemDelete(wide as CFDictionary)\n",
        ),
    ),
    "raw: data strips the service (r3 item 1)": (
        "raw",
        replaced(RAW_TEXT, RAW_DATA_FIRST, RAW_DATA_FIRST + "        item.removeValue(forKey: kSecAttrService as String)\n"),
    ),
    "raw: set strips the service (r3 item 1)": (
        "raw",
        replaced(RAW_TEXT, RAW_SET_FIRST, RAW_SET_FIRST + '        item.removeValue(forKey: "svce")\n'),
    ),
    # === round 3, item 2: the tripwire compiled out, disabled or moved out of a @Test ===
    "tripwire: the whole file inside #if false (r3 item 2)": (
        "tripwire",
        "#if false\n" + TRIPWIRE_TEXT + "#endif\n",
    ),
    "tripwire: one assertion inside #if false (r3 item 2)": (
        "tripwire",
        replaced(TRIPWIRE_TEXT, TRIPWIRE_SIGNER, "#if false\n" + TRIPWIRE_SIGNER + "#endif\n"),
    ),
    "tripwire: an @Test attribute removed (r3 item 2)": (
        "tripwire",
        replaced(TRIPWIRE_TEXT, "    @Test func theSharedStoreRefusesARead() {", "    func theSharedStoreRefusesARead() {"),
    ),
    "tripwire: an .enabled(if: false) trait (r3 item 2)": (
        "tripwire",
        replaced(
            TRIPWIRE_TEXT,
            "    @Test func theSharedStoreRefusesARead() {",
            "    @Test(.enabled(if: false)) func theSharedStoreRefusesARead() {",
        ),
    ),
    "tripwire: a .disabled trait on another test (r3 item 2)": (
        "tripwire",
        replaced(
            TRIPWIRE_TEXT,
            "    @Test func theSignersReportNotConfigured() {",
            '    @Test(.disabled("x")) func theSignersReportNotConfigured() {',
        ),
    ),
    "tripwire: the suite disabled (r3 item 2)": (
        "tripwire",
        replaced(TRIPWIRE_TEXT, "@Suite(.serialized)", '@Suite(.disabled("x"))'),
    ),
    "tripwire: the suite attribute removed (r3 item 2)": (
        "tripwire",
        replaced(TRIPWIRE_TEXT, "@Suite(.serialized)\n", ""),
    ),
    "tripwire: the signer checks moved into a non-test helper (r3 item 2)": (
        "tripwire",
        replaced(
            TRIPWIRE_TEXT,
            TRIPWIRE_SIGNER_BLOCK,
            TRIPWIRE_SIGNER_BLOCK.replace("    @Test func theSignersReportNotConfigured() {", "    private static func signerChecks() {")
            + "\n    @Test func theSignersReportNotConfigured() {\n        #expect(Self.protectedName.isEmpty == false)\n    }\n",
        ),
    ),
    "tripwire: a test that asserts nothing (r3 item 2)": (
        "tripwire",
        replaced(
            TRIPWIRE_TEXT,
            TRIPWIRE_SIGNER_BLOCK,
            "    @Test func theSignersReportNotConfigured() {\n    }\n",
        ),
    ),
    # === round 3, item 3: the tripwire writing through the KeychainStorage property wrapper ===
    "tripwire: KeychainStorage(...).wrappedValue = x (r3 item 3)": (
        "tripwire",
        replaced(
            TRIPWIRE_TEXT,
            TRIPWIRE_FIRST_TEST,
            TRIPWIRE_FIRST_TEST + '        KeychainStorage(wrappedValue: "", .alpacaApiKey, scope: .workspace(.uk)).wrappedValue = "x"\n',
        ),
    ),
    "tripwire: an @KeychainStorage property (r3 item 3)": (
        "tripwire",
        replaced(
            TRIPWIRE_TEXT,
            TRIPWIRE_FIRST_TEST,
            '    @KeychainStorage(.alpacaApiKey, scope: .workspace(.uk)) private var stored = ""\n\n' + TRIPWIRE_FIRST_TEST,
        ),
    ),
    "tripwire: an assignment to a member (r3 item 3)": (
        "tripwire",
        replaced(TRIPWIRE_TEXT, TRIPWIRE_FIRST_TEST, TRIPWIRE_FIRST_TEST + "        Self.state.value = 1\n"),
    ),
    "tripwire: an assignment to a subscript (r3 item 3)": (
        "tripwire",
        replaced(TRIPWIRE_TEXT, TRIPWIRE_FIRST_TEST, TRIPWIRE_FIRST_TEST + '        Self.table["x"] = 1\n'),
    ),
    # === round 3, item 4: the guard's own calls shadowed one layer down ===
    "guard: ProcessInfo no longer module-qualified (r3 item 4)": (
        "guard",
        replaced(GUARD_TEXT, "Foundation.ProcessInfo.processInfo", "ProcessInfo.processInfo"),
    ),
    "guard: NSClassFromString no longer module-qualified (r3 item 4)": (
        "guard",
        replaced(GUARD_TEXT, 'Foundation.NSClassFromString("XCTestCase")', 'NSClassFromString("XCTestCase")'),
    ),
    "guard: detection back to contains(where:) (r3 item 4)": (
        "guard",
        replaced(
            GUARD_TEXT,
            GUARD_DETECT_TEXT,
            "        xctestCaseClassLoaded || environmentKeys.contains { environment[$0] != nil }\n",
        ),
    ),
    "guard: detection loop returns false on a hit (r3 item 4)": (
        "guard",
        replaced(GUARD_TEXT, "if environment[key] != nil { return true }", "if environment[key] != nil { return false }"),
    ),
    # === item 1: a SecItem function that is not on the allow-list, or a guard that is not unconditional ===
    "store: a new named owner with a ternary guard (Codex probeDelete)": (
        "store",
        replaced(
            STORE_TEXT,
            "    private func baseQuery(account: String)",
            "    func probeDelete(account: String) -> OSStatus {\n"
            "        let _ = false ? (try assertAccessAllowed(for: .delete)) : ()\n"
            "        return SecItemDelete(baseQuery(account: account) as CFDictionary)\n"
            "    }\n\n    private func baseQuery(account: String)",
        ),
    ),
    "store: a new named owner with a real guard is still not allow-listed (item 1)": (
        "store",
        replaced(
            STORE_TEXT,
            "    private func baseQuery(account: String)",
            "    func probeDelete(account: String) throws -> OSStatus {\n"
            "        try assertAccessAllowed(for: .delete)\n"
            "        return SecItemDelete(baseQuery(account: account) as CFDictionary)\n"
            "    }\n\n    private func baseQuery(account: String)",
        ),
    ),
    "store: an allow-listed function guarded by a ternary (item 1)": (
        "store",
        replaced(
            STORE_TEXT,
            RAW_DATA_HEAD + GUARD_LINE["rawData"],
            RAW_DATA_HEAD + "        let _ = false ? (try assertAccessAllowed(for: .read)) : ()\n",
        ),
    ),
    "store: a SecItem closure in a property (item 1)": (
        "store",
        replaced(
            STORE_TEXT,
            "    private let service: String\n",
            "    private let service: String\n    private let hook: () -> OSStatus = { SecItemDelete([:] as CFDictionary) }\n",
        ),
    ),
    "store: a nested function inside an allow-listed one (item 1)": (
        "store",
        replaced(
            STORE_TEXT,
            RAW_REMOVE_HEAD + GUARD_LINE["rawRemove"],
            RAW_REMOVE_HEAD + GUARD_LINE["rawRemove"] + "        func inner() -> OSStatus { SecItemDelete([:] as CFDictionary) }\n",
        ),
    ),
    "cleanup: a new named owner with a ternary guard (item 1)": (
        "cleanup",
        replaced(
            CLEANUP_TEXT,
            "    private static let logger",
            "    static func probeDelete(account: String) -> OSStatus {\n"
            "        let _ = false ? ProductionKeychainGuard.recordRefusal(site: .delete) : ()\n"
            '        return SecItemDelete(exactQuery(service: "x", account: account) as CFDictionary)\n'
            "    }\n\n    private static let logger",
        ),
    ),
    "cleanup: a SecItem closure in a property (item 1)": (
        "cleanup",
        replaced(
            CLEANUP_TEXT,
            "    private static let logger",
            "    static let hook: () -> OSStatus = { SecItemDelete([:] as CFDictionary) }\n\n    private static let logger",
        ),
    ),
    "raw: a new named owner (item 1)": (
        "raw",
        replaced(
            RAW_TEXT,
            "    private func query(",
            "    func probe(account: String) -> OSStatus {\n"
            "        SecItemDelete(query(account) as CFDictionary)\n"
            "    }\n\n    private func query(",
        ),
    ),
    "raw: a SecItem closure in a property (item 1)": (
        "raw",
        replaced(RAW_TEXT, "    let service: String\n", "    let service: String\n    let hook = { SecItemDelete([:] as CFDictionary) }\n"),
    ),
    # === item 2: the service the guard checks is the service in the real query ===
    "store: baseQuery drops the service line (item 2)": ("store", replaced(STORE_TEXT, SERVICE_LINE, "")),
    "store: baseQuery uses the production constant (item 2)": (
        "store",
        replaced(STORE_TEXT, SERVICE_LINE, "            kSecAttrService as String: Self.productionService,\n"),
    ),
    "store: baseQuery uses a concatenated literal (item 2)": (
        "store",
        replaced(STORE_TEXT, SERVICE_LINE, '            kSecAttrService as String: "san.Growin.credentials." + "v1",\n'),
    ),
    "store: baseQuery has a second service key (item 2)": (
        "store",
        replaced(STORE_TEXT, SERVICE_LINE, SERVICE_LINE + '            kSecAttrService as String: "other",\n'),
    ),
    "store: init stores something else (item 2)": (
        "store",
        replaced(STORE_TEXT, "        self.service = service\n", "        self.service = ProductionKeychainGuard.productionService\n"),
    ),
    "store: the service becomes a var (item 2)": (
        "store",
        replaced(STORE_TEXT, "    private let service: String\n", "    private var service: String\n"),
    ),
    "store: rawData builds its own query (item 2)": (
        "store",
        replaced(
            STORE_TEXT,
            "        var query = baseQuery(account: account)\n",
            '        var query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrAccount as String: account]\n',
        ),
    ),
    "cleanup: exactQuery drops the service line (item 2)": ("cleanup", replaced(CLEANUP_TEXT, SERVICE_LINE, "")),
    "cleanup: exactQuery uses the production constant (item 2)": (
        "cleanup",
        replaced(CLEANUP_TEXT, SERVICE_LINE, "            kSecAttrService as String: ProductionKeychainGuard.productionService,\n"),
    ),
    "cleanup: exactQuery uses a concatenated literal (item 2)": (
        "cleanup",
        replaced(CLEANUP_TEXT, SERVICE_LINE, '            kSecAttrService as String: "san.Growin.credentials." + "v1",\n'),
    ),
    "cleanup: exactQuery has a second service key (item 2)": (
        "cleanup",
        replaced(CLEANUP_TEXT, SERVICE_LINE, SERVICE_LINE + '            kSecAttrService as String: "other",\n'),
    ),
    "cleanup: deleteExactItem queries another service (item 2)": (
        "cleanup",
        replaced(
            CLEANUP_TEXT,
            "return Security.SecItemDelete(exactQuery(service: service, account: account) as CFDictionary)",
            'return Security.SecItemDelete(exactQuery(service: "san.Growin.credentials." + "v1", account: account) as CFDictionary)',
        ),
    ),
    "raw: query drops the service line (item 2)": ("raw", replaced(RAW_TEXT, SERVICE_LINE, "")),
    "raw: query uses a concatenated literal (item 2)": (
        "raw",
        replaced(RAW_TEXT, SERVICE_LINE, '            kSecAttrService as String: "san.Growin.credentials." + "v1",\n'),
    ),
    "raw: query has a second service key (item 2)": (
        "raw",
        replaced(RAW_TEXT, SERVICE_LINE, SERVICE_LINE + '            kSecAttrService as String: "other",\n'),
    ),
    "raw: query uses the production constant (item 2)": (
        "raw",
        replaced(RAW_TEXT, SERVICE_LINE, "            kSecAttrService as String: ProductionKeychainGuard.productionService,\n"),
    ),
    # === KeychainStore: removing, moving, nesting or swallowing the guard ===
    "store: guard removed from rawData": ("store", replaced(STORE_TEXT, RAW_DATA_HEAD + GUARD_LINE["rawData"], RAW_DATA_HEAD)),
    "store: guard removed from rawSet": ("store", replaced(STORE_TEXT, RAW_SET_HEAD + GUARD_LINE["rawSet"], RAW_SET_HEAD)),
    "store: guard removed from rawRemove": (
        "store",
        replaced(STORE_TEXT, RAW_REMOVE_HEAD + GUARD_LINE["rawRemove"], RAW_REMOVE_HEAD),
    ),
    "store: rawData guards with the wrong site": (
        "store",
        replaced(STORE_TEXT, GUARD_LINE["rawData"], "        try assertAccessAllowed(for: .write)\n"),
    ),
    "store: guard moved after the SecItem call": (
        "store",
        replaced(
            replaced(STORE_TEXT, RAW_REMOVE_HEAD + GUARD_LINE["rawRemove"], RAW_REMOVE_HEAD),
            "Security.SecItemDelete(baseQuery(account: account) as CFDictionary)\n",
            "Security.SecItemDelete(baseQuery(account: account) as CFDictionary)\n        try assertAccessAllowed(for: .delete)\n",
        ),
    ),
    "store: guard nested in an if": (
        "store",
        replaced(
            STORE_TEXT,
            RAW_DATA_HEAD + GUARD_LINE["rawData"],
            RAW_DATA_HEAD + "        if true { try assertAccessAllowed(for: .read) }\n",
        ),
    ),
    "store: guard swallowed with try?": (
        "store",
        replaced(STORE_TEXT, RAW_DATA_HEAD + GUARD_LINE["rawData"], RAW_DATA_HEAD + "        try? assertAccessAllowed(for: .read)\n"),
    ),
    "store: guard forced with try!": (
        "store",
        replaced(STORE_TEXT, RAW_SET_HEAD + GUARD_LINE["rawSet"], RAW_SET_HEAD + "        try! assertAccessAllowed(for: .write)\n"),
    ),
    "store: an early return before the guard": (
        "store",
        replaced(
            STORE_TEXT,
            RAW_DATA_HEAD + GUARD_LINE["rawData"],
            RAW_DATA_HEAD + "        if account.isEmpty { return nil }\n" + GUARD_LINE["rawData"],
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
            "        try Self.checkAccess(\n            service: service,\n            protectedService: service,\n"
            "            testHost: false,\n            site: site)\n",
        ),
    ),
    "store: the check no longer throws": (
        "store",
        replaced(STORE_TEXT, "throw KeychainStoreError.productionKeychainRefusedUnderTests", "return"),
    ),
    "store: checkAccess condition gets && false (finding 4)": (
        "store",
        replaced(STORE_TEXT, "testHost: testHost) {\n" + STORE_RECORD_LINE, "testHost: testHost) && false {\n" + STORE_RECORD_LINE),
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
            '        if ProcessInfo.processInfo.environment["GROWIN_ALLOW_REAL_KEYCHAIN"] != nil { return }\n' + STORE_ASSERT_TEXT,
        ),
    ),
    "store: the guard line inside #if !DEBUG (finding 4)": (
        "store",
        replaced(
            STORE_TEXT,
            RAW_DATA_HEAD + GUARD_LINE["rawData"],
            RAW_DATA_HEAD + "        #if !DEBUG\n" + GUARD_LINE["rawData"] + "        #endif\n",
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
    # === item 5: the refusal log line ===
    "store: checkAccess no longer logs the refusal (item 5)": ("store", replaced(STORE_TEXT, STORE_RECORD_LINE, "")),
    "store: checkAccess logs a different site (item 5)": (
        "store",
        replaced(STORE_TEXT, STORE_RECORD_LINE, "            ProductionKeychainGuard.recordRefusal(site: .read)\n"),
    ),
    "cleanup: refusedOutcome no longer logs (item 5)": ("cleanup", replaced(CLEANUP_TEXT, CLEANUP_RECORD_LINE, "")),
    "cleanup: deleteExactItem refusal no longer logs (item 5)": (
        "cleanup",
        replaced(CLEANUP_TEXT, "            ProductionKeychainGuard.recordRefusal(site: .delete)\n", ""),
    ),
    "guard: the refusal log names the account (item 5)": (
        "guard",
        replaced(
            GUARD_TEXT,
            "site=\\(site.rawValue, privacy: .public)\")",
            "site=\\(site.rawValue, privacy: .public) account=\\(account, privacy: .public)\")",
        ),
    ),
    "guard: the refusal log names the service (item 5)": (
        "guard",
        replaced(
            GUARD_TEXT,
            "site=\\(site.rawValue, privacy: .public)\")",
            "site=\\(site.rawValue, privacy: .public) service=\\(productionService, privacy: .public)\")",
        ),
    ),
    "guard: the refusal log is emitted at debug level (item 5)": (
        "guard",
        replaced(GUARD_TEXT, "logger.error(", "logger.debug("),
    ),
    "guard: recordRefusal logs nothing (item 5)": ("guard", replaced(GUARD_TEXT, GUARD_RECORD_LINE, "")),
    "guard: recordRefusal returns early (item 5)": (
        "guard",
        replaced(GUARD_TEXT, GUARD_RECORD_LINE, "        return\n" + GUARD_RECORD_LINE),
    ),
    "guard: a second logging call (item 5)": (
        "guard",
        replaced(GUARD_TEXT, "isProductionStore && testHost", 'print("refused") ; return isProductionStore && testHost'),
    ),
    "guard: a different logger subsystem (item 5)": ("guard", replaced(GUARD_TEXT, '"production-keychain-guard"', '"other"')),
    "guard: the site enum gains a case that carries data (item 5)": (
        "guard",
        replaced(GUARD_TEXT, "case read, write, delete, cleanup", "case read, write, delete, cleanup, account"),
    ),
    # === LegacyT212KeychainCleanup ===
    "cleanup: guard removed from deleteExactItem": (
        "cleanup",
        replaced(CLEANUP_TEXT, CLEANUP_DELETE_TEXT, ""),
    ),
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
    "cleanup: a refused delete is not terminal, only recorded as a failure (round 1 item A)": (
        "cleanup",
        replaced(CLEANUP_TEXT, CLEANUP_TERMINAL_TEXT, ""),
    ),
    "cleanup: the terminal check moved after the failure is recorded (item A)": (
        "cleanup",
        replaced(
            CLEANUP_TEXT,
            CLEANUP_TERMINAL_TEXT + "                failed.append(status)\n",
            "                failed.append(status)\n" + CLEANUP_TERMINAL_TEXT,
        ),
    ),
    "cleanup: the terminal check tests the wrong status (item A)": (
        "cleanup",
        replaced(CLEANUP_TEXT, "if status == refusedUnderTestsStatus {", "if status == errSecSuccess {"),
    ),
    "cleanup: the terminal check returns an ordinary outcome (item A)": (
        "cleanup",
        replaced(
            CLEANUP_TEXT,
            "if status == refusedUnderTestsStatus { return refusedOutcome(log: log) }",
            "if status == refusedUnderTestsStatus { break }",
        ),
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
    # === ProductionKeychainGuard ===
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
    "guard: service comparison inverted": ("guard", replaced(GUARD_TEXT, "service == protectedService", "service != protectedService")),
    "guard: current process ignores the real test host": (
        "guard",
        replaced(GUARD_TEXT, "testHost: TestHostDetection.isTestHost())", "testHost: false)"),
    ),
    "guard: refusesCurrentProcess uses isTestHost() && false (finding 4)": (
        "guard",
        replaced(GUARD_TEXT, "testHost: TestHostDetection.isTestHost())", "testHost: TestHostDetection.isTestHost() && false)"),
    ),
    "guard: a test-host signal dropped": ("guard", replaced(GUARD_TEXT, '        "XCTestSessionIdentifier",\n', "")),
    "guard: the UI-test-host key dropped from environmentKeys (item B)": (
        "guard",
        replaced(GUARD_TEXT, f'        "{UI_TEST_HOST_KEY}",\n', ""),
    ),
    "guard: the UI-test-host key misspelled in environmentKeys (item B)": (
        "guard",
        replaced(GUARD_TEXT, f'"{UI_TEST_HOST_KEY}"', '"GROWIN_UI_TEST_HOS"'),
    ),
    "guard: the suppression key reused as a signal (item B)": (
        "guard",
        replaced(GUARD_TEXT, f'        "{UI_TEST_HOST_KEY}",\n', f'        "{UI_TEST_HOST_KEY}",\n        "{SKIP_KEY}",\n'),
    ),
    "guard: XCTestCase class signal dropped": (
        "guard",
        replaced(
            GUARD_TEXT,
            'xctestCaseClassLoaded: Bool = Foundation.NSClassFromString("XCTestCase") != nil',
            "xctestCaseClassLoaded: Bool = false",
        ),
    ),
    "guard: XCTestCase signal only in a comment (finding 5)": (
        "guard",
        replaced(
            GUARD_TEXT,
            'xctestCaseClassLoaded: Bool = Foundation.NSClassFromString("XCTestCase") != nil',
            'xctestCaseClassLoaded: Bool = false // Foundation.NSClassFromString("XCTestCase") != nil',
        ),
    ),
    "guard: typo in an env key, correct name in a comment (finding 5)": (
        "guard",
        replaced(GUARD_TEXT, '"XCTestBundlePath",', '"XCTestBundlePth", // "XCTestBundlePath"'),
    ),
    "guard: detection ignores the class signal": (
        "guard",
        replaced(GUARD_TEXT, "        if xctestCaseClassLoaded { return true }\n", ""),
    ),
    "guard: refusal code changed": ("guard", replaced(GUARD_TEXT, "PRODUCTION_KEYCHAIN_REFUSED_UNDER_TESTS", "REFUSED")),
    # === the UI-test app process carries the key (item B) ===
    "ui: the UI-test-host key line is missing from makeApp (item B)": ("ui", replaced(UI_HELPER_TEXT, UI_KEY_LINE, "")),
    "ui: the UI-test-host key is set to 0 (item B)": (
        "ui",
        replaced(UI_HELPER_TEXT, UI_KEY_LINE, UI_KEY_LINE.replace('"1"', '"0"')),
    ),
    "ui: the UI-test-host key is misspelled (item B)": (
        "ui",
        replaced(UI_HELPER_TEXT, UI_KEY_LINE, UI_KEY_LINE.replace("UI_TEST_HOST", "UI_TEST_HOS")),
    ),
    "ui: the UI-test-host key replaced by the suppression key twice (item B)": (
        "ui",
        replaced(UI_HELPER_TEXT, UI_KEY_LINE, UI_KEY_LINE.replace(UI_TEST_HOST_KEY, SKIP_KEY)),
    ),
    # === RawKeychain (finding 3) ===
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
    "raw: prefix loosened": ("raw", replaced(RAW_TEXT, '"san.Growin.credentials.v1.test."', '"san.Growin."')),
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
            replaced(RAW_TEXT, "        precondition(\n", "        #if DEBUG\n        precondition(\n"),
            'test-prefixed service")\n',
            'test-prefixed service")\n        #endif\n',
        ),
    ),
    # === item 4: the tripwire stays read-only, refusal-only and silent ===
    "tripwire: a write call (item 4)": (
        "tripwire",
        replaced(
            TRIPWIRE_TEXT,
            TRIPWIRE_READ,
            TRIPWIRE_READ + '        try? KeychainStore.shared.set("x", for: .alpacaApiKey, scope: .workspace(.uk))\n',
        ),
    ),
    "tripwire: a remove call (item 4)": (
        "tripwire",
        replaced(
            TRIPWIRE_TEXT,
            TRIPWIRE_READ,
            TRIPWIRE_READ + "        try? KeychainStore.shared.remove(.alpacaApiKey, scope: .workspace(.uk))\n",
        ),
    ),
    "tripwire: a migrate call (item 4)": (
        "tripwire",
        replaced(
            TRIPWIRE_TEXT,
            TRIPWIRE_READ,
            TRIPWIRE_READ + "        _ = KeychainStore.shared.migrateFlatItemsToScoped()\n",
        ),
    ),
    "tripwire: print(value) (item 4)": (
        "tripwire",
        replaced(
            TRIPWIRE_TEXT,
            TRIPWIRE_READ,
            "        let value = try? KeychainStore.shared.string(for: .alpacaApiKey, scope: .workspace(.uk))\n"
            "        print(value)\n",
        ),
    ),
    "tripwire: a read bound to a name (item 4)": (
        "tripwire",
        replaced(
            TRIPWIRE_TEXT,
            "_ = try KeychainStore.shared.string(",
            "let value = try KeychainStore.shared.string(",
        ),
    ),
    "tripwire: a fixed delete account (item 4)": (
        "tripwire",
        replaced(TRIPWIRE_TEXT, "account: UUID().uuidString)", 'account: "legacy")'),
    ),
    "tripwire: the delete targets another service (item 4)": (
        "tripwire",
        replaced(TRIPWIRE_TEXT, "service: Self.protectedName, account:", 'service: "x", account:'),
    ),
    "tripwire: a presence read outside any assertion (item 4)": (
        "tripwire",
        replaced(
            TRIPWIRE_TEXT,
            TRIPWIRE_SIGNER,
            "        let configured = LocalApprovalSigner.shared.isConfigured(for: .uk)\n",
        ),
    ),
    "tripwire: a delete outside any assertion (item 4)": (
        "tripwire",
        replaced(
            TRIPWIRE_TEXT,
            TRIPWIRE_DELETE,
            "        _ = LegacyT212KeychainCleanup.deleteExactItem(service: Self.protectedName, account: UUID().uuidString)\n",
        ),
    ),
    "tripwire: an assertion that expects success (item 4)": (
        "tripwire",
        replaced(TRIPWIRE_TEXT, "isConfigured(for: .uk) == false)", "isConfigured(for: .uk) == true)"),
    ),
    "tripwire: an assertion with != (item 4)": (
        "tripwire",
        replaced(TRIPWIRE_TEXT, "        #expect(refusal == Self.refused)\n", "        #expect(refusal != Self.refused)\n"),
    ),
    "tripwire: an assertion that is always true (item 4)": (
        "tripwire",
        replaced(
            TRIPWIRE_TEXT, "        #expect(refusal == Self.refused)\n", "        #expect(refusal == Self.refused || true)\n"
        ),
    ),
    "tripwire: the helper always reports a refusal (item 4)": (
        "tripwire",
        replaced(TRIPWIRE_TEXT, 'return "NO_REFUSAL"', 'return "PRODUCTION_KEYCHAIN_REFUSED_UNDER_TESTS"'),
    ),
    "tripwire: a read with try? swallowing the error (item 4)": (
        "tripwire",
        replaced(TRIPWIRE_TEXT, "_ = try KeychainStore.shared", "_ = try? KeychainStore.shared"),
    ),
    "tripwire: a second credential read, beyond the one exempt call (item 4)": (
        "tripwire",
        replaced(TRIPWIRE_TEXT, ".string(for: .alpacaApiKey,", ".string(for: .openaiApiKey,"),
    ),
    "tripwire: a store built in the file (item 4)": (
        "tripwire",
        replaced(TRIPWIRE_TEXT, TRIPWIRE_READ, TRIPWIRE_READ + '        let own = KeychainStore(service: "x")\n'),
    ),
    "tripwire: an attachment (item 4)": (
        "tripwire",
        replaced(TRIPWIRE_TEXT, TRIPWIRE_READ, TRIPWIRE_READ + '        let note = XCTAttachment(string: "x")\n'),
    ),
    "tripwire: string interpolation in the file (item 4)": (
        "tripwire",
        replaced(TRIPWIRE_TEXT, TRIPWIRE_READ, TRIPWIRE_READ + '        let note = "\\(Self.refused)"\n'),
    ),
    "tripwire: a disabled test (item 4)": (
        "tripwire",
        replaced(TRIPWIRE_TEXT, "    @Test func theSharedStoreRefusesARead() {", '    @Test(.disabled("x")) func theSharedStoreRefusesARead() {'),
    ),
    "tripwire: the shared-store check removed, no longer covered (item 4)": (
        "tripwire",
        replaced(TRIPWIRE_TEXT, TRIPWIRE_READ, ""),
    ),
    "tripwire: the protected name is no longer the production service (item 4)": (
        "tripwire",
        replaced(TRIPWIRE_TEXT, '"san.Growin.credentials." + "v1"', '"san.Growin.credentials.v1.test.x"'),
    ),
}


def _indent(lines: list[str]) -> list[str]:
    return [("    " + line) if line.strip() else line for line in lines]


def block_variants(block: str) -> dict[str, str]:
    """Every way to keep a pinned test's #expect in its text yet skip, weaken or disable it."""
    lines = block.split("\n")
    head, body, tail = lines[0], lines[1:-1], lines[-1]

    def build(new_head: str, new_body: list[str]) -> str:
        return "\n".join([new_head, *new_body, tail])

    def statement_end(start: int) -> int:
        depth = 0
        for index in range(start, len(body)):
            depth += body[index].count("(") - body[index].count(")")
            if depth <= 0:
                return index
        return len(body) - 1

    firsts = [i for i, line in enumerate(body) if "#expect(" in line]
    first, last = firsts[0], firsts[-1]
    first_end, last_end = statement_end(first), statement_end(last)
    throwing = head.replace(") {", ") throws {", 1) if "throws" not in head else head
    first_statement = "\n".join(body[first : first_end + 1])
    variants = {
        "return as the first statement": build(head, ["        return", *body]),
        "return after the first assertion": build(head, [*body[: first_end + 1], "        return", *body[first_end + 1 :]]),
        "assertions in a closure that is never called": build(head, ["        let run = {", *_indent(body), "        }"]),
        "assertions inside if false": build(head, ["        if false {", *_indent(body), "        }"]),
        "assertions inside if 1 == 2": build(head, ["        if 1 == 2 {", *_indent(body), "        }"]),
        "assertions after guard false else { return }": build(head, ["        guard false else { return }", *body]),
        "the first assertion removed": build(head, [*body[:first], *body[first_end + 1 :]]),
        "the last assertion removed": build(head, [*body[:last], *body[last_end + 1 :]]),
        "assertions in a defer that a throw precedes": build(
            throwing,
            ["        if true { throw KeychainStoreError.unexpectedData }", "        defer {", *_indent(body), "        }"],
        ),
        "the @Test attribute dropped": build(head.replace("@Test ", "", 1), body),
        "a .disabled trait": build(head.replace("@Test ", '@Test(.disabled("x")) ', 1), body),
        "an .enabled(if: false) trait": build(head.replace("@Test ", "@Test(.enabled(if: false)) ", 1), body),
        "renamed so the pinned test disappears": build(re.sub(r"(func\s+)(\w+)", r"\1\2Copy", head, count=1), body),
        "a duplicated test": block + "\n\n    " + block,
        "#expect swapped for a no-op local expect": build(
            head,
            ["        func expect(_ condition: Bool) {}", *[line.replace("#expect(", "expect(") for line in body]],
        ),
        "the first assertion inverted": "\n".join(
            [head, *body[:first], first_statement.replace("#expect(", "#expect(!", 1), *body[first_end + 1 :], tail]
        ),
        "the first assertion or true": "\n".join(
            [head, *body[:first], first_statement.replace("#expect(", "#expect(true || ", 1), *body[first_end + 1 :], tail]
        ),
    }
    if any("try " in line for line in body):
        swallowed = []
        done = False
        for line in body:
            if not done and "try " in line:
                line, done = line.replace("try ", "try? ", 1), True
            swallowed.append(line)
        variants["a try? swallowing the throwing expression"] = build(head, swallowed)
    return variants


def _add_generated_plants(kind: str, text: str, blocks: dict[str, str], tag: str) -> None:
    for name, block in blocks.items():
        for label, variant in block_variants(block).items():
            PLANTS[f"{kind}: {name}: {label} ({tag})"] = (kind, replaced(text, block, variant))


_add_generated_plants("tripwire", TRIPWIRE_TEXT, TRIPWIRE_TEST_BLOCKS, "r4 item 1")
_add_generated_plants("canary_scope", SCOPE_TESTS_TEXT, CANARY_SCOPE, "r4 item 4")
_add_generated_plants("canary_legacy", LEGACY_TESTS_TEXT, CANARY_LEGACY, "r4 item 4")

SUITE_VARIANTS = {
    "a .disabled() trait": "@Suite(.serialized, .disabled())",
    'a .disabled("flaky") trait': '@Suite(.serialized, .disabled("flaky"))',
    "an .enabled(if: false) trait": "@Suite(.serialized, .enabled(if: false))",
    "a .tags(...) trait plus .disabled": "@Suite(.serialized, .tags(.keychain), .disabled())",
    "the traits in another order": "@Suite(.disabled(), .serialized)",
    "the .serialized trait dropped": "@Suite",
    "the .serialized trait replaced by .disabled": "@Suite(.disabled())",
}
for _kind, _text, _header, _struct in (
    ("canary_scope", SCOPE_TESTS_TEXT, SCOPE_SUITE_HEADER, "KeychainScopeTests"),
    ("canary_legacy", LEGACY_TESTS_TEXT, LEGACY_SUITE_HEADER, "LegacyT212KeychainCleanupTests"),
):
    for _label, _attribute in SUITE_VARIANTS.items():
        PLANTS[f"{_kind}: {_struct}: {_label} (r5 item 1)"] = (
            _kind,
            replaced(_text, _header, _header.replace("@Suite(.serialized)", _attribute, 1)),
        )
    PLANTS[f"{_kind}: {_struct}: a second @Suite attribute (r5 item 1)"] = (
        _kind,
        replaced(_text, _header, "@Suite(.disabled())\n" + _header),
    )
    PLANTS[f"{_kind}: {_struct}: a second @Suite attribute after the first (r5 item 1)"] = (
        _kind,
        replaced(_text, _header, _header.replace("\nstruct", '\n@Suite("renamed")\nstruct', 1)),
    )

DELETE_ARGUMENT = "account: UUID().uuidString"
PLANTS.update(
    {
        # the delete's account must stay a fresh UUID: a fixed one could, with the guard broken, delete a real item
        "tripwire: the delete account becomes a literal (r4 item 1)": (
            "tripwire",
            replaced(TRIPWIRE_TEXT, DELETE_ARGUMENT, 'account: "legacy"'),
        ),
        "tripwire: the delete account hoisted into a fixed let (r4 item 1)": (
            "tripwire",
            replaced(
                replaced(TRIPWIRE_TEXT, DELETE_ARGUMENT, "account: account"),
                "        #expect(\n            LegacyT212KeychainCleanup.deleteExactItem(",
                '        let account = "legacy"\n        #expect(\n            LegacyT212KeychainCleanup.deleteExactItem(',
            ),
        ),
        "tripwire: the delete account becomes a shared static (r4 item 1)": (
            "tripwire",
            replaced(TRIPWIRE_TEXT, DELETE_ARGUMENT, "account: Self.refused"),
        ),
        # a credential read inside an assertion would be printed by a failing run
        "tripwire: an assertion reads the shared store inside #expect (r4 item 5)": (
            "tripwire",
            replaced(
                TRIPWIRE_TEXT,
                TRIPWIRE_READ,
                "        #expect(try KeychainStore.shared.string(for: .alpacaApiKey, scope: .workspace(.uk)) == nil)\n",
            ),
        ),
        "canary_scope: a store string read inside #expect (r4 item 5)": (
            "canary_scope",
            replaced(
                SCOPE_TESTS_TEXT,
                "            #expect(aStringIsNil)\n",
                "            #expect(try a.string(for: .alpacaApiKey, scope: .workspace(.uk)) == nil)\n",
            ),
        ),
        "canary_scope: a raw data read inside #expect (r4 item 5)": (
            "canary_scope",
            replaced(
                SCOPE_TESTS_TEXT,
                '            #expect(afterRemove, "B survives a remove on A")\n',
                "            #expect(try rawB.data(account: Self.canaryScopedAccount) == planted)\n",
            ),
        ),
        "canary_scope: a legacy flat read inside #expect (r4 item 5)": (
            "canary_scope",
            replaced(
                SCOPE_TESTS_TEXT,
                "            #expect(aFlatIsNil)\n",
                "            #expect(try a.legacyFlatData(for: .alpacaApiKey) == nil)\n",
            ),
        ),
        "canary_legacy: a raw data read inside #expect (r4 item 5)": (
            "canary_legacy",
            replaced(
                LEGACY_TESTS_TEXT,
                '            #expect(untouched, "B untouched after deletes aimed at A")\n',
                "            #expect(try b.raw.data(account: account) == Self.canary)\n",
            ),
        ),
        # the helper that runs every canary body
        "canary_scope: requireKeychain no longer calls the body (r4 item 4)": (
            "canary_scope",
            replaced(SCOPE_TESTS_TEXT, "            try body()\n", "            _ = body\n"),
        ),
        "canary_scope: requireKeychain swallows every error (r4 item 4)": (
            "canary_scope",
            replaced(SCOPE_TESTS_TEXT, "            try body()\n", "            try? body()\n"),
        ),
        "canary_scope: the positive control removed (r4 item 4)": (
            "canary_scope",
            replaced(
                SCOPE_TESTS_TEXT,
                "            let plantedReadsBack = try rawB.data(account: Self.canaryScopedAccount) == planted\n"
                '            #expect(plantedReadsBack, "positive control")\n',
                "",
            ),
        ),
        "canary_scope: a string literal in a canary changed (r4 item 4)": (
            "canary_scope",
            replaced(SCOPE_TESTS_TEXT, 'let planted = Data("CANARY-B".utf8)', 'let planted = Data("".utf8)'),
        ),
        "canary_legacy: the loop over the accounts emptied (r4 item 4)": (
            "canary_legacy",
            replaced(
                LEGACY_TESTS_TEXT,
                "        try b.seed(Cleanup.legacyAccounts)\n\n        for account in Cleanup.legacyAccounts {\n            let notFound",
                "        try b.seed([])\n\n        for account in Cleanup.legacyAccounts {\n            let notFound",
            ),
        ),
    }
)


def run_scan(kind: str, text: str) -> list[str]:
    if kind == "ui":
        return ui_helper_key_offences(text, GUARD_TEXT)
    scans = {
        "store": keychain_store_offences,
        "cleanup": legacy_cleanup_offences,
        "guard": guard_file_offences,
        "raw": raw_keychain_offences,
        "tripwire": tripwire_offences,
        "canary_scope": canary_scope_offences,
        "canary_legacy": canary_legacy_offences,
    }
    return scans[kind](text)


REAL = {
    "store": STORE_TEXT,
    "cleanup": CLEANUP_TEXT,
    "guard": GUARD_TEXT,
    "raw": RAW_TEXT,
    "tripwire": TRIPWIRE_TEXT,
    "canary_scope": SCOPE_TESTS_TEXT,
    "canary_legacy": LEGACY_TESTS_TEXT,
    "ui": UI_HELPER_TEXT,
}


@pytest.mark.parametrize("label", sorted(PLANTS))
def test_each_planted_guard_break_is_caught(label):
    kind, planted = PLANTS[label]
    if isinstance(planted, BrokenPlant):
        pytest.fail(f"{label}: {planted.message}")
    assert planted != REAL[kind], "the plant changed nothing"
    assert run_scan(kind, planted), f"the scan missed: {label}"


def test_every_plant_applies_to_the_current_source():
    broken = {label: text.message for label, (_, text) in PLANTS.items() if isinstance(text, BrokenPlant)}
    assert broken == {}, "plants whose anchor text is gone (update them with the source change): " + "; ".join(
        f"{label}: {message}" for label, message in sorted(broken.items())
    )


def test_no_planted_break_goes_undetected():
    undetected = [
        label
        for label, (kind, text) in PLANTS.items()
        if not isinstance(text, BrokenPlant) and not run_scan(kind, text)
    ]
    assert undetected == [], f"{len(undetected)} planted breaks go undetected"
    assert len(PLANTS) >= 100


# --- planted shadows (item 3): extra or changed files in the Growin/ and GrowinTests/ tree ---

SHADOW_PLANTS: dict[str, dict[Path, str]] = {
    # --- round 3, item 4: one layer down (Opus CompatShims.swift) ---
    "a top-level NSClassFromString that returns nil (Opus)": {
        Path("Growin/Util/CompatShims.swift"): "func NSClassFromString(_ name: String) -> AnyClass? { nil }\n"
    },
    "an Array extension that overrides contains(where:) (Opus)": {
        Path("Growin/Util/CompatShims.swift"): "extension Array where Element == String {\n"
        "    func contains(where predicate: (String) throws -> Bool) rethrows -> Bool { false }\n}\n"
    },
    "a Sequence extension": {
        Path("Growin/Util/CompatShims.swift"): "extension Sequence {\n    func contains(where p: (Element) throws -> Bool) rethrows -> Bool { false }\n}\n"
    },
    "a Dictionary extension with a more specific subscript": {
        Path("Growin/Util/CompatShims.swift"): "extension Dictionary where Key == String, Value == String {\n"
        "    subscript(key: String) -> String? { nil }\n}\n"
    },
    "a ProcessInfo extension with an environment": {
        Path("Growin/Util/CompatShims.swift"): "extension ProcessInfo {\n    var environment: [String: String] { [:] }\n}\n"
    },
    "an Optional extension": {Path("Growin/Util/CompatShims.swift"): "extension Optional {\n    var isNil: Bool { false }\n}\n"},
    "a Foundation-qualified String extension": {
        Path("Growin/Util/CompatShims.swift"): "extension Swift.String {\n    var isEmpty: Bool { false }\n}\n"
    },
    "a global == on Bool": {Path("Growin/Util/CompatShims.swift"): "func == (lhs: Bool, rhs: Bool) -> Bool { true }\n"},
    "a global && on Bool": {
        Path("Growin/Util/CompatShims.swift"): "func && (lhs: Bool, rhs: @autoclosure () -> Bool) -> Bool { false }\n"
    },
    "a global || on Bool": {
        Path("Growin/Util/CompatShims.swift"): "func || (lhs: Bool, rhs: @autoclosure () throws -> Bool) rethrows -> Bool { false }\n"
    },
    "a global prefix ! on Bool": {Path("Growin/Util/CompatShims.swift"): "prefix func ! (value: Bool) -> Bool { value }\n"},
    "a global != on an optional class": {
        Path("Growin/Util/CompatShims.swift"): "func != (lhs: AnyClass?, rhs: _OptionalNilComparisonType) -> Bool { false }\n"
    },
    "a static == inside a type that takes String": {
        Path("Growin/Util/CompatShims.swift"): "enum Shim {\n    static func == (lhs: String, rhs: String) -> Bool { true }\n}\n"
    },
    "a static && inside a type that takes Bool": {
        Path("Growin/Util/CompatShims.swift"): "enum Shim {\n    static func && (lhs: Bool, rhs: Bool) -> Bool { false }\n}\n"
    },
    "a module-level enum named Foundation": {Path("Growin/Util/CompatShims.swift"): "enum Foundation {}\n"},
    "a module-level struct named ProcessInfo": {Path("Growin/Util/CompatShims.swift"): "struct ProcessInfo {}\n"},
    "a module-level let environment": {Path("Growin/Util/CompatShims.swift"): "let environment: [String: String] = [:]\n"},
    "a module-level var processInfo": {Path("Growin/Util/CompatShims.swift"): "var processInfo = 1\n"},
    "an extension that declares a static environment": {
        Path("Growin/Util/CompatShims.swift"): "extension KeychainStore {\n    static var environment: [String: String] { [:] }\n}\n"
    },
    "a nested Foundation type inside an extension": {
        Path("Growin/Util/CompatShims.swift"): "extension KeychainStore {\n    enum Foundation {}\n}\n"
    },
    "a module-level typealias of Darwin": {Path("Growin/Util/CompatShims.swift"): "typealias Darwin = Int\n"},
    "a nested TestHostDetection in an extension of the store (Opus)": {
        Path("Growin/Models/Shadow.swift"): "extension KeychainStore {\n    enum TestHostDetection {\n"
        "        static func isTestHost() -> Bool { false }\n    }\n}\n"
    },
    "a nested ProductionKeychainGuard in an extension of the cleanup (Opus)": {
        Path("Growin/Models/Shadow.swift"): "extension LegacyT212KeychainCleanup {\n    enum ProductionKeychainGuard {\n"
        "        static func refusesCurrentProcess(service: String) -> Bool { false }\n    }\n}\n"
    },
    "a typealias of TestHostDetection (Opus)": {
        Path("Growin/Models/Shadow.swift"): "enum Fake { static func isTestHost() -> Bool { false } }\n"
        "extension KeychainStore { typealias TestHostDetection = Fake }\n"
    },
    "a typealias of ProductionKeychainGuard (Opus)": {
        Path("Growin/Models/Shadow.swift"): "enum Fake {}\ntypealias ProductionKeychainGuard = Fake\n"
    },
    "a struct with the guard's name in the tests": {
        Path("GrowinTests/Shadow.swift"): "struct TestHostDetection {}\n"
    },
    "an extension of the real type with an external-label overload (Opus protectedService p:)": {
        Path("Growin/Models/Shadow.swift"): "extension ProductionKeychainGuard {\n"
        "    static func refuses(service: String, protectedService p: String, testHost: Bool) -> Bool { false }\n}\n"
    },
    "a static func isTestHost in another type": {
        Path("Growin/Models/Shadow.swift"): "enum Other {\n    static func isTestHost() -> Bool { false }\n}\n"
    },
    "a static var named like the guard type": {
        Path("Growin/Models/Shadow.swift"): "extension KeychainStore {\n    static var TestHostDetection: Int { 0 }\n}\n"
    },
    "a function refuses in another file": {
        Path("Growin/Models/Shadow.swift"): "extension LegacyT212KeychainCleanup {\n"
        "    static func refuses(service: String, protectedService: String, testHost: Bool) -> Bool { false }\n}\n"
    },
    "a second checkAccess in the app": {
        Path("Growin/Models/Shadow.swift"): "extension LegacyT212KeychainCleanup {\n"
        "    static func checkAccess(service: String) throws {}\n}\n"
    },
    "an assertAccessAllowed in another type": {
        Path("Growin/Models/Shadow.swift"): "struct Local {\n    func assertAccessAllowed(for site: RefusalSite) throws {}\n}\n"
    },
    "a recordRefusal in another type": {
        Path("Growin/Models/Shadow.swift"): "struct Local {\n    static func recordRefusal(site: RefusalSite) {}\n}\n"
    },
    "a backticked declaration": {
        Path("Growin/Models/Shadow.swift"): "extension KeychainStore {\n    enum `TestHostDetection` {}\n}\n"
    },
    "a generic parameter with the guard's name": {
        Path("Growin/Models/Shadow.swift"): "func f<ProductionKeychainGuard>(_ x: ProductionKeychainGuard) {}\n"
    },
    "a parameter named like the guard type": {
        Path("Growin/Models/Shadow.swift"): "func f(TestHostDetection: Int) {}\n"
    },
    "an isTestHost let in the tests": {
        Path("GrowinTests/Shadow.swift"): "struct S {\n    static let isTestHost = { false }\n}\n"
    },
}


_SCOPE_WITH_WIPE = replaced(
    SCOPE_TESTS_TEXT,
    "    private static func cleanUp(_ store: KeychainStore) {",
    "    private static func wipe() { _ = SecItemDelete([:] as CFDictionary) }\n\n    private static func cleanUp(_ store: KeychainStore) {",
)
SHADOW_PLANTS.update(
    {
        # --- r4 item 1 (Opus 1): Security names ---
        "Opus: a file that redefines kSecAttrService as another constant (r4 Opus 1)": {
            Path("Growin/Util/KeychainKeys.swift"): "import Security\nlet kSecAttrService: CFString = kSecUseOperationPrompt\n"
        },
        "Opus: a static var kSecAttrService in an extension of the cleanup (r4 Opus 1)": {
            Path("Growin/Models/CleanupShim.swift"): "extension LegacyT212KeychainCleanup {\n"
            "    static var kSecAttrService: CFString { kSecUseOperationPrompt }\n}\n"
        },
        "a parameter named kSecAttrService (r4 Opus 1)": {
            Path("Growin/Models/Shim.swift"): "func build(kSecAttrService: String) {}\n"
        },
        "an underscore parameter whose name is kSecAttrService (r4 Opus 1)": {
            Path("Growin/Models/Shim.swift"): "func build(_ kSecAttrService: String) {}\n"
        },
        "an external label that is kSecAttrService (r4 Opus 1)": {
            Path("Growin/Models/Shim.swift"): "func build(kSecAttrService service: String) {}\n"
        },
        "a local let kSecClass (r4 Opus 1)": {
            Path("Growin/Models/Shim.swift"): "func f() {\n    let kSecClass = 1\n    _ = kSecClass\n}\n"
        },
        "a function named SecItemDelete (r4 Opus 1)": {
            Path("Growin/Models/Shim.swift"): "func SecItemDelete(_ query: CFDictionary) -> OSStatus { 0 }\n"
        },
        "a var named errSecItemNotFound (r4 Opus 1)": {Path("Growin/Models/Shim.swift"): "var errSecItemNotFound: OSStatus { 0 }\n"},
        "a typealias named SecKeychainRef (r4 Opus 1)": {Path("Growin/Models/Shim.swift"): "typealias SecKeychainRef = Int\n"},
        "an enum case named kSecClass (r4 Opus 1)": {Path("Growin/Models/Shim.swift"): "enum Keys {\n    case kSecClass\n}\n"},
        "an enum case list that includes errSecSuccess (r4 Opus 1)": {
            Path("Growin/Models/Shim.swift"): "enum Keys {\n    case ok, errSecSuccess\n}\n"
        },
        "a for loop variable named kSecClass (r4 Opus 1)": {
            Path("Growin/Models/Shim.swift"): "func f() {\n    for kSecClass in [1] { _ = kSecClass }\n}\n"
        },
        "a tuple binding that includes kSecAttrAccount (r4 Opus 1)": {
            Path("Growin/Models/Shim.swift"): "func f() {\n    let (a, kSecAttrAccount) = (1, 2)\n    _ = (a, kSecAttrAccount)\n}\n"
        },
        "a static var kSecAttrService in the tests (r4 Opus 1)": {
            Path("GrowinTests/Shim.swift"): "struct S {\n    static var kSecAttrService: String { \"x\" }\n}\n"
        },
        # --- r4 item 2 (Opus 2): SecItem mentions in tests ---
        "a direct SecItemDelete in another test file (r4 Opus 2)": {
            Path("GrowinTests/WorkspaceIsolation/OtherTests.swift"): "import Security\nfunc wipe() { _ = SecItemDelete([:] as CFDictionary) }\n"
        },
        "a direct SecItemDelete in a test helper (r4 Opus 2)": {
            Path("GrowinTests/WorkspaceIsolation/KeychainScopeTests.swift"): _SCOPE_WITH_WIPE
        },
        "a Security-qualified SecItemCopyMatching in a test (r4 Opus 2)": {
            Path("GrowinTests/WorkspaceIsolation/OtherTests.swift"): "func peek() { _ = Security.SecItemCopyMatching([:] as CFDictionary, nil) }\n"
        },
        "a SecKeychain call in a test (r4 Opus 2)": {
            Path("GrowinTests/WorkspaceIsolation/OtherTests.swift"): "func old() { _ = SecKeychainCopyDefault(nil) }\n"
        },
        # --- r4 item 3 (Opus 6): sugared and aliased extensions ---
        "an extension of the dictionary sugar (r4 Opus 6)": {
            Path("Growin/Util/Shim.swift"): "extension [String: String] {\n    var isEmpty: Bool { false }\n}\n"
        },
        "an extension of the array sugar (r4 Opus 6)": {
            Path("Growin/Util/Shim.swift"): "extension [String] {\n    func contains(where p: (String) -> Bool) -> Bool { false }\n}\n"
        },
        "an extension of an alias of Dictionary (r4 Opus 6)": {
            Path("Growin/Util/Shim.swift"): "typealias D = Dictionary<String, Any>\nextension D {\n    var isEmpty: Bool { false }\n}\n"
        },
        "an alias in one file extended in another (r4 Opus 6)": {
            Path("Growin/Util/Alias.swift"): "typealias Words = [String]\n",
            Path("Growin/Util/Shim.swift"): "extension Words {\n    var first: String? { nil }\n}\n",
        },
        "an extension of an optional sugar (r4 Opus 6)": {Path("Growin/Util/Shim.swift"): "extension String? {\n}\n"},
        # --- r4 item 7 (Opus 7): dynamic lookup ---
        "a dlsym on a built string (r4 Opus 7)": {
            Path("Growin/Util/Lookup.swift"): 'let handle = dlsym(dlopen(nil, 0), "Sec" + "ItemDelete")\n'
        },
        "a dlopen in the app (r4 Opus 7)": {Path("Growin/Util/Lookup.swift"): "let handle = dlopen(nil, 0)\n"},
        "an NSSelectorFromString (r4 Opus 7)": {Path("Growin/Util/Lookup.swift"): 'let selector = NSSelectorFromString("x")\n'},
        "a dlsym in a test (r4 Opus 7)": {Path("GrowinTests/Lookup.swift"): 'let symbol = dlsym(nil, "x")\n'},
        # --- r5 item 2: module-qualified lookups, @_silgen_name, CFBundleGetFunctionPointerForName ---
        "a Darwin-qualified dlsym on a built string (r5 item 2)": {
            Path("Growin/Util/Lookup.swift"): 'let handle = Darwin.dlsym(nil, "Sec" + "ItemDelete")\n'
        },
        "a Darwin-qualified dlopen (r5 item 2)": {Path("Growin/Util/Lookup.swift"): "let handle = Darwin.dlopen(nil, 0)\n"},
        "a Foundation-qualified NSSelectorFromString (r5 item 2)": {
            Path("Growin/Util/Lookup.swift"): 'let selector = Foundation.NSSelectorFromString("x")\n'
        },
        "an @_silgen_name bound to SecItemDelete (r5 item 2)": {
            Path("Growin/Util/Lookup.swift"): '@_silgen_name("SecItemDelete") func zap(_ query: CFDictionary) -> OSStatus\n'
        },
        "a CFBundleGetFunctionPointerForName lookup (r5 item 2)": {
            Path("Growin/Util/Lookup.swift"): 'let pointer = CFBundleGetFunctionPointerForName(bundle, "SecItemDelete" as CFString)\n'
        },
        "a Darwin-qualified dlsym in a test (r5 item 2)": {Path("GrowinTests/Lookup.swift"): 'let symbol = Darwin.dlsym(nil, "x")\n'},
        "an @_silgen_name in a test (r5 item 2)": {
            Path("GrowinTests/Lookup.swift"): '@_silgen_name("SecItemDelete") func zap(_ query: CFDictionary) -> OSStatus\n'
        },
        "a CFBundleGetFunctionPointerForName in a test (r5 item 2)": {
            Path("GrowinTests/Lookup.swift"): 'let pointer = CFBundleGetFunctionPointerForName(bundle, "x" as CFString)\n'
        },
        # --- r4 item 1: the testing vocabulary in GrowinTests ---
        "a local func expect (r4 item 1)": {Path("GrowinTests/Shim.swift"): "func expect(_ condition: Bool) {}\n"},
        "a macro named expect (r4 item 1)": {
            Path("GrowinTests/Shim.swift"): 'macro expect(_ condition: Bool) = #externalMacro(module: "X", type: "Y")\n'
        },
        "a func named require (r4 item 1)": {Path("GrowinTests/Shim.swift"): "func require(_ condition: Bool) throws {}\n"},
        "a struct named Issue (r4 item 1)": {Path("GrowinTests/Shim.swift"): "struct Issue {}\n"},
        "a typealias named Issue (r4 item 1)": {Path("GrowinTests/Shim.swift"): "typealias Issue = Int\n"},
        "a func named withKnownIssue (r4 item 1)": {Path("GrowinTests/Shim.swift"): "func withKnownIssue(_ body: () -> Void) {}\n"},
        "a nested func expect inside a test (r4 item 1)": {
            Path("GrowinTests/Shim.swift"): "struct S {\n    @Test func t() {\n        func expect(_ c: Bool) {}\n    }\n}\n"
        },
        "a let expect closure (r4 item 1)": {Path("GrowinTests/Shim.swift"): "let expect = { (c: Bool) in }\n"},
    }
)


def test_the_lookup_words_in_strings_and_comments_are_not_findings():
    files = swift_files()
    files[Path("Growin/Models/Fine4.swift")] = (
        '// Darwin.dlsym and @_silgen_name are only named in this comment\n'
        'let note = "Foundation.NSSelectorFromString CFBundleGetFunctionPointerForName dlopen"\n'
    )
    assert shadow_offences(files) == []


def test_a_switch_pattern_named_like_a_security_constant_is_not_a_declaration():
    files = swift_files()
    files[Path("Growin/Models/Fine3.swift")] = (
        "func f(status: Int32) -> Bool {\n    switch status {\n    case errSecSuccess: return true\n"
        "    case errSecItemNotFound: return false\n    default: return false\n    }\n}\n"
        "func g() { _ = [Security.kSecClass as String: 1] }\n"
    )
    assert shadow_offences(files) == []


def test_the_secret_in_expect_rule_reads_only_assertion_arguments():
    bad = "#expect(try store.string(for: .x, scope: .y) == nil)\n"
    assert secret_expect_offences("x", bad)
    assert secret_expect_offences("x", "#require(try raw.data(account: a) != nil)\n")
    assert secret_expect_offences("x", "#expect(try a.legacyFlatData(for: .x) == nil)\n")
    assert secret_expect_offences("x", "#expect(rawData(a) == nil)\n")
    # A credential read outside the assertion, with only a Bool asserted, is the allowed shape.
    good = "let isNil = try store.string(for: .x, scope: .y) == nil\n#expect(isNil)\n"
    assert secret_expect_offences("x", good) == []
    assert secret_expect_offences("x", '// #expect(try store.string(for: .x) == nil)\nlet s = "#expect(a.string(b))"\n') == []


@pytest.mark.parametrize("label", sorted(SHADOW_PLANTS))
def test_each_planted_shadow_is_caught(label):
    files = swift_files()
    files.update(SHADOW_PLANTS[label])
    assert shadow_offences(files), f"the scan missed: {label}"


def test_the_shadow_scan_leaves_members_and_custom_type_operators_alone():
    files = swift_files()
    files[Path("Growin/Models/Fine2.swift")] = (
        "struct Status {\n    let environment: String?\n    var processInfo: Int = 0\n}\n"
        "enum Mode { case environment }\n"
        "struct Card: Equatable {\n    static func == (lhs: Card, rhs: Card) -> Bool { true }\n}\n"
        "extension Card {\n    var title: String { \"\" }\n}\n"
        "func run() {\n    let environment = 1\n    _ = environment\n}\n"
    )
    assert shadow_offences(files) == []


def test_the_shadow_scan_leaves_ordinary_uses_alone():
    files = swift_files()
    files[Path("Growin/Models/Fine.swift")] = (
        "// enum TestHostDetection is only named in a comment\n"
        'let note = "enum ProductionKeychainGuard { }"\n'
        "func f() -> Bool { TestHostDetection.isTestHost() && ProductionKeychainGuard.refusesCurrentProcess(service: s) }\n"
        "func g(protected: Int) {}\n"
    )
    assert shadow_offences(files) == []


# --- the exemption in the #573 scans (item 4): planted widening in Python and in the Swift mirror ---


def _widened_by_suffix(relative: Path, text: str) -> str:
    if str(relative).endswith("TripwireTests.swift"):
        return text.replace(TRIPWIRE_SHARED_CALL, "refusedRead()")
    return text


def _widened_to_a_second_file(relative: Path, text: str) -> str:
    if relative in (TRIPWIRE_PATH, Path("WorkspaceIsolation") / "OtherTests.swift"):
        return text.replace(TRIPWIRE_SHARED_CALL, "refusedRead()")
    return text


def _widened_to_any_shared_call(relative: Path, text: str) -> str:
    if relative == TRIPWIRE_PATH:
        return text.replace("KeychainStore.shared", "refusedStore")
    return text


def _not_exempting_anything(relative: Path, text: str) -> str:
    return text


@pytest.mark.parametrize(
    "exempt",
    [_widened_by_suffix, _widened_to_a_second_file, _widened_to_any_shared_call, _not_exempting_anything],
    ids=lambda fn: fn.__name__,
)
def test_a_widened_or_missing_exemption_is_caught(exempt):
    assert exemption_offences(exempt), exempt.__name__


MIRROR_PLANTS = {
    "mirror widened by suffix": (
        'path == tripwirePath ? text.replacingOccurrences',
        'path.hasSuffix("TripwireTests.swift") ? text.replacingOccurrences',
    ),
    "mirror widened by contains": (
        'path == tripwirePath ? text.replacingOccurrences',
        'path.contains("Tripwire") ? text.replacingOccurrences',
    ),
    "mirror exempts every file": ('path == tripwirePath ? text.replacingOccurrences', 'true ? text.replacingOccurrences'),
    "mirror path changed": (
        'tripwirePath = "GrowinTests/WorkspaceIsolation/ProductionTripwireTests.swift"',
        'tripwirePath = "GrowinTests/WorkspaceIsolation/OtherTests.swift"',
    ),
    "mirror call widened": ('".shared.string(for: .alpacaApiKey, scope: .workspace(.uk))"', '".shared"'),
}


@pytest.mark.parametrize("label", sorted(MIRROR_PLANTS))
def test_a_widened_swift_mirror_exemption_is_caught(label):
    old, new = MIRROR_PLANTS[label]
    assert old in MIRROR_TEXT, "the plant did not apply"
    assert mirror_exemption_offences(MIRROR_TEXT.replace(old, new, 1)), label


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
        "try KeychainStore.checkAccess(service: s, protectedService: s, testHost: false, site: .read)\n",
        "let flag = ProductionKeychainGuard.refuses(isProductionStore: true, testHost: false)\n",
    ):
        files = app_files()
        files[Path("Views/Sneaky.swift")] = sample
        assert any("Sneaky.swift" in item for item in app_wide_offences(files)), sample


def test_the_guard_inputs_named_only_in_comments_or_strings_are_not_findings():
    files = app_files()
    files[Path("Views/Fine.swift")] = (
        "// protectedService: and testHost: are guard inputs, SecItemDelete(x), SecKeychainFoo\n"
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
        "    try assertAccessAllowed(for: .read)\n"
        "    func inner() { SecItemDelete(q as CFDictionary) }\n"
        "    SecItemAdd(q as CFDictionary, nil)\n"
        "}\n"
    )
    found = sec_item_offences(read("x.swift", text), STORE_GUARD, frozenset({"outer", "inner"}))
    # The comment and the string are not calls. The nested function has no guard of its own.
    assert len(found) == 1 and "inner()" in found[0]
    # Without inner on the allow-list it is rejected for that reason too.
    assert any("allow-list" in item for item in sec_item_offences(read("x.swift", text), STORE_GUARD, frozenset({"outer"})))


def test_a_guard_after_the_call_or_inside_a_closure_is_not_a_guard():
    after = "func f() throws {\n    SecItemDelete(q as CFDictionary)\n    try assertAccessAllowed(for: .read)\n}\n"
    nested = "func f() throws {\n    run { try assertAccessAllowed(for: .read) }\n    SecItemDelete(q as CFDictionary)\n}\n"
    ok = "func f() throws {\n    try assertAccessAllowed(for: .read)\n    SecItemDelete(q as CFDictionary)\n}\n"
    owners = frozenset({"f"})
    assert sec_item_offences(read("x", after), STORE_GUARD, owners) != []
    assert sec_item_offences(read("x", nested), STORE_GUARD, owners) != []
    assert sec_item_offences(read("x", ok), STORE_GUARD, owners) == []


def test_opens_with_accepts_only_the_exact_first_statement():
    statement = "try assertAccessAllowed(for: .read)"
    assert opens_with("\n    try assertAccessAllowed(for: .read)\n    next()\n", statement)
    assert opens_with("try  assertAccessAllowed( for: .read ) ; next()", statement)
    for bad in (
        "let _ = false ? (try assertAccessAllowed(for: .read)) : ()\n",
        "if x { try assertAccessAllowed(for: .read) }\n",
        "try? assertAccessAllowed(for: .read)\n",
        "try assertAccessAllowed(for: .write)\n",
        "try assertAccessAllowed(for: .read) ? a : b\n",
        "x()\ntry assertAccessAllowed(for: .read)\n",
        "try assertAccessAllowed(for: .read)",
    ):
        assert not opens_with(bad, statement), bad


def test_a_pinned_body_ignores_layout_and_comments_but_nothing_else():
    source = read("x.swift", "func f() -> Bool {\n    // note\n    a  &&\n        b\n}\n")
    assert pinned(source, "f", "a && b") == []
    assert pinned(source, "f", "a || b") != []
    assert pinned(source, "g", "a && b") != []


def test_a_plant_whose_anchor_is_gone_fails_by_name_not_at_collection():
    broken = replaced("some source text", "an anchor that is gone", "x")
    assert isinstance(broken, BrokenPlant)
    assert "an anchor that is gone" in broken.message
    # Chained plants pass the marker through, so the final text still reports the first missing anchor.
    chained = replaced(broken, "some", "any")
    assert isinstance(chained, BrokenPlant) and chained.message == broken.message
    # A plant that applies is an ordinary string.
    assert replaced("some source text", "source", "other") == "some other text"
    assert not isinstance(replaced("some source text", "source", "other"), BrokenPlant)
