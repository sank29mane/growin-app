"""Phase 66-03 Task 2: live unreachable, one send, keys, and in-flight guards.

Every test runs on ``httpx.MockTransport`` through ``FakeDemoBroker`` or a
scripted handler. An autouse guard fails on any real transport or socket
connect. Key values are canaries: the tests then look for them everywhere.
"""

from __future__ import annotations

import ast
import asyncio
import base64
import json
import logging
import re
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import workspace_credentials
from brokers.trading212.practice_dispatcher import (
    PracticeOrderRefused,
    PracticePinError,
    build_limit_body,
)
from brokers.trading212.practice_metadata import (
    CACHE_TTL_SECONDS,
    MetadataUnavailable,
    PracticeMetadata,
)
from brokers.trading212.practice_transport import (
    DEMO_BASE_URL,
    PracticeHostRefused,
    PracticeRateLimited,
    PracticeTransport,
    PracticeTransportError,
)
from execution import (
    ExecutionConflictError,
    ExecutionDisabledError,
    OrderIntent,
)
from execution.service import BrokerExecutionError, BrokerOutcomeUnknownError
from routes import ai_routes
from t212_practice_testkit import (
    CANARIES,
    DEMO_LIMIT_URL,
    KEY_CANARY,
    LIVE_KEY_CANARY,
    LIVE_SECRET_CANARY,
    PRACTICE_ACCOUNT,
    SECRET_CANARY,
    FakeDemoBroker,
    all_ledger_text,
    place,
    practice_env,
    practice_proposal_dict,
    prepare_fixture,
    sign_and_approve,
    start_practice_stack,
)
from t212_testkit import Fixture, FakeClock, install_no_real_network
from venue_seam_testkit import write_practice_files

REPO = Path(__file__).resolve().parents[2]
PRACTICE_SRC = REPO / "backend" / "brokers" / "trading212"
EXECUTION_SRC = REPO / "backend" / "execution"


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    install_no_real_network(monkeypatch)


def basic(key: str, secret: str) -> str:
    return "Basic " + base64.b64encode(f"{key}:{secret}".encode()).decode()


def sources(directory: Path) -> dict[Path, str]:
    return {path: path.read_text(encoding="utf-8") for path in sorted(directory.glob("*.py"))}


