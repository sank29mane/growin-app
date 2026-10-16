"""The app no longer sends Trading 212 keys to the backend (66-02 Task 3, D-05, D-07).

A source scan of the Swift app, so it runs in CI without Xcode. The Xcode build and
unit tests are the other half of the proof and are recorded in the 66-02 SUMMARY.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
APP = REPO / "Growin"

FORBIDDEN = (
    "mcp/trading212/config",
    "switchAccountConfig",
    "struct TradingConfig:",
    "TradingConfig(",
    '"invest_key"',
    '"invest_secret"',
    '"isa_key"',
    '"isa_secret"',
    "invest_key",
    "isa_key",
)


def offences(root: Path) -> list[str]:
    found = []
    for path in sorted(root.rglob("*.swift")):
        text = path.read_text(encoding="utf-8")
        for token in FORBIDDEN:
            if token in text:
                found.append(f"{path.relative_to(root)}: {token}")
    return found


# The Keychain credential enum is the one place these names may exist: it classifies the
# legacy items (policy .fixed(.uk)) so the launch migration can still find and scope them.
CREDENTIAL_ENUM = Path("Security") / "Workspace.swift"

# The launch cleanup must spell the legacy account names to delete them. It is NOT exempt as a
# file: only the exact stripped lines below are skipped, and the credential scan reads every other
# line. `legacy_cleanup_write_offences` separately holds the file to "delete only".
LEGACY_CLEANUP = Path("Security") / "LegacyT212KeychainCleanup.swift"
CREDENTIAL_LINE_ALLOWLIST = {
    LEGACY_CLEANUP: frozenset({'static let flatAccounts = ["trading212ApiKey", "trading212ApiSecret"]'}),
}

# A Trading 212 key or secret by name: trading212ApiKey, trading212IsaApiSecret, t212InvestKey,
# "t212_isa_secret" and so on. `t212AccountType` and `let trading212: String` do not match.
# `key(?!chain)` keeps the word "Keychain" (LegacyT212KeychainCleanup, T212Keychain) from counting
# as "key"; t212KeychainKey still matches on its trailing Key.
T212_SECRET_NAME = re.compile(r"(?i)(?:trading212|t212)\w*?(?:key(?!chain)|secret)")

# What the delete-only cleanup file may never contain, checked on code with comments and string
# literals removed. Each is a way to store a credential (add, update, value data, property
# wrappers) or to read one back (copy, return flags, the app's own Keychain wrappers, a dump of
# all defaults). `kSecReturn` is a prefix on purpose: Data, Attributes, Ref, PersistentRef read.
CLEANUP_FORBIDDEN = (
    "SecItemAdd",
    "SecItemUpdate",
    "SecItemCopyMatching",
    "kSecValue",
    "kSecReturn",
    "KeychainStore",
    "KeychainStorage",
    "setValue",
    "@AppStorage",
    "@SceneStorage",
    "dictionaryRepresentation",
)
# The file keeps two non-secret progress flags in UserDefaults. Every write and every read of a
# default must name exactly one of them. `removeObject(forKey:)` deletes and is not checked.
CLEANUP_FLAG_KEYS = frozenset({"completionKey", "failedLaunchesKey"})
CLEANUP_DEFAULTS_CALL = re.compile(
    r"\.\s*(?:set|string|data|object|value|array|dictionary|stringArray|bool|integer|double|float|url)\s*\("
)
CLEANUP_FOR_KEY = re.compile(r"(?:^|,)\s*forKey\s*:\s*(\w+)\s*$")
CLEANUP_DELETE_CALL = re.compile(r"\bSecItemDelete\s*\(")


def strip_swift_comments_and_strings(text: str) -> str:
    """Swift source with comments blanked and string literals reduced to `""`.

    One pass, so `//` inside a string is not a comment and a quote inside a comment is not a
    string. Block comments nest, as in Swift. Newlines are kept so line numbers still line up.
    """
    out = []
    i, n = 0, len(text)
    while i < n:
        two = text[i : i + 2]
        if text.startswith('"""', i):
            end = text.find('"""', i + 3)
            stop = n if end < 0 else end + 3
            out.append('""' + "\n" * text.count("\n", i, stop))
            i = stop
        elif text[i] == '"':
            i += 1
            while i < n and text[i] != '"' and text[i] != "\n":
                i += 2 if text[i] == "\\" else 1
            i += 1
            out.append('""')
        elif two == "//":
            while i < n and text[i] != "\n":
                i += 1
            out.append(" ")
        elif two == "/*":
            depth = 0
            while i < n:
                if text.startswith("/*", i):
                    depth += 1
                    i += 2
                elif text.startswith("*/", i):
                    depth -= 1
                    i += 2
                    if depth == 0:
                        break
                else:
                    out.append("\n" if text[i] == "\n" else "")
                    i += 1
            out.append(" ")
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _call_arguments(code: str, open_paren: int) -> str | None:
    """Text between the parenthesis at code[open_paren] and its match, or None if unbalanced."""
    depth = 0
    for i in range(open_paren, len(code)):
        if code[i] == "(":
            depth += 1
        elif code[i] == ")":
            depth -= 1
            if depth == 0:
                return code[open_paren + 1 : i]
    return None


