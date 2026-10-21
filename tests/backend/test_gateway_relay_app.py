"""Relay core through the real FastAPI app (growin-relay/1, Phase 61-05).

Only the network is faked: the real BreezeClient, EgressChecker, SessionManager,
EventLog and middleware run over a FakeTransport. No network, generated fake
credentials only, no order of any kind.

The harness at the top is reused by test_gateway_session.py and
test_gateway_relay_config.py.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import io
import json
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, quote_plus, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "gateway" / "vm") not in sys.path:
    sys.path.insert(0, str(ROOT / "gateway" / "vm"))

from fastapi.testclient import TestClient  # noqa: E402
from gateway_vm import app as relay_app  # noqa: E402
from gateway_vm import contract, events as events_mod  # noqa: E402
from gateway_vm.breeze_client import BreezeClient  # noqa: E402
from gateway_vm.egress import EgressChecker  # noqa: E402
from gateway_vm.events import EventLog  # noqa: E402
from gateway_vm.session import (  # noqa: E402
    BudgetExceeded,
    BudgetUnavailable,
    Credentials,
    SessionManager,
)
from gateway_vm.transport import HttpResponse, TransportError  # noqa: E402

PKG = ROOT / "gateway" / "vm" / "gateway_vm"

EXPECTED_IP = "203.0.113.7"
OTHER_IP = "203.0.113.99"
USERID = "FAKEUSER01"
APP_KEY = "CANARY-APPKEY-0f3a9c11"
SECRET_KEY = "CANARY-SECRET-77d1b2e0"
CODE = "48291037"  # the captured apisession
BREEZE_TOKEN = base64.b64encode(f"{USERID}:55501234".encode()).decode()
BASE = "http://127.0.0.1:8443"
CONTRACT_HDR = {contract.HDR_CONTRACT: contract.CONTRACT}
T0 = datetime(2026, 10, 21, 4, 30, tzinfo=timezone.utc)  # 10:00 IST

BARS_BODY = json.dumps(
    {
        "Success": [
            {
                "datetime": "2025-06-02 00:00:00",
                "stock_code": "RELIND",
                "exchange_code": "NSE",
                "open": 1.5,
                "high": 2.5,
                "low": 1.25,
                "close": 2.0,
                "volume": 10,
            }
        ],
        "Status": 200,
        "Error": None,
    },
    separators=(",", ":"),
).encode()
BARS_QUERY = "/v1/bars/daily?stock_code=RELIND&from=2025-06-02&to=2025-06-13"


def breeze_body(success, status=200, error=None) -> bytes:
    return json.dumps({"Success": success, "Status": status, "Error": error}).encode()


def customer_body(userid=USERID, equity="Y", trading="Y") -> bytes:
    return breeze_body(
        {
            "idirect_userid": userid,
            "session_token": BREEZE_TOKEN,
            "segments_allowed": {"Trading": trading, "Equity": equity},
            "exg_trade_date": {"NSE": "21-Oct-2026"},
        }
    )


# ---------------------------------------------------------------- fakes


class FakeClock:
    def __init__(self) -> None:
        self.mono = 1000.0
        self.now = T0

    def monotonic(self) -> float:
        return self.mono

    def now_utc(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.mono += seconds
        self.now += timedelta(seconds=seconds)


class FakeTransport:
    """Serves metadata, echo, customerdetails and v2 bars. Records every request."""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str, dict, bytes | None]] = []
        self.lock = threading.Lock()
        self.metadata_ip: str | Exception = EXPECTED_IP
        self.echo_ip: str | Exception = EXPECTED_IP
        self.customer: HttpResponse | Exception = HttpResponse(200, {}, customer_body())
        self.bars: HttpResponse | Exception = HttpResponse(200, {}, BARS_BODY)

    def request(self, method, url, *, headers, body, timeouts, max_bytes):
        with self.lock:
            self.requests.append((method, url, dict(headers), body))
        host = urlsplit(url).hostname
        if host == "metadata.google.internal":
            return self._ip(self.metadata_ip)
        if host == "api.ipify.org":
            return self._ip(self.echo_ip)
        if host == "api.icicidirect.com" and url.endswith("/customerdetails"):
            return self._reply(self.customer)
        if host == "breezeapi.icicidirect.com":
            return self._reply(self.bars)
        raise AssertionError(f"unexpected request to {host}")

    @staticmethod
    def _ip(value):
        if isinstance(value, Exception):
            raise value
        return HttpResponse(200, {}, value.encode())

    @staticmethod
    def _reply(value):
        if isinstance(value, Exception):
            raise value
        return value

    def breeze_requests(self) -> list:
        return [r for r in self.requests if urlsplit(r[1]).hostname not in (
            "metadata.google.internal", "api.ipify.org")]

    def bars_requests(self) -> list:
        return [r for r in self.requests if urlsplit(r[1]).hostname == "breezeapi.icicidirect.com"]


class BudgetDouble:
    def __init__(self) -> None:
        self.kinds: list[str] = []
        self.raises: Exception | None = None
        self.lock = threading.Lock()

    def charge(self, kind: str) -> None:
        if self.raises is not None:
            raise self.raises
        with self.lock:
            self.kinds.append(kind)

    def snapshot(self) -> dict:
        return {}


class Stack:
    def __init__(self, **session_kwargs) -> None:
        self.clock = FakeClock()
        self.transport = FakeTransport()
        self.sink = io.StringIO()
        self.events = EventLog(self.sink, self.clock)
        self.budget = BudgetDouble()
        self.credentials = Credentials(app_key=APP_KEY, secret_key=SECRET_KEY)
        self.breeze = BreezeClient(self.transport, clock=self.clock)
        self.egress = EgressChecker(
            self.transport,
            expected_ip=EXPECTED_IP,
            echo_url="https://api.ipify.org",
            clock=self.clock,
        )
        self.session = SessionManager(
            clock=self.clock,
            egress=self.egress,
            breeze=self.breeze,
            credentials=self.credentials,
            expected_userid=USERID,
            events=self.events,
            budget=self.budget,
            **session_kwargs,
        )
        self.app = relay_app.create_app(
            session=self.session,
            breeze=self.breeze,
            credentials=self.credentials,
            budget=self.budget,
            events=self.events,
            clock=self.clock,
        )
        self.client = TestClient(self.app, base_url=BASE)

    def new_client(self) -> TestClient:
        return TestClient(self.app, base_url=BASE)

    def lines(self) -> list[dict]:
        return [json.loads(line) for line in self.sink.getvalue().splitlines()]

    def events_named(self, name: str) -> list[dict]:
        return [e for e in self.lines() if e["event"] == name]

    def arm(self, token: str | None = None, client: TestClient | None = None):
        return (client or self.client).post(
            "/v1/login/arm", json={}, headers={**CONTRACT_HDR, **auth(token)}
        )

    def complete(self, attempt_id: str, token: str | None, code: str = CODE, client=None):
        return (client or self.client).post(
            "/v1/login/complete",
            json={"attempt_id": attempt_id, "apisession": code},
            headers={**CONTRACT_HDR, **auth(token)},
        )

    def login(self) -> str:
        armed = self.arm()
        assert armed.status_code == 200, armed.text
        body = armed.json()
        done = self.complete(body["attempt_id"], body["relay_token"])
        assert done.status_code == 200, done.text
        return body["relay_token"]

    def bars(self, token: str | None, query: str = BARS_QUERY, client=None):
        return (client or self.client).get(query, headers={**CONTRACT_HDR, **auth(token)})

    def revoke(self, token: str | None):
        return self.client.post(
            "/v1/session/revoke", json={}, headers={**CONTRACT_HDR, **auth(token)}
        )


def auth(token: str | None) -> dict:
    return {"Authorization": f"Bearer {token}"} if token is not None else {}


@pytest.fixture()
def stack() -> Stack:
    return Stack()


def secret_forms() -> list[str]:
    forms: list[str] = []
    for value in (APP_KEY, SECRET_KEY, CODE, BREEZE_TOKEN, USERID):
        forms += [value, quote_plus(value), base64.b64encode(value.encode()).decode()]
    return forms


def assert_envelope(resp) -> None:
    assert resp.headers[contract.HDR_CONTRACT] == contract.CONTRACT
    assert contract.REQUEST_ID_RE.fullmatch(resp.headers[contract.HDR_REQUEST_ID])
    assert resp.headers["cache-control"] == "no-store"


# ------------------------------------------------------------ tracer


def test_tracer_arm_complete_bars_revoke(stack: Stack):
    armed = stack.arm()
    assert_envelope(armed)
    assert armed.status_code == 200
    body = armed.json()
    assert body["login_url"].startswith("https://api.icicidirect.com/apiuser/login?api_key=")
    assert body["login_url"].endswith(quote_plus(APP_KEY))
    assert len(body["relay_token"]) == 43
    assert body["expires_at_utc"] == "2026-10-21T04:32:00.000Z"  # 120 s ahead
    assert stack.transport.requests == []  # arm talks to nobody

    token = body["relay_token"]
    # complete without the token, or with another one: 401, attempt still usable
    for bad in (None, "A" * 43):
        refused = stack.complete(body["attempt_id"], bad)
        assert refused.status_code == 401
        assert refused.json()["error"]["code"] == "UNAUTHORIZED"
    assert stack.transport.requests == []
    done = stack.complete(body["attempt_id"], token)
    assert done.status_code == 200
    assert done.json()["state"] == "active"
    assert done.json()["expires_at_ist"] == "2026-10-22T00:00:00+05:30"

    n = len(stack.transport.requests)
    unauth = stack.bars(None)
    assert unauth.status_code == 401
    assert unauth.json()["error"]["code"] == "UNAUTHORIZED"
    assert len(stack.transport.requests) == n  # no Breeze request

    ok = stack.bars(token)
    assert ok.status_code == 200
    assert ok.content == BARS_BODY
    assert ok.headers[contract.HDR_UP_SHA256] == hashlib.sha256(BARS_BODY).hexdigest()
    assert ok.headers[contract.HDR_UP_FETCHED_AT] == "2026-10-21T04:30:00.000Z"
    assert ok.headers[contract.HDR_UP_API] == "breeze-historicalcharts-v2"
    assert ok.headers[contract.HDR_UP_INTERVAL] == "1day"
    assert ok.headers[contract.HDR_BREEZE_STATUS] == "200"
    assert ok.headers[contract.HDR_EMPTY] == "false"
    assert ok.headers["content-type"] == "application/json"
    assert_envelope(ok)

    sent = stack.transport.bars_requests()[-1]
    query = dict(parse_qsl(urlsplit(sent[1]).query))
    upstream = ok.headers[contract.HDR_UP_REQUEST]
    assert upstream == json.dumps(
        {
            "exch_code": "NSE",
            "from_date": query["from_date"],
            "interval": "1day",
            "product_type": "cash",
            "stock_code": "RELIND",
            "to_date": query["to_date"],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    assert query["from_date"] == "2025-06-02T00:00:00.000Z"
    assert query["to_date"] == "2025-06-13T23:59:59.000Z"
    assert APP_KEY not in upstream and BREEZE_TOKEN not in upstream

    revoked = stack.revoke(token)
    assert revoked.status_code == 200
    assert revoked.json() == {"contract": "growin-relay/1", "state": "none", "expires_at_ist": None}

    n = len(stack.transport.requests)
    after = stack.bars(token)  # even with the old bearer
    assert after.status_code == 503
    assert after.json()["error"]["code"] == "NO_SESSION"
    assert_envelope(after)
    assert len(stack.transport.requests) == n


def test_every_response_carries_the_envelope_even_errors(stack: Stack):
    token = stack.login()
    responses = [
        stack.client.get("/v1/health", headers=CONTRACT_HDR),
        stack.bars(token),
        stack.bars(None),
        stack.client.get("/v1/nope", headers=CONTRACT_HDR),  # 404
        stack.client.post("/v1/health", json={}, headers=CONTRACT_HDR),  # 405
        stack.client.get("/v1/bars/daily?stock_code=bad", headers={**CONTRACT_HDR, **auth(token)}),
        stack.client.get("/v1/health"),  # guard refusal
        stack.client.get("/v1/health", headers={contract.HDR_CONTRACT: "growin-relay/2"}),
    ]
    assert [r.status_code for r in responses] == [200, 200, 401, 404, 405, 422, 403, 400]
    for r in responses:
        assert_envelope(r)
    ids = {r.headers[contract.HDR_REQUEST_ID] for r in responses}
    assert len(ids) == len(responses)


def test_guard_rejects_before_any_route_runs(stack: Stack):
    no_header = stack.client.get("/v1/health")
    assert no_header.status_code == 403
    assert no_header.json()["error"]["code"] == "FORBIDDEN_CLIENT"
    wrong = stack.client.get("/v1/health", headers={contract.HDR_CONTRACT: "growin-relay/2"})
    assert wrong.status_code == 400
    assert wrong.json()["error"]["code"] == "CONTRACT_MISMATCH"
    evil = TestClient(stack.app, base_url="http://evil.example")
    refused = evil.get("/v1/health", headers=CONTRACT_HDR)
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "FORBIDDEN_CLIENT"
    # a login attempt through a bad host never reaches the session
    refused = evil.post("/v1/login/arm", json={}, headers=CONTRACT_HDR)
    assert refused.status_code == 403
    assert stack.events_named("login_armed") == []
    assert stack.transport.requests == []


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.1:9000", "localhost", "localhost:8443", "[::1]", "[::1]:8443"])
def test_loopback_hosts_are_accepted(stack: Stack, host):
    resp = stack.client.get("/v1/health", headers={**CONTRACT_HDR, "Host": host})
    assert resp.status_code == 200


@pytest.mark.parametrize(
    "host", ["127.0.0.1.evil.example", "evil.example:8443", "10.0.0.2", "0.0.0.0", "localhost.evil.example"]
)
def test_non_loopback_hosts_are_refused(stack: Stack, host):
    resp = stack.client.get("/v1/health", headers={**CONTRACT_HDR, "Host": host})
    assert resp.status_code == 403


def test_health_has_no_token_and_no_account_data(stack: Stack):
    stack.login()
    resp = stack.client.get("/v1/health", headers=CONTRACT_HDR)  # no bearer, session bound
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "contract", "state", "login_pending", "expires_at_ist", "egress",
        "server_time_utc", "version",
    }
    assert body["state"] == "active"
    assert body["egress"]["ok"] is True
    text = resp.text
    for secret in secret_forms() + [EXPECTED_IP]:
        assert secret not in text


def test_event_log_holds_the_audit_trail_with_allowlisted_keys(stack: Stack):
    token = stack.login()
    stack.bars(None)
    stack.bars(token)
    stack.revoke(token)
    stack.bars(token)
    lines = stack.lines()
    for line in lines:
        assert set(line) <= events_mod.ALLOWED_KEYS
    seen = [
        (e["event"], e.get("route"), e.get("result"), e.get("reason"), e.get("http"))
        for e in lines
        if e["event"] != "egress_check"
    ]
    assert ("login_armed", None, "ok", None, None) in seen
    assert ("login_completed", None, "ok", None, None) in seen
    order = [
        ("request", "/v1/bars/daily", "rejected", "unauthorized", 401),
        ("request", "/v1/bars/daily", "ok", None, 200),
        ("session_revoked", None, "ok", None, None),
        ("request", "/v1/session/revoke", "ok", None, 200),
        ("request", "/v1/bars/daily", "rejected", None, 503),
    ]
    cursor = 0
    for item in seen:
        if cursor < len(order) and item == order[cursor]:
            cursor += 1
    assert cursor == len(order), seen
    for line in lines:
        if line["event"] == "request":
            assert contract.REQUEST_ID_RE.fullmatch(line["request_id"])
            assert isinstance(line["ms"], int) and line["ms"] >= 0


def test_no_leak_of_secrets_in_bodies_events_or_401s(stack: Stack):
    armed = stack.arm()
    body = armed.json()
    token = body["relay_token"]
    visible = [
        stack.complete(body["attempt_id"], None),
        stack.complete(body["attempt_id"], token),
        stack.bars(None),
        stack.bars(token),
        stack.bars(token, "/v1/bars/daily?stock_code=bad&" + CODE + "=1"),
        stack.client.post(
            "/v1/login/complete",
            json={"attempt_id": "x", "apisession": APP_KEY + SECRET_KEY},
            headers={**CONTRACT_HDR, **auth(token)},
        ),
        stack.revoke(None),
        stack.revoke(token),
        stack.bars(token),
        stack.client.get("/v1/health", headers=CONTRACT_HDR),
    ]
    blob = "\n".join(
        r.text + "".join(f"{k}:{v}" for k, v in r.headers.items()) for r in visible
    )
    blob += stack.sink.getvalue()
    for secret in secret_forms():
        assert secret not in blob, "a credential leaked into a response or the event log"
    # the one allowed place for the raw relay token is the arm response
    assert token in armed.text
    assert token not in blob
    assert APP_KEY in body["login_url"] or quote_plus(APP_KEY) in body["login_url"]


def test_bars_request_shape_is_the_shipped_one(stack: Stack):
    """No smoke-driven request knob applies (s2_variant_passed was null)."""
    token = stack.login()
    stack.bars(token)
    method, url, headers, body = stack.transport.bars_requests()[-1]
    assert method == "GET" and body is None
    assert "user-agent" not in {k.lower() for k in headers}
    assert headers["apikey"] == APP_KEY and headers["X-SessionToken"] == BREEZE_TOKEN
    assert "%3A" not in url  # colons stay literal in the dates
    assert dict(parse_qsl(urlsplit(url).query))["exch_code"] == "NSE"
    assert contract.BARS_REQUEST_KNOBS == {}


# ------------------------------------------------------- validation (C3)


def code_of(resp) -> str:
    return resp.json()["error"]["code"]


@pytest.mark.parametrize(
    "query, fields",
    [
        ("stock_code=relind&from=2025-06-02&to=2025-06-13", ["stock_code"]),
        ("stock_code=RELIANCEINDUSTRIES&from=2025-06-02&to=2025-06-13", ["stock_code"]),
        ("from=2025-06-02&to=2025-06-13", ["stock_code"]),
        ("stock_code=RELIND&to=2025-06-13", ["from"]),
        ("stock_code=RELIND&from=2025-06-02", ["to"]),
        ("stock_code=RELIND&from=2025-6-2&to=2025-06-13", ["from"]),
        ("stock_code=RELIND&from=2025-02-30&to=2025-06-13", ["from"]),
        ("stock_code=RELIND&from=2025-06-13&to=2025-06-02", ["from", "to"]),
        ("stock_code=RELIND&from=2015-01-01&to=2025-06-13", ["from", "to"]),
        ("stock_code=RELIND&from=2025-06-02&to=2025-06-13&exchange=BSE", ["exchange"]),
        ("stock_code=RELIND&stock_code=TCS&from=2025-06-02&to=2025-06-13", ["stock_code"]),
        ("stock_code=RELIND&from=2025-06-02&to=2025-06-13&interval=1minute", ["unknown"]),
        ("stock_code=RELIND&from=2025-06-02&to=2025-06-13&product_type=margin", ["unknown"]),
    ],
)
def test_bars_query_violations_are_422_with_names_only(stack: Stack, query, fields):
    token = stack.login()
    n = len(stack.transport.requests)
    resp = stack.bars(token, "/v1/bars/daily?" + query)
    assert resp.status_code == 422 and code_of(resp) == "VALIDATION"
    assert resp.json()["error"]["fields"] == fields
    assert len(stack.transport.requests) == n
    for value in ("relind", "RELIANCEINDUSTRIES", "BSE", "1minute", "margin", "2025-02-30"):
        assert value not in resp.text


def test_exchange_nse_and_a_3000_day_span_are_accepted(stack: Stack):
    token = stack.login()
    ok = stack.bars(token, "/v1/bars/daily?stock_code=RELIND&from=2017-01-01&to=2025-03-20&exchange=NSE")
    assert ok.status_code == 200  # exactly 3000 days
    over = stack.bars(token, "/v1/bars/daily?stock_code=RELIND&from=2017-01-01&to=2025-03-21")
    assert over.status_code == 422


def test_validation_runs_after_authorize_not_before(stack: Stack):
    stack.login()
    resp = stack.bars(None, "/v1/bars/daily?stock_code=bad")
    assert resp.status_code == 401  # no hint about parameters to an unauthenticated caller


def test_bars_pad_days_are_the_smoke_derived_zero(stack: Stack):
    assert (contract.BARS_FROM_PAD_DAYS, contract.BARS_TO_PAD_DAYS) == (0, 0)
    assert contract.API_SESSION_RE.pattern == "^[A-Za-z0-9]{6,64}$"
    assert contract.API_SESSION_RE.fullmatch("12345678")
    assert not contract.API_SESSION_RE.fullmatch("12345")
    assert not contract.API_SESSION_RE.fullmatch("a" * 65)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"content": b"{}", "headers": {"Content-Type": "text/plain"}},
        {"content": b"{}", "headers": {}},  # no content type at all
        {"content": b"{not json", "headers": {"Content-Type": "application/json"}},
        {"json": {"extra": 1}},
        {"json": []},
    ],
)
def test_post_bodies_must_be_json_objects_without_unknown_keys(stack: Stack, kwargs):
    headers = {**CONTRACT_HDR, **kwargs.pop("headers", {})}
    for path in ("/v1/login/arm", "/v1/session/revoke"):
        resp = stack.client.post(path, headers=headers, **dict(kwargs))
        assert resp.status_code == 422 and code_of(resp) == "VALIDATION"
    assert stack.events_named("login_armed") == []


def test_complete_body_no_leak_of_unknown_keys_and_rejects_them(stack: Stack):
    armed = stack.arm().json()
    resp = stack.client.post(
        "/v1/login/complete",
        json={"attempt_id": armed["attempt_id"], "apisession": CODE, "SECRETKEYNAME-xyz": 1},
        headers={**CONTRACT_HDR, **auth(armed["relay_token"])},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["fields"] == ["body"]
    assert "SECRETKEYNAME" not in resp.text
    assert stack.transport.requests == []


def test_no_redirects_cors_or_cookies(stack: Stack):
    resp = stack.client.get("/v1/health/", headers=CONTRACT_HDR, follow_redirects=False)
    assert resp.status_code == 404
    assert "location" not in resp.headers
    options = stack.client.options("/v1/health", headers={**CONTRACT_HDR, "Origin": "http://evil.example"})
    assert options.status_code == 405
    for r in (resp, options, stack.client.get("/v1/health", headers={**CONTRACT_HDR, "Origin": "http://evil.example"})):
        assert not [h for h in r.headers if h.lower().startswith("access-control")]
        assert "set-cookie" not in r.headers
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert stack.client.get(path, headers=CONTRACT_HDR).status_code == 404


def test_an_escaped_exception_is_500_internal_with_no_detail(stack: Stack):
    token = stack.login()

    def boom(**_kwargs):
        raise RuntimeError("secret detail " + APP_KEY)

    stack.breeze.daily_bars_v2_raw = boom  # type: ignore[method-assign]
    resp = stack.bars(token)
    assert resp.status_code == 500 and code_of(resp) == "INTERNAL"
    assert "secret detail" not in resp.text and APP_KEY not in resp.text
    assert_envelope(resp)
    last = [e for e in stack.lines() if e["event"] == "request"][-1]
    assert (last["result"], last["reason"], last["http"]) == ("error", "internal", 500)
    assert APP_KEY not in stack.sink.getvalue()


# ---------------------------------------------------------------- route set


FORBIDDEN_WORDS = ("order", "fund", "holding", "position", "trade", "margin", "cancel", "modify")


def test_route_set_is_exactly_this_plans_five_and_nothing_order_like(stack: Stack):
    paths = {route.path for route in stack.app.routes}
    assert paths == {
        "/v1/health",
        "/v1/login/arm",
        "/v1/login/complete",
        "/v1/session/revoke",
        "/v1/bars/daily",
    }
    assert paths == set(contract.ROUTES)
    for path in paths:
        assert not any(word in path.lower() for word in FORBIDDEN_WORDS)
    methods = {route.path: route.methods for route in stack.app.routes}
    assert methods["/v1/health"] == {"GET"} and methods["/v1/bars/daily"] == {"GET"}
    for path in ("/v1/login/arm", "/v1/login/complete", "/v1/session/revoke"):
        assert methods[path] == {"POST"}


def test_no_order_style_url_is_reachable(stack: Stack):
    token = stack.login()
    for path in ("/v1/order", "/v1/orders", "/v1/funds", "/v1/holdings", "/v1/positions", "/v1/trades"):
        for method in ("get", "post"):
            resp = getattr(stack.client, method)(path, headers={**CONTRACT_HDR, **auth(token)})
            assert resp.status_code in (404, 405)
    assert stack.transport.breeze_requests()[-1][1].endswith("customerdetails")  # nothing else left


# ------------------------------------------------------ no_write AST scan

# The OD-4 day counter (61-08) is the only relay module allowed to write, and
# smoke_probe.py is the operator tool. gateway_vm/orders/ is Phase 63's own
# audited writer (ledger and audit log) and is not part of this relay core.
# Exemptions are paths relative to the package root, so a module of the same name
# in a new subpackage is scanned.
WRITE_EXEMPT = frozenset({"smoke_probe.py", "counter_store.py"})
WRITE_EXEMPT_DIRS = frozenset({"orders"})
WRITE_ATTRS = frozenset({"write_text", "write_bytes", "truncate", "mkdir"})
WRITE_OS_ATTRS = frozenset(
    {"write", "open", "replace", "rename", "remove", "unlink", "mkdir", "makedirs", "truncate", "fsync"}
)
WRITE_MODES = set("wax+")


MODE_SHAPE = re.compile(r"^[rwxabtU+]{1,4}$")


def _mode_offence(node: ast.Call, mode: ast.AST | None) -> list[tuple[int, str]]:
    if mode is None:
        return []  # read-only default
    if not (isinstance(mode, ast.Constant) and isinstance(mode.value, str)):
        return [(node.lineno, "open with a non-literal mode")]
    if WRITE_MODES & set(mode.value):
        return [(node.lineno, "open for writing")]
    return []


def write_offences(source: str) -> list[tuple[int, str]]:
    """Honest-mistake scan: it flags the usual ways to write, not hostile code."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in ("tempfile", "shutil"):
                    found.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] in ("tempfile", "shutil"):
                found.append((node.lineno, f"import {node.module}"))
        elif isinstance(node, ast.Call):
            func = node.func
            is_os = isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "os"
            if is_os and func.attr in WRITE_OS_ATTRS:
                found.append((node.lineno, f"os.{func.attr}"))
            elif isinstance(func, ast.Attribute) and func.attr in WRITE_ATTRS:
                found.append((node.lineno, func.attr))
            elif isinstance(func, ast.Name) and func.id == "open":
                mode = node.args[1] if len(node.args) > 1 else None
                for kw in node.keywords:
                    if kw.arg == "mode":
                        mode = kw.value
                found.extend(_mode_offence(node, mode))
            elif isinstance(func, ast.Attribute) and func.attr == "open":
                # Path.open(mode) takes the mode first, io.open(path, mode) second:
                # look at both places. A string that is not mode-shaped is a path.
                candidates = list(node.args[:2]) + [kw.value for kw in node.keywords if kw.arg == "mode"]
                for cand in candidates:
                    if isinstance(cand, ast.Constant) and isinstance(cand.value, str):
                        if MODE_SHAPE.fullmatch(cand.value):
                            found.extend(_mode_offence(node, cand))
                    else:
                        found.append((node.lineno, "open with a non-literal mode"))
    return found


