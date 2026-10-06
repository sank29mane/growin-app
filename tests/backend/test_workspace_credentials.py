"""UK-only environment credentials are refused outside a UK process (ISO-02).

Sentinel values stand in for secrets. No refusal message, exception text or
log record may contain one.
"""

import logging
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from data_engine import AlpacaClient
from app_context import AppState
from execution import ApprovalConflict, ExecutionConflictError
from execution.venue import VENUE_T212_PRACTICE, production_dispatcher_factories
from mcp_client import (
    MultiMCPManager,
    build_mcp_subprocess_environment,
)
from venue_seam_testkit import (
    SYNTH_ACCOUNT,
    RecordingDispatcher,
    enroll,
    practice_proposal,
    prepare,
    private_key,
    sign,
    write_json,
    write_practice_files,
)
from workspace_credentials import (
    CredentialScopeError,
    is_uk_only_credential,
    process_workspace,
    scrub_uk_only_credentials,
    uk_credential,
)

SENTINELS = {
    "TRADING212_API_KEY": "sentinel-t212-key-0001",
    "TRADING212_API_SECRET_ISA": "sentinel-t212-isa-secret-0002",
    "ALPACA_API_KEY": "sentinel-alpaca-key-0003",
    "ALPACA_SECRET_KEY": "sentinel-alpaca-secret-0004",
}
NON_UK_WORKSPACES = ["india", "us", "", "UK", None]


def _set_process(monkeypatch, workspace):
    if workspace is None:
        monkeypatch.delenv("GROWIN_WORKSPACE", raising=False)
    else:
        monkeypatch.setenv("GROWIN_WORKSPACE", workspace)


@pytest.fixture
def sentinel_env(monkeypatch):
    for name, value in SENTINELS.items():
        monkeypatch.setenv(name, value)
    return SENTINELS


def _assert_no_sentinel(text):
    for value in SENTINELS.values():
        assert value not in text


T212_CONFIG = {
    "name": "Trading 212",
    "type": "stdio",
    "command": "python3",
    "args": ["trading212_mcp_server.py"],
    "env": {},
}


# --- the lookup ---


@pytest.mark.parametrize("workspace", NON_UK_WORKSPACES)
@pytest.mark.parametrize("name", sorted(SENTINELS))
def test_uk_credential_is_refused_outside_a_uk_process(
    monkeypatch, sentinel_env, workspace, name
):
    _set_process(monkeypatch, workspace)

    with pytest.raises(CredentialScopeError) as raised:
        uk_credential(name)

    text = str(raised.value)
    assert name in text
    # Only "india" is a known non-UK workspace; anything else reads as unset.
    expected = "india" if workspace == "india" else "unset"
    assert f"process workspace is {expected}" in text
    _assert_no_sentinel(text)


def test_india_process_message_names_variable_and_workspace(monkeypatch, sentinel_env):
    _set_process(monkeypatch, "india")

    with pytest.raises(CredentialScopeError) as raised:
        uk_credential("TRADING212_API_KEY")

    assert str(raised.value) == (
        "TRADING212_API_KEY is a UK-only credential; process workspace is india"
    )


def test_uk_process_gets_the_credential_and_the_scrub_keeps_it(monkeypatch, sentinel_env):
    _set_process(monkeypatch, "uk")

    assert process_workspace() == "uk"
    for name, value in SENTINELS.items():
        assert uk_credential(name) == value
    kept = scrub_uk_only_credentials(dict(SENTINELS, PATH="/bin"))
    assert kept == dict(SENTINELS, PATH="/bin")


def test_uk_credential_is_none_when_unset_or_blank(monkeypatch):
    _set_process(monkeypatch, "uk")
    monkeypatch.delenv("ALPACA_API_KEY", raising=False)
    monkeypatch.setenv("ALPACA_SECRET_KEY", "   ")

    assert uk_credential("ALPACA_API_KEY") is None
    assert uk_credential("ALPACA_SECRET_KEY") is None


def test_uk_credential_rejects_names_that_are_not_uk_only(monkeypatch):
    _set_process(monkeypatch, "uk")

    with pytest.raises(ValueError):
        uk_credential("FINNHUB_API_KEY")
    assert not is_uk_only_credential("FINNHUB_API_KEY")
    assert is_uk_only_credential("trading212_api_key")


# --- the MCP child boundary ---