def legacy_cleanup_write_offences(text: str) -> list[str]:
    """Reasons the cleanup source is more than a delete: any store of, or read of, a value."""
    code = strip_swift_comments_and_strings(text)
    found = [f"forbidden: {token}" for token in CLEANUP_FORBIDDEN if token in code]
    if not CLEANUP_DELETE_CALL.search(code):
        found.append("missing: SecItemDelete call")
    for match in CLEANUP_DEFAULTS_CALL.finditer(code):
        arguments = _call_arguments(code, match.end() - 1)
        key = CLEANUP_FOR_KEY.search(arguments) if arguments is not None else None
        if key is None or key.group(1) not in CLEANUP_FLAG_KEYS:
            shown = " ".join((arguments or "<unbalanced>").split())
            found.append(f"defaults access outside the two flags: {match.group(0).strip()}{shown})")
    return found

# SwiftUI controls that take typed input. The scan reads the control and its modifier chain.
INPUT_CONTROL = re.compile(r"\b(?:SecureField|TextField|TextEditor)\b")
T212_WORDING = re.compile(r"(?i)trading\s*212|t212")


def t212_credential_offences(root: Path) -> list[str]:
    """Swift lines that read, write or bind a Trading 212 key or secret by name."""
    found = []
    for path in sorted(root.rglob("*.swift")):
        relative = path.relative_to(root)
        if relative == CREDENTIAL_ENUM:
            continue
        allowed = CREDENTIAL_LINE_ALLOWLIST.get(relative, frozenset())
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip() in allowed:
                continue
            if T212_SECRET_NAME.search(line):
                found.append(f"{relative}:{number}: {line.strip()}")
    return found


def t212_input_field_offences(root: Path) -> list[str]:
    """Typed-input controls whose label, binding or modifiers mention Trading 212."""
    found = []
    for path in sorted(root.rglob("*.swift")):
        lines = path.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            if not INPUT_CONTROL.search(line):
                continue
            window = [line]
            for follow in lines[index + 1 : index + 7]:
                # Stop at a blank line or the next control: that is the next statement.
                if not follow.strip() or INPUT_CONTROL.search(follow):
                    break
                window.append(follow)
            if T212_WORDING.search("\n".join(window)):
                found.append(f"{path.relative_to(root)}:{index + 1}: {line.strip()}")
    return found


CONTAINER_NAME = re.compile(r"\b(?:Section|GroupBox|SettingsCard)\b")
TRAILING_CLOSURE = re.compile(r"(header|footer|label)\s*:\s*\{")


def _after_string(text: str, i: int) -> int:
    """Index just past the string literal that opens at text[i] == '"'."""
    i += 1
    while i < len(text) and text[i] != '"':
        i += 2 if text[i] == "\\" else 1
    return i + 1


