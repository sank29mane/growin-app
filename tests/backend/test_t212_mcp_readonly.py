"""The Trading 212 MCP server is read-only, honest about its host, and governed (66-02).

Every transport here is httpx.MockTransport. ``install_no_real_network`` fails the
test on any real transport or socket connect (plan prohibition).
"""

import ast
import base64
import json
import re
import subprocess
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

import trading212_mcp_server as server
from brokers.trading212.governor import Governor
from shared_types import (
    SENSITIVE_TOOLS,
    Trading212EnvironmentError,
    resolve_trading212_environment,
)
from t212_testkit import (
    DEMO_BASE,
    LIVE_BASE,
    FakeClock,
    Fixture,
    Recorder,
    install_no_real_network,
    serving,
)

BACKEND = Path(__file__).resolve().parents[2] / "backend"
SERVER_SOURCE = BACKEND / "trading212_mcp_server.py"

READ_ALLOW_LIST = {
    "analyze_portfolio",
    "get_position_details",
    "search_instruments",
    "get_historical_performance",
    "calculate_portfolio_metrics",
    "get_all_pies",
    "get_pie_details",
    "get_price_history",
    "get_ticker_analysis",
    "calculate_technical_indicators",
    "get_current_price",
}
REMOVED_TOOLS = {
    "place_market_order",
    "place_limit_order",
    "place_stop_order",
    "place_stop_limit_order",
    "cancel_order",
    "create_investment_pie",
    "update_investment_pie",
    "delete_investment_pie",
    "update_pie",
    "switch_account",
}
ENV_NAME = "TRADING212_USE_DEMO"


@pytest.fixture(autouse=True)
def _contained(monkeypatch, tmp_path):
    install_no_real_network(monkeypatch)
    monkeypatch.chdir(tmp_path)  # the client's FileCache writes .t212_cache.json to the cwd
    monkeypatch.delenv("GROWIN_TRADING212_READ_ONLY", raising=False)
    monkeypatch.delenv("GROWIN_ENABLE_TRADING212_READS", raising=False)


@asynccontextmanager
async def reader(recorder: Recorder, *, use_demo=True, clock=None, governor=None):
    clock = clock or FakeClock()
    governor = governor or Governor(clock=clock, sleep=clock.sleep)
    client = server.Trading212Client(
        "key-canary", "secret-canary", use_demo, governor=governor, transport=recorder.transport
    )
    try:
        yield client, clock
    finally:
        await client.close()


def ok_router(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json=[])


# --- Task 1: the tool list and the source ---------------------------------------


@pytest.mark.asyncio
async def test_tool_list_equals_the_pinned_read_allow_list_without_the_read_only_flag():
    names = [tool.name for tool in await server.list_tools()]
    assert sorted(names) == sorted(READ_ALLOW_LIST)
    assert len(names) == len(set(names))


def test_no_order_cancel_pie_mutation_or_switch_account_name_exists_in_the_server_source():
    tree = ast.parse(SERVER_SOURCE.read_text(encoding="utf-8"))
    offences = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in REMOVED_TOOLS:
                offences.append(f"{node.lineno} tool name {node.value}")
            if re.search(r"orders/(market|limit|stop|stop_limit)\b", node.value):
                offences.append(f"{node.lineno} order endpoint {node.value}")
        names = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(node.name)
        if isinstance(node, ast.Attribute):
            names.append(node.attr)
        if isinstance(node, ast.Name):
            names.append(node.id)
        for name in names:
            if re.match(r"(place_|cancel_|create_pie|update_pie|delete_pie|switch_account|handle_market)", name):
                offences.append(f"{node.lineno} identifier {name}")
    assert offences == []


def test_sensitive_tools_keeps_every_removed_tool_name_as_defence_in_depth():
    assert REMOVED_TOOLS <= set(SENSITIVE_TOOLS)