def relay_modules(root: Path = PKG) -> list[Path]:
    """Every module under the package, subpackages included, minus the named exemptions."""
    kept = []
    for path in root.rglob("*.py"):
        rel = path.relative_to(root)
        if rel.parts[0] in WRITE_EXEMPT_DIRS and len(rel.parts) > 1:
            continue
        if rel.as_posix() in WRITE_EXEMPT:
            continue
        kept.append(path)
    return sorted(kept)


def scan_package(root: Path = PKG) -> list[str]:
    offences: list[str] = []
    for path in relay_modules(root):
        rel = path.relative_to(root).as_posix()
        offences += [f"{rel}:{line}: {kind}" for line, kind in write_offences(path.read_text())]
    return offences


def test_no_write_relay_modules_never_write_to_disk():
    names = {p.name for p in relay_modules()}
    assert {"app.py", "session.py", "events.py", "contract.py", "config.py", "breeze_client.py",
            "transport.py", "egress.py"} <= names  # the scan really covers the relay
    assert scan_package() == []


def test_no_write_exemptions_are_exactly_the_named_modules():
    assert WRITE_EXEMPT == {"smoke_probe.py", "counter_store.py"}
    assert WRITE_EXEMPT_DIRS == {"orders"}


def test_no_write_scan_recurses_into_subpackages_and_exempts_only_orders(tmp_path):
    root = tmp_path / "gateway_vm"
    planted = {
        "ok.py": "x = 1\n",
        "smoke_probe.py": "open('x', 'w')\n",  # exempt at the package root
        "counter_store.py": "open('x', 'w')\n",  # exempt at the package root
        "orders/ledger.py": "open('x', 'w')\n",  # Phase 63's audited writer
        "orders/deep/more.py": "open('x', 'w')\n",
        "cache/store.py": "open('x', 'w')\n",  # a new subpackage: scanned
        "cache/deeper/blob.py": "import os\nos.makedirs('d')\n",
        "cache/smoke_probe.py": "open('x', 'w')\n",  # same name, not at the root: scanned
        "orders.py": "open('x', 'w')\n",  # a module called orders is not the orders package
    }
    for rel, source in planted.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    offences = scan_package(root)
    flagged = {line.split(":")[0] for line in offences}
    assert flagged == {"cache/store.py", "cache/deeper/blob.py", "cache/smoke_probe.py", "orders.py"}


