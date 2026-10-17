"""Closes the two #566 P3 scan bypasses, with scans that run in CI without Xcode.

1. Aliasing the shared Keychain store. A scan for `KeychainStore.shared.<call>` is dodged by
   `let s = KeychainStore.shared` and then `s.set(...)`. The rule here does not follow calls. It
   forbids holding the store (or its type) as a value:
   - In the app, only four Security files may name `KeychainStore` at all. Everywhere else the
     store cannot be aliased because the type cannot be written. The two signers get the store
     injected and are pinned to one `store: .shared` each.
   - In every scanned app file, `KeychainStore.shared` must be followed by a member access, and
     `KeychainStore.self`, `KeychainStore.init`, a typealias of the type and a `.shared` default
     for a KeychainStore-typed value are all refused.
   - GrowinTests may name the type but never reach the shared (production) store in any form,
     member calls included, and every `KeychainStore(service:)` it writes must take a plain
     `san.Growin.credentials.v1.test.` literal.
2. Launching a UI-test app without the suppression key. The `GrowinTests` scan looked for
   `XCUIApplication(` and `.launch()` only. Here GrowinUITests may name `XCUIApplication` in
   `LaunchSupport.swift` and nowhere else (so `.init()`, `(bundleIdentifier:)`, `(url:)`, a typealias
   and `.self` are all caught by the one rule), may not touch `launchEnvironment` (a test could
   clear the key), and may not call `.launch()` or `.activate()` unless the file also gets its app
   from `GrowinLaunch.makeApp()`. The helper is pinned to its exact shape. Neither test target may
   start a process or open an app another way (`Process`, `posix_spawn`, `NSWorkspace`, ...).

Every scan reads the output of `reduce_swift`: comments removed, every string literal (plain,
multi-line, raw with any number of `#`) reduced to `""`, and every interpolation kept as code. A
file the reducer cannot read to the end (an unterminated string or comment) is an offence, so a
stray delimiter cannot hide code from the scans.

The GrowinUITests target compiles `GrowinUITests/` only: it is a file-system-synchronized group with
no explicit sources and no exception sets. `tests/macos-ui/` is not in the Xcode project, so it is
not compiled and not scanned. A test below fails if that membership changes.
"""

import re
from dataclasses import dataclass
from pathlib import Path

from test_app_does_not_push_t212_keys import strip_swift_comments_and_strings

REPO = Path(__file__).resolve().parents[2]
APP = REPO / "Growin"
APP_TESTS = REPO / "GrowinTests"
UI_TESTS = REPO / "GrowinUITests"
PROJECT = REPO / "Growin.xcodeproj" / "project.pbxproj"
LAUNCH_MIGRATIONS = APP / "Security" / "LaunchMigrations.swift"

# --------------------------------------------------------------------------------------------
# 0. Reading Swift
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Literal:
    """One string literal: where its `""` sits in the reduced code, its kind, and its source text."""

    start: int
    end: int
    kind: str  # "plain", "multiline" or "raw"
    source: str


@dataclass(frozen=True)
class Reduced:
    bare: str
    literals: tuple[Literal, ...]
    errors: tuple[str, ...]


RAW_OPEN = re.compile(r'(#+)("""|")')


def reduce_swift(text: str) -> Reduced:
    """Swift source read in one pass, the way the compiler splits code from text.

    Comments go (block comments nest). Each string literal becomes `""`, then ` (expression) ` for
    every interpolation in it, so `print("x \\(Self.cleanUp(.shared))")` and the raw form
    `#"x \\#(Self.cleanUp(.shared))"#` both leave the expression in the code. In a raw string with
    n `#`, only `\\` followed by n `#` escapes or interpolates. Backticks are dropped from code, so
    KeychainStore.`shared` reads as KeychainStore.shared. Newlines are kept.

    Anything that does not close (a string, a raw string, an interpolation, a block comment) is
    recorded in `errors`: the callers report it as an offence instead of trusting the rest.
    """
    n = len(text)
    out: list[str] = []
    size = 0
    literals: list[Literal] = []
    errors: list[str] = []

    def emit(piece: str) -> None:
        nonlocal size
        out.append(piece)
        size += len(piece)

    def line(at: int) -> int:
        return text.count("\n", 0, at) + 1

    def block_comment(i: int) -> int:
        start, depth = i, 0
        while i < n:
            if text.startswith("/*", i):
                depth += 1
                i += 2
            elif text.startswith("*/", i):
                depth -= 1
                i += 2
                if depth == 0:
                    emit(" ")
                    return i
            else:
                if text[i] == "\n":
                    emit("\n")
                i += 1
        errors.append(f"unterminated block comment at line {line(start)}")
        return i

    def code(i: int, until_close: bool) -> int:
        start, depth = i, 0
        while i < n:
            ch = text[i]
            if text.startswith("//", i):
                end = text.find("\n", i)
                i = n if end < 0 else end
                emit(" ")
                continue
            if text.startswith("/*", i):
                i = block_comment(i)
                continue
            raw = RAW_OPEN.match(text, i)
            if raw:
                i = literal(i, raw.end(), len(raw.group(1)), raw.group(2) == '"""', "raw")
                continue
            if text.startswith('"""', i):
                i = literal(i, i + 3, 0, True, "multiline")
                continue
            if ch == '"':
                i = literal(i, i + 1, 0, False, "plain")
                continue
            if ch == "`":
                i += 1
                continue
            if until_close:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    if depth == 0:
                        return i + 1
                    depth -= 1
            emit(ch)
            i += 1
        if until_close:
            errors.append(f"unterminated interpolation at line {line(start)}")
        return i

    def literal(start: int, i: int, hashes: int, multiline: bool, kind: str) -> int:
        begin = size
        emit('""')
        close = ('"""' if multiline else '"') + "#" * hashes
        escape = "\\" + "#" * hashes
        newlines = 0
        closed = False
        while i < n:
            if text.startswith(escape, i):
                after = i + len(escape)
                if text.startswith("(", after):
                    emit(" (")
                    i = code(after + 1, True)
                    emit(") ")
                    continue
                if after < n and text[after] == "\n":
                    if not multiline:
                        break
                    newlines += 1
                i = after + 1
                continue
            if text.startswith(close, i):
                i += len(close)
                closed = True
                break
            if text[i] == "\n":
                if not multiline:
                    break
                newlines += 1
            i += 1
        if not closed:
            errors.append(f"unterminated {kind} string literal at line {line(start)}")
        emit("\n" * newlines)
        literals.append(Literal(begin, size, kind, text[start:i]))
        return i

    code(0, False)
    return Reduced("".join(out), tuple(literals), tuple(errors))


def unreadable(reduced: Reduced) -> list[str]:
    return [f"cannot be read to the end, so it is refused: {error}" for error in reduced.errors]