def scripted(broker: FakeDemoBroker, post):
    """A handler: the broker answers everything except POST, which ``post`` answers."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            broker.requests.append(request)
            return post(request)
        return broker(request)

    return handle


# --- live is unreachable by construction (D-13) ------------------------------------------

OTHER_URLS = [
    "https://live.trading212.com/api/v0/equity/orders/limit",
    "https://example.com/api/v0/equity/orders/limit",
    "http://demo.trading212.com/api/v0/equity/orders/limit",
    "https://demo.trading212.com.evil.test/api/v0/equity/orders/limit",
    "https://demo.trading212.com:8443/api/v0/equity/orders/limit",
    "https://demo.trading212.com@live.trading212.com/api/v0/equity/orders/limit",
    "https://demo.trading212.com/other/path",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", OTHER_URLS)
async def test_a_request_to_any_other_host_scheme_or_path_is_refused_before_it_is_sent(url):
    clock = FakeClock()
    broker = FakeDemoBroker(clock)
    transport = PracticeTransport(
        KEY_CANARY, SECRET_CANARY, clock=clock, sleep=clock.sleep, transport=httpx.MockTransport(broker)
    )
    with pytest.raises(PracticeTransportError) as refused:
        await transport._client.request("POST", url, json={"x": 1})
    assert refused.value.sent is False
    assert broker.requests == [], "the request hook refused it before the transport saw it"
    await transport.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_a_redirect_is_never_followed_and_is_not_a_success(status):
    clock = FakeClock()
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(status, headers={"Location": "https://live.trading212.com/api/v0/x"})

    transport = PracticeTransport(
        KEY_CANARY, SECRET_CANARY, clock=clock, sleep=clock.sleep, transport=httpx.MockTransport(handle)
    )
    with pytest.raises(PracticeTransportError) as refused:
        await transport.post_limit_order(
            {"ticker": "VODl_EQ", "quantity": 1, "limitPrice": 50.0, "timeValidity": "DAY"}
        )
    assert refused.value.kind == "redirect_refused"
    assert refused.value.sent is True, "a redirected POST may have been processed: unknown, not failed"
    assert seen == [DEMO_LIMIT_URL], "no second request, and never to the redirect target"
    with pytest.raises(PracticeTransportError):
        await transport.get("/equity/account/summary")
    assert len(seen) == 2
    await transport.aclose()


def test_no_live_host_string_exists_in_the_practice_or_execution_source():
    live = re.compile(r"live\.trading212|trading212\.com(?!/)|api/v0.*live", re.IGNORECASE)
    offenders = []
    for directory in (PRACTICE_SRC, EXECUTION_SRC):
        for path, text in sources(directory).items():
            for number, line in enumerate(text.splitlines(), 1):
                if "live.trading212.com" in line.lower():
                    offenders.append(f"{path.name}:{number}")
                elif live.search(line) and "demo.trading212.com" not in line:
                    offenders.append(f"{path.name}:{number}")
    assert offenders == []


def test_the_demo_base_url_is_the_only_host_the_practice_package_defines():
    hosts = set()
    for path, text in sources(PRACTICE_SRC).items():
        hosts.update(re.findall(r"[A-Za-z0-9.-]+\.trading212\.com", text))
    assert hosts == {"demo.trading212.com"}
    assert DEMO_BASE_URL == "https://demo.trading212.com/api/v0"


def test_the_practice_package_imports_no_mcp_server_or_client():
    banned = ("trading212_mcp_server", "mcp_client", "mcp")
    offenders = []
    for path, text in sources(PRACTICE_SRC).items():
        for node in ast.walk(ast.parse(text)):
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                if name.split(".")[0] in banned:
                    offenders.append(f"{path.name}: {name}")
    assert offenders == []


def test_the_practice_package_reads_no_environment_variable_and_never_names_use_demo():
    offenders = []
    for path, text in sources(PRACTICE_SRC).items():
        assert "TRADING212_USE_DEMO" not in text, path.name
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv", "environb"}:
                offenders.append(f"{path.name}:{node.lineno}")
            if isinstance(node, ast.Name) and node.id in {"environ", "getenv"}:
                offenders.append(f"{path.name}:{node.lineno}")
    assert offenders == []


def test_the_practice_package_has_no_random_and_no_wall_clock_call():
    offenders = []
    for path, text in sources(PRACTICE_SRC).items():
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
                if any(name.split(".")[0] == "random" for name in names):
                    offenders.append(f"{path.name}: random import")
            if isinstance(node, ast.Call):
                func = node.func
                dotted = ast.unparse(func)
                if dotted in {"time.time", "time.monotonic", "datetime.now", "datetime.utcnow", "asyncio.sleep"}:
                    offenders.append(f"{path.name}:{node.lineno} {dotted}()")
    assert offenders == []


# --- the account pin (D-13) --------------------------------------------------------------


def _summary_response(**changes):
    body = {"id": int(PRACTICE_ACCOUNT), "currency": "GBP"}
    body.update(changes)
    return httpx.Response(200, json=body)


PIN_FAILURES = {
    "other-account": (lambda: _summary_response(id=99999999), "PIN_ACCOUNT_MISMATCH"),
    "other-currency": (lambda: _summary_response(currency="USD"), "PIN_CURRENCY_MISMATCH"),
    "unauthorised": (lambda: httpx.Response(401, text="Unauthorized"), "PIN_AUTH_FAILED"),
    "forbidden": (lambda: httpx.Response(403, text="Forbidden"), "PIN_AUTH_FAILED"),
    "server-error": (lambda: httpx.Response(500, text="boom"), "PIN_RESPONSE_INVALID"),
    "not-json": (lambda: httpx.Response(200, text="<html>"), "PIN_RESPONSE_INVALID"),
    "id-missing": (lambda: httpx.Response(200, json={"currency": "GBP"}), "PIN_RESPONSE_INVALID"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(PIN_FAILURES))
async def test_a_wrong_account_or_currency_or_failed_read_leaves_execution_disabled(
    case, tmp_path, private_config_dir, monkeypatch
):
    build, code = PIN_FAILURES[case]
    clock = FakeClock()
    broker = FakeDemoBroker(clock)
    broker.summary_override = build
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch, broker=broker)
    try:
        assert stack.started is False
        assert stack.app.execution_authority is False
        assert stack.app.execution_mode == "disabled"
        assert code in stack.app.execution_startup_error
        assert stack.app.execution_service.execution_enabled is False
        assert broker.mutations == []
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_timeout_on_the_pin_read_leaves_execution_disabled(
    tmp_path, private_config_dir, monkeypatch
):
    clock = FakeClock()
    broker = FakeDemoBroker(clock)

    def handle(request):
        raise httpx.ReadTimeout("slow", request=request)

    stack = await start_practice_stack(
        tmp_path, private_config_dir, monkeypatch, broker=broker, handler=handle
    )
    try:
        assert stack.started is False
        assert "PIN_UNREACHABLE" in stack.app.execution_startup_error
    finally:
        stack.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", sorted(PIN_FAILURES))
async def test_approve_answers_503_when_the_pin_failed(
    case, tmp_path, private_config_dir, monkeypatch
):
    build, _ = PIN_FAILURES[case]
    clock = FakeClock()
    broker = FakeDemoBroker(clock)
    broker.summary_override = build
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch, broker=broker)
    api = FastAPI()
    api.include_router(ai_routes.router)
    monkeypatch.setattr(ai_routes, "state", stack.app)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=api, client=("127.0.0.1", 1)), base_url="http://test"
        ) as client:
            response = await client.post(
                "/api/ai/trade/approval/complete",
                json={
                    "proposal_id": "p-503",
                    "challenge_id": "00000000-0000-0000-0000-000000000001",
                    "signature_der_b64": base64.b64encode(b"x" * 12).decode(),
                    "workspace": "uk",
                },
            )
        assert response.status_code == 503
        assert broker.mutations == []
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_an_unverified_account_takes_no_order_and_consumes_no_approval(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch, verify=False)
    try:
        assert stack.adapter.execution_ready is False
        prepare_fixture(stack, "unverified-1")
        with pytest.raises(ExecutionDisabledError):
            await sign_and_approve(stack, "unverified-1")
        assert stack.broker.requests == []
        assert stack.ledger.get_order("unverified-1").state == "PENDING"
        assert stack.ledger.approval_evidence_count("unverified-1") == 0
    finally:
        stack.close()


# --- keys come only from uk_credential, both required (D-07) -------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variant",
    ["no-key", "no-secret", "neither", "live-set-practice-unset", "blank-key", "blank-secret"],
)
async def test_the_venue_does_not_start_without_both_practice_halves_and_sends_nothing(
    variant, tmp_path, private_config_dir, monkeypatch
):
    practice_env(monkeypatch, practice_pair=False, live_pair=True)
    if variant == "no-key":
        monkeypatch.setenv("TRADING212_PRACTICE_API_SECRET", SECRET_CANARY)
    elif variant == "no-secret":
        monkeypatch.setenv("TRADING212_PRACTICE_API_KEY", KEY_CANARY)
    elif variant == "blank-key":
        monkeypatch.setenv("TRADING212_PRACTICE_API_KEY", "   ")
        monkeypatch.setenv("TRADING212_PRACTICE_API_SECRET", SECRET_CANARY)
    elif variant == "blank-secret":
        monkeypatch.setenv("TRADING212_PRACTICE_API_KEY", KEY_CANARY)
        monkeypatch.setenv("TRADING212_PRACTICE_API_SECRET", "")
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch, env=False)
    try:
        assert stack.started is False
        assert "PRACTICE_CREDENTIALS_MISSING" in stack.app.execution_startup_error
        assert stack.broker.requests == []
        assert LIVE_KEY_CANARY not in stack.app.execution_startup_error
    finally:
        stack.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("process", [None, "india", "us", "UK"])
async def test_an_india_or_unset_process_cannot_start_the_practice_venue(
    process, tmp_path, private_config_dir, monkeypatch
):
    practice_env(monkeypatch, workspace=process)
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch, env=False)
    try:
        assert stack.started is False
        assert stack.app.execution_authority is False
        assert stack.broker.requests == []
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_the_practice_pair_is_read_only_through_uk_credential_and_never_the_live_names(
    tmp_path, private_config_dir, monkeypatch
):
    asked: list[str] = []
    real = workspace_credentials.uk_credential

    def spy(name: str):
        asked.append(name)
        return real(name)

    monkeypatch.setattr(workspace_credentials, "uk_credential", spy)
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        assert stack.started
        assert sorted(asked) == ["TRADING212_PRACTICE_API_KEY", "TRADING212_PRACTICE_API_SECRET"]
    finally:
        stack.close()


# --- the exact 66-05 environment (D-13, C9) ------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("use_demo", ["false", "true", None, "garbage"])
async def test_the_practice_order_goes_to_the_demo_url_whatever_use_demo_says(
    use_demo, tmp_path, private_config_dir, monkeypatch
):
    practice_env(monkeypatch, use_demo=use_demo, practice_pair=True, live_pair=True)
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch, env=False)
    try:
        assert stack.started, stack.app.execution_startup_error
        await place(stack, "env-1")
        assert len(stack.broker.posts) == 1
        post = stack.broker.posts[0]
        assert str(post.url) == "https://demo.trading212.com/api/v0/equity/orders/limit"
        assert post.headers["Authorization"] == basic(KEY_CANARY, SECRET_CANARY)
        for request in stack.broker.requests:
            assert request.url.host == "demo.trading212.com"
            blob = repr(dict(request.headers)) + request.content.decode("utf-8", "replace")
            assert LIVE_KEY_CANARY not in blob and LIVE_SECRET_CANARY not in blob
            assert basic(LIVE_KEY_CANARY, LIVE_SECRET_CANARY) not in blob
    finally:
        stack.close()


# --- D-15 classification: one POST per row, never a resend ---------------------------------

FAILED = "FAILED"
UNKNOWN = "UNKNOWN"
ACK = "ACKNOWLEDGED"


def _fixture_post(name: str):
    clock = FakeClock()
    return lambda request: Fixture(name).response(clock)


def _raise(exc_type):
    def post(request):
        raise exc_type("synthetic", request=request)

    return post


CLASSIFICATION = [
    ("200-with-id", _fixture_post("order_limit_200"), ACK),
    ("200-without-id", _fixture_post("order_limit_200_no_id"), UNKNOWN),
    ("400", _fixture_post("order_limit_400"), FAILED),
    ("401", _fixture_post("order_limit_401"), FAILED),
    ("403", _fixture_post("order_limit_403"), FAILED),
    ("408", _fixture_post("order_limit_408"), UNKNOWN),
    ("429", _fixture_post("order_limit_429"), UNKNOWN),
    ("500", _fixture_post("order_limit_500"), UNKNOWN),
    ("503", _fixture_post("order_limit_503"), UNKNOWN),
    ("404-not-in-table", lambda request: httpx.Response(404, text="nope"), UNKNOWN),
    ("201-not-in-table", lambda request: httpx.Response(201, json={"id": 5}), UNKNOWN),
    ("200-not-json", lambda request: httpx.Response(200, text="ok"), UNKNOWN),
    ("200-id-is-text", lambda request: httpx.Response(200, json={"id": "abc"}), UNKNOWN),
    ("200-id-is-bool", lambda request: httpx.Response(200, json={"id": True}), UNKNOWN),
    ("200-id-zero", lambda request: httpx.Response(200, json={"id": 0}), UNKNOWN),
    ("ConnectError", _raise(httpx.ConnectError), FAILED),
    ("ConnectTimeout", _raise(httpx.ConnectTimeout), FAILED),
    ("ReadTimeout", _raise(httpx.ReadTimeout), UNKNOWN),
    ("WriteTimeout", _raise(httpx.WriteTimeout), UNKNOWN),
    ("RemoteProtocolError", _raise(httpx.RemoteProtocolError), UNKNOWN),
    ("ReadError", _raise(httpx.ReadError), UNKNOWN),
    ("PoolTimeout", _raise(httpx.PoolTimeout), UNKNOWN),
    ("RuntimeError", _raise(RuntimeError), UNKNOWN),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("name,post,expected", CLASSIFICATION, ids=[row[0] for row in CLASSIFICATION])
async def test_each_outcome_sends_exactly_one_post_and_lands_in_its_tabled_state(
    name, post, expected, tmp_path, private_config_dir, monkeypatch
):
    clock = FakeClock()
    broker = FakeDemoBroker(clock)
    stack = await start_practice_stack(
        tmp_path, private_config_dir, monkeypatch, broker=broker, clock=clock,
        handler=scripted(broker, post),
    )
    try:
        assert stack.started
        outcome = None
        try:
            outcome = await place(stack, "class-1")
        except BrokerExecutionError as exc:
            assert expected == FAILED, f"{name}: {exc!r}"
        except BrokerOutcomeUnknownError as exc:
            assert expected == UNKNOWN, f"{name}: {exc!r}"
        else:
            assert expected == ACK
            assert outcome.broker_order_id.isdigit()
        assert len(broker.posts) == 1, "never a second send"
        order = stack.ledger.get_order("class-1")
        assert order.state == expected
        reservation = stack.ledger.get_reservation("class-1")
        if expected == FAILED:
            assert reservation.outstanding == 0 and reservation.state == "SETTLED"
        else:
            assert reservation.state == "ACTIVE", "UNKNOWN and ACK keep the reservation"
        # An UNKNOWN order never becomes FAILED without broker evidence.
        if expected == UNKNOWN:
            assert order.acknowledgment is None
            events = [e.to_state for e in stack.ledger.list_events("class-1")]
            assert "FAILED" not in events
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_failed_order_records_a_short_non_secret_reason_code(
    tmp_path, private_config_dir, monkeypatch
):
    clock = FakeClock()
    broker = FakeDemoBroker(clock)
    stack = await start_practice_stack(
        tmp_path, private_config_dir, monkeypatch, broker=broker, clock=clock,
        handler=scripted(broker, _fixture_post("order_limit_403")),
    )
    try:
        with pytest.raises(BrokerExecutionError):
            await place(stack, "reason-1")
        failed = [e for e in stack.ledger.list_events("reason-1") if e.to_state == "FAILED"]
        assert failed[-1].payload == {"reason_code": "HTTP_403"}
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_429_on_a_get_is_retried_once_after_the_governor_wait_and_a_second_429_raises():
    clock = FakeClock()
    calls = []

    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(
            429, headers={"x-ratelimit-reset": str(int(clock.now) + 5)}, text="Limited"
        )

    transport = PracticeTransport(
        KEY_CANARY, SECRET_CANARY, clock=clock, sleep=clock.sleep, transport=httpx.MockTransport(handle)
    )
    with pytest.raises(PracticeRateLimited):
        await transport.get("/equity/account/summary")
    assert len(calls) == 2, "one retry, never a third request"
    assert any(sleep >= 4.0 for sleep in clock.sleeps), "the retry waited for the reset"
    await transport.aclose()


# --- orders the adapter refuses before any request (D-04) ------------------------------------

GOOD = dict(ticker="VODl_EQ", side="BUY", quantity=Decimal("3"), limit_price=Decimal("50"))


@pytest.mark.parametrize("order_type", ["MARKET", "STOP", "STOP_LIMIT", "stop_limit", "", None])
def test_only_a_limit_order_type_can_be_built(order_type):
    with pytest.raises(PracticeOrderRefused) as refused:
        build_limit_body(**GOOD, order_type=order_type)
    assert refused.value.code == "ORDER_TYPE_REFUSED"


@pytest.mark.parametrize("validity", ["GOOD_TILL_CANCEL", "GTC", "IOC", "day", "", None, "UNKNOWN_VALUE"])
def test_only_day_is_accepted_as_time_validity(validity):
    with pytest.raises(PracticeOrderRefused) as refused:
        build_limit_body(**GOOD, time_validity=validity)
    assert refused.value.code == "TIME_VALIDITY_REFUSED"


@pytest.mark.parametrize("flag", [True, 1, "true"])
def test_extended_hours_is_refused(flag):
    with pytest.raises(PracticeOrderRefused) as refused:
        build_limit_body(**GOOD, extended_hours=flag)
    assert refused.value.code == "EXTENDED_HOURS_REFUSED"


@pytest.mark.parametrize("quantity", ["1.5", "0.1", "0", "-3", "NaN", "Infinity"])
def test_fractional_zero_and_negative_quantities_are_refused(quantity):
    with pytest.raises(PracticeOrderRefused) as refused:
        build_limit_body(**{**GOOD, "quantity": Decimal(quantity)})
    assert refused.value.code == "QUANTITY_NOT_WHOLE"


@pytest.mark.parametrize("price", ["0", "-1", "-0.01", "NaN"])
def test_zero_negative_and_non_finite_prices_are_refused(price):
    with pytest.raises(PracticeOrderRefused) as refused:
        build_limit_body(**{**GOOD, "limit_price": Decimal(price)})
    assert refused.value.code == "LIMIT_PRICE_REFUSED"


def test_a_sell_body_carries_a_negative_quantity_and_a_buy_a_positive_one():
    sell = build_limit_body(**{**GOOD, "side": "SELL"})
    buy = build_limit_body(**GOOD)
    assert sell == {"ticker": "VODl_EQ", "quantity": -3, "limitPrice": 50.0, "timeValidity": "DAY"}
    assert buy["quantity"] == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        {"timeValidity": "GOOD_TILL_CANCEL"},
        {"timeValidity": "UNKNOWN_VALUE"},
        {"extendedHours": True},
        {"stopPrice": 40.0},
        {"timeValidity": None},
    ],
)
async def test_the_transport_refuses_a_non_limit_day_body_before_any_request(mutation):
    clock = FakeClock()
    broker = FakeDemoBroker(clock)
    transport = PracticeTransport(
        KEY_CANARY, SECRET_CANARY, clock=clock, sleep=clock.sleep, transport=httpx.MockTransport(broker)
    )
    body = {"ticker": "VODl_EQ", "quantity": 1, "limitPrice": 50.0, "timeValidity": "DAY", **mutation}
    with pytest.raises(PracticeTransportError):
        await transport.post_limit_order(body)
    assert broker.requests == []
    await transport.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides,code",
    [
        ({"order_type": None, "limit_price": None}, "ORDER_TYPE_REFUSED"),
        ({"quantity": "1.5"}, "QUANTITY_NOT_WHOLE"),
        ({"ticker": "VODl EQ; DROP"}, "TICKER_REFUSED"),
    ],
)
async def test_an_intent_the_adapter_cannot_send_fails_with_a_stable_code_and_no_request(
    overrides, code, tmp_path, private_config_dir, monkeypatch
):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        proposal = practice_proposal_dict("refuse-1")
        proposal.update(overrides)
        from execution.venue import PRICE_SOURCE_TEST_REPLAY

        admission = stack.service.prepare(
            proposal, currency="GBP", price="0.5", price_source=PRICE_SOURCE_TEST_REPLAY,
            **stack.app._local_paper_preflight(),
        )
        assert admission.decision.value == "ADMITTED"
        with pytest.raises(BrokerExecutionError) as refused:
            await sign_and_approve(stack, "refuse-1")
        assert refused.value.code == code
        assert stack.broker.posts == []
        assert stack.ledger.get_order("refuse-1").state == "FAILED"
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_sell_posts_a_negative_quantity_while_the_ledger_keeps_it_positive(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        await place(stack, "buy-3", quantity="3")
        stack.broker.fill(7000001, price=49.0)
        assert (await stack.adapter.reconciler.reconcile("buy-3")).state == "FILLED"

        await place(stack, "sell-3", side="SELL", quantity="3", limit_price="48", broker_available="3")
        post = stack.broker.posts[-1]
        assert str(post.url) == DEMO_LIMIT_URL
        assert json.loads(post.content) == {
            "ticker": "VODl_EQ", "quantity": -3, "limitPrice": 48.0, "timeValidity": "DAY",
        }
        stored = stack.ledger.get_order("sell-3")
        assert stored.intent["side"] == "SELL" and stored.intent["quantity"] == "3"
        assert stack.ledger.get_reservation("sell-3").reserved == Decimal("3")
    finally:
        stack.close()


# --- one in-flight order per ticker (D-16) ---------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("held_state", ["ACKNOWLEDGED", "UNKNOWN", "PARTIALLY_FILLED", "SUBMITTING"])
async def test_a_second_order_on_a_ticker_with_an_order_in_flight_is_refused_before_any_request(
    held_state, tmp_path, private_config_dir, monkeypatch
):
    clock = FakeClock()
    broker = FakeDemoBroker(clock)
    if held_state == "UNKNOWN":
        handler = scripted(broker, lambda request: httpx.Response(408, text="Timed-out"))
    else:
        handler = None
    stack = await start_practice_stack(
        tmp_path, private_config_dir, monkeypatch, broker=broker, clock=clock, handler=handler
    )
    try:
        if held_state == "UNKNOWN":
            with pytest.raises(BrokerOutcomeUnknownError):
                await place(stack, "first")
        elif held_state == "SUBMITTING":
            prepare_fixture(stack, "first")
            challenge = stack.service.create_approval_challenge("first", workspace="uk")
            from venue_seam_testkit import sign

            stack.ledger.claim_with_approval(
                proposal_id="first",
                challenge_id=challenge.challenge_id,
                key_id=challenge.key_id,
                signature_der=sign(stack.key, challenge.signed_payload),
                verified_payload_hash=__import__("hashlib").sha256(challenge.signed_payload).hexdigest(),
                now_epoch=int(__import__("time").time()),
            )
        else:
            await place(stack, "first")
            if held_state == "PARTIALLY_FILLED":
                stack.broker.fill(7000001, price=49.0, quantity=1)
                await stack.adapter.reconciler.reconcile("first")
        assert stack.ledger.get_order("first").state == held_state
        posts_before = len(broker.posts)

        prepare_fixture(stack, "second")
        with pytest.raises(ExecutionConflictError):
            await sign_and_approve(stack, "second")
        assert len(broker.posts) == posts_before, "refused before any request"
        assert stack.ledger.get_order("second").state == "PENDING"
        assert stack.ledger.approval_evidence_count("second") == 0, "the refusal consumed nothing"
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_the_ticker_is_free_again_once_the_first_order_reconciles_cancelled(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        await place(stack, "first")
        prepare_fixture(stack, "second")
        with pytest.raises(ExecutionConflictError):
            await sign_and_approve(stack, "second")

        stack.broker.expire(7000001)
        result = await stack.adapter.reconciler.reconcile("first")
        assert result.state == "CANCELLED"
        await sign_and_approve(stack, "second")
        assert stack.ledger.get_order("second").state == "ACKNOWLEDGED"
        assert len(stack.broker.posts) == 2
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_different_tickers_may_be_in_flight_together(tmp_path, private_config_dir, monkeypatch):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        for index, ticker in enumerate(["VODl_EQ", "LLOYl_EQ", "BARCl_EQ"]):
            await place(stack, f"far-{index}", ticker=ticker, quantity="1")
        assert len(stack.broker.posts) == 3
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_two_concurrent_approvals_on_one_ticker_cannot_both_dispatch(
    tmp_path, private_config_dir, monkeypatch
):
    clock = FakeClock()
    broker = FakeDemoBroker(clock)
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch, broker=broker, clock=clock)
    try:
        prepare_fixture(stack, "race-a")
        prepare_fixture(stack, "race-b")
        results = await asyncio.gather(
            sign_and_approve(stack, "race-a"),
            sign_and_approve(stack, "race-b"),
            return_exceptions=True,
        )
        assert len(broker.posts) == 1
        refused = [r for r in results if isinstance(r, ExecutionConflictError)]
        assert len(refused) == 1 and len(results) == 2
    finally:
        stack.close()


# --- governor on every request (UKT-03, D-14) ------------------------------------------------


@pytest.mark.asyncio
async def test_every_request_takes_its_governor_slot_first_and_orders_are_spaced_two_seconds(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        governor = stack.adapter.transport.governor
        slots: list[str] = []
        real_acquire = governor.acquire

        async def spy(method, path):
            slots.append(f"{method} {path}")
            return await real_acquire(method, path)

        monkeypatch.setattr(governor, "acquire", spy)
        before = len(stack.broker.requests)
        await place(stack, "gov-a", ticker="VODl_EQ", quantity="1")
        first = stack.clock.now
        await place(stack, "gov-b", ticker="LLOYl_EQ", quantity="1")
        assert stack.clock.now - first >= 2.0 - 1e-6, "limit POSTs are at least 2 s apart"
        sent = len(stack.broker.requests) - before
        assert len(slots) == sent and sent == 2
        assert all(slot.startswith("POST /equity/orders/limit") for slot in slots)
    finally:
        stack.close()


# --- instrument and exchange metadata (D-20) -----------------------------------------------------


@pytest.mark.asyncio
async def test_the_metadata_cache_answers_from_governed_reads_and_refreshes_after_ten_minutes():
    clock = FakeClock()
    broker = FakeDemoBroker(clock)
    transport = PracticeTransport(
        KEY_CANARY, SECRET_CANARY, clock=clock, sleep=clock.sleep, transport=httpx.MockTransport(broker)
    )
    metadata = PracticeMetadata(transport, clock=clock)
    assert metadata.fresh() is False and metadata.instrument("VODl_EQ") is None

    await metadata.refresh_if_stale()
    assert metadata.fresh() is True
    vod = metadata.instrument("VODl_EQ")
    assert vod.currency_code == "GBX" and vod.max_open_quantity == Decimal("50000")
    assert metadata.instrument("AAPL_US_EQ").currency_code == "USD"
    assert metadata.instrument("NOPE") is None
    assert metadata.exchange_open(vod.working_schedule_id) is True
    assert metadata.exchange_open(303) is False, "closed per the cached schedule"
    assert metadata.exchange_open(None) is False and metadata.exchange_open(9999) is False

    reads = len(broker.requests)
    await metadata.refresh_if_stale()
    assert len(broker.requests) == reads, "served from cache inside the TTL"
    clock.now += CACHE_TTL_SECONDS + 1
    await metadata.refresh_if_stale()
    assert len(broker.requests) == reads + 2, "both halves refreshed after the TTL"
    paths = {request.url.path for request in broker.requests}
    assert paths == {"/api/v0/equity/metadata/instruments", "/api/v0/equity/metadata/exchanges"}
    await transport.aclose()


@pytest.mark.asyncio
async def test_a_failed_metadata_read_raises_a_stable_code_and_leaves_the_cache_unfresh():
    clock = FakeClock()
    broker = FakeDemoBroker(clock)
    broker.get_override = lambda request: httpx.Response(500, text="boom")
    transport = PracticeTransport(
        KEY_CANARY, SECRET_CANARY, clock=clock, sleep=clock.sleep, transport=httpx.MockTransport(broker)
    )
    metadata = PracticeMetadata(transport, clock=clock)
    with pytest.raises(MetadataUnavailable) as failed:
        await metadata.refresh_if_stale()
    assert failed.value.code == "METADATA_UNAVAILABLE"
    assert metadata.fresh() is False
    await transport.aclose()


@pytest.mark.asyncio
async def test_a_schedule_whose_last_event_is_old_says_closed():
    clock = FakeClock()
    broker = FakeDemoBroker(clock)
    transport = PracticeTransport(
        KEY_CANARY, SECRET_CANARY, clock=clock, sleep=clock.sleep, transport=httpx.MockTransport(broker)
    )
    metadata = PracticeMetadata(transport, clock=clock)
    await metadata.refresh_if_stale()
    assert metadata.exchange_open(202) is True
    clock.now += 3 * 24 * 3600
    assert metadata.exchange_open(202) is False, "a stale schedule is never 'open'"
    await transport.aclose()


# --- no key material anywhere (D-25) ------------------------------------------------------------


def _canary_forms() -> list[str]:
    forms = list(CANARIES)
    for key, secret in ((KEY_CANARY, SECRET_CANARY), (LIVE_KEY_CANARY, LIVE_SECRET_CANARY)):
        forms.append(base64.b64encode(f"{key}:{secret}".encode()).decode())
    return forms


@pytest.mark.asyncio
@pytest.mark.parametrize("name,post,expected", CLASSIFICATION[:6] + CLASSIFICATION[15:19],
                         ids=[row[0] for row in CLASSIFICATION[:6] + CLASSIFICATION[15:19]])
async def test_no_key_material_reaches_logs_errors_acks_or_ledger_rows(
    name, post, expected, tmp_path, private_config_dir, monkeypatch, caplog
):
    caplog.set_level(logging.DEBUG)
    clock = FakeClock()
    broker = FakeDemoBroker(clock)
    stack = await start_practice_stack(
        tmp_path, private_config_dir, monkeypatch, broker=broker, clock=clock,
        handler=scripted(broker, post),
    )
    texts: list[str] = []
    try:
        try:
            ack = await place(stack, "leak-1")
            texts.append(repr(ack) + json.dumps(ack.raw))
        except (BrokerExecutionError, BrokerOutcomeUnknownError) as exc:
            texts.append(repr(exc) + str(exc))
            for part in (exc.__cause__, exc.__context__):
                texts.append(repr(part))
        texts.append(all_ledger_text(stack.ledger))
        texts.append(caplog.text)
        texts.append(repr(stack.adapter) + repr(stack.adapter.transport) + repr(stack.app.venue_adapter))
        blob = "\n".join(texts)
        for form in _canary_forms():
            assert form not in blob, f"{form[:12]}... leaked"
        assert "Basic " not in all_ledger_text(stack.ledger)
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_start_up_failure_names_neither_the_key_nor_the_secret(
    tmp_path, private_config_dir, monkeypatch
):
    clock = FakeClock()
    broker = FakeDemoBroker(clock)
    broker.summary_override = lambda: httpx.Response(401, text=f"bad {KEY_CANARY}")
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch, broker=broker)
    try:
        assert stack.started is False
        for form in _canary_forms():
            assert form not in stack.app.execution_startup_error
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_returned_ack_carries_only_an_allow_list_of_order_fields(
    tmp_path, private_config_dir, monkeypatch
):
    clock = FakeClock()
    broker = FakeDemoBroker(clock)

    def post(request):
        body = Fixture("order_limit_200").body
        body["note"] = f"echo {KEY_CANARY}"
        body["authorization"] = basic(KEY_CANARY, SECRET_CANARY)
        return httpx.Response(200, json=body)

    stack = await start_practice_stack(
        tmp_path, private_config_dir, monkeypatch, broker=broker, clock=clock,
        handler=scripted(broker, post),
    )
    try:
        # Call the dispatcher directly: the ledger strips ``raw``, the adapter must too.
        proposal = practice_proposal_dict("raw-1")
        intent = OrderIntent(
            proposal_id="raw-1", workspace="uk", account=PRACTICE_ACCOUNT, broker="t212_practice",
            mode="PRACTICE", ticker="VODl_EQ", side="BUY", quantity=Decimal("2"),
            order_type="LIMIT", limit_price=Decimal("50"),
        )
        ack = await stack.adapter.dispatch(intent)
        assert set(ack.raw) <= {"id", "status", "ticker", "type", "quantity", "limitPrice", "createdAt"}
        assert KEY_CANARY not in json.dumps(ack.raw) and "authorization" not in ack.raw
    finally:
        stack.close()