@pytest.mark.parametrize("flag", [None, "1"])
@pytest.mark.parametrize("tool_name", sorted(REMOVED_TOOLS))
@pytest.mark.asyncio
async def test_a_mutation_tool_name_is_refused_whatever_the_read_only_flag_says(
    monkeypatch, tool_name, flag
):
    if flag is not None:
        monkeypatch.setenv("GROWIN_TRADING212_READ_ONLY", flag)
    with pytest.raises(PermissionError, match="read-only mode"):
        await server.call_tool(tool_name, {"ticker": "X", "quantity": 1})


# --- Task 1: v0 reads, normalised ------------------------------------------------


@pytest.mark.asyncio
async def test_get_account_info_makes_one_get_to_the_v0_summary_and_returns_the_legacy_keys():
    clock = FakeClock()
    recorder = serving(Fixture("account_summary"), clock=clock)
    async with reader(recorder, clock=clock) as (client, _):
        info = await client.get_account_info()

    assert recorder.count == 1
    assert recorder.methods == ["GET"]
    assert recorder.requests[0].url == httpx.URL(f"{DEMO_BASE}/equity/account/summary")
    assert info == {"id": 20260001, "currencyCode": "GBP"}


@pytest.mark.asyncio
async def test_get_account_cash_maps_the_summary_to_the_keys_the_app_reads():
    clock = FakeClock()
    recorder = serving(Fixture("account_summary"), clock=clock)
    async with reader(recorder, clock=clock) as (client, _):
        cash = await client.get_account_cash()

    assert recorder.count == 1
    assert cash == {
        "free": 1234.56,
        "total": 4361.09,
        "invested": 2900.0,
        "ppl": 174.03,
        "result": 55.25,
        "pieCash": 12.5,
        "blocked": 40.0,
    }


@pytest.mark.asyncio
async def test_get_all_positions_maps_v0_positions_to_the_legacy_item_keys():
    clock = FakeClock()
    recorder = serving(Fixture("positions"), clock=clock)
    async with reader(recorder, clock=clock) as (client, _):
        positions = await client.get_all_positions()

    assert recorder.requests[0].url == httpx.URL(f"{DEMO_BASE}/equity/positions")
    assert positions[0] == {
        "ticker": "AAPL_US_EQ",
        "quantity": 3.0,
        "averagePrice": 150.25,
        "currentPrice": 172.4,
        "ppl": 45.1,
        "fxPpl": -2.3,
        "initialFillDate": "2026-03-02T09:30:00.000+00:00",
        "pieQuantity": 0.0,
        "maxSell": 3.0,
        "currency": "USD",
    }
    assert positions[1]["ticker"] == "VODl_EQ"
    assert positions[1]["maxSell"] == 80.0 and positions[1]["pieQuantity"] == 20.0
    assert all("maxBuy" not in position for position in positions)