# A newline does not end a declaration when the line ends in one of these, or the next line starts
# with one of the second set. Inside `(`, `[` or a generic `<` it never does.
_CONTINUES_AFTER = re.compile(
    r"(?:[,:=\&.<|]|->|@\w+"
    r"|(?<![A-Za-z0-9_])(?:func|var|let|static|class|final|private|fileprivate|internal|public|mutating"
    r"|nonmutating|override|throws|rethrows|async|inout|some|any|where|borrowing|consuming|sending))\s*\Z"
)
_CONTINUES_BEFORE = re.compile(r"\s*(?:[(.\&<=|?:+*%^~\-]|(?:where|throws|rethrows|async)(?![A-Za-z0-9_]))")
_CLOSERS = {")": "(", "]": "[", "}": "{"}


def logical_lines(bare: str) -> str:
    """Reduced code with each multi-line declaration joined onto one line.

    `func cleanUp(scope store: Optional<\\n KeychainStore>)` and `func allows(\\n _ s: KeychainStore\\n)`
    become single lines, so the one-line declaration patterns below see the whole declaration. A
    newline is joined when it sits inside `(`, `[` or a generic `<` (one written right after a name),
    or when the line or the next one continues the declaration. Inside `{` lines stay separate.
    """
    out: list[str] = []
    current: list[str] = []
    stack: list[str] = []
    for i, ch in enumerate(bare):
        if ch == "\n":
            if (
                (stack and stack[-1] in "([<")
                or _CONTINUES_AFTER.search("".join(current[-48:]))
                or _CONTINUES_BEFORE.match(bare, i + 1)
            ):
                current.append(" ")
            else:
                out.append("".join(current) + "\n")
                current = []
            continue
        previous = bare[i - 1] if i else ""
        if ch in "([{":
            if ch == "{":
                while stack and stack[-1] == "<":
                    stack.pop()
            stack.append(ch)
        elif ch == "<" and (previous.isalnum() or previous == "_"):
            stack.append("<")
        elif ch == ">" and stack and stack[-1] == "<" and previous != "-":
            stack.pop()
        elif ch in _CLOSERS:
            while stack and stack[-1] == "<":
                stack.pop()
            if stack and stack[-1] == _CLOSERS[ch]:
                stack.pop()
        elif ch in "=;" and stack and stack[-1] == "<":
            while stack and stack[-1] == "<":
                stack.pop()
        current.append(ch)
    out.append("".join(current))
    return "".join(out)


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
    # forms are caught by `implicit_shared_uses`.
)
# An argument or default written `store: .shared`.
STORE_ARGUMENT = re.compile(_NOT_AFTER_IDENT + r"store\s*:\s*\.shared" + _NOT_BEFORE_IDENT)

# `.shared` with no type in front of it takes its type from context, so the scan cannot tell a
# KeychainStore from anything else: `Self.cleanUp(.shared)`, `run(on: .shared)`, `store = .shared`,
# `var s: KeychainStore! = .shared`, `{ .shared }`, `return .shared`. The only implicit `.shared`
# allowed is the KeychainScope enum case, in the three shapes below, removed before the scan. The
# exemption is only as good as the type of the thing it is passed to, so the declarations that
# could make it a KeychainStore are refused separately (see SCOPE_SHAPED_DECLARATIONS).
KEYCHAIN_SCOPE_CONTEXTS = re.compile(
    _NOT_AFTER_IDENT + r"scope\s*:\s*\.shared(?![A-Za-z0-9_])"
    r"|\.\s*policy\s*[=!]=\s*\.shared(?![A-Za-z0-9_])"
    r"|\.\s*policy\s*\.\s*allows\s*\(\s*\.shared(?![A-Za-z0-9_])"
)
# A declaration that gives a KeychainStore the label or name an exemption relies on, or that makes
# a custom `==`, `!=`, `~=` or `allows`. All of these let `scope: .shared`, `x.policy == .shared`
# or `x.policy.allows(.shared)` mean the shared store. They are read on `logical_lines`, so a
# declaration split over several lines is one line here.
SCOPE_SHAPED_DECLARATIONS = (
    (
        "parameter or property labelled scope with a KeychainStore type",
        re.compile(
            _NOT_AFTER_IDENT + r"scope(?:\s+\w+)?\s*:\s*[^,)\n]*?(?<![A-Za-z0-9_])KeychainStore" + _NOT_BEFORE_IDENT
        ),
    ),
    (
        "generic parameter or where clause bound to KeychainStore",
        re.compile(
            r"<\s*\w+\s*:\s*KeychainStore" + _NOT_BEFORE_IDENT
            + r"|\bwhere\b[^{\n]*\b\w+\s*(?::|==)\s*KeychainStore" + _NOT_BEFORE_IDENT
        ),
    ),
    (
        "custom ==, !=, ~=, allows or policy taking or holding a KeychainStore",
        re.compile(
            r"\b(?:func|var|let)\s*(?:==|!=|~=|allows\b|policy\b)[^{\n]*?(?<![A-Za-z0-9_])KeychainStore"
            + _NOT_BEFORE_IDENT
        ),
    ),
)
DOT_SHARED = re.compile(r"\.\s*shared(?![A-Za-z0-9_])")
STORE_LABEL_BEFORE = re.compile(r"(?<![A-Za-z0-9_])store\s*:\s*$")
# A keyword before `.shared` still takes an implicit member (`return .shared`, `{ _ in .shared }`,
# `_read { yield .shared }`).
KEYWORDS_BEFORE_IMPLICIT = frozenset({"return", "in", "else", "try", "await", "throw", "yield", "case"})
EXTENSION_OF_STORE = re.compile(r"\bextension\s+KeychainStore" + _NOT_BEFORE_IDENT)
PRODUCTION_SERVICE = re.compile(_NOT_AFTER_IDENT + r"productionService" + _NOT_BEFORE_IDENT)
# Tests build their own store and never need the shared one, so a test may not name it at all,
# member call or not.
SHARED_STORE = re.compile(_NOT_AFTER_IDENT + r"KeychainStore\s*\.\s*shared" + _NOT_BEFORE_IDENT)