PLANTED_WRITES = {
    "open w": "f = open('x', 'w')\n",
    "open wb keyword": "f = open('x', mode='wb')\n",
    "open append": "f = open('x', 'a')\n",
    "open read write": "f = open('x', 'r+')\n",
    "open dynamic mode": "m = 'w'\nf = open('x', m)\n",
    "open dynamic mode keyword": "m = 'w'\nf = open('x', mode=m)\n",
    "path open": "from pathlib import Path\nPath('x').open('w')\n",
    "path open w keyword": "from pathlib import Path\nPath('x').open(mode='w')\n",
    "path open dynamic mode": "from pathlib import Path\nm = 'w'\nPath('x').open(m)\n",
    "path open dynamic mode keyword": "from pathlib import Path\nm = 'w'\nPath('x').open(mode=m)\n",
    "io.open dynamic mode": "import io\nm = 'w'\nio.open('x', m)\n",
    "os.open": "import os\nos.open('x', os.O_WRONLY)\n",
    "os.write": "import os\nos.write(3, b'x')\n",
    "write_text": "from pathlib import Path\nPath('x').write_text('y')\n",
    "write_bytes": "from pathlib import Path\nPath('x').write_bytes(b'y')\n",
    "tempfile": "import tempfile\n",
    "tempfile from": "from tempfile import mkstemp\n",
    "shutil": "import shutil\n",
    "os.replace": "import os\nos.replace('a', 'b')\n",
    "os.makedirs": "import os\nos.makedirs('d')\n",
    "os.mkdir": "import os\nos.mkdir('d')\n",
    "path mkdir": "from pathlib import Path\nPath('d').mkdir()\n",
    "path mkdir parents": "from pathlib import Path\nPath('d').mkdir(parents=True, exist_ok=True)\n",
}


