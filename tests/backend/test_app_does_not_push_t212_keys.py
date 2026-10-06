"""The app no longer sends Trading 212 keys to the backend (66-02 Task 3, D-05, D-07).

A source scan of the Swift app, so it runs in CI without Xcode. The Xcode build and
unit tests are the other half of the proof and are recorded in the 66-02 SUMMARY.
"""

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


def test_the_scan_catches_a_planted_key_push(tmp_path):
    planted = tmp_path / "Planted.swift"
    planted.write_text(
        'let url = "\\(base)/mcp/trading212/config"\nlet body = ["invest_key": key]\n',
        encoding="utf-8",
    )
    found = offences(tmp_path)
    assert any("mcp/trading212/config" in item for item in found)
    assert any("invest_key" in item for item in found)
