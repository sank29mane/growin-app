"""Closes the two #566 P3 scan bypasses, with scans that run in CI without Xcode.

1. Aliasing the shared Keychain store. A scan for `KeychainStore.shared.<call>` is dodged by
   `let s = KeychainStore.shared` and then `s.set(...)`. The rule here does not follow calls. It
   forbids holding the store (or its type) as a value:
   - In the app, only four Security files may name `KeychainStore` at all. Everywhere else the
     store cannot be aliased because the type cannot be written. The two signers get the store
     injected and are pinned to one `store: .shared` each.
   - In every scanned file, `KeychainStore.shared` must be followed by a member access, and
     `KeychainStore.self`, `KeychainStore.init`, a typealias of the type and a `.shared` default
     for a KeychainStore-typed value are all refused.
   - GrowinTests gets the alias rules only. Tests build their own `KeychainStore(service:)` and may
     name the type, but never reach the shared (production) store in any form.
2. Launching a UI-test app without the suppression key. The `GrowinTests` scan looked for
   `XCUIApplication(` and `.launch()` only. Here GrowinUITests may name `XCUIApplication` in
   `LaunchSupport.swift` and nowhere else (so `.init()`, `(bundleIdentifier:)`, `(url:)`, a typealias
   and `.self` are all caught by the one rule), may not touch `launchEnvironment` (a test could
   clear the key), and may not call `.launch()` or `.activate()` unless the file also gets its app
   from `GrowinLaunch.makeApp()`. The helper is pinned to its exact shape.

The GrowinUITests target compiles `GrowinUITests/` only: it is a file-system-synchronized group with
no explicit sources and no exception sets. `tests/macos-ui/` is not in the Xcode project, so it is
not compiled and not scanned. A test below fails if that membership changes.
"""

import re
from pathlib import Path

from test_app_does_not_push_t212_keys import strip_swift_comments_and_strings

REPO = Path(__file__).resolve().parents[2]
APP = REPO / "Growin"
APP_TESTS = REPO / "GrowinTests"
UI_TESTS = REPO / "GrowinUITests"
PROJECT = REPO / "Growin.xcodeproj" / "project.pbxproj"
LAUNCH_MIGRATIONS = APP / "Security" / "LaunchMigrations.swift"

# --------------------------------------------------------------------------------------------
# 1. KeychainStore aliasing
# --------------------------------------------------------------------------------------------

# The only app files that may name the `KeychainStore` type: the definition, the launch migration
# steps, and the two signers (which take the store as an injected argument).
KEYCHAIN_STORE_NAMERS = frozenset(
    {
        Path("Security") / "KeychainStore.swift",
        Path("Security") / "LaunchMigrations.swift",
        Path("Security") / "LocalApprovalSigner.swift",
        Path("Security") / "SecureEnclaveApprovalSigner.swift",
    }
)
# Exact number of `store: .shared` arguments per app file. Anything else is a new door to the store.
STORE_ARGUMENT_PINS = {
    Path("Security") / "LocalApprovalSigner.swift": 1,
    Path("Security") / "SecureEnclaveApprovalSigner.swift": 1,
}

# `(?<!["...])` skips a string needle such as `line.contains("KeychainStore.shared.")`: the scans
# in GrowinTests spell the names inside strings. Code never has a quote right before the name.
_NOT_AFTER_IDENT_OR_QUOTE = r'(?<![A-Za-z0-9_"])'
_NOT_AFTER_IDENT = r"(?<![A-Za-z0-9_])"
_NOT_BEFORE_IDENT = r"(?![A-Za-z0-9_])"

KEYCHAIN_STORE_NAME = re.compile(_NOT_AFTER_IDENT + "KeychainStore" + _NOT_BEFORE_IDENT)
ALIAS_PATTERNS = (
    (
        # `KeychainStore.shared` not directly followed by `.member`. `.self` is not a member.
        "KeychainStore.shared held as a value",
        re.compile(
            _NOT_AFTER_IDENT_OR_QUOTE + r"KeychainStore\s*\.\s*shared(?!\s*\.\s*(?!self\b)[A-Za-z_])"
        ),
    ),
    (
        "KeychainStore type taken as a value",
        re.compile(_NOT_AFTER_IDENT_OR_QUOTE + r"KeychainStore\s*\.\s*(?:self|init)" + _NOT_BEFORE_IDENT),
    ),
    (
        "typealias of KeychainStore",
        re.compile(r"\btypealias\s+\w+\s*(?:<[^>]*>)?\s*=\s*KeychainStore" + _NOT_BEFORE_IDENT),
    ),
    # `: KeychainStore = .shared`, `var s: KeychainStore { .shared }` and the rest of the implicit
    # forms are caught by `implicit_shared_uses`, which reads code with strings removed, so a test
    # that plants such a line inside a string literal is not a finding.
)
# An argument or default written `store: .shared`. Read on code with strings removed: a planted
# line inside a test string is not an argument.
STORE_ARGUMENT = re.compile(_NOT_AFTER_IDENT + r"store\s*:\s*\.shared" + _NOT_BEFORE_IDENT)