@pytest.mark.parametrize("name", list(PLANTED_WRITES))
def test_no_write_scanner_flags_planted_offence(name):
    assert write_offences(PLANTED_WRITES[name]), f"scanner missed: {name}"


def test_no_write_scanner_accepts_reads():
    source = (
        "f = open('x')\ng = open('x', 'rb')\nh = open('x', mode='r')\nimport json, os\nos.stat('x')\n"
        "from pathlib import Path\nPath('x').open()\nPath('x').open('rb')\nPath('x').exists()\n"
    )
    assert write_offences(source) == []


# --------------------------------------------------- event log allowlist


def make_log():
    sink = io.StringIO()
    return EventLog(sink, FakeClock()), sink


def test_event_lines_are_compact_sorted_ascii_json():
    log, sink = make_log()
    log.emit("request", route="/v1/health", result="ok", reason=None, http=200, ms=3,
             request_id="0123456789abcdef", breeze_status=None)
    line = sink.getvalue()
    assert line.endswith("\n") and line.count("\n") == 1
    record = json.loads(line)
    assert list(record) == sorted(record)
    assert record["ts"] == "2026-10-21T04:30:00.000Z"
    assert " " not in line.strip()


@pytest.mark.parametrize(
    "event, fields",
    [
        ("unknown_event", {}),
        ("request", {"body": "x"}),
        ("request", {"token": "x"}),
        ("request", {"ts": "2026-01-01"}),  # the log stamps the time itself
        ("request", {"result": "maybe"}),
        ("request", {"reason": "Invalid session."}),  # free text is not a reason
        ("request", {"reason": 5}),
        ("request", {"route": "/v1/anything-else"}),
        ("request", {"route": "/v1/bars/daily?stock_code=RELIND"}),
        ("request", {"state": "logged_in"}),
        ("request", {"http": -1}),
        ("request", {"http": True}),
        ("request", {"http": "200"}),
        ("request", {"ms": 1.5}),
        ("request", {"count": -3}),
        ("request", {"breeze_status": "200"}),
        ("request", {"request_id": "ABCDEF0123456789"}),
        ("request", {"request_id": "0123456789abcde"}),
        ("request", {"request_id": "0123456789abcdef0"}),
    ],
)
def test_event_log_refuses_anything_outside_its_vocabulary(event, fields):
    log, sink = make_log()
    with pytest.raises(ValueError) as err:
        log.emit(event, **fields)
    assert sink.getvalue() == ""  # nothing was written
    for value in fields.values():
        if isinstance(value, str) and len(value) > 3:
            assert value not in str(err.value)  # the error never echoes the value


