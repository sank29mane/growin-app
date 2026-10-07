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


# =================================================================================
# Task 2: no mutation surface, no POST retries, explicit environment, scrubbed child
# =================================================================================

FORBIDDEN_METHODS = ["POST", "DELETE", "PUT", "PATCH", "post", "Delete", "OPTIONS", "HEAD"]
BODY = {"ticker": "AAPL_US_EQ", "quantity": 1, "limitPrice": 1.0, "timeValidity": "DAY"}


def test_the_client_has_no_method_that_writes():
    public = {name for name in dir(server.Trading212Client) if not name.startswith("_")}
    assert public, "client has no public methods?"
    offenders = {name for name in public if not (name.startswith("get_") or name == "close")}
    assert offenders == set()
    assert not any(
        re.match(r"(post|put|patch|delete|place|cancel|create|update|switch)", name)
        for name in public
    )


def test_the_server_source_holds_no_write_method_literal():
    tree = ast.parse(SERVER_SOURCE.read_text(encoding="utf-8"))
    literals = [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.upper() in {"POST", "DELETE", "PUT", "PATCH"}
    ]
    assert literals == []


@pytest.mark.parametrize("method", FORBIDDEN_METHODS)
@pytest.mark.parametrize("use_demo", [True, False])
@pytest.mark.asyncio
async def test_request_refuses_every_method_but_get_before_the_network(method, use_demo):
    recorder = Recorder(ok_router)
    clock = FakeClock()
    governor = RecordingGovernor(clock=clock, sleep=clock.sleep)
    async with reader(recorder, use_demo=use_demo, clock=clock, governor=governor) as (client, _):
        with pytest.raises(PermissionError, match="read-only transport"):
            await client._request(method, "equity/orders/limit", json=BODY)
    assert recorder.count == 0
    assert governor.acquired == []  # refused before it even took a rate slot


@pytest.mark.parametrize("keyword", ["json", "data", "content", "files"])
@pytest.mark.asyncio
async def test_a_get_may_not_carry_a_request_body(keyword):
    recorder = Recorder(ok_router)
    async with reader(recorder) as (client, _):
        with pytest.raises(PermissionError, match="no request body"):
            await client._request("GET", "equity/orders", **{keyword: BODY if keyword == "json" else b"{}"})
    assert recorder.count == 0


# --- 429, timeouts and 5xx: at most one retry, only for a GET's first 429 ----------


@pytest.mark.asyncio
async def test_a_get_429_waits_until_the_reset_and_retries_exactly_once():
    clock = FakeClock()
    reset = int(clock.now) + 7
    answers = iter(
        [
            Fixture("rate_limited_429").response(clock, headers={"x-ratelimit-reset": str(reset)}),
            Fixture("positions").response(clock),
        ]
    )
    recorder = Recorder(lambda request: next(answers))
    async with reader(recorder, clock=clock) as (client, _):
        positions = await client.get_all_positions()

    assert recorder.count == 2
    assert [p["ticker"] for p in positions] == ["AAPL_US_EQ", "VODl_EQ"]
    assert clock.now >= reset
    assert clock.sleeps == [pytest.approx(7.0)]


@pytest.mark.asyncio
async def test_a_second_429_raises_with_no_third_attempt():
    clock = FakeClock()
    recorder = Recorder(lambda request: Fixture("rate_limited_429").response(clock))
    async with reader(recorder, clock=clock) as (client, _):
        with pytest.raises(httpx.HTTPStatusError) as caught:
            await client.get_account_summary()
    assert caught.value.response.status_code == 429
    assert recorder.count == 2


