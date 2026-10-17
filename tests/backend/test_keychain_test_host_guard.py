"""Source scan for the production Keychain guard (issue #66, follow-ups of PR #573).

GrowinTests is app-hosted, so `xcodebuild test` runs the real Growin app with the operator's real
Keychain. `Growin/Security/ProductionKeychainGuard.swift` makes every Keychain call on the production
service fail closed in a test host. Swift is CI-only here, so these scans read the source and catch the
honest accident: someone moves, wraps or drops the guard, adds a second route to SecItem, strips the
service from a query, or disables a test that proves the guard.

Threat model: an honest but fallible author. A deliberately hostile edit (shadowing a symbol, a dynamic
lookup, rewriting a test body) is out of scope: it is visible in review, and Safety Guard and the operator
label gate every change under `Growin/Security`. Whether the tripwire and canary tests actually ran is a
separate CI follow-up (fail Swift CI if any of the 8 names is missing or not passed).

Each rule has an id, and each id has one planted break (a few for the rules with several accidents). A plant
must be caught by its rule and by nothing else: with that rule switched off the plant scans clean.

  R1  Only allow-listed functions may call SecItem, and each opens with the guard (first statement).
  R2  SecItem and SecKeychain appear only in KeychainStore.swift and LegacyT212KeychainCleanup.swift.
  R3  Each query builder carries `kSecAttrService ... : service`; an owner never calls `removeValue`,
      `filter` or `merge` on a query.
  R10 `kSecClass`, `kSecAttrService` and `kSecAttrAccount` appear only inside that file's query builder, so an
      inline query, a nil-ed service key or a second dictionary cannot be built in an owner.
  R4  TestHostDetection keeps its four env keys and the XCTestCase check; `refuses` stays `a && b`.
  R5  The cleanup refuses on the real process first (the complete opening condition and its return, in
      both entry points), and a refused delete ends before any defaults write.
  R6  The refusal log line interpolates only the code and the call-site kind.
  R7  The tripwire and the canaries keep their @Test names and plain @Test, a top-level
      `@Suite(.serialized)`, no `#if` or `@available`; the tripwire writes nothing; no assertion argument
      reads a credential.
  R9  None of the 8 named test bodies contains `return`, `withKnownIssue` or a stored closure
      (`let x = { ... }`), the three ways to keep every `#expect` in the text and stop it mattering.
  R11 `requireKeychain`, the helper that runs every canary body, keeps its exact (layout-normalised) body.
  R8  GrowinTests mentions SecItem or SecKeychain only in RawKeychain.swift.
"""

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from test_app_does_not_push_t212_keys import strip_swift_comments_and_strings
from test_swift_scan_bypasses import reduce_swift

REPO = Path(__file__).resolve().parents[2]
APP = REPO / "Growin"
TESTS = REPO / "GrowinTests"
ISOLATION = TESTS / "WorkspaceIsolation"

PATHS = {
    "store": APP / "Security" / "KeychainStore.swift",
    "cleanup": APP / "Security" / "LegacyT212KeychainCleanup.swift",
    "guard": APP / "Security" / "ProductionKeychainGuard.swift",
    "raw": ISOLATION / "RawKeychain.swift",
    "tripwire": ISOLATION / "ProductionTripwireTests.swift",
    "scope": ISOLATION / "KeychainScopeTests.swift",
    "legacy": ISOLATION / "LegacyT212KeychainCleanupTests.swift",
}
SEC_ITEM_FILES = {PATHS["store"].relative_to(REPO), PATHS["cleanup"].relative_to(REPO), PATHS["raw"].relative_to(REPO)}

SEC_ITEM_CALL = re.compile(r"(?<![A-Za-z0-9_])SecItem[A-Za-z]*\s*\(")
SEC_MENTION = re.compile(r"(?<![A-Za-z0-9_])(?:SecItem|SecKeychain)[A-Za-z]*")
COMPILE_OUT = re.compile(r"(?m)^\s*#(?:if|elseif|else|endif)\b|(?<![A-Za-z0-9_])@available(?![A-Za-z0-9_])")


# --------------------------------------------------------------------------------------------
# Reading Swift
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Func:
    name: str
    params: str
    start: int
    body_start: int
    body_end: int

    def body(self, text: str) -> str:
        return text[self.body_start : self.body_end]


@dataclass(frozen=True)
class Source:
    """`bare`: comments and string contents gone. `code`: comments gone, strings kept."""

    bare: str
    code: str
    errors: tuple[str, ...]