def test_event_log_accepts_every_documented_key_and_enum():
    log, sink = make_log()
    for state in ("none", "active", "expired", "suspended_egress"):
        log.emit("session_expired", state=state, result="error", reason="deadline")
    for reason in sorted(events_mod.REASONS):
        log.emit("request", result="rejected", reason=reason)
    for event in sorted(events_mod.EVENTS):
        log.emit(event, count=0)
    assert len(sink.getvalue().splitlines()) == 4 + len(events_mod.REASONS) + len(events_mod.EVENTS)


# ---------------------------------------- C3 query allowlist on every route


@pytest.mark.parametrize(
    "method, path",
    [
        ("get", "/v1/health"),
        ("post", "/v1/login/arm"),
        ("post", "/v1/login/complete"),
        ("post", "/v1/session/revoke"),
    ],
)
@pytest.mark.parametrize("extra", ["debug=1", "stock_code=RELIND", "a=1&a=2"])
def test_unlisted_query_parameters_are_422_on_every_route(stack: Stack, method, path, extra):
    token = stack.login()
    n = len(stack.transport.requests)
    kwargs = {"json": {}} if method == "post" else {}
    if path == "/v1/login/complete":
        armed = stack.arm(token).json()
        n = len(stack.transport.requests)
        kwargs = {"json": {"attempt_id": armed["attempt_id"], "apisession": CODE}}
        token = armed["relay_token"]
    resp = getattr(stack.client, method)(
        f"{path}?{extra}", headers={**CONTRACT_HDR, **auth(token)}, **kwargs
    )
    assert resp.status_code == 422 and code_of(resp) == "VALIDATION"
    assert resp.json()["error"]["fields"] == ["unknown"]
    assert len(stack.transport.requests) == n  # the handler never ran
    for value in ("debug", "stock_code", "RELIND"):
        assert value not in resp.text
    assert_envelope(resp)
    # and the same call without the query string is fine, so the query was the only fault
    again = getattr(stack.client, method)(path, headers={**CONTRACT_HDR, **auth(token)}, **kwargs)
    assert again.status_code == 200