def _closing(text: str, i: int) -> int:
    """Index of the bracket matching text[i], skipping string literals. -1 if unbalanced."""
    opener = text[i]
    closer = {"(": ")", "{": "}"}[opener]
    depth = 0
    while i < len(text):
        char = text[i]
        if char == '"':
            i = _after_string(text, i)
            continue
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def labelled_containers(text: str) -> list[tuple[int, str, str]]:
    """(offset, label text, body text) for each Section / GroupBox / SettingsCard with a body.

    The label is the call arguments plus any `header:` / `label:` closure, so
    `Section("Trading 212")`, `GroupBox(label: Text("T212"))` and
    `Section { ... } header: { Text("Trading 212") }` all expose their wording.
    """
    found = []
    for match in CONTAINER_NAME.finditer(text):
        j = match.end()
        while j < len(text) and text[j].isspace():
            j += 1
        label = ""
        if j < len(text) and text[j] == "(":
            end = _closing(text, j)
            if end < 0:
                continue
            label = text[j + 1 : end]
            j = end + 1
            while j < len(text) and text[j].isspace():
                j += 1
        if j >= len(text) or text[j] != "{":
            continue
        end = _closing(text, j)
        if end < 0:
            continue
        body = text[j + 1 : end]
        j = end + 1
        while True:
            while j < len(text) and text[j].isspace():
                j += 1
            trailing = TRAILING_CLOSURE.match(text, j)
            if not trailing:
                break
            open_at = trailing.end() - 1
            end = _closing(text, open_at)
            if end < 0:
                break
            if trailing.group(1) in ("header", "label"):
                label += "\n" + text[open_at + 1 : end]
            j = end + 1
        found.append((match.start(), label, body))
    return found


def t212_container_offences(root: Path) -> list[str]:
    """Typed-input controls inside a Section / GroupBox / SettingsCard whose label names Trading 212.

    Catches `Section("Trading 212") { SecureField("API Key", ...) }`, where neither the field nor
    its modifiers mention Trading 212. Obfuscation such as "trading212" + "ApiKey" is out of scope:
    the threat model is an honest but fallible change, not someone hiding a field from this scan.
    """
    found = []
    for path in sorted(root.rglob("*.swift")):
        text = path.read_text(encoding="utf-8")
        for offset, label, body in labelled_containers(text):
            if T212_WORDING.search(label) and INPUT_CONTROL.search(body):
                line = text.count("\n", 0, offset) + 1
                found.append(f"{path.relative_to(root)}:{line}: input control inside a Trading 212 container")
    return found


def assert_exemptions_are_not_vacuous() -> None:
    """The enum file and every allow-listed line must really exist, or the allowance does nothing."""
    assert (APP / CREDENTIAL_ENUM).is_file(), CREDENTIAL_ENUM
    for relative, lines in CREDENTIAL_LINE_ALLOWLIST.items():
        assert (APP / relative).is_file(), relative
        present = {line.strip() for line in (APP / relative).read_text(encoding="utf-8").splitlines()}
        for line in lines:
            assert line in present, f"allow-listed line missing from {relative}: {line}"


def test_no_swift_source_names_the_key_push_route_or_its_payload():
    assert len(list(APP.rglob("*.swift"))) > 20
    assert offences(APP) == []


def test_account_switching_still_posts_to_account_active():
    service = (APP / "Services" / "PortfolioDataService.swift").read_text(encoding="utf-8")
    assert '"/account/active"' in service
    model = (APP / "ViewModels" / "PortfolioViewModel.swift").read_text(encoding="utf-8")
    assert "syncAccount(accountType: newType)" in model


def test_the_trading_212_settings_section_reads_no_keychain_keys_and_makes_no_request():
    settings = (APP / "Views" / "SettingsView.swift").read_text(encoding="utf-8")
    start = settings.index("struct TradingConfigSection: View {")
    end = settings.index("struct AccountStatusSection: View {")
    section = settings[start:end]
    assert "KeychainStorage" not in section
    assert "URLSession" not in section and "URLRequest" not in section
    assert "SecureField" not in section
    assert "launch environment" in section


def test_no_swift_source_reintroduces_a_t212_key_field_or_keychain_write():
    """ConfigView and SettingsView lost their Trading 212 key fields in 66-02. Keep them gone."""
    assert len(list(APP.rglob("*.swift"))) > 20
    # The exemption must point at a real file, or the scan silently exempts nothing.
    assert_exemptions_are_not_vacuous()
    assert t212_input_field_offences(APP) == []
    assert t212_credential_offences(APP) == []
    assert t212_container_offences(APP) == []