# `.shared` with no type in front of it takes its type from context, so the scan cannot tell a
# KeychainStore from anything else: `Self.cleanUp(.shared)`, `run(on: .shared)`, `store = .shared`,
# `var s: KeychainStore! = .shared`, `{ .shared }`, `return .shared`. The only implicit `.shared`
# allowed is the KeychainScope enum case, in these four shapes. They are removed before the scan.
KEYCHAIN_SCOPE_CONTEXTS = re.compile(
    r"scope\s*:\s*\.shared(?![A-Za-z0-9_])"
    r"|[=!]=\s*\.shared(?![A-Za-z0-9_])"
    r"|\ballows\s*\(\s*\.shared(?![A-Za-z0-9_])"
    r"|\bcase\s+\.shared(?![A-Za-z0-9_])"
)
DOT_SHARED = re.compile(r"\.\s*shared(?![A-Za-z0-9_])")
STORE_LABEL_BEFORE = re.compile(r"(?<![A-Za-z0-9_])store\s*:\s*$")
# A keyword before `.shared` still takes an implicit member (`return .shared`, `{ _ in .shared }`).
KEYWORDS_BEFORE_IMPLICIT = frozenset({"return", "in", "else", "try", "await", "throw"})
EXTENSION_OF_STORE = re.compile(r"\bextension\s+KeychainStore" + _NOT_BEFORE_IDENT)
PRODUCTION_SERVICE = re.compile(_NOT_AFTER_IDENT + r"productionService" + _NOT_BEFORE_IDENT)


def implicit_shared_uses(code: str) -> list[str]:
    """Each `.shared` written without a type in front of it, other than the KeychainScope case.

    A `.shared` is a member access, and fine here, when the previous non-space character is part of
    an identifier or is `)` or `]`. Anything else (`(`, `,`, `=`, `{`, `:`, a line start) or a
    keyword such as `return` means the type comes from context. A `store: .shared` argument is
    left to the pin check.
    """
    code = KEYCHAIN_SCOPE_CONTEXTS.sub(" ", code)
    found = []
    for match in DOT_SHARED.finditer(code):
        before = code[: match.start()].rstrip()
        if STORE_LABEL_BEFORE.search(before):
            continue
        last = before[-1:]
        if last and (last.isalnum() or last in "_)]"):
            word = re.search(r"([A-Za-z_][A-Za-z0-9_]*)$", before)
            if not (word and word.group(1) in KEYWORDS_BEFORE_IMPLICIT):
                continue
        context = " ".join(before[-24:].split())
        found.append(f"implicit .shared (type from context): {context} .shared")
    return found


def keychain_alias_offences(
    text: str,
    store_argument_pin: int = 0,
    implicit_shared: bool = True,
    allow_extension: bool = False,
) -> list[str]:
    """Ways one Swift file holds `KeychainStore.shared`, or the type, as a value.

    Backticks are removed first, so KeychainStore.`shared` is read as KeychainStore.shared.
    """
    code = strip_swift_comments_and_strings(text, keep_strings=True).replace("`", "")
    bare = strip_swift_comments_and_strings(text).replace("`", "")
    found = []
    for label, pattern in ALIAS_PATTERNS:
        for match in pattern.finditer(code):
            found.append(f"{label}: {' '.join(match.group(0).split())}")
    arguments = len(STORE_ARGUMENT.findall(bare))
    if arguments != store_argument_pin:
        found.append(f"`store: .shared` appears {arguments}x, allowed {store_argument_pin}")
    if implicit_shared:
        found += implicit_shared_uses(bare)
    if not allow_extension and EXTENSION_OF_STORE.search(bare):
        found.append("extension KeychainStore outside KeychainStore.swift")
    return found


def app_keychain_offences(
    root: Path,
    namers: frozenset[Path] = KEYCHAIN_STORE_NAMERS,
    pins: dict[Path, int] = STORE_ARGUMENT_PINS,
) -> list[str]:
    """App files that name KeychainStore without being allow-listed, plus every alias offence.

    The implicit `.shared` rule runs on the allow-listed files only: every other app file cannot
    name the type, and uses `.shared` legitimately for URLSession and the signer router.
    """
    found = []
    for path in sorted(root.rglob("*.swift")):
        relative = path.relative_to(root)
        text = path.read_text(encoding="utf-8")
        code = strip_swift_comments_and_strings(text, keep_strings=True)
        if relative not in namers and KEYCHAIN_STORE_NAME.search(code):
            found.append(f"{relative}: names KeychainStore and is not on the allow-list")
        offences = keychain_alias_offences(
            text,
            pins.get(relative, 0),
            implicit_shared=relative in namers,
            allow_extension=relative == Path("Security") / "KeychainStore.swift",
        )
        found += [f"{relative}: {item}" for item in offences]
    return found