@pytest.mark.parametrize("status", [408, 500, 502, 503, 504, 401, 403, 404])
@pytest.mark.asyncio
async def test_any_other_status_raises_after_one_attempt(status):
    recorder = Recorder(lambda request: httpx.Response(status, text="no"))
    async with reader(recorder) as (client, _):
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_all_orders()
    assert recorder.count == 1


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("slow"),
        httpx.ConnectTimeout("slow"),
        httpx.ConnectError("down"),
        httpx.RemoteProtocolError("cut"),
    ],
    ids=lambda e: type(e).__name__,
)
@pytest.mark.asyncio
async def test_a_transport_error_raises_after_one_attempt(error):
    def router(request: httpx.Request) -> httpx.Response:
        raise error

    recorder = Recorder(router)
    async with reader(recorder) as (client, _):
        with pytest.raises(type(error)):
            await client.get_all_positions()
    assert recorder.count == 1


@pytest.mark.asyncio
async def test_a_redirect_is_never_followed():
    def router(request: httpx.Request) -> httpx.Response:
        return httpx.Response(307, headers={"location": f"{LIVE_BASE}/equity/orders/limit"})

    recorder = Recorder(router)
    async with reader(recorder, use_demo=True) as (client, _):
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_all_orders()
    assert recorder.count == 1
    assert recorder.methods == ["GET"]


# --- operator rule: the live host is read with GET only ---------------------------

LIVE_ENV = {
    ENV_NAME: "false",
    "TRADING212_API_KEY": "live-key-canary",
    "TRADING212_API_SECRET": "live-secret-canary",
}


@pytest.mark.parametrize("method", ["POST", "DELETE", "PUT", "PATCH", "OPTIONS", "HEAD"])
@pytest.mark.asyncio
async def test_a_non_get_request_to_the_live_host_is_refused_before_sending(method):
    recorder = Recorder(ok_router)
    built = server.build_clients(LIVE_ENV, transport=recorder.transport)
    client = built["invest"]
    try:
        assert client.base_url == LIVE_BASE

        # 1. the MCP request path
        with pytest.raises(PermissionError):
            await client._request(method, "equity/orders/limit", json=BODY)
        # 2. the httpx client underneath, bypassing _request
        with pytest.raises(PermissionError):
            await client.client.request(method, f"{LIVE_BASE}/equity/orders/limit", json=BODY)
        # 3. a request object built by hand and sent
        built_request = client.client.build_request(method, f"{LIVE_BASE}/equity/orders/limit")
        with pytest.raises(PermissionError):
            await client.client.send(built_request)
        # 4. the verb helpers
        if method in {"POST", "PUT", "PATCH"}:
            helper = getattr(client.client, method.lower())
            with pytest.raises(PermissionError):
                await helper(f"{LIVE_BASE}/equity/orders/limit", json=BODY)
        if method == "DELETE":
            with pytest.raises(PermissionError):
                await client.client.delete(f"{LIVE_BASE}/equity/orders/1")

        assert recorder.count == 0
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_every_read_on_the_live_host_is_a_bodyless_get_to_the_live_host_only():
    clock = FakeClock()
    recorder = Recorder(_generic_router)
    built = server.build_clients(LIVE_ENV, transport=recorder.transport)
    client = built["invest"]
    client.governor = Governor(clock=clock, sleep=clock.sleep)
    try:
        for method_name, args, _ in READ_CALLS:
            await getattr(client, method_name)(*args)
    finally:
        await client.close()

    assert recorder.count == len(READ_CALLS)
    assert set(recorder.methods) == {"GET"}
    assert {request.url.host for request in recorder.requests} == {"live.trading212.com"}
    assert all(request.content == b"" for request in recorder.requests)


@pytest.mark.asyncio
async def test_the_hook_also_stops_a_non_get_on_the_demo_host():
    recorder = Recorder(ok_router)
    async with reader(recorder, use_demo=True) as (client, _):
        with pytest.raises(PermissionError):
            await client.client.request("POST", f"{DEMO_BASE}/equity/orders/limit", json=BODY)
    assert recorder.count == 0