def test_the_container_scan_really_sees_the_settings_trading_212_card():
    """Guards the container scan against passing vacuously because the parser found nothing."""
    settings = (APP / "Views" / "SettingsView.swift").read_text(encoding="utf-8")
    cards = [
        (label, body)
        for _, label, body in labelled_containers(settings)
        if T212_WORDING.search(label)
    ]
    assert cards, "no Trading 212 container parsed in SettingsView: the container scan is blind"
    assert any("Picker" in body for _, body in cards)
    assert not any(INPUT_CONTROL.search(body) for _, body in cards)


def test_the_container_scan_catches_a_field_inside_a_trading_212_container(tmp_path):
    planted = {
        "SectionArgs.swift": 'Section("Trading 212") {\n    SecureField("API Key", text: $draft)\n}\n',
        "GroupBoxLabel.swift": 'GroupBox(label: Text("T212 credentials")) {\n    VStack { TextField("Key", text: $k) }\n}\n',
        "HeaderClosure.swift": (
            "Section {\n    SecureField(\"Secret\", text: $s)\n} header: {\n    Text(\"Trading 212 MCP\")\n}\n"
        ),
        "CardTitle.swift": 'SettingsCard(title: "Trading 212 API", icon: "x") {\n    VStack {\n        TextEditor(text: $t)\n    }\n}\n',
        "BraceInString.swift": 'Section("Trading 212 {") {\n    SecureField("API Key", text: $draft)\n}\n',
    }
    for name, body in planted.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    hits = t212_container_offences(tmp_path)
    for name in planted:
        assert any(hit.startswith(name) for hit in hits), name


def test_the_container_scan_ignores_other_containers_and_non_field_t212_content(tmp_path):
    (tmp_path / "Fine.swift").write_text(
        'Section("OpenAI & Gemini") {\n    SecureField("sk-...", text: $openai)\n}\n'
        'Section("Trading 212") {\n    Picker("Account", selection: $t) { Text("Invest").tag("invest") }\n}\n'
        'SettingsCard(title: "Trading 212 API", icon: "x") {\n    Text("Keys come from the launch environment")\n}\n'
        'Section {\n    Text("x")\n} header: {\n    Text("Trading 212")\n}\n'
        'Section("Other") {\n    SecureField("sk", text: $a)\n}\n',
        encoding="utf-8",
    )
    assert t212_container_offences(tmp_path) == []


def test_the_config_view_has_no_trading_212_binding_or_field():
    config = (APP / "Views" / "ConfigView.swift").read_text(encoding="utf-8")
    assert "SecureField" in config, "ConfigView changed shape: re-point this scan"
    assert not T212_WORDING.search(config)
    assert not T212_SECRET_NAME.search(config)