def keychain_test_text_offences(text: str) -> list[str]:
    """One test file: the alias rules, any implicit `.shared`, and the production service name.

    `KeychainStore(service: KeychainStore.productionService)` is the shared store under another name,
    so a test may not name `productionService` at all.
    """
    found = keychain_alias_offences(text)
    if PRODUCTION_SERVICE.search(strip_swift_comments_and_strings(text)):
        found.append("names productionService")
    return found


def keychain_offences_in_test_sources(root: Path) -> list[str]:
    """Test files that hold the shared store or the type as a value. Naming the type is allowed."""
    found = []
    for path in sorted(root.rglob("*.swift")):
        text = path.read_text(encoding="utf-8")
        found += [f"{path.relative_to(root)}: {item}" for item in keychain_test_text_offences(text)]
    return found


ALIAS_SAMPLES = {
    "plain alias": "let store = KeychainStore.shared\ntry store.set(v, for: .x, scope: .shared)\n",
    "typed alias": "let s: KeychainStore = KeychainStore.shared\n",
    "alias then call on one line": "let s = KeychainStore.shared; s.set(v, for: .x, scope: .shared)\n",
    "parenthesised": "let s = (KeychainStore.shared)\n",
    "cast": "let s = KeychainStore.shared as KeychainStore\n",
    "spaced": "let s = KeychainStore .shared\n",
    "dot on the next line": "let s = KeychainStore\n    .shared\n",
    "member named self": "let s = KeychainStore.shared.self\n",
    "optional chain": "let s = KeychainStore.shared?.self\n",
    "type value": "let t = KeychainStore.self\n",
    "initializer value": "let make = KeychainStore.init\n",
    "typealias": "typealias Store = KeychainStore\n",
    "generic typealias": "typealias Pair<T> = KeychainStore\n",
    "implicit default": "func run(store: KeychainStore = .shared) {}\n",
    "implicit binding": "let s: KeychainStore = .shared\n",
    "optional implicit binding": "var s: KeychainStore? = .shared\n",
    "computed property": "var store: KeychainStore { .shared }\n",
    "computed property with return": "func store() -> KeychainStore { return .shared }\n",
    "labelled argument": "let signer = LocalApprovalSigner(store: .shared)\n",
    "inside interpolation": 'let m = "\\(KeychainStore.shared)"\n',
    "after a block comment": "/* c */ let s = KeychainStore.shared\n",
    "after a line comment": "// c\nlet s = KeychainStore.shared\n",
}

LEGITIMATE_SAMPLES = {
    "direct member call": "try KeychainStore.shared.set(v, for: .x, scope: .shared)\n",
    "member on the next line": "try KeychainStore.shared\n    .string(for: n, scope: s)\n",
    "launch migration step": "legacyUserDefaults: { _ = KeychainStore.shared.migrateLegacyUserDefaults() },\n",
    "production service constant": "LegacyT212KeychainCleanup.runOnce(service: KeychainStore.productionService)\n",
    "a test's own store": 'let store = KeychainStore(service: "san.Growin.credentials.v1.test.\\(UUID().uuidString)")\n',
    "type annotation": "private let store: KeychainStore\ninit(store: KeychainStore) { self.store = store }\n",
    "error type": "throw KeychainStoreError.unexpectedData\n#expect(throws: KeychainStoreError.self) {}\n",
    "string needle": 'if line.contains("KeychainStore.shared.") && !line.contains("scope:") {}\n',
    "string needle with self": 'let needles = ["KeychainStore.self", "KeychainStore.init"]\n',
    "commented out": "// let s = KeychainStore.shared\n/* let t = KeychainStore.self */\n",
    "doc comment": "/// Uses `KeychainStore.shared` for the real items.\n",
    "scope enum case": "@KeychainStorage(.openaiApiKey, scope: .shared) private var openaiApiKey = \"\"\n",
    "planted code in a string": 'let planted = "let s = LocalApprovalSigner(store: .shared)"\n',
    "other store argument": "let x = Cache(storeURL: url, store: other)\n",
    "other type named like it": "let x = MyKeychainStore.shared\nlet y = KeychainStoreProxy.self\n",
}


def test_every_planted_keychain_alias_is_caught():
    for name, text in ALIAS_SAMPLES.items():
        assert keychain_alias_offences(text), f"went undetected: {name}"


def test_legitimate_keychain_store_use_is_not_flagged():
    for name, text in LEGITIMATE_SAMPLES.items():
        assert keychain_alias_offences(text) == [], name


def test_the_old_dotted_needle_missed_most_of_the_aliases():
    """Proof the rule is stronger than the old `KeychainStore.shared.` line scan, not just different."""
    missed = [name for name, text in ALIAS_SAMPLES.items() if "KeychainStore.shared." not in text]
    assert len(missed) >= 14, missed


def test_a_store_argument_is_allowed_only_as_far_as_its_pin():
    one = "static let shared = LocalApprovalSigner(store: .shared)\n"
    assert keychain_alias_offences(one, store_argument_pin=1) == []
    assert keychain_alias_offences(one, store_argument_pin=0)
    assert keychain_alias_offences(one + one, store_argument_pin=1)
    assert keychain_alias_offences("let x = 1\n", store_argument_pin=1)