@pytest.mark.parametrize("workspace", NON_UK_WORKSPACES)
def test_non_uk_child_environment_carries_no_uk_credentials(
    monkeypatch, sentinel_env, workspace
):
    _set_process(monkeypatch, workspace)
    configs = [
        T212_CONFIG,
        {"name": "Local Research", "type": "stdio", "command": "python3", "args": [], "env": {}},
        {
            **T212_CONFIG,
            "env": {
                "TRADING212_API_KEY": "sentinel-custom-env-0005",
                "ALPACA_API_KEY": "sentinel-custom-alpaca-0006",
                "KEEP_ME": "yes",
            },
        },
    ]

    for config in configs:
        environment = build_mcp_subprocess_environment(config)
        offenders = [
            key for key in environment if key.upper().startswith(("TRADING212_", "ALPACA_"))
        ]
        assert offenders == []
    broker = build_mcp_subprocess_environment(T212_CONFIG)
    assert broker["GROWIN_TRADING212_READ_ONLY"] == "1"
    assert build_mcp_subprocess_environment(configs[2])["KEEP_ME"] == "yes"


def test_uk_child_environment_keeps_credentials(monkeypatch, sentinel_env):
    _set_process(monkeypatch, "uk")

    environment = build_mcp_subprocess_environment(T212_CONFIG)

    for name, value in SENTINELS.items():
        assert environment[name] == value
    assert environment["GROWIN_TRADING212_READ_ONLY"] == "1"


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace", NON_UK_WORKSPACES)
async def test_non_uk_process_never_opens_the_trading212_connection(
    monkeypatch, caplog, workspace
):
    _set_process(monkeypatch, workspace)
    monkeypatch.setenv("GROWIN_ENABLE_TRADING212_READS", "true")
    stdio_client = MagicMock()
    monkeypatch.setattr("mcp_client.stdio_client", stdio_client)
    manager = MultiMCPManager()

    with caplog.at_level(logging.WARNING):
        connected = await manager.connect_server(dict(T212_CONFIG))

    assert connected is False
    assert manager.sessions == {}
    stdio_client.assert_not_called()
    assert "Trading 212 connection blocked: process workspace is not uk" in caplog.text


@pytest.mark.asyncio
async def test_uk_process_with_reads_enabled_reaches_the_stdio_client(monkeypatch):
    _set_process(monkeypatch, "uk")
    monkeypatch.setenv("GROWIN_ENABLE_TRADING212_READS", "true")
    stdio_client = MagicMock(side_effect=RuntimeError("stop after the gate"))
    monkeypatch.setattr("mcp_client.stdio_client", stdio_client)
    manager = MultiMCPManager()

    connected = await manager.connect_server(dict(T212_CONFIG))

    assert connected is False
    stdio_client.assert_called_once()


# --- Alpaca ---


def _recording_alpaca_modules(monkeypatch):
    trading = MagicMock(name="TradingClient")
    data = MagicMock(name="StockHistoricalDataClient")
    monkeypatch.setitem(
        sys.modules, "alpaca.trading.client", SimpleNamespace(TradingClient=trading)
    )
    monkeypatch.setitem(
        sys.modules, "alpaca.data.historical", SimpleNamespace(StockHistoricalDataClient=data)
    )
    return trading, data


