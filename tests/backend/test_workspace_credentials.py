"""UK-only environment credentials are refused outside a UK process (ISO-02).

Sentinel values stand in for secrets. No refusal message, exception text or
log record may contain one.
"""

import logging
import sys
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from data_engine import AlpacaClient
from execution import OrderIntent, Trading212Dispatcher
from execution.service import BrokerExecutionError
from mcp_client import (
    MultiMCPManager,
    build_mcp_subprocess_environment,
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


# --- the Trading 212 dispatcher ---


def _intent(**updates):
    values = {
        "proposal_id": "proposal-58-07",
        "client_order_id": "growin-proposal-58-07-v1",
        "workspace": "uk",
        "account": "invest",
        "broker": "trading212",
        "ticker": "AAPL_US_EQ",
        "side": "BUY",
        "quantity": Decimal("2"),
    }
    values.update(updates)
    return OrderIntent(**values)


def _recording_client():
    client = MagicMock()
    client.call_tool = AsyncMock(
        return_value=SimpleNamespace(
            isError=False, content=[{"orderId": "t212-1", "status": "ACKNOWLEDGED"}]
        )
    )
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("process", "intent_updates"),
    [
        ("india", {}),
        (None, {}),
        ("us", {}),
        ("uk", {"workspace": "india", "account": "india-paper"}),
        ("uk", {"broker": "paper"}),
    ],
)
async def test_dispatcher_refuses_before_calling_the_mcp_client(
    monkeypatch, process, intent_updates
):
    _set_process(monkeypatch, process)
    client = _recording_client()

    with pytest.raises(BrokerExecutionError, match="accepts only UK trading212 intents"):
        await Trading212Dispatcher(client).dispatch(_intent(**intent_updates))

    client.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatcher_passes_a_uk_trading212_intent_in_a_uk_process(monkeypatch):
    _set_process(monkeypatch, "uk")
    client = _recording_client()

    ack = await Trading212Dispatcher(client).dispatch(_intent())

    client.call_tool.assert_awaited_once_with(
        "place_market_order",
        {"ticker": "AAPL_US_EQ", "quantity": 2.0, "order_type": "BUY"},
    )
    assert ack.broker == "trading212"
    assert ack.broker_order_id == "t212-1"