@pytest.mark.parametrize("url", [
    "https://attacker.invalid/api/v0/equity/account/cash",
    "http://live.trading212.com/api/v0/equity/account/cash",
    "https://demo.trading212.com:8443/api/v0/equity/account/cash",
])
@pytest.mark.asyncio
async def test_get_to_an_untrusted_origin_is_refused_before_transport(url):
    recorder = Recorder(ok_router)
    async with reader(recorder) as (client, _):
        with pytest.raises(PermissionError, match="untrusted broker origin"):
            await client.client.get(url)
        request = client.client.build_request("GET", url)
        with pytest.raises(PermissionError, match="untrusted broker origin"):
            await client.client.send(request)
        client.base_url = url.rsplit("/equity", 1)[0]
        with pytest.raises(PermissionError, match="untrusted broker origin"):
            await client._request("GET", "equity/account/cash")
    assert recorder.count == 0


# --- explicit environment: no default, one helper for the server and the status ----

BAD_VALUES = [None, "", "yes", "1", "0", "TRUE", "True", "FALSE", " true", "true ", "demo", "live"]


@pytest.mark.parametrize("value", BAD_VALUES)
def test_an_unset_empty_or_unrecognised_environment_builds_no_client_and_names_the_variable(
    monkeypatch, value
):
    constructed = []
    monkeypatch.setattr(server, "Trading212Client", lambda *a, **k: constructed.append(a))
    real_init = httpx.AsyncClient.__init__
    http_clients = []

    def counting_init(self, *a, **k):
        http_clients.append(1)
        real_init(self, *a, **k)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", counting_init)
    environ = {"TRADING212_API_KEY": "k", "TRADING212_API_SECRET": "s"}
    if value is not None:
        environ[ENV_NAME] = value

    with pytest.raises(Trading212EnvironmentError, match=ENV_NAME):
        server.build_clients(environ)

    assert constructed == [] and http_clients == []


def test_true_selects_the_demo_base_url_and_false_selects_the_live_one():
    base = {"TRADING212_API_KEY": "k", "TRADING212_API_SECRET": "s"}
    demo = server.build_clients({**base, ENV_NAME: "true"})
    live = server.build_clients({**base, ENV_NAME: "false"})
    assert demo["invest"].base_url == DEMO_BASE
    assert live["invest"].base_url == LIVE_BASE


@pytest.mark.parametrize(
    "value,reported",
    [(None, "unset"), ("", "unset"), ("yes", "invalid"), ("true", "demo"), ("false", "live")],
)
@pytest.mark.asyncio
async def test_the_status_route_reports_the_value_the_server_acts_on(monkeypatch, value, reported):
    from routes.status_routes import get_system_status

    if value is None:
        monkeypatch.delenv(ENV_NAME, raising=False)
    else:
        monkeypatch.setenv(ENV_NAME, value)

    status = await get_system_status()

    assert status["environment"]["trading212"] == reported
    assert reported == resolve_trading212_environment()
    environ = {"TRADING212_API_KEY": "k", "TRADING212_API_SECRET": "s"}
    if value is not None:
        environ[ENV_NAME] = value
    if reported in {"demo", "live"}:
        built = server.build_clients(environ)
        acts_on = DEMO_BASE if reported == "demo" else LIVE_BASE
        assert built["invest"].base_url == acts_on
    else:
        with pytest.raises(Trading212EnvironmentError):
            server.build_clients(environ)


def test_the_status_route_has_no_default_environment_of_its_own():
    source = (BACKEND / "routes" / "status_routes.py").read_text(encoding="utf-8")
    assert "TRADING212_USE_DEMO" not in source
    assert "resolve_trading212_environment" in source


@pytest.mark.asyncio
async def test_a_server_started_with_a_bad_environment_answers_broker_tools_with_the_variable_name(
    monkeypatch,
):
    monkeypatch.setattr(server, "clients", {})
    monkeypatch.setattr(
        server,
        "startup_error",
        str(Trading212EnvironmentError(f"{ENV_NAME} must be exactly 'true' or 'false'")),
    )
    with pytest.raises(ValueError, match=ENV_NAME):
        server.get_active_client()


# --- credentials: Basic only, practice names never held ---------------------------