@pytest.mark.parametrize("workspace", NON_UK_WORKSPACES)
def test_alpaca_client_stays_offline_outside_a_uk_process(
    monkeypatch, sentinel_env, caplog, workspace
):
    _set_process(monkeypatch, workspace)
    trading, data = _recording_alpaca_modules(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        client = AlpacaClient()

    assert client.trading_client is None
    assert client.data_client is None
    trading.assert_not_called()
    data.assert_not_called()
    _assert_no_sentinel(caplog.text)


def test_alpaca_client_builds_sdk_clients_in_a_uk_process(monkeypatch, sentinel_env):
    _set_process(monkeypatch, "uk")
    trading, data = _recording_alpaca_modules(monkeypatch)

    client = AlpacaClient()

    trading.assert_called_once()
    assert trading.call_args.args[0] == SENTINELS["ALPACA_API_KEY"]
    assert trading.call_args.args[1] == SENTINELS["ALPACA_SECRET_KEY"]
    data.assert_called_once()
    assert client.trading_client is trading.return_value


def test_data_engine_no_longer_reads_alpaca_keys_at_import():
    import data_engine

    assert not hasattr(data_engine, "API_KEY")
    assert not hasattr(data_engine, "API_SECRET")


# --- the venue seam keeps the UK-only property (ISO-02, Phase 66) ---
#
# These replace the cases that targeted the deleted Trading212Dispatcher. The
# property is the same: a Trading 212 venue cannot start in an India or
# unknown process, cannot start on an India ledger, and a UK practice ledger
# refuses an order from any other broker. No broker is contacted; the practice
# dispatcher is a recording double behind the factory map.


class _CountingFactory:
    def __init__(self):
        self.double = RecordingDispatcher()
        self.calls = 0

    def __call__(self, _context):
        self.calls += 1
        return self.double


def _factories(counting):
    return {**production_dispatcher_factories(), VENUE_T212_PRACTICE: counting}


@pytest.mark.parametrize("process", ["india", None, "us"])
def test_practice_venue_does_not_start_outside_a_uk_process(
    monkeypatch, tmp_path, private_config_dir, process
):
    _set_process(monkeypatch, process)
    write_practice_files(private_config_dir)
    counting = _CountingFactory()
    app_state = AppState()

    started = app_state.start_execution(
        tmp_path / "practice.sqlite3",
        workspace="uk",
        private_dir=private_config_dir,
        dispatcher_factories=_factories(counting),
    )

    assert started is False
    assert app_state.execution_authority is False
    assert "VENUE_WORKSPACE_MISMATCH" in app_state.execution_startup_error
    assert counting.calls == 0
    assert not (tmp_path / "practice.sqlite3").exists()


def test_practice_venue_does_not_start_on_an_india_ledger(
    monkeypatch, tmp_path, private_config_dir
):
    _set_process(monkeypatch, "uk")
    write_json(
        private_config_dir / "india" / "execution.json",
        {
            "schema_version": 1,
            "workspace": "india",
            "venue": VENUE_T212_PRACTICE,
            "account_id": SYNTH_ACCOUNT,
            "currency": "GBP",
        },
    )
    counting = _CountingFactory()
    app_state = AppState()

    started = app_state.start_execution(
        tmp_path / "india.sqlite3",
        workspace="india",
        private_dir=private_config_dir,
        dispatcher_factories=_factories(counting),
    )

    assert started is False
    assert "VENUE_NOT_ALLOWED" in app_state.execution_startup_error
    assert counting.calls == 0


def test_practice_ledger_refuses_an_order_from_another_broker_or_workspace(
    monkeypatch, tmp_path, private_config_dir
):
    _set_process(monkeypatch, "uk")
    write_practice_files(private_config_dir)
    counting = _CountingFactory()
    app_state = AppState()
    assert app_state.start_execution(
        tmp_path / "practice.sqlite3",
        workspace="uk",
        private_dir=private_config_dir,
        dispatcher_factories=_factories(counting),
    )
    try:
        ledger = app_state._execution_ledger
        # broker "paper" (the old ("uk", {"broker": "paper"}) case)
        wrong_broker = practice_proposal("wrong-broker", broker="paper")
        app_state.execution_service.register_proposal(wrong_broker)
        with pytest.raises(ApprovalConflict, match="broker"):
            app_state.execution_service.create_approval_challenge("wrong-broker", workspace="uk")
        # an India intent (the old ("uk", {"workspace": "india"}) case)
        with pytest.raises(ExecutionConflictError, match="workspace"):
            app_state.execution_service.register_proposal(
                practice_proposal("india-intent", workspace="india", account="india-paper")
            )
        assert ledger.list_attempts() == []
        assert counting.double.intents == []
    finally:
        app_state.close_execution()


@pytest.mark.asyncio
async def test_a_uk_process_reaches_the_seam_double_with_a_uk_practice_intent(
    monkeypatch, tmp_path, private_config_dir
):
    _set_process(monkeypatch, "uk")
    write_practice_files(private_config_dir)
    counting = _CountingFactory()
    app_state = AppState()
    assert app_state.start_execution(
        tmp_path / "practice.sqlite3",
        workspace="uk",
        private_dir=private_config_dir,
        dispatcher_factories=_factories(counting),
        allow_test_price_sources=True,
    )
    try:
        service = app_state.execution_service
        key = private_key()
        enroll(service._approval_service, key)
        prepare(app_state, practice_proposal("uk-ok"))
        challenge = service.create_approval_challenge("uk-ok", workspace="uk")
        ack = await service.approve_signed(
            "uk-ok", challenge.challenge_id, sign(key, challenge.signed_payload), workspace="uk"
        )
    finally:
        app_state.close_execution()

    assert counting.calls == 1
    assert [intent.proposal_id for intent in counting.double.intents] == ["uk-ok"]
    assert counting.double.intents[0].workspace.value == "uk"
    assert ack.broker == VENUE_T212_PRACTICE