def read(text: str) -> Source:
    reduced = reduce_swift(text)
    return Source(reduced.bare, strip_swift_comments_and_strings(text, keep_strings=True), tuple(reduced.errors))


def match(text: str, start: int, opener: str, closer: str) -> int | None:
    depth = 0
    for index in range(start, len(text)):
        depth += text[index] == opener
        depth -= text[index] == closer
        if depth == 0:
            return index
    return None


def functions(text: str) -> list[Func]:
    found = []
    for hit in re.finditer(r"(?<![A-Za-z0-9_])func\s+(?P<name>[A-Za-z_]\w*)", text):
        open_paren = text.find("(", hit.end())
        close_paren = match(text, open_paren, "(", ")") if open_paren >= 0 else None
        open_brace = text.find("{", close_paren) if close_paren is not None else -1
        close_brace = match(text, open_brace, "{", "}") if open_brace >= 0 else None
        if close_brace is not None:
            found.append(Func(hit.group("name"), text[open_paren + 1 : close_paren], hit.start(), open_brace + 1, close_brace))
    return found


def enclosing(funcs: list[Func], position: int) -> Func | None:
    inside = [f for f in funcs if f.body_start <= position < f.body_end]
    return max(inside, key=lambda f: f.start) if inside else None


def norm(text: str) -> str:
    return re.sub(r"\s+", "", text)


def squash(text: str) -> str:
    return " ".join(text.split())


def opens_with(body: str, statement: str) -> bool:
    """The first statement of `body` is exactly `statement`, ended by a newline or `;` (layout aside).

    `try?`, an `if`, a `#if` or any other prefix fails, so the guard cannot be neutralised on its own line.
    """
    want, i = norm(statement), 0
    for char in want:
        while i < len(body) and body[i].isspace():
            i += 1
        if i >= len(body) or body[i] != char:
            return False
        i += 1
    return re.match(r"[ \t]*(?:;|\n)", body[i:]) is not None


def declaration_scope(bare: str, position: int) -> str:
    """`top` when the position is at module level, else `nested`."""
    return "top" if bare[:position].count("{") == bare[:position].count("}") else "nested"


# --------------------------------------------------------------------------------------------
# Rules. Each takes a Source and returns messages; `scan` prefixes the rule id.
# --------------------------------------------------------------------------------------------

STORE_OWNERS = {
    "rawData": "try assertAccessAllowed(for: .read)",
    "rawSet": "try assertAccessAllowed(for: .write)",
    "rawRemove": "try assertAccessAllowed(for: .delete)",
}
CLEANUP_OWNERS = {
    "deleteExactItem": "guard !ProductionKeychainGuard.refusesCurrentProcess(service: service) else {",
}
RAW_OWNERS = {"set": None, "remove": None, "data": None}


def owners_rule(owners: dict[str, str | None]):
    """R1: SecItem is called only from the allow-listed functions, each opening with its guard."""

    def rule(src: Source) -> list[str]:
        found = []
        funcs, code_funcs = functions(src.bare), functions(src.code)
        for call in SEC_ITEM_CALL.finditer(src.bare):
            owner = enclosing(funcs, call.start())
            if owner is None:
                found.append("a SecItem call outside any function")
            elif owner.name not in owners:
                found.append(f"SecItem in {owner.name}() is not on the allow-list {sorted(owners)}")
        for name, statement in owners.items():
            hits = [f for f in code_funcs if f.name == name]
            if len(hits) != 1:
                found.append(f"{name}() must exist exactly once")
            elif statement and not opens_with(hits[0].body(src.code), statement):
                found.append(f"{name}() must open with `{statement}`")
        return found

    return rule


def raw_init_rule(src: Source) -> list[str]:
    """R1 (RawKeychain): its only initializer starts with the test-prefix precondition."""
    if not re.search(r"init\(service candidate: String\)\s*\{\s*precondition\(\s*candidate\.hasPrefix\(Self\.testServicePrefix\)", src.code):
        return ["RawKeychain.init(service:) must start with the test-prefix precondition"]
    return []


def query_rule(builder: str, owners: list[str]):
    """R3: the builder has the service entry, and no owner strips, filters or merges a query."""

    def rule(src: Source) -> list[str]:
        found = []
        built = [f for f in functions(src.code) if f.name == builder]
        if len(built) != 1 or not re.search(r"kSecAttrService\s+as\s+String\s*:\s*service\s*,", built[0].body(src.code)):
            found.append(f"{builder}() must contain `kSecAttrService as String: service`")
        for func in functions(src.bare):
            if func.name in owners and re.search(r"\.\s*(?:removeValue|removeAll|filter|merge|reduce)\s*[({]", func.body(src.bare)):
                found.append(f"{func.name}() changes a query after it was built")
        return found

    return rule