def test_an_app_file_off_the_allow_list_cannot_name_the_type(tmp_path):
    (tmp_path / "Views").mkdir()
    (tmp_path / "Security").mkdir()
    for name, text in {
        "Views/Param.swift": "func f(store: KeychainStore) {}\n",
        "Views/Alias.swift": "typealias S = KeychainStore\n",
        "Views/Interp.swift": 'let m = "\\(KeychainStore.productionService)"\n',
        "Views/Direct.swift": "try KeychainStore.shared.set(v, for: .x, scope: .shared)\n",
    }.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    hits = app_keychain_offences(tmp_path)
    for name in ("Param", "Alias", "Interp", "Direct"):
        assert any(hit.startswith(f"Views/{name}.swift") and "allow-list" in hit for hit in hits), name


def test_an_allow_listed_app_file_is_still_held_to_the_alias_rules(tmp_path):
    (tmp_path / "Security").mkdir()
    allowed = Path("Security") / "LaunchMigrations.swift"
    namers = frozenset({allowed})
    path = tmp_path / allowed
    path.write_text("_ = KeychainStore.shared.migrateFlatItemsToScoped()\n", encoding="utf-8")
    assert app_keychain_offences(tmp_path, namers, {}) == []
    path.write_text("let s = KeychainStore.shared\n_ = s.migrateFlatItemsToScoped()\n", encoding="utf-8")
    assert app_keychain_offences(tmp_path, namers, {})
    # A file that is not allow-listed and holds an alias is reported for both reasons.
    other = tmp_path / "Security" / "Other.swift"
    other.write_text("let s = KeychainStore.shared\n", encoding="utf-8")
    other_hits = [hit for hit in app_keychain_offences(tmp_path, namers, {}) if hit.startswith("Security/Other.swift")]
    assert len(other_hits) == 2, other_hits


def test_a_test_source_may_name_the_type_but_not_hold_the_shared_store(tmp_path):
    (tmp_path / "Fine.swift").write_text(
        "private static func cleanUp(_ store: KeychainStore) {}\n"
        'let own = KeychainStore(service: "t.\\(UUID().uuidString)")\n'
        'let needle = "KeychainStore.shared."\n',
        encoding="utf-8",
    )
    assert keychain_offences_in_test_sources(tmp_path) == []
    (tmp_path / "Bad.swift").write_text("let s = KeychainStore.shared\n", encoding="utf-8")
    assert keychain_offences_in_test_sources(tmp_path) == [
        "Bad.swift: KeychainStore.shared held as a value: KeychainStore.shared"
    ]


def test_the_allow_list_and_pins_are_not_vacuous():
    for relative in KEYCHAIN_STORE_NAMERS:
        text = (APP / relative).read_text(encoding="utf-8")
        code = strip_swift_comments_and_strings(text, keep_strings=True)
        assert KEYCHAIN_STORE_NAME.search(code), f"{relative} is allow-listed but does not name KeychainStore"
    for relative, pin in STORE_ARGUMENT_PINS.items():
        assert relative in KEYCHAIN_STORE_NAMERS
        bare = strip_swift_comments_and_strings((APP / relative).read_text(encoding="utf-8"))
        assert len(STORE_ARGUMENT.findall(bare)) == pin, relative
    # The direct-member shape really occurs, so the held-as-a-value rule is looking at live code.
    migrations = strip_swift_comments_and_strings(LAUNCH_MIGRATIONS.read_text(encoding="utf-8"), keep_strings=True)
    assert re.search(r"KeychainStore\.shared\.migrate\w+\(", migrations)


def test_no_app_source_aliases_or_names_the_keychain_store_outside_the_allow_list():
    assert len(list(APP.rglob("*.swift"))) > 20
    assert app_keychain_offences(APP) == []


def test_no_test_source_holds_the_shared_keychain_store_as_a_value():
    assert len(list(APP_TESTS.rglob("*.swift"))) > 10
    assert any(
        KEYCHAIN_STORE_NAME.search(path.read_text(encoding="utf-8")) for path in APP_TESTS.rglob("*.swift")
    ), "no test source names KeychainStore: the scan may be looking at the wrong tree"
    assert keychain_offences_in_test_sources(APP_TESTS) == []