def test_the_t212_scans_catch_planted_fields_and_writes(tmp_path):
    (tmp_path / "Security").mkdir()
    # The enum file is exempt: the legacy credential names live there.
    (tmp_path / CREDENTIAL_ENUM).write_text("case trading212ApiKey\ncase t212IsaSecret\n", encoding="utf-8")
    allowed_line = next(iter(CREDENTIAL_LINE_ALLOWLIST[LEGACY_CLEANUP]))
    (tmp_path / LEGACY_CLEANUP).write_text(f"    {allowed_line}\n", encoding="utf-8")
    assert t212_credential_offences(tmp_path) == []
    # The cleanup file is exempt for that one line only. Anything else in it is scanned.
    (tmp_path / LEGACY_CLEANUP).write_text(
        f"{allowed_line}\n"
        'defaults.set(secret, forKey: "trading212ApiKey")\n'
        '@AppStorage("trading212ApiKey") var trading212ApiKey = ""\n'
        'let v = defaults.string (forKey: "trading212ApiKey")\n'
        f"{allowed_line} // trailing text makes it a different line\n",
        encoding="utf-8",
    )
    cleanup_hits = t212_credential_offences(tmp_path)
    assert sorted(int(hit.split(":")[1]) for hit in cleanup_hits) == [2, 3, 4, 5]

    planted = {
        "KeychainField.swift": (
            '@KeychainStorage(.trading212ApiKey, scope: .workspace(.uk)) private var trading212ApiKey = ""\n'
            'SecureField("Your T212 API Key", text: $trading212ApiKey)\n'
        ),
        "SecretWrite.swift": "try KeychainStore.shared.set(secret, for: .trading212ApiSecret, scope: .workspace(.uk))\n",
        "InvestKey.swift": '@KeychainStorage(.t212InvestKey, scope: .workspace(.uk)) private var k = ""\n',
        "DefaultsWrite.swift": 'defaults.set(key, forKey: "trading212ApiKey")\n',
        "AppStorage.swift": '@AppStorage("t212_isa_secret") private var s = ""\n',
        "FieldByLabel.swift": 'TextField("Trading 212 key", text: $draft)\n    .accessibilityLabel("field")\n',
        "FieldByModifier.swift": (
            'SecureField("API Key", text: $draft)\n'
            "    .padding()\n"
            '    .accessibilityLabel("Trading 212 Live API Key")\n'
        ),
        "Editor.swift": "TextEditor(text: $t212Notes)\n",
    }
    for name, body in planted.items():
        (tmp_path / name).write_text(body, encoding="utf-8")
    credential_hits = t212_credential_offences(tmp_path)
    field_hits = t212_input_field_offences(tmp_path)
    for name in ("KeychainField.swift", "SecretWrite.swift", "InvestKey.swift", "DefaultsWrite.swift", "AppStorage.swift"):
        assert any(hit.startswith(name) for hit in credential_hits), name
    for name in ("KeychainField.swift", "FieldByLabel.swift", "FieldByModifier.swift", "Editor.swift"):
        assert any(hit.startswith(name) for hit in field_hits), name


def test_the_t212_scans_leave_legitimate_t212_wording_alone(tmp_path):
    (tmp_path / "Fine.swift").write_text(
        'Picker("Trading 212 Account Type", selection: $t212AccountType) { Text("Invest").tag("invest") }\n'
        'Text("Trading 212 keys come from the backend\'s launch environment.")\n'
        "let trading212: String\n"
        "\n"
        'SecureField("sk-...", text: $openaiApiKey)\n',
        encoding="utf-8",
    )
    assert t212_credential_offences(tmp_path) == []
    assert t212_input_field_offences(tmp_path) == []


def test_the_scan_catches_a_planted_key_push(tmp_path):
    planted = tmp_path / "Planted.swift"
    planted.write_text(
        'let url = "\\(base)/mcp/trading212/config"\nlet body = ["invest_key": key]\n',
        encoding="utf-8",
    )
    found = offences(tmp_path)
    assert any("mcp/trading212/config" in item for item in found)
    assert any("invest_key" in item for item in found)


def test_the_secret_name_pattern_skips_keychain_but_keeps_real_key_names():
    # "Keychain" starts with "key" and used to count as one: these are not credential names.
    for fine in (
        "LegacyT212KeychainCleanup.runOnce(service: KeychainStore.productionService)",
        "enum LegacyT212KeychainCleanup {",
        "let t212Keychain = 1",
        'Logger(category: "legacy-t212-keychain")',
    ):
        assert not T212_SECRET_NAME.search(fine), fine
    # Real names still match, including one that mentions Keychain and then says Key.
    for named in (
        "case trading212ApiKey",
        "let trading212ApiSecret = x",
        "t212Secret",
        "t212KeychainKey",
        "trading212Keychain_secret",
        '"t212_isa_secret"',
        "T212INVESTKEY",
    ):
        assert T212_SECRET_NAME.search(named), named


def test_the_legacy_cleanup_file_is_delete_only_and_its_allow_listed_lines_exist():
    assert_exemptions_are_not_vacuous()
    text = (APP / LEGACY_CLEANUP).read_text(encoding="utf-8")
    assert legacy_cleanup_write_offences(text) == []


def test_the_comment_and_string_stripper_keeps_code_and_drops_the_rest():
    stripped = strip_swift_comments_and_strings(
        'let a = "x // not a comment" // SecItemAdd\n'
        "/* outer /* nested SecItemAdd */ still comment */ let b = 1\n"
        'let c = "quote \\" SecItemAdd"\n'
        'let d = """\nSecItemAdd\n"""\n'
        "SecItemDelete(q)\n"
    )
    assert "SecItemAdd" not in stripped
    assert "let b = 1" in stripped and "SecItemDelete(q)" in stripped
    assert stripped.count("\n") == 7  # line numbers survive