def test_an_unlisted_query_on_arm_does_not_arm_or_clear_the_pending_attempt(stack: Stack):
    armed = stack.arm().json()
    resp = stack.client.post(
        "/v1/login/arm?x=1", json={}, headers={**CONTRACT_HDR}
    )
    assert resp.status_code == 422
    assert len(stack.events_named("login_armed")) == 1
    assert stack.complete(armed["attempt_id"], armed["relay_token"]).status_code == 200


def test_unlisted_query_on_revoke_leaves_the_session_alone(stack: Stack):
    token = stack.login()
    resp = stack.client.post(
        "/v1/session/revoke?force=1", json={}, headers={**CONTRACT_HDR, **auth(token)}
    )
    assert resp.status_code == 422
    assert stack.bars(token).status_code == 200


def test_every_route_in_the_app_has_a_query_allowlist(stack: Stack):
    assert set(contract.ALLOWED_QUERY_BY_ROUTE) == set(contract.ROUTES)
    assert {route.path for route in stack.app.routes} == set(contract.ALLOWED_QUERY_BY_ROUTE)
    assert contract.ALLOWED_QUERY_BY_ROUTE[contract.ROUTE_BARS_DAILY] == contract.ALLOWED_BARS_QUERY


# ----------------------------- fullmatch, never match (trailing newline)