# Implicit `.shared`: the type comes from context, so nothing here spells `KeychainStore.shared`.
# The first three are real helper shapes: `cleanUp(_ store: KeychainStore)` exists in three test files,
# and in ApprovalWorkspaceTests it removes the signing keys of every workspace.
IMPLICIT_SAMPLES = {
    "helper call": "Self.cleanUp(.shared)\n",
    "bare helper call": "cleanUp(.shared)\n",
    "function taking a store": "func migrate(_ s: KeychainStore) { _ = s.migrateFlatItemsToScoped() }\nmigrate(.shared)\n",
    "assignment in setUp": "var store: KeychainStore!\nfunc setUp() { store = .shared }\n",
    "other label": "run(on: .shared)\n",
    "implicitly unwrapped default": "var s: KeychainStore! = .shared\n",
    "optional generic": "var s: Optional<KeychainStore> = .shared\n",
    "backticked member": "let s = KeychainStore.`shared`\n",
    "backticked implicit": "let s: KeychainStore = .`shared`\n",
    "getter": "var s: KeychainStore { get { .shared } }\n",
    "closure initialiser": "let s: KeychainStore = { .shared }()\n",
    "extension alias": "extension KeychainStore { static var alias: KeychainStore { shared } }\n",
    "return": "func f() -> KeychainStore { return .shared }\n",
    "closure with in": "let f: (Int) -> KeychainStore = { _ in .shared }\n",
    "nil coalescing": "let s: KeychainStore = maybe ?? .shared\n",
    "ternary": "let s: KeychainStore = flag ? .shared : other\n",
    "array element": "let all: [KeychainStore] = [.shared]\n",
    "argument on its own line": "cleanUp(\n    .shared\n)\n",
    "dot then space": "cleanUp(. shared)\n",
}
IMPLICIT_LEGITIMATE = {
    "URLSession member": "let s = URLSession.shared\n",
    "scope label": "store.set(v, for: .x, scope: .shared)\n",
    "policy comparison": "#expect(name.policy == .shared)\n#expect(a != .shared)\n",
    "policy allows": "#expect(!policy.allows(.shared))\n",
    "pattern": "case .shared: return true\n",
    "router": "ApprovalSignerRouter.shared.sign(x)\n",
    "member chain on the next line": "let r = ApprovalSignerRouter\n    .shared\n",
    "member of a call result": "make().shared\nlist[0].shared\n",
    "longer names": "let sharedCount = 1\nlet x = a.shared2\n",
    "commented and quoted": '// cleanUp(.shared)\nlet n = "cleanUp(.shared)"\n',
    "own store": "let own = KeychainStore(service: unique)\ncleanUp(own)\n",
    "extension of the error": "extension KeychainStoreError { var x: Int { 1 } }\n",
}
PRODUCTION_SERVICE_SAMPLES = {
    "named through the type": "let s = KeychainStore(service: KeychainStore.productionService)\n",
    "bare": "let s = KeychainStore(service: productionService)\n",
    "with a space": "let s = KeychainStore(service: KeychainStore . productionService)\n",
}


def test_every_planted_implicit_shared_is_caught_in_test_sources():
    for name, text in IMPLICIT_SAMPLES.items():
        assert keychain_test_text_offences(text), f"went undetected: {name}"


def test_legitimate_shared_uses_are_not_flagged_in_test_sources():
    for name, text in IMPLICIT_LEGITIMATE.items():
        assert keychain_test_text_offences(text) == [], name
    # Everything the alias rules already allowed stays allowed.
    # (The production service constant is legal in the app and refused in tests, see below.)
    for name, text in LEGITIMATE_SAMPLES.items():
        if name != "production service constant":
            assert keychain_test_text_offences(text) == [], name


def test_the_production_service_name_is_refused_in_test_sources():
    for name, text in PRODUCTION_SERVICE_SAMPLES.items():
        assert keychain_test_text_offences(text), f"went undetected: {name}"
    assert keychain_test_text_offences('let n = "productionService"\n// productionService\n') == []
    assert keychain_test_text_offences("let n = productionServiceName\n") == []


def test_the_implicit_rule_is_stronger_than_the_alias_rules_alone():
    """The first-round rules (no implicit scan, no extension ban) missed most of these."""
    missed = [
        name
        for name, text in IMPLICIT_SAMPLES.items()
        if not keychain_alias_offences(text, implicit_shared=False, allow_extension=True)
    ]
    assert len(missed) >= 15, missed


def test_the_implicit_rule_runs_on_allow_listed_app_files_only(tmp_path):
    (tmp_path / "Security").mkdir()
    (tmp_path / "Views").mkdir()
    namers = frozenset({Path("Security") / "LaunchMigrations.swift"})
    (tmp_path / "Security" / "LaunchMigrations.swift").write_text(
        "func f() { Self.cleanUp(.shared) }\n", encoding="utf-8"
    )
    # Not a namer, so the type cannot be written here and URLSession's `.shared` stays legal.
    (tmp_path / "Views" / "Net.swift").write_text("init(session: URLSession = .shared) {}\n", encoding="utf-8")
    hits = app_keychain_offences(tmp_path, namers, {})
    assert any(hit.startswith("Security/LaunchMigrations.swift") and "implicit .shared" in hit for hit in hits)
    assert not any(hit.startswith("Views/") for hit in hits)


def test_extension_of_the_store_is_allowed_only_in_its_own_file(tmp_path):
    (tmp_path / "Security").mkdir()
    own = Path("Security") / "KeychainStore.swift"
    (tmp_path / own).write_text("extension KeychainStore { func x() {} }\n", encoding="utf-8")
    assert app_keychain_offences(tmp_path, frozenset({own}), {}) == []
    other = Path("Security") / "LaunchMigrations.swift"
    (tmp_path / other).write_text("extension KeychainStore { func x() {} }\n", encoding="utf-8")
    assert app_keychain_offences(tmp_path, frozenset({own, other}), {})