def test_a_key_without_a_secret_builds_no_client():
    env = {ENV_NAME: "true", "TRADING212_API_KEY": "only-a-key"}
    assert server.build_clients(env) == {}
    with pytest.raises(ValueError):
        server.Trading212Client("only-a-key", "", True)
    with pytest.raises(ValueError):
        server.Trading212Client("", "only-a-secret", True)


def test_an_isa_key_without_a_secret_does_not_build_an_isa_client():
    env = {
        ENV_NAME: "true",
        "TRADING212_API_KEY_INVEST": "ik",
        "TRADING212_API_SECRET_INVEST": "is",
        "TRADING212_API_KEY_ISA": "ak",
    }
    built = server.build_clients(env)
    assert set(built) == {"invest"}


@pytest.mark.asyncio
async def test_authorization_is_always_basic_with_key_and_secret():
    recorder = Recorder(ok_router)
    async with reader(recorder) as (client, _):
        await client.get_all_orders()
    header = recorder.requests[0].headers["authorization"]
    expected = "Basic " + base64.b64encode(b"key-canary:secret-canary").decode()
    assert header == expected
    assert header != "key-canary"


def test_the_practice_credential_names_are_never_read_by_the_server():
    env = {
        ENV_NAME: "true",
        "TRADING212_PRACTICE_API_KEY": "practice-key-canary",
        "TRADING212_PRACTICE_API_SECRET": "practice-secret-canary",
    }
    assert server.build_clients(env) == {}
    with_live = {**env, "TRADING212_API_KEY": "lk", "TRADING212_API_SECRET": "ls"}
    built = server.build_clients(with_live)
    assert built["invest"].api_key == "lk" and built["invest"].api_secret == "ls"


def test_drop_practice_credentials_removes_every_practice_name_and_only_those():
    environ = {
        "TRADING212_PRACTICE_API_KEY": "a",
        "trading212_practice_api_secret": "b",
        "TRADING212_API_KEY": "live",
        "TRADING212_USE_DEMO": "true",
    }
    removed = server.drop_practice_credentials(environ)
    assert sorted(removed) == ["TRADING212_PRACTICE_API_KEY", "trading212_practice_api_secret"]
    assert environ == {"TRADING212_API_KEY": "live", "TRADING212_USE_DEMO": "true"}


@pytest.mark.parametrize("workspace", ["uk", "india", None])
@pytest.mark.parametrize(
    "config",
    [
        {"name": "Trading 212", "command": "python3", "args": ["trading212_mcp_server.py"]},
        {"name": "Local Research", "command": "python3", "args": ["research_mcp_server.py"]},
    ],
    ids=["trading212", "other"],
)
def test_the_mcp_child_environment_never_contains_a_practice_credential(
    monkeypatch, workspace, config
):
    from mcp_client import build_mcp_subprocess_environment

    if workspace is None:
        monkeypatch.delenv("GROWIN_WORKSPACE", raising=False)
    else:
        monkeypatch.setenv("GROWIN_WORKSPACE", workspace)
    monkeypatch.setenv("TRADING212_PRACTICE_API_KEY", "practice-key-canary")
    monkeypatch.setenv("TRADING212_PRACTICE_API_SECRET", "practice-secret-canary")
    environment = build_mcp_subprocess_environment(
        {**config, "env": {"trading212_practice_api_key": "custom-canary", "OTHER": "kept"}}
    )
    assert [k for k in environment if k.upper().startswith("TRADING212_PRACTICE_")] == []
    assert "practice-key-canary" not in environment.values()
    assert "custom-canary" not in environment.values()
    assert environment.get("OTHER") == "kept"


def test_the_live_key_still_reaches_a_uk_trading212_child(monkeypatch):
    from mcp_client import build_mcp_subprocess_environment

    monkeypatch.setenv("GROWIN_WORKSPACE", "uk")
    monkeypatch.setenv("TRADING212_API_KEY", "live-key-canary")
    environment = build_mcp_subprocess_environment(
        {"name": "Trading 212", "command": "python3", "args": ["trading212_mcp_server.py"]}
    )
    assert environment["TRADING212_API_KEY"] == "live-key-canary"