# Every store a test builds takes one plain literal under the test prefix. The only interpolation
# allowed is the `\(UUID().uuidString)` every real call site uses, after the whole prefix, so the
# value can never be the production service and the interpolation carries no other code.
TEST_SERVICE_PREFIX = "san.Growin.credentials.v1.test."
TEST_SERVICE_LITERAL = re.compile(
    '"' + re.escape(TEST_SERVICE_PREFIX) + r"[A-Za-z0-9._-]*(?:\\\(UUID\(\)\.uuidString\)[A-Za-z0-9._-]*)?" + '"'
)
# `KeychainStore(`, `KeychainStore.init(`, and any `.init(service:` (implicit, `type(of:)`, a metatype).
STORE_CONSTRUCTION = re.compile(
    _NOT_AFTER_IDENT + r"KeychainStore\s*(?:\.\s*init\s*)?\("
    + r"|" + _NOT_AFTER_IDENT + r"init\s*\((?=\s*service\s*:)"
)
SERVICE_LABEL = re.compile(r"\s*service\s*:\s*")
CLOSES_CALL = re.compile(r"\s*\)")
SERVICE_NAME_ARGUMENT = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\)")
# The one identifier form allowed: `let name = "<test literal>"` and, as the very next statement,
# `let x = KeychainStore(service: name)` or `return (KeychainStore(service: name), ...)`. Nothing
# can rebind the name in between, and the binding is a `let`.
SERVICE_BINDING_GAP = re.compile(
    r"\s*;?\s*(?:(?:let|var)\s+[A-Za-z_]\w*\s*(?::\s*KeychainStore\s*)?=\s*|return\s*\(?\s*)"
)


def _is_test_service_literal(literal: Literal) -> bool:
    return literal.kind == "plain" and TEST_SERVICE_LITERAL.fullmatch(literal.source) is not None


def _bound_to_test_literal(reduced: Reduced, name: str, call_start: int) -> bool:
    before = [literal for literal in reduced.literals if literal.end <= call_start]
    if not before:
        return False
    literal = max(before, key=lambda item: item.end)
    if not _is_test_service_literal(literal):
        return False
    if not SERVICE_BINDING_GAP.fullmatch(reduced.bare, literal.end, call_start):
        return False
    binding = re.compile(_NOT_AFTER_IDENT + r"let\s+" + re.escape(name) + r"\s*(?::\s*String\s*)?=\s*\Z")
    return binding.search(reduced.bare[: literal.start]) is not None


def service_argument_offences(reduced: Reduced) -> list[str]:
    """Each store a test builds whose service is not a plain test literal (or a `let` bound to one)."""
    found = []
    by_start = {literal.start: literal for literal in reduced.literals}
    bare = reduced.bare
    for call in STORE_CONSTRUCTION.finditer(bare):
        where = f"line {bare.count(chr(10), 0, call.start()) + 1}"
        label = SERVICE_LABEL.match(bare, call.end())
        if label is None:
            found.append(f"KeychainStore built without a service: argument ({where})")
            continue
        literal = by_start.get(label.end())
        if literal is not None:
            if _is_test_service_literal(literal) and CLOSES_CALL.match(bare, literal.end):
                continue
        else:
            name = SERVICE_NAME_ARGUMENT.match(bare, label.end())
            if name and _bound_to_test_literal(reduced, name.group(1), call.start()):
                continue
        found.append(f"KeychainStore service is not one plain {TEST_SERVICE_PREFIX} literal ({where})")
    return found


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
    reduced: Reduced | None = None,
) -> list[str]:
    """Ways one Swift file holds `KeychainStore.shared`, or the type, as a value."""
    reduced = reduced or reduce_swift(text)
    bare = reduced.bare
    found = unreadable(reduced)
    for label, pattern in ALIAS_PATTERNS:
        for match in pattern.finditer(bare):
            found.append(f"{label}: {' '.join(match.group(0).split())}")
    arguments = len(STORE_ARGUMENT.findall(bare))
    if arguments != store_argument_pin:
        found.append(f"`store: .shared` appears {arguments}x, allowed {store_argument_pin}")
    if implicit_shared:
        found += implicit_shared_uses(bare)
        logical = logical_lines(bare)
        for label, pattern in SCOPE_SHAPED_DECLARATIONS:
            for match in pattern.finditer(logical):
                found.append(f"{label}: {' '.join(match.group(0).split())}")
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
        reduced = reduce_swift(text)
        if relative not in namers and KEYCHAIN_STORE_NAME.search(reduced.bare):
            found.append(f"{relative}: names KeychainStore and is not on the allow-list")
        offences = keychain_alias_offences(
            text,
            pins.get(relative, 0),
            implicit_shared=relative in namers,
            allow_extension=relative == Path("Security") / "KeychainStore.swift",
            reduced=reduced,
        )
        found += [f"{relative}: {item}" for item in offences]
    return found


def keychain_test_text_offences(text: str) -> list[str]:
    """One test file: the alias rules, any implicit `.shared`, any `KeychainStore.shared`, the
    production service name, and the service of every store the file builds.

    `KeychainStore(service: KeychainStore.productionService)` is the shared store under another name,
    so a test may not name `productionService` at all, and may not build a store on anything but a
    test literal.
    """
    reduced = reduce_swift(text)
    found = keychain_alias_offences(text, reduced=reduced)
    if SHARED_STORE.search(reduced.bare):
        found.append("names the shared store (a test builds its own)")
    if PRODUCTION_SERVICE.search(reduced.bare):
        found.append("names productionService")
    found += service_argument_offences(reduced)
    return found


# The one exemption from the shared-store ban. The tripwire (issue #66, PR #575) calls the REAL shared
# store inside the real test host and only expects a refusal. It is exempt by EXACT relative path, never
# a pattern, and only for the ONE exact expression below: that text is removed from this one file before
# scanning, so any other mention of the shared store in the same file, and every other rule, still fires.
# `tests/backend/test_keychain_test_host_guard.py` pins the tripwire file's shape.
TRIPWIRE_PATH = Path("WorkspaceIsolation") / "ProductionTripwireTests.swift"
TRIPWIRE_SHARED_CALL = "KeychainStore.shared.string(for: .alpacaApiKey, scope: .workspace(.uk))"


def tripwire_exempted(relative: Path, text: str) -> str:
    """`text` with the one allowed shared-store expression removed, in the exact tripwire path only."""
    if relative != TRIPWIRE_PATH:
        return text
    return text.replace(TRIPWIRE_SHARED_CALL, "refusedRead()")


def keychain_offences_in_test_sources(root: Path) -> list[str]:
    """Test files that reach the shared store or build one on a non-test service."""
    found = []
    for path in sorted(root.rglob("*.swift")):
        relative = path.relative_to(root)
        text = tripwire_exempted(relative, path.read_text(encoding="utf-8"))
        found += [f"{relative}: {item}" for item in keychain_test_text_offences(text)]
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
# Legal in the four app files, refused in tests: the shared store and its service name.
APP_ONLY_SAMPLES = ("direct member call", "member on the next line", "launch migration step", "production service constant")


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
        "Views/RawInterp.swift": 'let m = #"\\#(KeychainStore.productionService)"#\n',
        "Views/Direct.swift": "try KeychainStore.shared.set(v, for: .x, scope: .shared)\n",
    }.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    hits = app_keychain_offences(tmp_path)
    for name in ("Param", "Alias", "Interp", "RawInterp", "Direct"):
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
        'let own = KeychainStore(service: "san.Growin.credentials.v1.test.\\(UUID().uuidString)")\n'
        'let needle = "KeychainStore.shared."\n',
        encoding="utf-8",
    )
    assert keychain_offences_in_test_sources(tmp_path) == []
    (tmp_path / "Bad.swift").write_text("let s = KeychainStore.shared\n", encoding="utf-8")
    assert keychain_offences_in_test_sources(tmp_path) == [
        "Bad.swift: KeychainStore.shared held as a value: KeychainStore.shared",
        "Bad.swift: names the shared store (a test builds its own)",
    ]