# --------------------------------------------------------------------------------------------
# 2. GrowinUITests launch scan
# --------------------------------------------------------------------------------------------

UI_HELPER = "LaunchSupport.swift"
UI_APP_TYPE = "XCUIApplication"
HELPER_CALL = "GrowinLaunch.makeApp()"
SKIP_KEY_DECLARATION = re.compile(r'static\s+let\s+skipEnvironmentKey\s*=\s*"([A-Z0-9_]+)"')
LAUNCH_OR_ACTIVATE = re.compile(r"\.\s*(?:launch|activate)\s*\(")
LAUNCH_ENVIRONMENT = re.compile(_NOT_AFTER_IDENT + r"launchEnvironment" + _NOT_BEFORE_IDENT)
# Other ways to start the app from a UI test without going through XCUIApplication.
OTHER_LAUNCHERS = re.compile(
    _NOT_AFTER_IDENT + r"(?:NSWorkspace|NSTask|posix_spawn|Process\s*\()"
)
MAKE_APP_SIGNATURE = re.compile(r"\bfunc\s+makeApp\s*\(\s*\)\s*->\s*" + UI_APP_TYPE + r"\s*\{")


def _make_app_body_pattern(skip_key: str) -> re.Pattern[str]:
    """The whole body of makeApp: create the app, set the suppression key on it, return it."""
    return re.compile(
        r"\s*let\s+(\w+)\s*=\s*" + UI_APP_TYPE + r"\s*\(\s*\)\s*;?\s*"
        r"\1\s*\.\s*launchEnvironment\s*\[\s*\"" + re.escape(skip_key) + r"\"\s*\]\s*=\s*\"1\"\s*;?\s*"
        r"return\s+\1\s*"
    )


def _split_make_app(code: str) -> tuple[str, str] | None:
    """(body of makeApp, the helper code with the whole function removed), or None if not found once."""
    found = list(MAKE_APP_SIGNATURE.finditer(code))
    if len(found) != 1:
        return None
    open_at = found[0].end() - 1
    depth = 0
    for index in range(open_at, len(code)):
        if code[index] == "{":
            depth += 1
        elif code[index] == "}":
            depth -= 1
            if depth == 0:
                return code[open_at + 1 : index], code[: found[0].start()] + code[index + 1 :]
    return None


def helper_offences(helper: str, skip_key: str) -> list[str]:
    """The helper must be one makeApp that creates the app, sets the key and returns that app.

    The whole body is matched, not a substring, so an early `return .init(bundleIdentifier:)`, a
    second return, a different returned variable or any extra statement fails. Outside makeApp the
    helper may not name the application type or call an initializer at all.
    """
    code = strip_swift_comments_and_strings(helper, keep_strings=True)
    split = _split_make_app(code)
    if split is None:
        return [f"{UI_HELPER}: must declare makeApp() -> {UI_APP_TYPE} exactly once"]
    body, outside = split
    found = []
    if not _make_app_body_pattern(skip_key).fullmatch(body):
        found.append(f"{UI_HELPER}: makeApp body is not exactly create, set the suppression key, return")
    if UI_APP_TYPE in outside:
        found.append(f"{UI_HELPER}: names {UI_APP_TYPE} outside makeApp")
    if re.search(r"\.\s*init(?![A-Za-z0-9_])", outside):
        found.append(f"{UI_HELPER}: calls an initializer outside makeApp")
    return found


def ui_launch_offences(sources: dict[str, str], skip_key: str) -> list[str]:
    """Reasons a UI-test source could start Growin without the suppression key.

    `sources` maps a path relative to GrowinUITests to its text.
    """
    helper = sources.get(UI_HELPER)
    if helper is None:
        return [f"{UI_HELPER}: helper missing"]
    found = helper_offences(helper, skip_key)
    for path, text in sorted(sources.items()):
        if path == UI_HELPER:
            continue
        code = strip_swift_comments_and_strings(text)
        if UI_APP_TYPE in text:
            found.append(f"{path}: names {UI_APP_TYPE} outside the helper")
        if LAUNCH_ENVIRONMENT.search(code):
            found.append(f"{path}: touches launchEnvironment outside the helper")
        if OTHER_LAUNCHERS.search(code):
            found.append(f"{path}: starts a process without the helper")
        if LAUNCH_OR_ACTIVATE.search(code) and HELPER_CALL not in code:
            found.append(f"{path}: launches or activates an app that did not come from the helper")
    return found


def ui_sources(root: Path = UI_TESTS) -> dict[str, str]:
    return {
        str(path.relative_to(root)): path.read_text(encoding="utf-8") for path in sorted(root.rglob("*.swift"))
    }


def skip_key_from_app() -> str:
    match = SKIP_KEY_DECLARATION.search(LAUNCH_MIGRATIONS.read_text(encoding="utf-8"))
    assert match, "LaunchMigrations no longer declares skipEnvironmentKey: re-point this scan"
    return match.group(1)


