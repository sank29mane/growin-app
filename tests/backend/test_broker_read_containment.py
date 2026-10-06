import logging
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException

import trading212_mcp_server
from brokers.trading212.governor import Governor
from app_context import state
from mcp_client import (
    MultiMCPManager,
    build_mcp_subprocess_environment,
    trading212_read_access_enabled,
)
from routes.market_routes import get_live_portfolio
from shared_types import SENSITIVE_TOOLS
from t212_testkit import FakeClock, Recorder, install_no_real_network


@pytest.fixture(autouse=True)
def _no_real_network(monkeypatch, tmp_path):
    install_no_real_network(monkeypatch)
    monkeypatch.chdir(tmp_path)


@pytest.mark.asyncio
async def test_trading212_connection_is_blocked_without_explicit_read_access(
    monkeypatch,
):
    monkeypatch.delenv("GROWIN_ENABLE_TRADING212_READS", raising=False)
    stdio_client = MagicMock()
    monkeypatch.setattr("mcp_client.stdio_client", stdio_client)
    manager = MultiMCPManager()

    connected = await manager.connect_server(
        {
            "name": "Trading 212",
            "type": "stdio",
            "command": "python3",
            "args": ["trading212_mcp_server.py"],
            "env": {},
        }
    )

    assert connected is False
    assert manager.sessions == {}
    stdio_client.assert_not_called()


@pytest.mark.asyncio
async def test_trading212_script_alias_is_also_blocked_by_default(monkeypatch):
    monkeypatch.delenv("GROWIN_ENABLE_TRADING212_READS", raising=False)
    stdio_client = MagicMock()
    monkeypatch.setattr("mcp_client.stdio_client", stdio_client)
    manager = MultiMCPManager()

    connected = await manager.connect_server(
        {
            "name": "My Broker Data",
            "type": "stdio",
            "command": "python3",
            "args": ["/opt/growin/trading212_mcp_server.py"],
            "env": {},
        }
    )

    assert connected is False
    stdio_client.assert_not_called()


@pytest.mark.asyncio
async def test_startup_never_auto_connects_trading212_even_when_reads_are_enabled(
    monkeypatch,
):
    monkeypatch.setenv("GROWIN_ENABLE_TRADING212_READS", "true")
    manager = MultiMCPManager()
    connect_server = AsyncMock(return_value=True)
    manager.connect_server = connect_server

    async with manager.connect_all(
        [
            {
                "name": "Trading 212",
                "type": "stdio",
                "command": "python3",
                "args": ["trading212_mcp_server.py"],
                "env": {},
            },
            {
                "name": "Local Research",
                "type": "stdio",
                "command": "python3",
                "args": ["research_mcp_server.py"],
                "env": {},
            },
        ]
    ):
        pass

    connect_server.assert_awaited_once()
    assert connect_server.await_args.args[0]["name"] == "Local Research"


def test_allowed_trading212_subprocess_is_forced_read_only(monkeypatch):
    monkeypatch.setenv("GROWIN_TRADING212_READ_ONLY", "0")
    environment = build_mcp_subprocess_environment(
        {
            "name": "Broker Alias",
            "command": "python3",
            "args": ["trading212_mcp_server.py"],
            "env": {"GROWIN_TRADING212_READ_ONLY": "false"},
        }
    )

    assert environment["GROWIN_TRADING212_READ_ONLY"] == "1"


def test_trading212_read_access_requires_explicit_true_value(monkeypatch):
    monkeypatch.delenv("GROWIN_ENABLE_TRADING212_READS", raising=False)
    assert trading212_read_access_enabled() is False

    monkeypatch.setenv("GROWIN_ENABLE_TRADING212_READS", "true")
    assert trading212_read_access_enabled() is True


@pytest.mark.asyncio
async def test_portfolio_poll_does_not_call_mcp_without_trading212_session():
    original_client = state._mcp_client
    mock_client = MagicMock()
    mock_client.sessions = {}
    mock_client.call_tool = AsyncMock()
    state._mcp_client = mock_client

    try:
        with pytest.raises(HTTPException) as exc_info:
            await get_live_portfolio(account_type="invest")
    finally:
        state._mcp_client = original_client

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "Trading 212 reads are not enabled"
    mock_client.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_trading212_429_retry_uses_defined_logger():
    clock = FakeClock()
    throttled = httpx.Response(
        429,
        headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(clock.now) + 5)},
        text="Limited: 1 / 5s",
    )
    success_response = httpx.Response(200, json={"cash": {"availableToTrade": 100.0}})
    answers = iter([throttled, success_response])
    recorder = Recorder(lambda request: next(answers))

    client = trading212_mcp_server.Trading212Client(
        "key",
        "secret",
        True,
        governor=Governor(clock=clock, sleep=clock.sleep),
        transport=recorder.transport,
    )
    result = await client._request("GET", "equity/account/summary")
    await client.close()

    assert isinstance(trading212_mcp_server.logger, logging.Logger)
    assert result == {"cash": {"availableToTrade": 100.0}}
    assert recorder.count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [None, "1"])
async def test_read_only_transport_rejects_mutation_before_network(monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv("GROWIN_TRADING212_READ_ONLY", raising=False)
    else:
        monkeypatch.setenv("GROWIN_TRADING212_READ_ONLY", flag)
    client = trading212_mcp_server.Trading212Client("key", "secret", True)
    client.client.request = AsyncMock()

    with pytest.raises(PermissionError, match="read-only transport"):
        await client._request("POST", "equity/orders/market", json={})
    await client.close()

    client.client.request.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", [None, "1"])
async def test_read_only_server_hides_and_rejects_all_sensitive_tools(monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv("GROWIN_TRADING212_READ_ONLY", raising=False)
    else:
        monkeypatch.setenv("GROWIN_TRADING212_READ_ONLY", flag)

    advertised_names = {tool.name for tool in await trading212_mcp_server.list_tools()}
    assert advertised_names.isdisjoint(SENSITIVE_TOOLS)

    for tool_name in SENSITIVE_TOOLS:
        with pytest.raises(PermissionError, match="read-only mode"):
            await trading212_mcp_server.call_tool(tool_name, {})