def detection_rule(src: Source) -> list[str]:
    """R4: the env key list, the XCTestCase check and the one-line predicate."""
    found = []
    keys = re.search(r"static let environmentKeys\s*=\s*\[(.*?)\]", src.code, re.S)
    wanted = ["XCTestConfigurationFilePath", "XCTestBundlePath", "XCTestSessionIdentifier", "GROWIN_UI_TEST_HOST"]
    if keys is None or re.findall(r'"([^"]*)"', keys.group(1)) != wanted:
        found.append(f"environmentKeys must be exactly {wanted}")
    if not re.search(r'NSClassFromString\(\s*"XCTestCase"\s*\)\s*!=\s*nil', src.code):
        found.append("isTestHost must still check the XCTestCase class")
    predicate = [f for f in functions(src.code) if f.name == "refuses" and "isProductionStore" in f.params]
    if len(predicate) != 1 or norm(predicate[0].body(src.code)) != "isProductionStore&&testHost":
        found.append("refuses(isProductionStore:testHost:) must be exactly `isProductionStore && testHost`")
    return found


PUBLIC_REFUSAL = "if ProductionKeychainGuard.refusesCurrentProcess(service: service) { return refusedOutcome(log: log) }"
EXPLICIT_REFUSAL = (
    "if ProductionKeychainGuard.refusesCurrentProcess(service: service) "
    "|| ProductionKeychainGuard.refuses(service: service, protectedService: protectedService, testHost: testHost) "
    "{ return refusedOutcome(log: log) }"
)


def cleanup_refusal_rule(src: Source) -> list[str]:
    """R5a: both runOnce entry points open with their COMPLETE refusal: the whole condition and its return.

    The explicit overload may only ADD a refusal (`||`); `&&` would let an injected `testHost: false` lift
    the real one.
    """
    entries = [f for f in functions(src.code) if f.name == "runOnce"]
    found = []
    if len(entries) != 2:
        return ["there must be exactly two runOnce entry points"]
    for func in entries:
        want = EXPLICIT_REFUSAL if "protectedService" in func.params else PUBLIC_REFUSAL
        if not norm(func.body(src.code)).startswith(norm(want)):
            found.append(f"a runOnce entry point must open with `{want}`")
    return found


def cleanup_terminal_rule(src: Source) -> list[str]:
    """R5b: a refused delete returns before any defaults write."""
    work = [f for f in functions(src.code) if f.name == "performRun"]
    if len(work) != 1:
        return ["performRun() must exist exactly once"]
    body = norm(work[0].body(src.code))
    terminal = body.find(norm("if status == refusedUnderTestsStatus { return refusedOutcome(log: log) }"))
    writes = [i for i in (body.find("defaults.removeObject("), body.find("defaults.set(")) if i >= 0]
    if terminal < 0 or not writes or terminal > min(writes):
        return ["performRun() must return on a refused delete before it writes any defaults"]
    return []


LOG_CALL = re.compile(r'(?:logger\s*\.\s*\w+|(?<![A-Za-z0-9_.])log)\s*\(\s*(?P<literal>"(?:[^"\\\n]|\\.)*")')
LOG_ALLOWED = {"refusalCode", "ProductionKeychainGuard.refusalCode", "site.rawValue"}


def log_rule(every_logger_call: bool):
    """R6: a refusal log line interpolates only the code and the call-site kind, never an item detail."""

    def rule(src: Source) -> list[str]:
        found = []
        for call in LOG_CALL.finditer(src.code):
            literal = call.group("literal")
            if not every_logger_call and "refus" not in literal.lower():
                continue
            for expression in re.findall(r"\\\(\s*([^,)]*)", literal):
                if expression.strip() not in LOG_ALLOWED:
                    found.append(f"a refusal log line interpolates `{expression.strip()}`")
        return found

    return rule


SILENCERS = re.compile(
    r"(?<![A-Za-z0-9_])(?:return|withKnownIssue)(?![A-Za-z0-9_])"
    r"|(?<![A-Za-z0-9_])(?:let|var)\s+\w+\s*(?::[^=\n]+)?=\s*\{"
)


def silenced_rule(names: tuple[str, ...]):
    """R9: no named test body contains `return`, `withKnownIssue` or a stored closure.

    Each keeps every `#expect` in the text while stopping it from mattering: an early return, a known-issue
    wrapper that turns a failure into a pass, or checks parked in a closure nobody calls.
    """

    def rule(src: Source) -> list[str]:
        found = []
        for func in functions(src.bare):
            if func.name in names:
                found += [
                    f"{func.name}() contains `{squash(hit.group(0))}`, which can silence its assertions"
                    for hit in SILENCERS.finditer(func.body(src.bare))
                ]
        return found

    return rule


