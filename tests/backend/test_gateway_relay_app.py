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


def test_nothing_secret_in_bodies_events_or_401s(stack: Stack):
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
