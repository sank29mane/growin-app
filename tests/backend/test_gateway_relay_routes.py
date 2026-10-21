"""The 61-08 relay routes: quotes, preview-order, security-master, budget.

Real app, real Budget over a tmp_path counter file, FakeTransport upstream.
Bodies are generated from the key sets in the Breeze smoke; every value is
invented. The harness comes from test_gateway_relay_app.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from test_gateway_relay_app import (
    APP_KEY,
    BREEZE_TOKEN,
    CONTRACT_HDR,
    MASTER_BODY,
    MASTER_LAST_MODIFIED,
    MASTER_URL,
    PREVIEW_BODY,
    PREVIEW_QUERY,
    QUOTE_BODY,
    QUOTES_QUERY,
    SECRET_KEY,
    Stack,
    assert_envelope,
    auth,
    breeze_body,
    code_of,
)

from gateway_vm import contract  # noqa: E402
from gateway_vm.budget import Budget  # noqa: E402
from gateway_vm.counter_store import COUNTER_FILE, FileCounterStore  # noqa: E402
from gateway_vm.transport import HttpResponse, TransportError  # noqa: E402

MASTER = "/v1/security-master"


def real_stack(tmp_path: Path, *, with_url: bool = True, **budget_kwargs) -> Stack:
    stack = Stack(master_url=MASTER_URL if with_url else None)
    budget = Budget(
        stack.clock,
        store=FileCounterStore(tmp_path),
        **{"per_minute": 80, "per_day": 4500, "security_master_per_hour": 6, **budget_kwargs},
    )
    return rebuild(stack, budget, with_url)


def rebuild(stack: Stack, budget: Budget, with_url: bool) -> Stack:
    """The same stack with a real Budget (the harness builds a double)."""
    fresh = Stack.__new__(Stack)
    fresh.__dict__.update(stack.__dict__)
    fresh.budget = budget
    from fastapi.testclient import TestClient

    from gateway_vm import app as relay_app
    from gateway_vm.session import SessionManager

    fresh.session = SessionManager(
        clock=stack.clock,
        egress=stack.egress,
        breeze=stack.breeze,
        credentials=stack.credentials,
        expected_userid="FAKEUSER01",
        events=stack.events,
        budget=budget,
    )
    fresh.app = relay_app.create_app(
        session=fresh.session,
        breeze=stack.breeze,
        credentials=stack.credentials,
        budget=budget,
        events=stack.events,
        clock=stack.clock,
        **({"security_master_url": MASTER_URL} if with_url else {}),
    )
    fresh.client = TestClient(fresh.app, base_url="http://127.0.0.1:8443")
    return fresh


def get(stack: Stack, path: str, token: str | None = None):
    return stack.client.get(path, headers={**CONTRACT_HDR, **auth(token)})


def counter(tmp_path: Path) -> int:
    return json.loads((tmp_path / COUNTER_FILE).read_text())["count"]


@pytest.fixture()
def rs(tmp_path) -> Stack:
    return real_stack(tmp_path)


# ------------------------------------------------------------- quotes


def test_quotes_returns_the_exact_upstream_bytes_with_the_provenance_headers(rs: Stack):
    token = rs.login()
    resp = get(rs, QUOTES_QUERY, token)
    assert resp.status_code == 200
    assert resp.content == QUOTE_BODY
    assert resp.headers["content-type"] == "application/json"
    assert resp.headers[contract.HDR_UP_SHA256] == hashlib.sha256(QUOTE_BODY).hexdigest()
    assert resp.headers[contract.HDR_UP_API] == contract.API_QUOTES_V1
    assert resp.headers[contract.HDR_BREEZE_STATUS] == "200"
    assert resp.headers[contract.HDR_EMPTY] == "false"
    assert contract.HDR_UP_INTERVAL not in resp.headers
    assert json.loads(resp.headers[contract.HDR_UP_REQUEST]) == {
        "stock_code": "RELIND",
        "exchange_code": "NSE",
        "expiry_date": "",
        "product_type": "cash",
        "right": "",
        "strike_price": "",
    }
    for value in resp.headers.values():
        assert isinstance(value, str)
    assert_envelope(resp)


def test_quotes_sends_one_signed_get_whose_body_is_the_reported_request(rs: Stack):
    token = rs.login()
    resp = get(rs, QUOTES_QUERY, token)
    (method, url, headers, body), = rs.transport.quote_requests()
    assert method == "GET" and urlsplit(url).path.endswith("/quotes")
    assert body is not None and json.loads(body) == json.loads(resp.headers[contract.HDR_UP_REQUEST])
    assert headers["X-AppKey"] == APP_KEY and headers["X-SessionToken"] == BREEZE_TOKEN
    assert headers["X-Checksum"].startswith("token ") and "X-Timestamp" in headers
    assert SECRET_KEY not in json.dumps(headers) and SECRET_KEY not in body.decode()


def test_quotes_exchange_nse_is_accepted_and_charges_one_breeze_call(rs: Stack, tmp_path):
    token = rs.login()
    before = counter(tmp_path)
    assert get(rs, "/v1/quotes?stock_code=RELIND&exchange=NSE", token).status_code == 200
    assert get(rs, "/v1/quotes?stock_code=TCS", token).status_code == 200
    assert counter(tmp_path) == before + 2


# ------------------------------------------------------- preview-order


def test_preview_returns_the_exact_upstream_bytes_and_a_calculator_request(rs: Stack):
    token = rs.login()
    resp = get(rs, PREVIEW_QUERY, token)
    assert resp.status_code == 200
    assert resp.content == PREVIEW_BODY
    assert resp.headers[contract.HDR_UP_SHA256] == hashlib.sha256(PREVIEW_BODY).hexdigest()
    assert resp.headers[contract.HDR_UP_API] == contract.API_PREVIEW_V1
    sent = json.loads(resp.headers[contract.HDR_UP_REQUEST])
    assert sent == {
        "stock_code": "RELIND",
        "exchange_code": "NSE",
        "product": "cash",
        "order_type": "limit",
        "price": "1402.30",  # the exact string that was validated, not a re-rendered float
        "action": "buy",
        "quantity": "35",
        "specialflag": "N",
    }
    (method, url, headers, body), = rs.transport.preview_requests()
    assert method == "GET" and urlsplit(url).path.endswith("/preview_order")
    assert json.loads(body) == sent
    assert "X-Checksum" in headers and "X-Timestamp" in headers


def test_preview_places_no_order_and_reaches_only_the_calculator_endpoint(rs: Stack):
    token = rs.login()
    get(rs, PREVIEW_QUERY, token)
    paths = [urlsplit(r[1]).path for r in rs.transport.breeze_requests()]
    assert paths[-1].endswith("/preview_order")
    assert not [p for p in paths if p.rstrip("/").endswith("/order")]
    assert {r[0] for r in rs.transport.breeze_requests()} == {"GET"}


@pytest.mark.parametrize("price", ["100", "100.5", "100.50", "0.05", "0.01", "999999.99", "1000000", "1000000.00"])
def test_preview_accepts_plain_decimal_prices_and_sends_the_text_unchanged(rs: Stack, price):
    token = rs.login()
    resp = get(rs, f"/v1/preview-order?stock_code=RELIND&action=sell&quantity=1&price={price}", token)
    assert resp.status_code == 200
    assert json.loads(resp.headers[contract.HDR_UP_REQUEST])["price"] == price


@pytest.mark.parametrize(
    "price",
    ["1402.305", "0", "0.00", "0.0", "1000000.01", "1000001", "-1", "+1", "1e3", "1E3", "1_000", " 1", "1 ", "1.",
     ".5", "01", "00.5", "1,5", "NaN", "inf", "0x10", "", "1402.30%0A", "%EF%BC%91", "12345678"],
)
def test_preview_refuses_a_bad_price_with_the_field_name_only(rs: Stack, price):
    token = rs.login()
    before = len(rs.transport.breeze_requests())
    resp = get(rs, f"/v1/preview-order?stock_code=RELIND&action=buy&quantity=1&price={price}", token)
    assert resp.status_code == 422 and code_of(resp) == "VALIDATION"
    assert resp.json()["error"]["fields"] == ["price"]
    assert len(rs.transport.breeze_requests()) == before


@pytest.mark.parametrize("quantity", ["0", "100001", "-1", "1.5", "1e2", "01", " 5", "5 ", "", "abc", "9999999"])
def test_preview_refuses_a_bad_quantity(rs: Stack, quantity):
    token = rs.login()
    resp = get(rs, f"/v1/preview-order?stock_code=RELIND&action=buy&quantity={quantity}&price=10", token)
    assert resp.status_code == 422 and resp.json()["error"]["fields"] == ["quantity"]


@pytest.mark.parametrize("quantity", ["1", "35", "99999", "100000"])
def test_preview_accepts_quantities_up_to_the_cap(rs: Stack, quantity):
    token = rs.login()
    assert get(rs, f"/v1/preview-order?stock_code=RELIND&action=buy&quantity={quantity}&price=10", token).status_code == 200


@pytest.mark.parametrize("action", ["BUY", "Sell", "hold", "short", "", "buy ", "b", "buy%0A"])
def test_preview_refuses_a_bad_action(rs: Stack, action):
    token = rs.login()
    resp = get(rs, f"/v1/preview-order?stock_code=RELIND&action={action}&quantity=1&price=10", token)
    assert resp.status_code == 422 and resp.json()["error"]["fields"] == ["action"]


@pytest.mark.parametrize(
    "query, fields",
    [
        ("stock_code=RELIND&action=buy&quantity=1", ["price"]),
        ("stock_code=RELIND&action=buy&price=10", ["quantity"]),
        ("stock_code=RELIND&quantity=1&price=10", ["action"]),
        ("action=buy&quantity=1&price=10", ["stock_code"]),
        ("stock_code=relind&action=buy&quantity=1&price=10", ["stock_code"]),
        ("stock_code=RELIND&stock_code=TCS&action=buy&quantity=1&price=10", ["stock_code"]),
        ("stock_code=RELIND&action=buy&quantity=1&price=10&exchange=BSE", ["exchange"]),
        ("stock_code=RELIND&action=buy&quantity=1&price=10&order_type=market", ["unknown"]),
        ("stock_code=RELIND&action=buy&quantity=1&price=10&price=11", ["price"]),
        ("stock_code=RELIND&action=hold&quantity=0&price=0", ["action", "price", "quantity"]),
    ],
)
def test_preview_query_violations_name_fields_only(rs: Stack, query, fields):
    token = rs.login()
    resp = get(rs, f"/v1/preview-order?{query}", token)
    assert resp.status_code == 422
    assert resp.json()["error"]["fields"] == fields
    assert not rs.transport.preview_requests()


@pytest.mark.parametrize(
    "query, fields",
    [
        ("", ["stock_code"]),
        ("stock_code=relind", ["stock_code"]),
        ("stock_code=RELIND&exchange=BSE", ["exchange"]),
        ("stock_code=RELIND&exchange=NSE&exchange=NSE", ["exchange"]),
        ("stock_code=RELIND&interval=1day", ["unknown"]),
        ("stock_code=RELIND%0A", ["stock_code"]),
    ],
)
def test_quotes_query_violations_name_fields_only(rs: Stack, query, fields):
    token = rs.login()
    resp = get(rs, f"/v1/quotes?{query}", token)
    assert resp.status_code == 422 and resp.json()["error"]["fields"] == fields
    assert not rs.transport.quote_requests()


def test_a_bad_query_does_not_spend_the_budget(rs: Stack, tmp_path):
    token = rs.login()
    before = counter(tmp_path)
    get(rs, "/v1/quotes?stock_code=relind", token)
    get(rs, "/v1/preview-order?stock_code=RELIND", token)
    assert counter(tmp_path) == before


# --------------------------------------------------------- token matrix


@pytest.mark.parametrize("path", [QUOTES_QUERY, PREVIEW_QUERY])
def test_breeze_routes_refuse_a_missing_or_wrong_token_before_any_breeze_request(rs: Stack, path):
    token = rs.login()
    before = len(rs.transport.requests)
    for bad in (None, "x" * 43, token[:-1] + ("A" if token[-1] != "A" else "B")):
        resp = get(rs, path, bad)
        assert resp.status_code == 401 and code_of(resp) == "UNAUTHORIZED"
    assert len(rs.transport.requests) == before


@pytest.mark.parametrize("path", [QUOTES_QUERY, PREVIEW_QUERY])
def test_breeze_routes_without_a_session_are_503_no_session_and_send_nothing(rs: Stack, path):
    resp = get(rs, path, None)
    assert resp.status_code in (401, 503)
    armed = rs.arm().json()  # armed but not completed: a token exists, no Breeze session yet
    resp = get(rs, path, armed["relay_token"])
    assert resp.status_code == 503 and code_of(resp) == "NO_SESSION"
    assert not rs.transport.quote_requests() and not rs.transport.preview_requests()


def test_budget_answers_200_without_a_session_and_quotes_is_no_session(rs: Stack):
    resp = get(rs, "/v1/budget")
    assert resp.status_code == 200 and resp.json()["contract"] == contract.CONTRACT
    armed = rs.arm().json()
    assert get(rs, "/v1/budget", armed["relay_token"]).status_code == 200
    assert get(rs, QUOTES_QUERY, armed["relay_token"]).status_code == 503


def test_budget_refuses_a_wrong_bearer_while_a_session_is_bound(rs: Stack):
    token = rs.login()
    bad = get(rs, "/v1/budget", "y" * 43)
    assert bad.status_code == 401
    assert get(rs, "/v1/budget", token).status_code == 200


def test_security_master_works_with_no_session_and_no_token(rs: Stack):
    resp = get(rs, MASTER)
    assert resp.status_code == 200 and resp.content == MASTER_BODY


def test_security_master_with_a_bound_session_needs_its_bearer(rs: Stack):
    token = rs.login()
    assert get(rs, MASTER).status_code == 401
    assert get(rs, MASTER, "z" * 43).status_code == 401
    assert get(rs, MASTER, token).status_code == 200


# ------------------------------------------------------ session death


BARS = "/v1/bars/daily?stock_code=RELIND&from=2025-06-02&to=2025-06-13"


@pytest.mark.parametrize("path", [QUOTES_QUERY, PREVIEW_QUERY, BARS])
def test_a_dead_session_error_flips_the_state_on_every_data_route_alike(path, tmp_path):
    stack = real_stack(tmp_path)
    token = stack.login()
    dead = HttpResponse(200, {}, breeze_body(None, status=500, error="Invalid session."))
    stack.transport.quotes = stack.transport.preview = stack.transport.bars = dead
    resp = get(stack, path, token)
    assert resp.status_code == 503 and code_of(resp) == "SESSION_EXPIRED"
    assert resp.json()["error"]["reason"] == "invalid_session"
    assert stack.client.get("/v1/health", headers=CONTRACT_HDR).json()["state"] != "active"
    again = get(stack, QUOTES_QUERY, token)  # stays closed: nothing more goes upstream
    assert again.status_code == 503
    assert len(stack.transport.quote_requests() + stack.transport.preview_requests() + stack.transport.bars_requests()) == 1


def test_the_smoke_error_text_something_went_wrong_is_other_not_session_dead(tmp_path):
    stack = real_stack(tmp_path)
    token = stack.login()
    stack.transport.quotes = HttpResponse(200, {}, breeze_body(None, status=500, error="Something went wrong"))
    resp = get(stack, QUOTES_QUERY, token)
    assert resp.status_code == 502 and code_of(resp) == "UPSTREAM_ERROR"
    assert stack.client.get("/v1/health", headers=CONTRACT_HDR).json()["state"] == "active"


@pytest.mark.parametrize("attr", ["quotes", "preview"])
def test_an_upstream_failure_on_a_new_route_is_a_mapped_error_with_no_upstream_text(rs: Stack, attr):
    token = rs.login()
    path = QUOTES_QUERY if attr == "quotes" else PREVIEW_QUERY
    setattr(rs.transport, attr, HttpResponse(200, {}, breeze_body(None, status=500, error="CANARY-UPSTREAM-TEXT")))
    resp = get(rs, path, token)
    assert resp.status_code == 502 and resp.json()["error"]["reason"] == "other"
    assert "CANARY-UPSTREAM-TEXT" not in resp.text
    setattr(rs.transport, attr, TransportError("timeout", "api.icicidirect.com"))
    resp = get(rs, path, token)
    assert resp.status_code == 504 and code_of(resp) == "UPSTREAM_TIMEOUT"
    assert resp.json()["error"]["reason"] == "upstream_timeout"
    setattr(rs.transport, attr, TransportError("connect", "api.icicidirect.com"))
    resp = get(rs, path, token)
    assert resp.status_code == 504 and resp.json()["error"]["reason"] == "upstream_unreachable"
    setattr(rs.transport, attr, TransportError("too_large", "api.icicidirect.com"))
    resp = get(rs, path, token)
    assert resp.status_code == 502 and resp.json()["error"]["reason"] == "upstream_shape"


# ------------------------------------------------------ security master


def test_security_master_success_returns_the_zip_bytes_and_provenance(rs: Stack):
    resp = get(rs, MASTER)
    assert resp.status_code == 200
    assert resp.content == MASTER_BODY
    assert resp.headers["content-type"] == "application/zip"
    assert resp.headers[contract.HDR_UP_SHA256] == hashlib.sha256(MASTER_BODY).hexdigest()
    assert resp.headers[contract.HDR_UP_API] == contract.API_SECURITY_MASTER
    assert json.loads(resp.headers[contract.HDR_UP_REQUEST]) == {"url": MASTER_URL}
    assert resp.headers[contract.HDR_UP_LAST_MODIFIED] == MASTER_LAST_MODIFIED
    assert resp.headers[contract.HDR_UP_FETCHED_AT].endswith("Z")
    assert contract.HDR_BREEZE_STATUS not in resp.headers
    (method, url, headers, body), = rs.transport.master_requests()
    assert method == "GET" and url == MASTER_URL and body is None
    assert "apikey" not in {k.lower() for k in headers} and "X-SessionToken" not in headers
    assert_envelope(resp)


def test_security_master_is_not_a_breeze_call_and_is_not_counted(rs: Stack, tmp_path):
    token = rs.login()
    before = counter(tmp_path)
    for _ in range(3):
        assert get(rs, MASTER, token).status_code == 200
    assert counter(tmp_path) == before
    assert rs.budget.snapshot()["calls_today_ist"] == before


def test_security_master_last_modified_is_copied_only_when_it_is_an_http_date(rs: Stack):
    for value in ("Tue, 20 Oct 2026 09:30:00 GMT", ):
        rs.transport.master = HttpResponse(200, {"last-modified": value}, MASTER_BODY)
        assert get(rs, MASTER).headers[contract.HDR_UP_LAST_MODIFIED] == value
    for bad in ("yesterday", "Tue, 20 Oct 2026 09:30:00 GMT\r\nX: y", "2026-10-20", "x" * 200, ""):
        rs.transport.master = HttpResponse(200, {"last-modified": bad}, MASTER_BODY)
        resp = get(rs, MASTER)
        assert resp.status_code == 200 and contract.HDR_UP_LAST_MODIFIED not in resp.headers
    rs.transport.master = HttpResponse(200, {}, MASTER_BODY)
    assert contract.HDR_UP_LAST_MODIFIED not in get(rs, MASTER).headers


def test_security_master_refuses_a_stale_or_wrong_egress_and_sends_no_directlink_request(rs: Stack):
    rs.transport.echo_ip = "203.0.113.99"
    rs.clock.advance(1000)  # the cached check is old: a fresh one runs and fails
    resp = get(rs, MASTER)
    assert resp.status_code == 503 and code_of(resp) == "EGRESS_MISMATCH"
    assert rs.transport.master_requests() == []


def test_security_master_egress_unreachable_is_refused_without_a_directlink_request(rs: Stack):
    assert get(rs, MASTER).status_code == 200
    rs.transport.master_requests().clear()
    rs.transport.echo_ip = TransportError("timeout", "api.ipify.org")
    rs.clock.advance(1000)
    n = len(rs.transport.master_requests())
    resp = get(rs, MASTER)
    assert resp.status_code == 503
    assert len(rs.transport.master_requests()) == n


def test_security_master_digest_is_rechecked_after_an_egress_refresh(rs: Stack):
    """A bearer replaced while the egress check ran must not read the file."""
    token = rs.login()
    real = rs.session.egress_fresh

    def refresh_then_relogin():
        rs.session.egress_fresh = real  # type: ignore[method-assign]
        fresh = real()
        armed = rs.arm(token).json()  # a new login binds another bearer mid-request
        assert rs.complete(armed["attempt_id"], armed["relay_token"]).status_code == 200
        return fresh

    rs.session.egress_fresh = refresh_then_relogin  # type: ignore[method-assign]
    resp = get(rs, MASTER, token)
    assert resp.status_code == 401 and code_of(resp) == "UNAUTHORIZED"
    assert rs.transport.master_requests() == []


def test_security_master_seventh_call_in_an_hour_is_429_with_retry_after(rs: Stack):
    for _ in range(6):
        assert get(rs, MASTER).status_code == 200
        rs.clock.advance(30)
    resp = get(rs, MASTER)
    assert resp.status_code == 429 and code_of(resp) == "BUDGET_EXHAUSTED"
    assert resp.json()["error"]["reason"] == "security_master_hourly"
    assert 1 <= resp.json()["error"]["retry_after_s"] <= 3600
    assert len(rs.transport.master_requests()) == 6
    rs.clock.advance(3600)
    assert get(rs, MASTER).status_code == 200


@pytest.mark.parametrize(
    "body",
    [b"", b"PK", b"PK\x03", b"<html>nope</html>", b"pk\x03\x04abc", b"\x00PK\x03\x04", b"PK\x05\x06" + b"\x00" * 18],
)
def test_security_master_refuses_anything_that_is_not_a_zip_local_header(rs: Stack, body):
    rs.transport.master = HttpResponse(200, {}, body)
    resp = get(rs, MASTER)
    assert resp.status_code == 502 and code_of(resp) == "UPSTREAM_ERROR"
    assert resp.json()["error"]["reason"] == "upstream_shape"


def test_security_master_non_200_is_upstream_http(rs: Stack):
    rs.transport.master = HttpResponse(404, {}, b"PK\x03\x04 not found page")
    resp = get(rs, MASTER)
    assert resp.status_code == 502 and resp.json()["error"]["reason"] == "upstream_http"


def test_security_master_over_the_size_cap_is_upstream_shape_and_the_cap_is_64_mib(rs: Stack):
    assert contract.SECURITY_MASTER_MAX_BYTES == 64 * 1024 * 1024
    rs.transport.master = TransportError("too_large", "directlink.icicidirect.com")
    resp = get(rs, MASTER)
    assert resp.status_code == 502 and resp.json()["error"]["reason"] == "upstream_shape"


def test_security_master_timeout_and_connect_failures_are_mapped(rs: Stack):
    rs.transport.master = TransportError("timeout", "directlink.icicidirect.com")
    assert get(rs, MASTER).status_code == 504
    rs.clock.advance(3700)
    rs.transport.master = TransportError("connect", "directlink.icicidirect.com")
    resp = get(rs, MASTER)
    assert resp.status_code == 504 and resp.json()["error"]["reason"] == "upstream_unreachable"


def test_security_master_sends_the_caps_and_deadlines_the_contract_names(tmp_path):
    seen: dict = {}
    stack = real_stack(tmp_path)
    original = stack.transport.request

    def spy(method, url, *, headers, body, timeouts, max_bytes):
        if "directlink" in url:
            seen["timeouts"], seen["max_bytes"] = timeouts, max_bytes
        return original(method, url, headers=headers, body=body, timeouts=timeouts, max_bytes=max_bytes)

    stack.transport.request = spy  # type: ignore[method-assign]
    assert get(stack, MASTER).status_code == 200
    assert seen["max_bytes"] == contract.SECURITY_MASTER_MAX_BYTES
    assert seen["timeouts"].total_s == contract.SECURITY_MASTER_TOTAL_S == 120.0
    assert seen["timeouts"].connect_s == 5.0 and seen["timeouts"].read_s == 30.0


def test_security_master_without_a_configured_url_is_500_config_invalid_and_one_alarm(tmp_path):
    stack = real_stack(tmp_path, with_url=False)
    resp = get(stack, MASTER)
    assert resp.status_code == 500 and code_of(resp) == "INTERNAL"
    assert resp.json()["error"]["reason"] == "config_invalid"
    alarms = stack.events_named("config_alarm")
    assert len(alarms) == 1 and alarms[0]["reason"] == "config_invalid"
    assert stack.transport.master_requests() == []
    assert stack.transport.requests == []  # no egress check either


# ---------------------------------------------------------- budget route


def test_budget_route_reports_the_counters(rs: Stack, tmp_path):
    token = rs.login()
    get(rs, QUOTES_QUERY, token)
    resp = get(rs, "/v1/budget", token)
    assert resp.status_code == 200
    body = resp.json()
    assert body["contract"] == contract.CONTRACT
    assert body["per_minute_limit"] == 80 and body["per_day_limit"] == 4500
    assert body["calls_today_ist"] == counter(tmp_path)
    assert body["remaining_today"] == 4500 - body["calls_today_ist"]
    assert body["calls_last_minute"] == body["calls_today_ist"]
    assert body["ist_date"] == "2026-10-21"
    for key in ("calls_last_minute", "calls_today_ist", "per_minute_limit", "per_day_limit", "remaining_today"):
        assert type(body[key]) is int
    assert_envelope(resp)


def test_budget_route_takes_no_query_and_does_not_charge(rs: Stack, tmp_path):
    token = rs.login()
    before = counter(tmp_path)
    assert get(rs, "/v1/budget?x=1", token).status_code == 422
    assert get(rs, "/v1/budget", token).status_code == 200
    assert counter(tmp_path) == before


def test_the_81st_breeze_call_in_a_minute_is_429_budget_minute(rs: Stack, tmp_path):
    token = rs.login()
    used = counter(tmp_path)  # the login's customerdetails call counted
    for _ in range(80 - used):
        assert get(rs, QUOTES_QUERY, token).status_code == 200
    resp = get(rs, QUOTES_QUERY, token)
    assert resp.status_code == 429 and code_of(resp) == "BUDGET_EXHAUSTED"
    assert resp.json()["error"]["reason"] == "budget_minute"
    assert 1 <= resp.json()["error"]["retry_after_s"] <= 60
    assert len(rs.transport.quote_requests()) == 80 - used
    assert counter(tmp_path) == 80
    rs.clock.advance(61)
    assert get(rs, QUOTES_QUERY, token).status_code == 200


def test_the_day_ceiling_survives_a_restart_of_the_whole_stack(tmp_path):
    first = real_stack(tmp_path, per_day=3)
    token = first.login()  # call 1
    assert get(first, QUOTES_QUERY, token).status_code == 200  # 2
    assert get(first, QUOTES_QUERY, token).status_code == 200  # 3
    resp = get(first, QUOTES_QUERY, token)
    assert resp.status_code == 429 and resp.json()["error"]["reason"] == "budget_day"
    second = real_stack(tmp_path, per_day=3)  # a restart: a new process, the same state directory
    armed = second.arm()
    assert armed.status_code == 200
    done = second.complete(armed.json()["attempt_id"], armed.json()["relay_token"])
    assert done.status_code == 429 and done.json()["error"]["reason"] == "budget_day"
    assert second.transport.breeze_requests() == []


def test_an_unwritable_counter_gives_503_on_breeze_routes_but_health_and_budget_answer(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    stack = real_stack(state)
    token = stack.login()
    state.chmod(0o500)
    try:
        for path in (QUOTES_QUERY, PREVIEW_QUERY, "/v1/bars/daily?stock_code=RELIND&from=2025-06-02&to=2025-06-13"):
            resp = get(stack, path, token)
            assert resp.status_code == 503 and code_of(resp) == "BUDGET_UNAVAILABLE"
        assert stack.client.get("/v1/health", headers=CONTRACT_HDR).status_code == 200
        budget = get(stack, "/v1/budget", token)
        assert budget.status_code == 200 and budget.json()["remaining_today"] == 0
        assert get(stack, MASTER, token).status_code == 200  # not a Breeze call
    finally:
        state.chmod(0o700)
    assert get(stack, QUOTES_QUERY, token).status_code == 200  # recovers once the file is writable


# ------------------------------------------------------------ route set


def test_every_new_route_has_a_query_allowlist_and_the_right_names():
    allowed = contract.ALLOWED_QUERY_BY_ROUTE
    assert set(allowed) == set(contract.ROUTES)
    assert allowed[contract.ROUTE_BUDGET] == () and allowed[contract.ROUTE_SECURITY_MASTER] == ()
    assert set(allowed[contract.ROUTE_QUOTES]) == {"stock_code", "exchange"}
    assert set(allowed[contract.ROUTE_PREVIEW_ORDER]) == {"stock_code", "action", "quantity", "price", "exchange"}


@pytest.mark.parametrize("path", ["/v1/budget", MASTER])
def test_no_parameter_routes_refuse_any_query_with_422_unknown(rs: Stack, path):
    resp = get(rs, f"{path}?foo=bar", rs.login())
    assert resp.status_code == 422 and resp.json()["error"]["fields"] == ["unknown"]
    assert rs.transport.master_requests() == []


def test_new_routes_carry_the_envelope_and_no_cors_or_cookie(rs: Stack):
    token = rs.login()
    for path in (QUOTES_QUERY, PREVIEW_QUERY, "/v1/budget", MASTER):
        resp = get(rs, path, token)
        assert_envelope(resp)
        assert resp.headers["cache-control"] == "no-store"
        assert "set-cookie" not in resp.headers and "access-control-allow-origin" not in resp.headers


def test_new_routes_refuse_a_missing_contract_header(rs: Stack):
    for path in (QUOTES_QUERY, PREVIEW_QUERY, "/v1/budget", MASTER):
        resp = rs.client.get(path)
        assert resp.status_code == 403 and code_of(resp) == "FORBIDDEN_CLIENT"
    assert rs.transport.requests == []


def test_nothing_secret_leaks_from_the_new_routes(rs: Stack):
    token = rs.login()
    for path in (QUOTES_QUERY, PREVIEW_QUERY, "/v1/budget", MASTER):
        get(rs, path, token)
    get(rs, "/v1/quotes?stock_code=CANARYBAD", token)
    blob = rs.sink.getvalue()
    for secret in (APP_KEY, SECRET_KEY, BREEZE_TOKEN, token):
        assert secret not in blob