NEWLINE_SAMPLES = {
    "STOCK_CODE_RE": "RELIND",
    "ATTEMPT_ID_RE": "A" * 22,
    "DATE_RE": "2025-06-02",
    "REQUEST_ID_RE": "0123456789abcdef",
    "API_SESSION_RE": CODE,
    "BEARER_RE": "Bearer " + "A" * 43,
    "HOST_RE": "127.0.0.1:8443",
}


@pytest.mark.parametrize("name", list(NEWLINE_SAMPLES))
def test_contract_patterns_refuse_a_trailing_newline_under_fullmatch(name):
    pattern = getattr(contract, name)
    sample = NEWLINE_SAMPLES[name]
    assert pattern.fullmatch(sample)
    assert pattern.fullmatch(sample + "\n") is None
    assert pattern.fullmatch("\n" + sample) is None


def test_every_compiled_pattern_in_contract_is_covered_by_the_newline_check():
    compiled = {n for n, v in vars(contract).items() if isinstance(v, re.Pattern)}
    assert compiled == set(NEWLINE_SAMPLES)


def test_events_and_contract_never_call_match_or_search():
    for name in ("events.py", "contract.py"):
        tree = ast.parse((PKG / name).read_text())
        calls = [
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]
        assert not {"match", "search"} & set(calls), f"{name} uses a non-anchored pattern call"