SERVICE_SYMBOLS = re.compile(r"(?<![A-Za-z0-9_])(?:kSecClass|kSecAttrService|kSecAttrAccount)(?![A-Za-z0-9_])")


def service_symbols_rule(builder: str):
    """R10: the class, service and account keys appear only inside the file's query builder."""

    def rule(src: Source) -> list[str]:
        funcs = functions(src.bare)
        found = []
        for hit in SERVICE_SYMBOLS.finditer(src.bare):
            owner = enclosing(funcs, hit.start())
            if owner is None or owner.name != builder:
                where = f"{owner.name}()" if owner else "no function"
                found.append(f"{hit.group(0)} is used in {where}, outside {builder}()")
        return found

    return rule


REQUIRE_KEYCHAIN_BODY = """
do {
    try body()
} catch KeychainStoreError.status(let status) {
    Issue.record("Keychain blocked with OSStatus \\(status)", sourceLocation: sourceLocation)
} catch {
    Issue.record("Unexpected error: \\(error)", sourceLocation: sourceLocation)
}
"""


def require_keychain_rule(src: Source) -> list[str]:
    """R11: the helper that runs every canary body keeps its exact body (layout aside)."""
    helper = [f for f in functions(src.code) if f.name == "requireKeychain"]
    if len(helper) != 1 or norm(helper[0].body(src.code)) != norm(REQUIRE_KEYCHAIN_BODY):
        return ["requireKeychain() must run its body, record every error and nothing else"]
    return []


TRIPWIRE_TESTS = (
    "thisProcessIsATestHost",
    "theSharedStoreRefusesARead",
    "theSignersReportNotConfigured",
    "theCleanupRefusesADeleteOnTheProductionService",
)
SCOPE_CANARIES = (
    "aStoreNeverSeesOrChangesAnotherServicesItem",
    "aStoreCleanUpAndLegacyCallsLeaveAnotherServicesItemsAlone",
    "rawKeychainOnOneServiceCannotSeeOrRemoveAnother",
)
LEGACY_CANARIES = ("theRealDeleteNeverTouchesAnotherServicesItem",)
TEST_DECLARATION = re.compile(
    r"@Test(?![A-Za-z0-9_])(?P<traits>\s*\((?:[^()]|\((?:[^()]|\([^()]*\))*\))*\))?\s*func\s+(?P<name>\w+)"
)


def suite_rule(names: tuple[str, ...], exact: bool):
    """R7: the expected @Test names, plain (no traits), in one top-level `@Suite(.serialized)`, not compiled out."""

    def rule(src: Source) -> list[str]:
        found = []
        declared = {}
        for hit in TEST_DECLARATION.finditer(src.bare):
            declared.setdefault(hit.group("name"), []).append(bool(hit.group("traits")))
        for name in names:
            if name not in declared:
                found.append(f"the @Test {name} is missing")
            elif len(declared[name]) != 1 or declared[name][0]:
                found.append(f"{name} must be one plain @Test with no traits")
        if exact and set(declared) != set(names):
            found.append(f"the @Test names are {sorted(declared)}, expected exactly {sorted(names)}")
        suites = list(re.finditer(r"@Suite(?![A-Za-z0-9_])", src.bare))
        suite_text = re.search(r"@Suite(?![A-Za-z0-9_])\s*(\([^)]*\))?", src.code)
        if len(suites) != 1 or suite_text is None or norm(suite_text.group(1) or "") != "(.serialized)":
            found.append("there must be exactly one `@Suite(.serialized)`")
        elif declaration_scope(src.bare, suites[0].start()) != "top":
            found.append("the suite must be declared at top level")
        for hit in COMPILE_OUT.finditer(src.bare):
            found.append(f"`{squash(hit.group(0))}` can compile the tests out")
        return found

    return rule


TRIPWIRE_WRITES = re.compile(
    r"\.\s*(?:set|remove|removeLegacyFlatItem|createIdentityIfNeeded|adoptLegacyKey|sign\w*|migrate\w*)\s*\("
    r"|(?<![A-Za-z0-9_])(?:KeychainStorage|wrappedValue|projectedValue|RawKeychain|runOnce|SecItem\w*)(?![A-Za-z0-9_])"
)