# --- the route, the request model and the handler are gone ------------------------


def test_the_config_route_returns_404_or_405_and_forwards_nothing(monkeypatch):
    from unittest.mock import AsyncMock

    from fastapi.testclient import TestClient

    from app_context import state
    from server import app

    forward = AsyncMock()
    monkeypatch.setattr(state._mcp_client, "call_tool", forward, raising=False)
    with TestClient(app) as client:
        response = client.post(
            "/mcp/trading212/config",
            json={"account_type": "invest", "invest_key": "k-canary", "invest_secret": "s-canary"},
        )
    assert response.status_code in {404, 405}
    assert "k-canary" not in response.text and "s-canary" not in response.text
    forward.assert_not_awaited()
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/mcp/trading212/config" not in paths


def test_the_request_model_and_the_market_order_handler_no_longer_exist():
    import app_context
    import t212_handlers

    assert not hasattr(app_context, "T212ConfigRequest")
    assert not hasattr(t212_handlers, "handle_market_order")
    assert "T212ConfigRequest" not in (BACKEND / "routes" / "mcp_routes.py").read_text(encoding="utf-8")


# --- main(): practice names dropped after .env loads, bad environment keeps the server up ---


@pytest.mark.asyncio
async def test_main_drops_practice_names_and_builds_no_client_on_a_bad_environment(monkeypatch):
    import os
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    @asynccontextmanager
    async def fake_stdio():
        yield (None, None)

    monkeypatch.setattr(server, "load_dotenv", lambda *a, **k: True)
    monkeypatch.setattr(server, "stdio_server", fake_stdio)
    run = AsyncMock()
    monkeypatch.setattr(server.app, "run", run)
    monkeypatch.setattr(server, "clients", {"stale": object()})
    monkeypatch.setattr(server, "startup_error", None)  # restored on teardown
    monkeypatch.setattr(server, "active_account_type", "invest")
    monkeypatch.delenv(ENV_NAME, raising=False)
    monkeypatch.setenv("TRADING212_API_KEY", "k")
    monkeypatch.setenv("TRADING212_API_SECRET", "s")
    monkeypatch.setenv("TRADING212_PRACTICE_API_KEY", "practice-key-canary")
    monkeypatch.setenv("TRADING212_PRACTICE_API_SECRET", "practice-secret-canary")

    await server.main()

    assert [name for name in os.environ if name.upper().startswith("TRADING212_PRACTICE_")] == []
    assert server.clients == {}
    assert ENV_NAME in (server.startup_error or "")
    run.assert_awaited_once()  # the server stays up for its non-broker tools
    with pytest.raises(ValueError, match=ENV_NAME):
        server.get_active_client()


@pytest.mark.asyncio
async def test_a_429_with_only_a_reset_header_still_waits_for_that_reset():
    clock = FakeClock()
    reset = int(clock.now) + 9
    answers = iter(
        [
            httpx.Response(429, headers={"x-ratelimit-reset": str(reset)}, text="Limited: 1 / 1s"),
            Fixture("positions").response(clock),
        ]
    )
    recorder = Recorder(lambda request: next(answers))
    async with reader(recorder, clock=clock) as (client, _):
        await client.get_all_positions()
    assert recorder.count == 2
    assert clock.sleeps == [pytest.approx(9.0)]


@pytest.mark.asyncio
async def test_a_429_with_no_rate_headers_waits_one_full_period_of_that_endpoint():
    clock = FakeClock()
    answers = iter(
        [httpx.Response(429, text="Limited"), Fixture("history_orders").response(clock)]
    )
    recorder = Recorder(lambda request: next(answers))
    async with reader(recorder, clock=clock) as (client, _):
        await client.get_historical_orders(limit=20)
    assert recorder.count == 2
    assert clock.sleeps == [pytest.approx(60.0)]  # the history period, not its 3 s spacing