@pytest.mark.asyncio
async def test_get_position_by_ticker_asks_for_one_ticker_and_returns_one_legacy_item():
    clock = FakeClock()
    fx = Fixture("positions")
    body = [fx.body[0]]

    def router(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v0/equity/positions"
        assert request.url.params["ticker"] == "AAPL_US_EQ"
        return httpx.Response(200, json=body)

    recorder = Recorder(router)
    async with reader(recorder, clock=clock) as (client, _):
        position = await client.get_position_by_ticker("AAPL_US_EQ")

    assert recorder.count == 1
    assert position["ticker"] == "AAPL_US_EQ" and position["averagePrice"] == 150.25


@pytest.mark.asyncio
async def test_get_position_by_ticker_returns_an_empty_dict_when_nothing_is_held():
    recorder = Recorder(lambda request: httpx.Response(200, json=[]))
    async with reader(recorder) as (client, _):
        assert await client.get_position_by_ticker("NOPE_US_EQ") == {}


@pytest.mark.parametrize(
    "field,dropped",
    [
        (("cash", "availableToTrade"), "free"),
        (("totalValue",), "total"),
        (("investments", "totalCost"), "invested"),
        (("investments", "unrealizedProfitLoss"), "ppl"),
        (("investments", "realizedProfitLoss"), "result"),
        (("cash", "inPies"), "pieCash"),
        (("cash", "reservedForOrders"), "blocked"),
    ],
)
@pytest.mark.asyncio
async def test_a_cash_key_with_no_v0_source_is_omitted_never_zero(field, dropped):
    summary = Fixture("account_summary").body
    node = summary
    for step in field[:-1]:
        node = node[step]
    del node[field[-1]]
    recorder = Recorder(lambda request: httpx.Response(200, json=summary))
    async with reader(recorder) as (client, _):
        cash = await client.get_account_cash()
    assert dropped not in cash


@pytest.mark.parametrize(
    "path,dropped",
    [
        (("walletImpact", "unrealizedProfitLoss"), "ppl"),
        (("walletImpact", "fxImpact"), "fxPpl"),
        (("averagePricePaid",), "averagePrice"),
        (("currentPrice",), "currentPrice"),
        (("quantityInPies",), "pieQuantity"),
        (("quantityAvailableForTrading",), "maxSell"),
        (("createdAt",), "initialFillDate"),
    ],
)
@pytest.mark.asyncio
async def test_a_position_key_with_no_v0_source_is_omitted_never_zero(path, dropped):
    positions = Fixture("positions").body
    node = positions[0]
    for step in path[:-1]:
        node = node[step]
    del node[path[-1]]
    recorder = Recorder(lambda request: httpx.Response(200, json=positions))
    async with reader(recorder) as (client, _):
        first = (await client.get_all_positions())[0]
    assert dropped not in first
    assert first["ticker"] == "AAPL_US_EQ"


@pytest.mark.asyncio
async def test_a_null_v0_value_is_omitted_not_turned_into_zero():
    summary = Fixture("account_summary").body
    summary["investments"]["unrealizedProfitLoss"] = None
    recorder = Recorder(lambda request: httpx.Response(200, json=summary))
    async with reader(recorder) as (client, _):
        assert "ppl" not in await client.get_account_cash()


# --- Task 1: every read goes through the governor ---------------------------------


class RecordingGovernor(Governor):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.acquired: list[tuple[str, str]] = []

    async def acquire(self, method, path):
        self.acquired.append((method, path))
        return await super().acquire(method, path)


READ_CALLS = [
    ("get_account_info", (), "equity/account/summary"),
    ("get_account_cash", (), "equity/account/summary"),
    ("get_all_positions", (), "equity/positions"),
    ("get_position_by_ticker", ("AAPL_US_EQ",), "equity/positions"),
    ("get_all_orders", (), "equity/orders"),
    ("get_order_by_id", ("77",), "equity/orders/77"),
    ("get_historical_orders", (), "equity/history/orders"),
    ("get_dividends", (), "equity/history/dividends"),
    ("get_transactions", (), "equity/history/transactions"),
    ("get_instruments", (), "equity/metadata/instruments"),
    ("get_exchanges", (), "equity/metadata/exchanges"),
    ("get_all_pies", (), "equity/pies"),
    ("get_pie", (9,), "equity/pies/9"),
]


def _generic_router(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/account/summary"):
        return httpx.Response(200, json=Fixture("account_summary").body)
    return httpx.Response(200, json=[])


@pytest.mark.parametrize("method_name,args,path", READ_CALLS)
@pytest.mark.asyncio
async def test_every_read_acquires_the_governor_slot_for_its_endpoint(method_name, args, path):
    clock = FakeClock()
    governor = RecordingGovernor(clock=clock, sleep=clock.sleep)
    recorder = Recorder(_generic_router)
    async with reader(recorder, clock=clock, governor=governor) as (client, _):
        await getattr(client, method_name)(*args)

    assert recorder.count == 1
    assert len(governor.acquired) == 1
    method, acquired_path = governor.acquired[0]
    assert method == "GET" and acquired_path.split("?")[0] == path
    assert recorder.requests[0].url.path == f"/api/v0/{path}"


@pytest.mark.asyncio
async def test_two_reads_of_one_endpoint_are_spaced_by_the_governor():
    clock = FakeClock()
    recorder = serving(Fixture("positions"), clock=clock)
    async with reader(recorder, clock=clock) as (client, _):
        await client.get_all_positions()
        await client.get_all_positions()
    assert recorder.count == 2
    assert clock.sleeps and clock.sleeps[0] >= 1.0 - 1e-9


@pytest.mark.asyncio
async def test_a_read_that_is_not_in_the_limit_table_fails_before_the_network():
    recorder = Recorder(ok_router)
    async with reader(recorder) as (client, _):
        with pytest.raises(ValueError, match="no Trading 212 limit"):
            await client._request("GET", "equity/portfolio")
    assert recorder.count == 0


# --- Task 1: a read goes server -> governor -> MockTransport -> normalised result ---


@pytest.mark.asyncio
async def test_get_position_details_runs_end_to_end_on_a_v0_fixture(monkeypatch):
    """Server, governor, MockTransport, normalised result, through the tool entry point.

    calculate_portfolio_metrics is not exercised here: it fails at the base commit
    (it hands raw instruments and Position models to code that expects other shapes).
    That is logged in deferred-items.md and left alone (scope boundary).
    """

    clock = FakeClock()
    one = [Fixture("positions").body[0]]
    recorder = Recorder(lambda request: httpx.Response(200, json=one))
    async with reader(recorder, clock=clock) as (client, _):
        monkeypatch.setattr(server, "clients", {"invest": client})
        monkeypatch.setattr(server, "active_account_type", "invest")
        result = await server.call_tool("get_position_details", {"ticker": "aapl_us_eq"})

    payload = json.loads(result[0].text)
    assert payload["ticker"] == "AAPL_US_EQ"
    assert payload["averagePrice"] == 150.25 and payload["account_type"] == "invest"
    assert recorder.methods == ["GET"]
    assert recorder.requests[0].url.params["ticker"] == "AAPL_US_EQ"


@pytest.mark.asyncio
async def test_analyze_portfolio_keeps_its_cash_and_position_keys_on_v0_fixtures(monkeypatch):
    clock = FakeClock()
    recorder = serving(
        Fixture("account_summary"),
        Fixture("positions"),
        Fixture("metadata_instruments"),
        clock=clock,
    )
    async with reader(recorder, clock=clock) as (client, _):
        monkeypatch.setattr(server, "clients", {"invest": client})
        monkeypatch.setattr(server, "active_account_type", "invest")
        result = await server.call_tool("analyze_portfolio", {"account_type": "invest"})

    payload = json.loads(result[0].text)
    summary = payload["summary"]
    assert summary["total_positions"] == 2
    assert summary["cash_balance"] == {"total": 4361.09, "free": 1234.56}
    assert {position["ticker"] for position in payload["positions"]} == {"AAPL_US_EQ", "VODl_EQ"}
    assert set(recorder.methods) == {"GET"}


# --- Task 1: package isolation (D-12) --------------------------------------------


def test_the_server_loads_the_governor_and_nothing_else_from_the_brokers_package():
    probe = (
        "import sys, json\n"
        f"sys.path.insert(0, {str(BACKEND)!r})\n"
        "import trading212_mcp_server\n"
        "print(json.dumps(sorted(m for m in sys.modules if m == 'brokers' or m.startswith('brokers.'))))\n"
    )
    done = subprocess.run(
        [sys.executable, "-I", "-c", probe], capture_output=True, text=True, timeout=120
    )
    assert done.returncode == 0, done.stderr[-500:]
    loaded = json.loads(done.stdout.strip().splitlines()[-1])
    assert loaded == ["brokers", "brokers.trading212", "brokers.trading212.governor"]