def tripwire_readonly_rule(src: Source) -> list[str]:
    """R7 (tripwire): it only reads and expects refusals. No write, migration, signing or property wrapper."""
    return [f"the tripwire uses `{squash(hit.group(0))}`" for hit in TRIPWIRE_WRITES.finditer(src.bare)]


CREDENTIAL_READ = re.compile(r"\.\s*(?:string|data)\s*\(|(?<![A-Za-z0-9_])(?:rawData|legacyFlatData)(?![A-Za-z0-9_])")


def assertion_reads(text: str) -> list[str]:
    bare = reduce_swift(text).bare
    found = []
    for hit in re.finditer(r"#(?:expect|require)\s*\(", bare):
        close = match(bare, hit.end() - 1, "(", ")")
        for read_ in CREDENTIAL_READ.finditer(bare[hit.end() : close] if close else ""):
            found.append(f"an assertion reads a credential (`{squash(read_.group(0))}`) and would print it on failure")
    return found


def secrets_rule(names: tuple[str, ...] | None):
    """R7 (secrets): no #expect or #require argument reads a credential. Whole file for the tripwire, the canaries for the rest."""

    def rule(src: Source) -> list[str]:
        if names is None:
            return assertion_reads(src.code)
        found = []
        for func in functions(src.code):
            if func.name in names:
                found += assertion_reads(func.body(src.code))
        return found

    return rule


RULES = {
    "store": [
        ("R1", owners_rule(STORE_OWNERS)),
        ("R3", query_rule("baseQuery", list(STORE_OWNERS))),
        ("R10", service_symbols_rule("baseQuery")),
    ],
    "cleanup": [
        ("R1", owners_rule(CLEANUP_OWNERS)),
        ("R3", query_rule("exactQuery", list(CLEANUP_OWNERS))),
        ("R10", service_symbols_rule("exactQuery")),
        ("R5a", cleanup_refusal_rule),
        ("R5b", cleanup_terminal_rule),
        ("R6", log_rule(every_logger_call=False)),
    ],
    "guard": [("R4", detection_rule), ("R6", log_rule(every_logger_call=True))],
    "raw": [
        ("R1", lambda src: owners_rule(RAW_OWNERS)(src) + raw_init_rule(src)),
        ("R3", query_rule("query", list(RAW_OWNERS))),
        ("R10", service_symbols_rule("query")),
    ],
    "tripwire": [
        ("R7", suite_rule(TRIPWIRE_TESTS, exact=True)),
        ("R7w", tripwire_readonly_rule),
        ("R7s", secrets_rule(None)),
        ("R9", silenced_rule(TRIPWIRE_TESTS)),
    ],
    "scope": [
        ("R7", suite_rule(SCOPE_CANARIES, exact=False)),
        ("R7s", secrets_rule(SCOPE_CANARIES)),
        ("R9", silenced_rule(SCOPE_CANARIES)),
        ("R11", require_keychain_rule),
    ],
    "legacy": [
        ("R7", suite_rule(LEGACY_CANARIES, exact=False)),
        ("R7s", secrets_rule(LEGACY_CANARIES)),
        ("R9", silenced_rule(LEGACY_CANARIES)),
    ],
}


def scan(kind: str, text: str, skip: frozenset[str] = frozenset()) -> list[str]:
    src = read(text)
    found = [f"unreadable: {error}" for error in src.errors]
    for rule_id, rule in RULES[kind]:
        if rule_id not in skip:
            found += [f"{rule_id}: {message}" for message in rule(src)]
    return found


def tree_scan(files: dict[Path, str], skip: frozenset[str] = frozenset()) -> list[str]:
    """R2 (app) and R8 (tests): SecItem and SecKeychain only in the three files that own them."""
    found = []
    for relative, text in sorted(files.items()):
        rule_id = "R2" if relative.parts[0] == "Growin" else "R8"
        if rule_id in skip or relative in SEC_ITEM_FILES:
            continue
        found += [f"{rule_id}: {relative} mentions {hit.group(0)}" for hit in SEC_MENTION.finditer(reduce_swift(text).bare)]
    return found


def real_tree() -> dict[Path, str]:
    return {p.relative_to(REPO): p.read_text(encoding="utf-8") for root in (APP, TESTS) for p in sorted(root.rglob("*.swift"))}


TEXTS = {kind: path.read_text(encoding="utf-8") for kind, path in PATHS.items()}


# --------------------------------------------------------------------------------------------
# The real tree is clean
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", sorted(RULES))
def test_the_real_file_passes_every_rule(kind):
    assert scan(kind, TEXTS[kind]) == []