def test_event_log_refuses_a_request_id_with_a_trailing_newline():
    log, sink = make_log()
    log.emit("request", request_id="0123456789abcdef")
    with pytest.raises(ValueError):
        log.emit("request", request_id="0123456789abcdef\n")
    assert sink.getvalue().count("\n") == 1  # only the good line was written


def test_a_trailing_newline_is_422_in_the_attempt_id_apisession_and_stock_code(stack: Stack):
    armed = stack.arm().json()
    token = armed["relay_token"]
    for body in (
        {"attempt_id": armed["attempt_id"] + "\n", "apisession": CODE},
        {"attempt_id": armed["attempt_id"], "apisession": CODE + "\n"},
    ):
        resp = stack.client.post(
            "/v1/login/complete", json=body, headers={**CONTRACT_HDR, **auth(token)}
        )
        assert resp.status_code == 422 and code_of(resp) == "VALIDATION"
    assert stack.transport.requests == []
    assert stack.complete(armed["attempt_id"], token).status_code == 200
    for query in (
        "stock_code=RELIND%0A&from=2025-06-02&to=2025-06-13",
        "stock_code=RELIND&from=2025-06-02%0A&to=2025-06-13",
        "stock_code=RELIND&from=2025-06-02&to=2025-06-13&exchange=NSE%0A",
    ):
        assert stack.bars(token, "/v1/bars/daily?" + query).status_code == 422


def test_no_leak_a_bad_apisession_is_not_echoed_in_the_422(stack: Stack):
    armed = stack.arm().json()
    marker = "SUPERSECRET-MARKER-9f2c!"
    resp = stack.client.post(
        "/v1/login/complete",
        json={"attempt_id": armed["attempt_id"], "apisession": marker},
        headers={**CONTRACT_HDR, **auth(armed["relay_token"])},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["fields"] == ["apisession"]
    assert marker not in resp.text + stack.sink.getvalue()


# --------------- X-Growin-Upstream-Request describes the real request


def sent_query(stack: Stack) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(stack.transport.bars_requests()[-1][1]).query))


def test_upstream_request_header_equals_the_query_actually_sent(stack: Stack):
    token = stack.login()
    resp = stack.bars(token)
    assert json.loads(resp.headers[contract.HDR_UP_REQUEST]) == sent_query(stack)


@pytest.mark.parametrize("name", ["exch_code", "exchange_code"])
def test_upstream_request_header_follows_the_exchange_param_knob(stack: Stack, monkeypatch, name):
    monkeypatch.setattr(relay_app, "BARS_REQUEST_KNOBS", {"exchange_param_name": name})
    token = stack.login()
    resp = stack.bars(token)
    assert resp.status_code == 200
    query = sent_query(stack)
    assert query[name] == "NSE" and ({"exch_code", "exchange_code"} - {name}).isdisjoint(query)
    header = json.loads(resp.headers[contract.HDR_UP_REQUEST])
    assert header == query
    assert header[name] == "NSE"