REAL_CLEANUP = (APP / LEGACY_CLEANUP).read_text(encoding="utf-8")
DELETE_CALL = "SecItemDelete(q as CFDictionary)\n"


def test_the_delete_only_check_catches_a_write_or_a_read():
    assert legacy_cleanup_write_offences(REAL_CLEANUP) == []
    planted = {
        "SecItemAdd": "SecItemAdd(q as CFDictionary, nil)",
        "SecItemAdd spaced": "SecItemAdd (q as CFDictionary, nil)",
        "SecItemUpdate": "SecItemUpdate(q as CFDictionary, a as CFDictionary)",
        "SecItemCopyMatching": "SecItemCopyMatching(q as CFDictionary, &out)",
        "kSecValueData": "q[kSecValueData as String] = data",
        "kSecReturnData": "q[kSecReturnData as String] = true",
        "kSecReturnAttributes": "q[kSecReturnAttributes as String] = true",
        "KeychainStore": "try KeychainStore.shared.set(v, for: a)",
        "KeychainStorage": "@KeychainStorage(.alpacaApiKey) var k = \"\"",
        "setValue": 'defaults.setValue(v, forKey: "x")',
        "dump of defaults": "let all = defaults.dictionaryRepresentation()",
        # Codex round 3, P2: one allowed call must not approve the rest of the line.
        "two calls on one line": (
            'defaults.set(true, forKey: completionKey); defaults.set(secret, forKey: "trading212ApiKey")'
        ),
        "spaced set": 'defaults.set (secret, forKey: "trading212ApiKey")',
        "dotted-space set": 'defaults\n    .set(secret, forKey: "trading212ApiKey")',
        "set with extra argument": "defaults.set(secret, forKey: completionKey, other)",
        "set on a flag name in a string": 'defaults.set(secret, forKey: "completionKey")',
        "AppStorage": '@AppStorage("trading212ApiKey") var trading212ApiKey = ""',
        "SceneStorage": '@SceneStorage("t212") var s = ""',
        "spaced string read": 'let v = defaults.string (forKey: "trading212ApiKey")',
        "string read": 'let v = defaults.string(forKey: "trading212ApiKey")',
        "object read": 'let v = defaults.object(forKey: failedKeyVariable)',
        "read of another bool": 'let v = defaults.bool(forKey: "other")',
        "read two keys": "let v = defaults.integer(forKey: completionKey) + defaults.data(forKey: other)",
    }
    for name, line in planted.items():
        assert legacy_cleanup_write_offences(REAL_CLEANUP + "\n" + line + "\n"), name
        assert legacy_cleanup_write_offences(DELETE_CALL + line + "\n"), name


def test_the_delete_check_needs_a_real_call_not_a_comment_or_a_string():
    # Codex round 3, P3: the word SecItemDelete is not a call.
    for source in (
        "let x = 1\n",
        "/* SecItemDelete(q) */\nlet x = 1\n",
        "// SecItemDelete(q)\nlet x = 1\n",
        'let marker = "SecItemDelete("\n',
        'let marker = "SecItemDelete"\n',
        "let f = SecItemDelete\n",
    ):
        assert legacy_cleanup_write_offences(source) == ["missing: SecItemDelete call"], source
    assert legacy_cleanup_write_offences("SecItemDelete (q as CFDictionary)\n") == []


def test_the_delete_only_check_allows_what_the_cleanup_really_does():
    assert legacy_cleanup_write_offences(
        DELETE_CALL
        + "if defaults.bool(forKey: completionKey) || defaults.integer(forKey: failedLaunchesKey) >= 3 {}\n"
        "defaults.set(true, forKey: completionKey)\n"
        "defaults.set(failedLaunches, forKey: failedLaunchesKey)\n"
        "defaults.set (\n    failedLaunches + 1,\n    forKey: failedLaunchesKey\n)\n"
        "defaults.removeObject(forKey: key)\n"
        "// SecItemAdd is only named in this comment\n"
        'let note = "kSecReturnData stays out of strings too"\n'
    ) == []