def test_the_real_tree_mentions_sec_item_only_where_allowed():
    files = real_tree()
    assert PATHS["store"].relative_to(REPO) in files  # positive control: the scan sees the app
    assert tree_scan(files) == []


# --------------------------------------------------------------------------------------------
# Break-proofs: one planted accident per rule. Each is caught by its rule and by no other.
# --------------------------------------------------------------------------------------------

STORE_QUERY_ENTRY = "            Security.kSecAttrService as String: service,\n"
CLEANUP_GUARD = (
    "        guard !ProductionKeychainGuard.refusesCurrentProcess(service: service) else {\n"
    "            ProductionKeychainGuard.recordRefusal(site: .delete)\n"
    "            return refusedUnderTestsStatus\n"
    "        }\n"
)
CLEANUP_EXPLICIT = (
    "        if ProductionKeychainGuard.refusesCurrentProcess(service: service)\n"
    "            || ProductionKeychainGuard.refuses("
)
CLEANUP_TERMINAL = "                if status == refusedUnderTestsStatus { return refusedOutcome(log: log) }\n"
CLEANUP_LOG = 'log("legacy cleanup refused: \\(ProductionKeychainGuard.refusalCode)")'
GUARD_LOG = 'site=\\(site.rawValue, privacy: .public)")'
CANARY_STRING = "            #expect(aStringIsNil)\n"