KEY = "GROWIN_SKIP_LAUNCH_MIGRATIONS"
GOOD_HELPER = (
    "enum GrowinLaunch {\n"
    "    @MainActor\n"
    "    static func makeApp() -> XCUIApplication {\n"
    "        let app = XCUIApplication()\n"
    f'        app.launchEnvironment["{KEY}"] = "1"\n'
    "        return app\n"
    "    }\n"
    "}\n"
)
GOOD_TEST = (
    "final class T: XCTestCase {\n"
    "    func testA() {\n"
    "        let app = GrowinLaunch.makeApp()\n"
    "        app.launch()\n"
    "        app.activate()\n"
    "    }\n"
    "}\n"
)


def _with(name: str, text: str) -> dict[str, str]:
    return {UI_HELPER: GOOD_HELPER, "GoodUITests.swift": GOOD_TEST, name: text}


def test_the_good_helper_and_test_pass_the_ui_launch_scan():
    assert ui_launch_offences({UI_HELPER: GOOD_HELPER, "GoodUITests.swift": GOOD_TEST}, KEY) == []


def test_the_ui_launch_scan_catches_every_way_to_make_an_unguarded_app():
    planted = {
        "plain creation": "let app = XCUIApplication()\napp.launch()\n",
        "init call": "let app = XCUIApplication.init()\napp.launch()\n",
        "init spaced": "let app = XCUIApplication .init ()\n",
        "bundle identifier": 'let app = XCUIApplication(bundleIdentifier: "san.Growin")\n',
        "url initializer": 'let app = XCUIApplication(url: URL(fileURLWithPath: "/x"))\n',
        "typealias": "typealias App = XCUIApplication\nlet app = App()\n",
        "type value": "let t = XCUIApplication.self\n",
        "class lookup by name": 'let t = NSClassFromString("XCUIApplication")\n',
        "type annotation only": "func f(_ app: XCUIApplication) {}\n",
        "commented mention": "// XCUIApplication is not allowed here\n",
    }
    for name, text in planted.items():
        assert ui_launch_offences(_with("BadUITests.swift", text), KEY), f"went undetected: {name}"


def test_the_ui_launch_scan_catches_launch_and_activate_on_an_app_not_from_the_helper():
    for name, text in {
        "launch on a parameter": "func go(_ app: Any) { app.launch() }\n",
        "activate": "func go() { other.activate() }\n",
        "activate spaced": "other . activate ()\n",
        "launch spaced": "other\n    .launch ()\n",
    }.items():
        assert ui_launch_offences(_with("BadUITests.swift", text), KEY), f"went undetected: {name}"
    # The same calls are fine in a file that gets its app from the helper.
    assert ui_launch_offences(_with("OkUITests.swift", "let a = GrowinLaunch.makeApp()\na.activate()\n"), KEY) == []


def test_the_ui_launch_scan_catches_a_test_that_clears_or_resets_the_key():
    for name, text in {
        "reassign": "let app = GrowinLaunch.makeApp()\napp.launchEnvironment = [:]\napp.launch()\n",
        "overwrite": f'let app = GrowinLaunch.makeApp()\napp.launchEnvironment["{KEY}"] = "0"\n',
        "remove": f'app.launchEnvironment.removeValue(forKey: "{KEY}")\n',
    }.items():
        assert ui_launch_offences(_with("BadUITests.swift", text), KEY), f"went undetected: {name}"


def test_the_ui_launch_scan_catches_other_launchers():
    for name, text in {
        "workspace": "NSWorkspace.shared.open(url)\n",
        "task": "let t = NSTask()\n",
        "process": 'let p = Process ()\np.executableURL = URL(fileURLWithPath: "/usr/bin/open")\n',
        "spawn": "posix_spawn(&pid, path, nil, nil, args, nil)\n",
    }.items():
        assert ui_launch_offences(_with("BadUITests.swift", text), KEY), f"went undetected: {name}"
    assert ui_launch_offences(_with("OkUITests.swift", "let info = ProcessInfo.processInfo\n"), KEY) == []


EARLY_RETURN_HELPER = (
    "enum GrowinLaunch {\n"
    "    @MainActor\n"
    "    static func makeApp() -> XCUIApplication {\n"
    '        if condition { return .init(bundleIdentifier: "san.Growin") }\n'
    "        let app = XCUIApplication()\n"
    f'        app.launchEnvironment["{KEY}"] = "1"\n'
    "        return app\n"
    "    }\n"
    "}\n"
)