def test_the_allow_list_and_pins_are_not_vacuous():
    for relative in KEYCHAIN_STORE_NAMERS:
        text = (APP / relative).read_text(encoding="utf-8")
        assert KEYCHAIN_STORE_NAME.search(reduce_swift(text).bare), (
            f"{relative} is allow-listed but does not name KeychainStore"
        )
    for relative, pin in STORE_ARGUMENT_PINS.items():
        assert relative in KEYCHAIN_STORE_NAMERS
        bare = strip_swift_comments_and_strings((APP / relative).read_text(encoding="utf-8"))
        assert len(STORE_ARGUMENT.findall(bare)) == pin, relative
    # The direct-member shape really occurs, so the held-as-a-value rule is looking at live code.
    migrations = reduce_swift(LAUNCH_MIGRATIONS.read_text(encoding="utf-8")).bare
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
    "yield": "var s: KeychainStore { _read { yield .shared } }\n",
    "switch case": "switch store {\ncase .shared: break\ndefault: break\n}\n",
    "bare allows call": "allows(.shared)\n",
    "bare equality": "let b = store == .shared\n",
    "a label ending in scope": "func f(myscope s: KeychainStore) {}\nf(myscope: .shared)\n",
    # Codex r2 P2: the scope exemption is only as good as the type of what it is passed to.
    "scope-labelled helper (Codex probe)": (
        "func cleanUp(scope store: KeychainStore) { _ = store }\ncleanUp(scope: .shared)\n"
    ),
    "scope label only": "func cleanUp(scope: KeychainStore) {}\ncleanUp(scope: .shared)\n",
    "scope optional": "func cleanUp(scope store: KeychainStore?) {}\n",
    "scope unwrapped": "func cleanUp(scope store: KeychainStore!) {}\n",
    "scope Optional generic": "func cleanUp(scope store: Optional<KeychainStore>) {}\n",
    "scope inout": "func cleanUp(scope store: inout KeychainStore) {}\n",
    "scope underscore name": "func cleanUp(scope _: KeychainStore) {}\n",
    "scope backticked type": "func cleanUp(scope store: `KeychainStore`) {}\n",
    "scope on its own line": "func cleanUp(\n    scope store: KeychainStore\n) {}\n",
    "scope initializer": "init(scope store: KeychainStore) {}\n",
    "scope property": "struct W { let scope: KeychainStore }\nW(scope: .shared)\n",
    "scope generic": "func cleanUp<S: KeychainStore>(scope store: S) {}\n",
    "scope where clause": "func cleanUp<S>(scope store: S) where S == KeychainStore {}\n",
    "custom equality": "static func == (l: KeychainStore, r: Foo) -> Bool { true }\nlet b = x.policy == .shared\n",
    "custom not equal": "func != (l: Foo, r: KeychainStore) -> Bool { true }\n",
    "custom pattern match": "func ~= (l: KeychainStore, r: Foo) -> Bool { true }\n",
    "custom allows": "func allows(_ s: KeychainStore) -> Bool { true }\n",
    "allows closure": "let allows: (KeychainStore) -> Bool = { _ in true }\n",
    "policy holding a store": "var policy: KeychainStore\n",
    # Codex r2 P2: an interpolation is executable code.
    "interpolated helper call (Codex probe)": 'print("cleanup: \\(Self.cleanUp(.shared))")\n',
    "nested parentheses": 'print("x \\(a(b(c(.shared))))")\n',
    "nested string inside": 'print("x \\(a("y", .shared))")\n',
    "multi-line string": 'let m = """\n  x \\(cleanUp(.shared))\n"""\n',
    "two interpolations": 'print("\\(a) and \\(cleanUp(.shared))")\n',
}
IMPLICIT_LEGITIMATE = {
    "URLSession member": "let s = URLSession.shared\n",
    "scope label": "store.set(v, for: .x, scope: .shared)\n",
    "policy comparison": "#expect(name.policy == .shared)\n#expect(a.policy != .shared)\n",
    "policy allows": "#expect(!CredentialName.x.policy.allows(.shared))\n",
    "scope declarations": (
        "func a(scope: KeychainScope) {}\nfunc b(scope store: KeychainScope) {}\nlet scope: KeychainScope\n"
        "func c(_ store: KeychainStore, scope: KeychainScope) {}\nfunc d(\n    store: KeychainStore,\n    scope: .shared\n) {}\n"
    ),
    "unrelated operators": "static func == (l: Foo, r: Foo) -> Bool { true }\nfunc allows(_ s: KeychainScope) -> Bool { true }\n",
    "interpolation with an explicit type": 'print("router: \\(ApprovalSignerRouter.shared.x)")\nlet t = "plain .shared text"\n',
    "raw string with a backslash paren": 'let r = #"allows\\(\\s*\\.shared"#\n',
    "raw string with quotes inside": 'let r = #"(?<![A-Za-z0-9_"])KeychainStore\\s*\\.\\s*shared"#\n',
    "router": "ApprovalSignerRouter.shared.sign(x)\n",
    "member chain on the next line": "let r = ApprovalSignerRouter\n    .shared\n",
    "member of a call result": "make().shared\nlist[0].shared\n",
    "longer names": "let sharedCount = 1\nlet x = a.shared2\n",
    "commented and quoted": '// cleanUp(.shared)\nlet n = "cleanUp(.shared)"\n',
    "own store": 'let unique = "san.Growin.credentials.v1.test.x"\nlet own = KeychainStore(service: unique)\ncleanUp(own)\n',
    "extension of the error": "extension KeychainStoreError { var x: Int { 1 } }\n",
}
PRODUCTION_SERVICE_SAMPLES = {
    "inside an interpolation": 'print("\\(KeychainStore.productionService)")\n',
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
    # Everything the alias rules allowed stays allowed, except the shared store and its service
    # name, which only the app may use.
    for name, text in LEGITIMATE_SAMPLES.items():
        if name in APP_ONLY_SAMPLES:
            assert keychain_test_text_offences(text), f"app-only use went undetected in a test: {name}"
        else:
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


# Review round 3, bug 1: a member call on the shared store passed the test-source scan, because the
# app rule (member calls allowed) was reused for tests.
TEST_SHARED_MEMBER_SAMPLES = {
    "member call (operator probe)": "try KeychainStore.shared.deleteAll()\n",
    "backticked member": "try KeychainStore.`shared`.deleteAll()\n",
    "backticked type": "try `KeychainStore`.shared.deleteAll()\n",
    "newline before .shared": "try KeychainStore\n    .shared.deleteAll()\n",
    "newline after the dot": "try KeychainStore.\n    shared.deleteAll()\n",
    "spaces around the dots": "try KeychainStore . shared . deleteAll()\n",
    "module qualified": "try Growin.KeychainStore.shared.deleteAll()\n",
    "inside an interpolation": 'print("\\(try? KeychainStore.shared.string(for: .x, scope: .shared))")\n',
    "inside a raw interpolation": 'print(#"\\#(try? KeychainStore.shared.deleteAll())"#)\n',
}


def test_a_test_source_may_not_call_any_member_of_the_shared_store():
    for name, text in TEST_SHARED_MEMBER_SAMPLES.items():
        assert any("shared store" in hit for hit in keychain_test_text_offences(text)), f"went undetected: {name}"
    # The probe is a member call, which the app rule allows in the four Security files.
    assert keychain_alias_offences("try KeychainStore.shared.deleteAll()\n") == []


# Review round 3, bug 2: a store built on the production service literal passed. The rule is now
# positive: one plain literal under the test prefix, or a `let` bound to one on the line before.
SERVICE_ARGUMENT_BAD = {
    "production literal (operator probe)": 'let s = KeychainStore(service: "san.Growin.credentials.v1")\n',
    "explicit init": 'let s = KeychainStore.init(service: "san.Growin.credentials.v1")\n',
    "implicit init": 'let s: KeychainStore = .init(service: "san.Growin.credentials.v1")\n',
    "metatype init": 'let s = type(of: other).init(service: "san.Growin.credentials.v1")\n',
    "backticked": 'let s = `KeychainStore`(`service`: "san.Growin.credentials.v1")\n',
    "multi-line arguments": 'let s = KeychainStore(\n    service:\n        "san.Growin.credentials.v1"\n)\n',
    "inside an interpolation": 'print("\\(KeychainStore(service: "san.Growin.credentials.v1"))")\n',
    "concatenated after the test prefix": 'let s = KeychainStore(service: "san.Growin.credentials.v1.test." + x)\n',
    "concatenated production name": 'let s = KeychainStore(service: "san.Growin.credentials." + "v1")\n',
    "whole value interpolated": 'let s = KeychainStore(service: "\\(name)")\n',
    "interpolation before the prefix": 'let s = KeychainStore(service: "\\(p)san.Growin.credentials.v1.test.x")\n',
    "other interpolation after the prefix": 'let s = KeychainStore(service: "san.Growin.credentials.v1.test.\\(name)")\n',
    "escape inside the prefix": 'let s = KeychainStore(service: "san.Growin.credentials.v1\\u{2E}test.x")\n',
    "prefix without its dot": 'let s = KeychainStore(service: "san.Growin.credentials.v1.testx")\n',
    "empty literal": 'let s = KeychainStore(service: "")\n',
    "raw string": 'let s = KeychainStore(service: #"san.Growin.credentials.v1.test.x"#)\n',
    "multi-line string": 'let s = KeychainStore(service: """\n    san.Growin.credentials.v1.test.x\n    """)\n',
    "literal with a member call": 'let s = KeychainStore(service: "san.Growin.credentials.v1.test.x".lowercased())\n',
    "ternary": 'let s = KeychainStore(service: flag ? "san.Growin.credentials.v1.test.x" : other)\n',
    "member access": "let s = KeychainStore(service: Self.name)\n",
    "closure argument": "let make = { KeychainStore(service: $0) }\n",
    "no label": 'let s = KeychainStore("san.Growin.credentials.v1.test.x")\n',
    "unbound identifier": "let s = KeychainStore(service: name)\n",
    "identifier bound to the production literal": 'let name = "san.Growin.credentials.v1"\nlet s = KeychainStore(service: name)\n',
    "identifier bound two statements back": (
        'let name = "san.Growin.credentials.v1.test.x"\nprint(1)\nlet s = KeychainStore(service: name)\n'
    ),
    "identifier bound with var": 'var name = "san.Growin.credentials.v1.test.x"\nlet s = KeychainStore(service: name)\n',
    "a different identifier bound": 'let other = "san.Growin.credentials.v1.test.x"\nlet s = KeychainStore(service: name)\n',
    "binding that continues": (
        'let name = "san.Growin.credentials.v1.test.x"\n    .uppercased()\nlet s = KeychainStore(service: name)\n'
    ),
}
SERVICE_ARGUMENT_GOOD = {
    "uuid suffix": 'let s = KeychainStore(service: "san.Growin.credentials.v1.test.\\(UUID().uuidString)")\n',
    "labelled uuid suffix": 'ukStore = KeychainStore(service: "san.Growin.credentials.v1.test.uk.\\(UUID().uuidString)")\n',
    "plain suffix": 'let s = KeychainStore(service: "san.Growin.credentials.v1.test.fixed")\n',
    "multi-line arguments": 'let s = KeychainStore(\n    service: "san.Growin.credentials.v1.test.\\(UUID().uuidString)"\n)\n',
    "bound on the line before": (
        'let service = "san.Growin.credentials.v1.test.\\(UUID().uuidString)"\n'
        "let store = KeychainStore(service: service)\n"
    ),
    "bound before a return": (
        'let service = "san.Growin.credentials.v1.test.\\(UUID().uuidString)"\n'
        "return (KeychainStore(service: service), service)\n"
    ),
    "planted text in a string": 'let n = "KeychainStore(service: other)"\n',
    "commented": '// KeychainStore(service: "san.Growin.credentials.v1")\n',
    "type name only": "func f(_ s: KeychainStore) {}\n",
    "another type with a service": "let raw = RawKeychain(service: service)\n",
}


def test_every_store_a_test_builds_takes_a_plain_test_literal():
    for name, text in SERVICE_ARGUMENT_BAD.items():
        assert service_argument_offences(reduce_swift(text)), f"went undetected: {name}"
        assert keychain_test_text_offences(text), f"went undetected: {name}"
    for name, text in SERVICE_ARGUMENT_GOOD.items():
        assert keychain_test_text_offences(text) == [], name


def test_the_real_store_construction_sites_are_all_seen_and_all_pass():
    """Positive control: the rule looks at the five real call sites, three literal and two `let` bound."""
    sites = []
    for path in sorted(APP_TESTS.rglob("*.swift")):
        reduced = reduce_swift(path.read_text(encoding="utf-8"))
        sites += [path.name for _ in STORE_CONSTRUCTION.finditer(reduced.bare)]
        assert service_argument_offences(reduced) == [], path.name
    assert sorted(sites) == [
        "ApprovalWorkspaceTests.swift",
        "KeychainScopeTests.swift",
        "PracticeApprovalBiometricTests.swift",
        "RelayOrderReviewTests.swift",
        "RelaySigningTestSupport.swift",
    ], sites


# Review round 3, Codex gap A: raw strings. The old prepass blanked a raw string whole, so the
# code in `\#(...)` was dropped, and it matched raw delimiters before comments and plain strings,
# so a stray `#"""` in a comment could blank real code.
RAW_STRING_SAMPLES = {
    "raw interpolation (Codex probe)": 'print(#"x \\#(Self.cleanUp(.shared)) y"#)\n',
    "two hashes": 'print(##"x \\##(cleanUp(.shared))"##)\n',
    "multi-line raw": 'let m = #"""\n    \\#(cleanUp(.shared))\n    """#\n',
    "raw interpolation of the store": 'let m = #"\\#(KeychainStore.shared)"#\n',
    "raw interpolation of the service": 'let m = #"\\#(KeychainStore(service: "san.Growin.credentials.v1"))"#\n',
    "raw delimiters in comments around code": '// #"""\nlet s = KeychainStore.shared\n// """#\n',
    "raw delimiters in strings around code": 'let a = "#\\"\\"\\""\nlet s = KeychainStore.shared\nlet b = "\\"\\"\\"#"\n',
}
# Fail closed: a file the reader cannot finish is an offence on its own, whatever it hides.
UNBALANCED_SAMPLES = {
    "unterminated multi-line raw": 'let r = #"""\nlet s = 1\n',
    "closed with too few hashes": 'let r = ##"abc"#\nlet s = 1\n',
    "unterminated single-line raw": 'let r = #"abc\nlet s = 1\n',
    "unterminated plain string": 'let r = "abc\nlet s = 1\n',
    "unterminated interpolation": 'let r = "\\(f(\n',
    "unterminated block comment": "/* let s = 1\n",
}


def test_raw_string_interpolations_are_scanned_as_code():
    for name, text in RAW_STRING_SAMPLES.items():
        assert keychain_test_text_offences(text), f"went undetected: {name}"
    # In a raw string `\(` is text, not an interpolation, and quotes inside do not end it.
    assert reduce_swift('let r = #"a \\(b) "c" d"#\n').bare == 'let r = ""\n'
    assert reduce_swift('let r = ##"\\#(x) \\##(y)"##\n').bare == 'let r = "" (y) \n'


def test_a_file_that_cannot_be_read_to_the_end_is_refused():
    for name, text in UNBALANCED_SAMPLES.items():
        assert reduce_swift(text).errors, f"reader did not notice: {name}"
        assert any("cannot be read" in hit for hit in keychain_test_text_offences(text)), name
        assert any("cannot be read" in hit for hit in launcher_offences(text)), name
        assert ui_launch_offences(_with("BadUITests.swift", text), KEY), name
    # The real trees all read to the end.
    for root in (APP, APP_TESTS, UI_TESTS):
        for path in root.rglob("*.swift"):
            assert reduce_swift(path.read_text(encoding="utf-8")).errors == (), path


# Review round 3, Codex gap B: a declaration split over lines escaped the one-line patterns.
MULTILINE_DECLARATION_SAMPLES = {
    "Optional generic over lines (Codex probe)": "func cleanUp(scope store: Optional<\n    KeychainStore\n>) {}\n",
    "type on the next line": "func cleanUp(\n    scope store:\n        KeychainStore?\n) {}\n",
    "optional property over lines": "var scope:\n    KeychainStore?\n",
    "Optional property over lines": "var scope: Optional<\n    KeychainStore> = nil\n",
    "allows over lines (Codex probe)": "func allows(\n    _ s: KeychainStore\n) -> Bool { true }\n",
    "allows with the paren on the next line": "func allows\n(_ s: KeychainStore) -> Bool { true }\n",
    "equality over lines": "static func ==\n(l: KeychainStore, r: Foo) -> Bool { true }\n",
    "operator on the next line": "static func\n== (l: KeychainStore, r: Foo) -> Bool { true }\n",
    "where clause on the next line": "func cleanUp<S>(scope store: S)\n    where S == KeychainStore {}\n",
    "policy over lines": "var policy:\n    KeychainStore\n",
    "allows closure over lines": "let allows: (\n    KeychainStore\n) -> Bool = { _ in true }\n",
}
MULTILINE_DECLARATION_LEGITIMATE = {
    "scope parameter on its own line": "func c(\n    _ store: KeychainStore,\n    scope: KeychainScope\n) {}\n",
    "scope property then a store": "let scope: KeychainScope\nlet store: KeychainStore\n",
    "Optional scope then a store": "var scope: Optional<\n    KeychainScope> = nil\nfunc f(_ s: KeychainStore) {}\n",
    "allows over lines on a scope": "func allows(\n    _ s: KeychainScope\n) -> Bool { true }\nlet store: KeychainStore\n",
    "policy read then a store": (
        'let policy = name.policy\nlet store = KeychainStore(service: "san.Growin.credentials.v1.test.x")\n'
    ),
    "comparison then a store": "if i<n { f() }\nlet store: KeychainStore\n",
}


def test_declarations_split_over_lines_are_caught():
    for name, text in MULTILINE_DECLARATION_SAMPLES.items():
        assert keychain_test_text_offences(text), f"went undetected: {name}"
    for name, text in MULTILINE_DECLARATION_LEGITIMATE.items():
        assert keychain_test_text_offences(text) == [], name


def test_logical_lines_join_only_inside_a_declaration():
    assert logical_lines("f(\n  a,\n  b\n)\nlet x = 1\n") == "f(   a,   b )\nlet x = 1\n"
    assert logical_lines("var s: Optional<\n  T> = nil\nlet y = 2\n") == "var s: Optional<   T> = nil\nlet y = 2\n"
    assert logical_lines("x {\n  a\n  b\n}\n") == "x {\n  a\n  b\n}\n"


# --------------------------------------------------------------------------------------------
# 2. GrowinUITests launch scan, and process launchers in both test targets
# --------------------------------------------------------------------------------------------

UI_HELPER = "LaunchSupport.swift"
UI_APP_TYPE = "XCUIApplication"
HELPER_CALL = "GrowinLaunch.makeApp()"
# The second key makeApp sets: it marks the UI-test app process as a test host for the Keychain guard
# (TestHostDetection reads it). Do not reuse the suppression key for that.
UI_TEST_HOST_KEY = "GROWIN_UI_TEST_HOST"
SKIP_KEY_DECLARATION = re.compile(r'static\s+let\s+skipEnvironmentKey\s*=\s*"([A-Z0-9_]+)"')
LAUNCH_OR_ACTIVATE = re.compile(r"\.\s*(?:launch|activate)\s*\(")
LAUNCH_ENVIRONMENT = re.compile(_NOT_AFTER_IDENT + r"launchEnvironment" + _NOT_BEFORE_IDENT)
# Ways to start or open an app from a test without going through XCUIApplication: Foundation's
# process types, the libc spawn and exec family, Launch Services, NSWorkspace, AppleScript, and the
# by-name class lookups that would reach any of these without spelling them. Read on reduced code,
# so `ProcessInfo`, `Font.system(size:)`, comments and string text are not findings. Neither test
# target uses any of these today, so there is no exemption.
LAUNCHERS = re.compile(
    _NOT_AFTER_IDENT
    + r"(?:Process|NSTask|NSWorkspace|NSRunningApplication|NSAppleScript|NSUserAppleScriptTask|NSUserUnixTask"
    + r"|OSAScript|popen|posix_spawn\w*|exec[lv]p?e?|LSOpen\w*|NSClassFromString|objc_getClass|objc_lookUpClass"
    + r"|dlopen|dlsym)"
    + _NOT_BEFORE_IDENT
    + r"|(?<![A-Za-z0-9_.])system\s*\("
    + r"|" + _NOT_AFTER_IDENT + r"(?:Darwin|Glibc|Foundation)\s*\.\s*system" + _NOT_BEFORE_IDENT
)
# The tool itself is a string, so it is looked for in the source text.
OPEN_TOOL = "/usr/bin/open"
MAKE_APP_SIGNATURE = re.compile(r"\bfunc\s+makeApp\s*\(\s*\)\s*->\s*" + UI_APP_TYPE + r"\s*\{")


def launcher_offences(text: str, reduced: Reduced | None = None) -> list[str]:
    """Each way one test file could start a process or open an app outside the UI-test helper."""
    reduced = reduced or reduce_swift(text)
    found = unreadable(reduced)
    found += [f"starts a process or app: {match.group(0)}" for match in LAUNCHERS.finditer(reduced.bare)]
    if OPEN_TOOL in text:
        found.append(f"names {OPEN_TOOL}")
    return found


def _make_app_body_pattern(skip_key: str) -> re.Pattern[str]:
    """The whole body of makeApp: create the app, set the suppression key, set the UI-test-host key, return it."""
    return re.compile(
        r"\s*let\s+(\w+)\s*=\s*" + UI_APP_TYPE + r"\s*\(\s*\)\s*;?\s*"
        r"\1\s*\.\s*launchEnvironment\s*\[\s*\"" + re.escape(skip_key) + r"\"\s*\]\s*=\s*\"1\"\s*;?\s*"
        r"\1\s*\.\s*launchEnvironment\s*\[\s*\"" + re.escape(UI_TEST_HOST_KEY) + r"\"\s*\]\s*=\s*\"1\"\s*;?\s*"
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
        found.append(
            f"{UI_HELPER}: makeApp body is not exactly create, set the suppression key, set the UI-test-host key, return"
        )
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
    found += [f"{UI_HELPER}: {item}" for item in launcher_offences(helper)]
    for path, text in sorted(sources.items()):
        if path == UI_HELPER:
            continue
        reduced = reduce_swift(text)
        code = reduced.bare
        if UI_APP_TYPE in text:
            found.append(f"{path}: names {UI_APP_TYPE} outside the helper")
        if LAUNCH_ENVIRONMENT.search(code):
            found.append(f"{path}: touches launchEnvironment outside the helper")
        found += [f"{path}: {item}" for item in launcher_offences(text, reduced)]
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
    f'        app.launchEnvironment["{UI_TEST_HOST_KEY}"] = "1"\n'
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
        "launch in an interpolation": 'print("\\(other.launch())")\n',
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


# Review round 3: the denylist knew four spellings. These are the rest of the ways to start Growin.
LAUNCHER_SAMPLES = {
    "process run (operator probe)": (
        'try Process.run(URL(fileURLWithPath: "/usr/bin/open"), arguments: ["-b", "san.Growin"])\n'
    ),
    "process with a space": 'let p = Process ()\np.executableURL = URL(fileURLWithPath: "/usr/bin/open")\n',
    "process qualified": "let p = Foundation.Process()\n",
    "process as a type": "var p: Process?\n",
    "process in an interpolation": 'print("\\(Process())")\n',
    "process in a raw interpolation": 'print(#"\\#(Process())"#)\n',
    "task": "let t = NSTask()\n",
    "popen": 'let f = popen("open -b san.Growin", "r")\n',
    "spawn": "posix_spawn(&pid, path, nil, nil, args, nil)\n",
    "spawnp": "posix_spawnp(&pid, path, nil, nil, args, nil)\n",
    "spawn attributes": "posix_spawnattr_init(&attributes)\n",
    "execv": "execv(path, args)\n",
    "execve": "execve(path, args, env)\n",
    "execvp": "execvp(name, args)\n",
    "execl": "execl(path, arg, nil)\n",
    "execlp": "execlp(name, arg, nil)\n",
    "execle": "execle(path, arg, nil, env)\n",
    "system": 'system("open -b san.Growin")\n',
    "system spaced": 'system ("open -b san.Growin")\n',
    "Darwin.system": 'Darwin.system("open -b san.Growin")\n',
    "Launch Services URL": "LSOpenCFURLRef(url, nil)\n",
    "Launch Services spec": "LSOpenFromURLSpec(&spec, nil)\n",
    "running application": "NSRunningApplication.runningApplications(withBundleIdentifier: id).first?.activate()\n",
    "workspace open": "NSWorkspace.shared.open(url)\n",
    "workspace open application": "NSWorkspace.shared.openApplication(at: url, configuration: c)\n",
    "AppleScript": 'NSAppleScript(source: "tell application id \\"san.Growin\\" to activate")\n',
    "user AppleScript task": "let t = try NSUserAppleScriptTask(url: url)\n",
    "user unix task": "let t = try NSUserUnixTask(url: url)\n",
    "OSA script": 'let s = OSAScript(source: "activate")\n',
    "class by name": 'let c = NSClassFromString("NS" + "Task")\n',
    "objc class by name": 'let c = objc_getClass("NSTask")\n',
    "dlsym": 'let f = dlsym(handle, "posix_spawn")\n',
    "open tool in a string": 'let tool = "/usr/bin/open"\n',
}
LAUNCHER_LEGITIMATE = {
    "process info": "let info = ProcessInfo.processInfo\nlet env = ProcessInfo.processInfo.environment\n",
    "name containing Process": "@Test func thisTestProcessIsSkipped() {}\n",
    "font system": "let f = Font.system(size: 12)\n",
    "member named system": "let s = config.system(x)\nlet t = .system(.body)\n",
    "longer names": "let systemImage = 1\nlet executableURL = url\nlet execution = 2\nlet processed = 3\n",
    "words in strings and comments": 'let n = "Process() NSWorkspace system("\n// NSWorkspace.shared.open(url)\n',
}


def test_the_ui_launch_scan_catches_other_launchers():
    for name, text in LAUNCHER_SAMPLES.items():
        assert ui_launch_offences(_with("BadUITests.swift", text), KEY), f"went undetected: {name}"
        assert launcher_offences(text), f"went undetected: {name}"
    for name, text in LAUNCHER_LEGITIMATE.items():
        assert ui_launch_offences(_with("OkUITests.swift", text), KEY) == [], name
        assert launcher_offences(text) == [], name
    # The helper itself may not launch either.
    assert ui_launch_offences({UI_HELPER: GOOD_HELPER + "let p = Process()\n", "GoodUITests.swift": GOOD_TEST}, KEY)


def test_the_old_launcher_denylist_missed_most_of_the_launchers():
    old = re.compile(_NOT_AFTER_IDENT + r"(?:NSWorkspace|NSTask|posix_spawn|Process\s*\()")
    missed = [name for name, text in LAUNCHER_SAMPLES.items() if not old.search(strip_swift_comments_and_strings(text))]
    assert len(missed) >= 20, missed


def test_no_app_test_source_starts_a_process():
    """GrowinTests is app-hosted, so a second Growin started from a unit test would run without the
    test-host signals and reach the real Keychain at launch."""
    sources = sorted(APP_TESTS.rglob("*.swift"))
    assert len(sources) > 10
    found = []
    for path in sources:
        found += [f"{path.relative_to(APP_TESTS)}: {item}" for item in launcher_offences(path.read_text(encoding="utf-8"))]
    assert found == []


EARLY_RETURN_HELPER = (
    "enum GrowinLaunch {\n"
    "    @MainActor\n"
    "    static func makeApp() -> XCUIApplication {\n"
    '        if condition { return .init(bundleIdentifier: "san.Growin") }\n'
    "        let app = XCUIApplication()\n"
    f'        app.launchEnvironment["{KEY}"] = "1"\n'
    f'        app.launchEnvironment["{UI_TEST_HOST_KEY}"] = "1"\n'
    "        return app\n"
    "    }\n"
    "}\n"
)


def test_the_whole_makeapp_body_is_pinned_not_a_substring_of_it():
    def offences_for(helper: str, key: str = KEY) -> list[str]:
        return ui_launch_offences({UI_HELPER: helper, "GoodUITests.swift": GOOD_TEST}, key)

    ui_line = f'        app.launchEnvironment["{UI_TEST_HOST_KEY}"] = "1"\n'
    body = f'        let app = XCUIApplication()\n        app.launchEnvironment["{KEY}"] = "1"\n{ui_line}        return app\n'
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
        # The UI-test-host key line (Keychain guard): present, exact value, exact name, on the app, in order.
        "UI-test-host key missing": with_body(body.replace(ui_line, "")),
        "UI-test-host key set to 0": with_body(body.replace(ui_line, ui_line.replace('"1"', '"0"'))),
        "UI-test-host key misspelled": with_body(body.replace(ui_line, ui_line.replace("UI_TEST_HOST", "UI_TEST_HOS"))),
        "UI-test-host key set on another variable": with_body(body.replace(ui_line, ui_line.replace("app.", "other."))),
        "UI-test-host key only in a comment": with_body(body.replace(ui_line, "        // " + ui_line.strip() + "\n")),
        "UI-test-host key before the suppression key": with_body(
            body.replace(ui_line, "").replace(f'        app.launchEnvironment["{KEY}"] = "1"\n', ui_line + f'        app.launchEnvironment["{KEY}"] = "1"\n')
        ),
        "UI-test-host key twice": with_body(body.replace(ui_line, ui_line + ui_line)),
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
        f'        let app = XCUIApplication() ; app\n            .launchEnvironment["{KEY}"] = "1";\n'
        f'        app.launchEnvironment["{UI_TEST_HOST_KEY}"] = "1";\n        return app\n'
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


# --------------------------------------------------------------------------------------------
# The tripwire exemption (PR #575): one exact path, one exact expression, nothing else loosened
# --------------------------------------------------------------------------------------------


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, text in files.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return tmp_path


def test_the_tripwire_exemption_is_one_exact_path_and_one_exact_call(tmp_path):
    allowed = f"func f() {{ _ = try {TRIPWIRE_SHARED_CALL} }}\n"
    exact = str(TRIPWIRE_PATH)
    assert keychain_offences_in_test_sources(_tree(tmp_path / "a", {exact: allowed})) == []
    # The same text anywhere else is still the ban: another folder, another name, the same name elsewhere.
    for other in (
        "Other/ProductionTripwireTests.swift",
        "WorkspaceIsolation/ProductionTripwireTests2.swift",
        "WorkspaceIsolation/OtherTests.swift",
        "ProductionTripwireTests.swift",
    ):
        found = keychain_offences_in_test_sources(_tree(tmp_path / other.replace("/", "_"), {other: allowed}))
        assert any("shared store" in item for item in found), other


def test_the_tripwire_exemption_does_not_loosen_anything_else_in_the_exact_file(tmp_path):
    exact = str(TRIPWIRE_PATH)
    planted = {
        "a second shared-store call": f"{TRIPWIRE_SHARED_CALL.replace('string(', 'set(')}\n",
        "the same call with another credential": "KeychainStore.shared.string(for: .openaiApiKey, scope: .shared)\n",
        "an alias": "let s = KeychainStore.shared\n",
        "the production service name": "let n = KeychainStore." + "production" + "Service\n",
        "a store built on a non-test literal": 'let s = KeychainStore(service: "x")\n',
        "an extension": "extension KeychainStore {}\n",
    }
    for label, extra in planted.items():
        text = f"func f() {{ _ = try {TRIPWIRE_SHARED_CALL} }}\n{extra}"
        found = keychain_offences_in_test_sources(_tree(tmp_path / label.replace(" ", "_"), {exact: text}))
        assert found, f"went undetected: {label}"


def test_the_real_tripwire_file_is_the_only_file_the_exemption_changes():
    changed = [
        str(path.relative_to(APP_TESTS))
        for path in sorted(APP_TESTS.rglob("*.swift"))
        if tripwire_exempted(path.relative_to(APP_TESTS), path.read_text(encoding="utf-8"))
        != path.read_text(encoding="utf-8")
    ]
    assert changed == [str(TRIPWIRE_PATH)]