# (rule id, file kind, anchor in the real file, replacement). A tuple of cases is one plant with several edits.
PLANTS = {
    "R1 a try? on the guard": ("R1", "store", "        try assertAccessAllowed(for: .delete)\n", "        try? assertAccessAllowed(for: .delete)\n"),
    "R1 the guard inside an if": (
        "R1",
        "store",
        "        try assertAccessAllowed(for: .delete)\n",
        "        if true { try assertAccessAllowed(for: .delete) }\n",
    ),
    "R1 the guard inside #if": (
        "R1",
        "store",
        "        try assertAccessAllowed(for: .delete)\n",
        "        #if !DEBUG\n        try assertAccessAllowed(for: .delete)\n        #endif\n",
    ),
    "R1 a new SecItem function": (
        "R1",
        "store",
        "    private func baseQuery(account: String)",
        "    func probe(account: String) -> OSStatus {\n        Security.SecItemDelete(baseQuery(account: account) as CFDictionary)\n    }\n\n"
        "    private func baseQuery(account: String)",
    ),
    "R1 the cleanup delete loses its guard": ("R1", "cleanup", CLEANUP_GUARD, ""),
    "R1 RawKeychain gains a SecItem function": (
        "R1",
        "raw",
        "    private func query(",
        "    func probe(account: String) -> OSStatus {\n        Security.SecItemDelete(query(account) as CFDictionary)\n    }\n\n    private func query(",
    ),
    "R1 RawKeychain loses its precondition": (
        "R1",
        "raw",
        "        precondition(\n            candidate.hasPrefix(Self.testServicePrefix) && candidate.count > Self.testServicePrefix.count,\n"
        '            "RawKeychain only works on a test-prefixed service")\n',
        "",
    ),
    "R3 baseQuery loses the service": ("R3", "store", STORE_QUERY_ENTRY, ""),
    "R3 exactQuery loses the service": ("R3", "cleanup", STORE_QUERY_ENTRY, ""),
    "R3 RawKeychain.query loses the service": ("R3", "raw", STORE_QUERY_ENTRY, ""),
    "R3 an owner strips the service": (
        "R3",
        "store",
        "        query[Security.kSecMatchLimit as String] = Security.kSecMatchLimitOne\n",
        "        query[Security.kSecMatchLimit as String] = Security.kSecMatchLimitOne\n        query.removeValue(forKey: \"svce\")\n",
    ),
    "R4 an env key dropped": ("R4", "guard", '        "GROWIN_UI_TEST_HOST",\n', ""),
    "R4 the XCTestCase check dropped": ("R4", "guard", 'Foundation.NSClassFromString("XCTestCase") != nil', "false"),
    "R4 the predicate weakened": ("R4", "guard", "isProductionStore && testHost", "isProductionStore || testHost"),
    "R5a the explicit overload trusts injected inputs": ("R5a", "cleanup", CLEANUP_EXPLICIT, "        if ProductionKeychainGuard.refuses("),
    "R5b a refused delete is no longer terminal": ("R5b", "cleanup", CLEANUP_TERMINAL, ""),
    "R6 the cleanup refusal log names the service": (
        "R6",
        "cleanup",
        CLEANUP_LOG,
        'log("legacy cleanup refused: \\(ProductionKeychainGuard.refusalCode) \\(service)")',
    ),
    "R6 the guard refusal log names the account": ("R6", "guard", GUARD_LOG, 'site=\\(site.rawValue, privacy: .public) \\(account)")'),
    "R7 a tripwire test renamed": (
        "R7",
        "tripwire",
        "@Test func theSharedStoreRefusesARead() {",
        "@Test func theSharedStoreRefusesAReadCopy() {",
    ),
    "R7 a trait on a tripwire test": (
        "R7",
        "tripwire",
        "@Test func theSignersReportNotConfigured() {",
        '@Test(.disabled("x")) func theSignersReportNotConfigured() {',
    ),
    "R7 the tripwire suite disabled": ("R7", "tripwire", "@Suite(.serialized)", '@Suite(.disabled("x"))'),
    "R7 a canary dropped its @Test": (
        "R7",
        "scope",
        "@Test func aStoreNeverSeesOrChangesAnotherServicesItem() {",
        "func aStoreNeverSeesOrChangesAnotherServicesItem() {",
    ),
    "R7 the canary suite gains .disabled": ("R7", "scope", "@Suite(.serialized)", "@Suite(.serialized, .disabled())"),
    "R7 the legacy canary suite gains a second @Suite": ("R7", "legacy", "@Suite(.serialized)", "@Suite(.disabled())\n@Suite(.serialized)"),
    "R7 an @available above the canary suite": ("R7", "legacy", "@Suite(.serialized)", "@available(*, unavailable)\n@Suite(.serialized)"),
    "R7w the tripwire writes": (
        "R7w",
        "tripwire",
        "        #expect(refusal == Self.refused)\n",
        "        #expect(refusal == Self.refused)\n        try? KeychainStore.shared.set(\"x\", for: .alpacaApiKey, scope: .workspace(.uk))\n",
    ),
    "R7s the tripwire reads a credential in #expect": (
        "R7s",
        "tripwire",
        "        #expect(refusal == Self.refused)\n",
        "        #expect(try KeychainStore.shared.string(for: .alpacaApiKey, scope: .workspace(.uk)) == nil)\n",
    ),
    "R7s a canary reads a credential in #expect": (
        "R7s",
        "scope",
        CANARY_STRING,
        "            #expect(try a.string(for: .alpacaApiKey, scope: .workspace(.uk)) == nil)\n",
    ),
}
PLANTS.update(
    {
        "R5a the explicit overload's || flipped to &&": (
            "R5a",
            "cleanup",
            "            || ProductionKeychainGuard.refuses(",
            "            && ProductionKeychainGuard.refuses(",
        ),
        "R9 a canary assertion inside withKnownIssue": (
            "R9",
            "legacy",
            "        #expect(removedAll)\n",
            '        withKnownIssue("flaky", isIntermittent: true) { #expect(removedAll) }\n',
        ),
        "R9 a tripwire #expect inside withKnownIssue": (
            "R9",
            "tripwire",
            "        #expect(refusal == Self.refused)\n",
            '        withKnownIssue("flaky", isIntermittent: true) { #expect(refusal == Self.refused) }\n',
        ),
        "R9 a CI guard with an early return in the tripwire": (
            "R9",
            "tripwire",
            "        #expect(refusal == Self.refused)\n",
            '        guard ProcessInfo.processInfo.environment["CI"] == nil else { return }\n        #expect(refusal == Self.refused)\n',
        ),
        "R9 a bare return in the legacy canary": (
            "R9",
            "legacy",
            "        try b.seed(Cleanup.legacyAccounts)\n",
            "        return\n        try b.seed(Cleanup.legacyAccounts)\n",
        ),
        "R9 checks moved into a closure nobody calls": (
            "R9",
            "tripwire",
            "        #expect(TestHostDetection.isTestHost())\n        #expect(ProductionKeychainGuard.refusesCurrentProcess(service: Self.protectedName))\n",
            "        let checks = {\n            #expect(TestHostDetection.isTestHost())\n"
            "            #expect(ProductionKeychainGuard.refusesCurrentProcess(service: Self.protectedName))\n        }\n",
        ),
        "R10 an inline account-only query in rawRemove": (
            "R10",
            "store",
            "        let status = Security.SecItemDelete(baseQuery(account: account) as CFDictionary)\n",
            "        let status = Security.SecItemDelete(\n"
            "            [Security.kSecClass as String: Security.kSecClassGenericPassword, Security.kSecAttrAccount as String: account]\n"
            "                as CFDictionary)\n",
        ),
        "R10 the service key set to nil in rawData": (
            "R10",
            "store",
            "        query[Security.kSecMatchLimit as String] = Security.kSecMatchLimitOne\n",
            "        query[Security.kSecMatchLimit as String] = Security.kSecMatchLimitOne\n        query[Security.kSecAttrService as String] = nil\n",
        ),
        "R10 an inline query in RawKeychain.remove": (
            "R10",
            "raw",
            "        Security.SecItemDelete(query(account) as CFDictionary)\n",
            "        Security.SecItemDelete(\n"
            "            [Security.kSecClass as String: Security.kSecClassGenericPassword, Security.kSecAttrAccount as String: account]\n"
            "                as CFDictionary)\n",
        ),
        "R11 requireKeychain uses try? on the body": ("R11", "scope", "            try body()\n", "            try? body()\n"),
        "R11 requireKeychain prints instead of recording": (
            "R11",
            "scope",
            'Issue.record("Unexpected error: \\(error)", sourceLocation: sourceLocation)',
            'print("Unexpected error: \\(error)")',
        ),
        "R11 requireKeychain has an empty catch-all": (
            "R11",
            "scope",
            '            Issue.record("Unexpected error: \\(error)", sourceLocation: sourceLocation)\n',
            "",
        ),
        "R11 requireKeychain no longer calls the body": ("R11", "scope", "            try body()\n", "            _ = body\n"),
    }
)