def test_the_whole_makeapp_body_is_pinned_not_a_substring_of_it():
    def offences_for(helper: str, key: str = KEY) -> list[str]:
        return ui_launch_offences({UI_HELPER: helper, "GoodUITests.swift": GOOD_TEST}, key)

    body = f'        let app = XCUIApplication()\n        app.launchEnvironment["{KEY}"] = "1"\n        return app\n'
    assert body in GOOD_HELPER

    def with_body(new_body: str) -> str:
        return GOOD_HELPER.replace(body, new_body)

    tail = "}\n}\n"
    broken = {
        # Codex r1 P2: a safe-looking tail after an unsafe early return.
        "early return of an inferred init": EARLY_RETURN_HELPER,
        "early return of a named creation": with_body("        if condition { return XCUIApplication() }\n" + body),
        "early return in a guard": with_body("        guard ok else { return .init(url: u) }\n" + body),
        "early return of a bundle id": with_body(
            '        if x { return XCUIApplication(bundleIdentifier: "b") }\n' + body
        ),
        "second return after the safe one": with_body(body + "        return XCUIApplication()\n"),
        "returns a different variable": with_body(body.replace("return app", "return other")),
        "returns a different app": with_body(body.replace("return app", "return XCUIApplication()")),
        "returns an inferred init": with_body(body.replace("return app", "return .init()")),
        "key set on another variable": with_body(
            f'        let app = XCUIApplication()\n        other.launchEnvironment["{KEY}"] = "1"\n        return app\n'
        ),
        "extra statement": with_body(body.replace("        return app", "        app.launchArguments = []\n        return app")),
        "extra statement before": with_body("        setup()\n" + body),
        "no key": with_body(body.replace(f'        app.launchEnvironment["{KEY}"] = "1"\n', "")),
        "wrong value": with_body(body.replace('= "1"', '= "0"')),
        "key only in a comment": with_body(
            body.replace(f'app.launchEnvironment["{KEY}"] = "1"', f'// app.launchEnvironment["{KEY}"] = "1"')
        ),
        "key set before the app exists": with_body(
            f'        app.launchEnvironment["{KEY}"] = "1"\n        let app = XCUIApplication()\n        return app\n'
        ),
        "second creator beside it": GOOD_HELPER.replace(
            tail, "}\n    static func raw() -> XCUIApplication { XCUIApplication() }\n}\n"
        ),
        "inferred init beside it": GOOD_HELPER.replace(
            tail, "}\n    static func raw() -> XCUIApplication { .init() }\n}\n"
        ),
        "typealias beside it": GOOD_HELPER.replace(tail, "}\n    typealias App = XCUIApplication\n}\n"),
        "makeApp declared twice": GOOD_HELPER.replace(
            tail, "}\n    static func makeApp() -> XCUIApplication { XCUIApplication() }\n}\n"
        ),
        "other key": GOOD_HELPER.replace(KEY, "OTHER"),
    }
    for name, helper in broken.items():
        assert offences_for(helper), f"went undetected: {name}"
        assert helper_offences(helper, KEY), f"helper_offences missed: {name}"
    assert offences_for(GOOD_HELPER) == []
    assert helper_offences(GOOD_HELPER, KEY) == []
    assert offences_for(GOOD_HELPER, key="DIFFERENT"), "a helper key that differs from the app's must fail"
    assert ui_launch_offences({"GoodUITests.swift": GOOD_TEST}, KEY) == [f"{UI_HELPER}: helper missing"]
    assert helper_offences("enum GrowinLaunch {}\n", KEY)
    # Non-plants: comments and formatting do not matter, a semicolon after a statement is the same code.
    commented = GOOD_HELPER.replace(
        "enum GrowinLaunch {", "/// XCUIApplication() and .init() are named here only in a comment.\nenum GrowinLaunch {"
    )
    assert helper_offences(commented, KEY) == []
    reflowed = with_body(
        f'        let app = XCUIApplication() ; app\n            .launchEnvironment["{KEY}"] = "1";\n        return app\n'
    )
    assert helper_offences(reflowed, KEY) == []


def test_the_real_ui_tests_launch_only_through_the_helper_with_the_apps_key():
    sources = ui_sources()
    assert UI_HELPER in sources
    assert any(
        name != UI_HELPER and HELPER_CALL in text and ".launch(" in text for name, text in sources.items()
    ), "no UI test launches through the helper: the scan may be looking at the wrong tree"
    key = skip_key_from_app()
    assert key == KEY
    assert ui_launch_offences(sources, key) == []


def test_the_ui_test_target_compiles_only_the_directory_the_scan_reads():
    project = PROJECT.read_text(encoding="utf-8")
    # The target's single synchronized group is the GrowinUITests folder, and that group is a
    # plain root group (no build-file exception sets).
    group = re.search(
        r"fileSystemSynchronizedGroups = \(\s*(\w+) /\* GrowinUITests \*/,\s*\);\s*name = GrowinUITests;", project
    )
    assert group, "GrowinUITests target now has other synchronized groups: extend the UI launch scan"
    assert re.search(
        group.group(1) + r" /\* GrowinUITests \*/ = \{\s*isa = PBXFileSystemSynchronizedRootGroup;\s*path = GrowinUITests;",
        project,
    )
    assert "PBXFileSystemSynchronizedBuildFileExceptionSet" not in project
    # No source file is referenced explicitly, so nothing outside the group is compiled.
    assert ".swift" not in project
    # tests/macos-ui holds old UI tests that create XCUIApplication() directly. They are not in the
    # project. If they are ever added, this fails and the scan must be pointed at them first.
    assert "macos-ui" not in project