# R7 plants that wrap the whole file need no anchor.
WHOLE_FILE_PLANTS = {
    "R7 the tripwire inside #if false": ("R7", "tripwire", "#if false\n{text}#endif\n"),
    "R7 the canary file inside #if false": ("R7", "scope", "#if false\n{text}#endif\n"),
    "R7 the canary suite nested in an enum": ("R7", "legacy", "enum Holder {\n{text}}\n"),
}


def plant(kind: str, anchor: str | None, new: str) -> str:
    text = TEXTS[kind]
    if anchor is None:
        return new.replace("{text}", text)
    assert anchor in text, f"the plant's anchor is gone from {kind}, update the plant: {anchor!r}"
    return text.replace(anchor, new, 1)


@pytest.mark.parametrize("label", sorted(PLANTS))
def test_each_planted_accident_is_caught_by_its_rule_alone(label):
    rule, kind, anchor, new = PLANTS[label]
    planted = plant(kind, anchor, new)
    assert planted != TEXTS[kind]
    assert any(m.startswith(f"{rule}:") for m in scan(kind, planted)), f"{rule} missed: {label}"
    assert scan(kind, planted, skip=frozenset({rule})) == [], f"{label} is caught by a rule other than {rule}"


@pytest.mark.parametrize("label", sorted(WHOLE_FILE_PLANTS))
def test_each_planted_wrapper_is_caught_by_its_rule_alone(label):
    rule, kind, template = WHOLE_FILE_PLANTS[label]
    planted = plant(kind, None, template)
    assert any(m.startswith(f"{rule}:") for m in scan(kind, planted))
    assert scan(kind, planted, skip=frozenset({rule})) == []


def test_a_new_sec_item_mention_in_the_app_is_caught_by_r2_alone():
    files = real_tree()
    files[Path("Growin/Util/Peek.swift")] = "func peek() { _ = SecItemCopyMatching([:] as CFDictionary, nil) }\n"
    assert any(m.startswith("R2:") for m in tree_scan(files))
    assert tree_scan(files, skip=frozenset({"R2"})) == []


def test_a_sec_item_mention_in_another_test_file_is_caught_by_r8_alone():
    files = real_tree()
    files[Path("GrowinTests/WorkspaceIsolation/OtherTests.swift")] = "func wipe() { _ = SecItemDelete([:] as CFDictionary) }\n"
    assert any(m.startswith("R8:") for m in tree_scan(files))
    assert tree_scan(files, skip=frozenset({"R8"})) == []


def test_the_rules_leave_comments_strings_and_switch_patterns_alone():
    files = real_tree()
    files[Path("Growin/Util/Fine.swift")] = (
        '// SecItemDelete( is only named in a comment\nlet note = "SecKeychainFoo SecItemAdd("\n'
        "func f(status: Int32) -> Bool {\n    switch status {\n    case errSecSuccess: return true\n    default: return false\n    }\n}\n"
    )
    assert tree_scan(files) == []
    assert assertion_reads('// #expect(try a.string(b) == nil)\nlet s = "#expect(a.data(b))"\n#expect(isNil)\n') == []
    assert assertion_reads("#expect(try a.string(for: .x, scope: .y) == nil)\n")
    assert assertion_reads("#require(try raw.data(account: a) != nil)\n")
