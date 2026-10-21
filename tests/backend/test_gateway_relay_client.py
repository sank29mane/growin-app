"""RelayClient for contract growin-relay/1 (Phase 61-09).

Happy paths run the real VM app through `fastapi.testclient.TestClient`
(an httpx.Client) with only the network faked. Tampered responses use
`httpx.MockTransport`. No test opens a socket, starts a tunnel or runs gcloud.
Tokens, codes and URLs in this file are generated fakes.
"""

from __future__ import annotations

import getpass
import hashlib
import io
import json
import logging
import re
import sys
import threading
import time
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from test_gateway_relay_app import (  # the harness of the 61-05 tests
    BARS_BODY,
    BASE,
    CODE,
    Stack,
)

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "gateway" / "vm") not in sys.path:
    sys.path.insert(0, str(ROOT / "gateway" / "vm"))

from gateway_vm import contract as vm  # noqa: E402

from brokers.breeze_gateway import cli as cli_mod
from brokers.breeze_gateway import client as client_mod
from brokers.breeze_gateway.config import ConfigError
from brokers.breeze_gateway import (
    CODE_TO_CLASS,
    CONTRACT,
    BudgetExhausted,
    BudgetUnavailable,
    ContractMismatch,
    EgressMismatch,
    ForbiddenClient,
    IdentityMismatch,
    InternalRelayError,
    LoginAttempt,
    LoginAttemptInvalid,
    MemoryTokenStore,
    MethodNotAllowed,
    NoSession,
    NotFound,
    RelayArgumentError,
    RelayClient,
    RelayContractMismatch,
    RelayError,
    RelayHashMismatch,
    RelayProtocolError,
    RelayUnavailable,
    RelayValidationError,
    SessionExpired,
    TunnelError,
    Unauthorized,
    UpstreamError,
    UpstreamPayload,
    UpstreamTimeout,
)

RID = "0123456789abcdef"
TOKEN = "T" * 43  # a fake relay token
OTHER_TOKEN = "U" * 43
ATTEMPT = "a" * 22
LOGIN_URL = "https://login.example.invalid/apiuser/login?api_key=CANARY-LOGIN-URL"
EXPIRY = datetime(2026, 10, 22, 0, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
FETCHED = "2026-10-21T04:30:00.000Z"
UPSTREAM_REQUEST = json.dumps({"stock_code": "RELIND"}, sort_keys=True, separators=(",", ":"))
ZIP_BODY = b"PK\x03\x04fake-zip"


# --------------------------------------------------------------- helpers


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def base_headers(rid: str = RID, ctype: str = "application/json") -> dict[str, str]:
    return {
        client_mod.HDR_CONTRACT: CONTRACT,
        client_mod.HDR_REQUEST_ID: rid,
        "Cache-Control": "no-store",
        "Content-Type": ctype,
    }


def bars_response(body: bytes = BARS_BODY, drop=(), **over: str) -> httpx.Response:
    headers = {
        **base_headers(),
        client_mod.HDR_UP_SHA256: sha(body),
        client_mod.HDR_UP_FETCHED_AT: FETCHED,
        client_mod.HDR_UP_API: client_mod.API_BARS_V2,
        client_mod.HDR_UP_REQUEST: UPSTREAM_REQUEST,
        client_mod.HDR_UP_INTERVAL: "1day",
        client_mod.HDR_BREEZE_STATUS: "200",
        client_mod.HDR_EMPTY: "false",
        **over,
    }
    for name in drop:
        headers.pop(name)
    return httpx.Response(200, headers=headers, content=body)


def json_response(payload: dict, status: int = 200, **extra: str) -> httpx.Response:
    return httpx.Response(
        status, headers={**base_headers(), **extra}, content=json.dumps(payload).encode()
    )


def error_response(code: str, status: int, **fields) -> httpx.Response:
    error = {
        "code": code,
        "reason": None,
        "breeze_status": None,
        "retryable": False,
        "retry_after_s": None,
        "fields": None,
        **fields,
    }
    return json_response({"contract": CONTRACT, "error": error}, status)


def mock_http(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), base_url=BASE)


def relay_with(handler, *, store=None) -> RelayClient:
    return RelayClient(http=mock_http(handler), token_store=store)


def one_response(response: httpx.Response) -> RelayClient:
    return relay_with(lambda request: response)


def bars(relay: RelayClient) -> UpstreamPayload:
    return relay.daily_bars("RELIND", date(2025, 6, 2), date(2025, 6, 13))


HEALTH_JSON = {
    "contract": CONTRACT,
    "state": "none",
    "login_pending": False,
    "expires_at_ist": None,
    "egress": {"ok": None, "checked_at_utc": None},
    "server_time_utc": FETCHED,
    "version": "61.0.0",
}
BUDGET_JSON = {
    "contract": CONTRACT,
    "calls_last_minute": 1,
    "calls_today_ist": 20,
    "per_minute_limit": 80,
    "per_day_limit": 4500,
    "remaining_today": 4480,
    "ist_date": "2026-10-21",
}
ARM_JSON = {
    "contract": CONTRACT,
    "attempt_id": ATTEMPT,
    "login_url": LOGIN_URL,
    "relay_token": TOKEN,
    "expires_at_utc": "2026-10-21T04:32:00.000Z",
}
SESSION_JSON = {"contract": CONTRACT, "state": "active", "expires_at_ist": EXPIRY.isoformat()}
REVOKED_JSON = {"contract": CONTRACT, "state": "none", "expires_at_ist": None}


class Canned:
    """Serves one valid answer per route and records every request."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path == "/v1/health":
            return json_response(HEALTH_JSON)
        if path == "/v1/budget":
            return json_response(BUDGET_JSON)
        if path == "/v1/login/arm":
            return json_response(ARM_JSON)
        if path == "/v1/login/complete":
            return json_response(SESSION_JSON)
        if path == "/v1/session/revoke":
            return json_response(REVOKED_JSON)
        if path == "/v1/security-master":
            return httpx.Response(
                200,
                headers={
                    **base_headers(ctype="application/zip"),
                    client_mod.HDR_UP_SHA256: sha(ZIP_BODY),
                    client_mod.HDR_UP_FETCHED_AT: FETCHED,
                    client_mod.HDR_UP_API: client_mod.API_SECURITY_MASTER,
                    client_mod.HDR_UP_REQUEST: json.dumps({"url": "https://x.example/m.zip"}),
                },
                content=ZIP_BODY,
            )
        api = {
            "/v1/bars/daily": client_mod.API_BARS_V2,
            "/v1/quotes": client_mod.API_QUOTES_V1,
            "/v1/preview-order": client_mod.API_PREVIEW_V1,
        }[path]
        extra = {client_mod.HDR_UP_INTERVAL: "1day"} if path == "/v1/bars/daily" else {}
        return bars_response(BARS_BODY, **{client_mod.HDR_UP_API: api, **extra})


# ------------------------------------------------------------- the tracer


def test_tracer_health_arm_complete_bars_revoke_against_the_real_vm_app(stack_fixture: Stack):
    stack = stack_fixture
    store = MemoryTokenStore()
    with RelayClient(http=stack.client, token_store=store) as relay:
        assert relay.health().state == "none"

        attempt = relay.login_arm()
        assert isinstance(attempt, LoginAttempt)
        assert "login_url" not in repr(attempt) and "relay_token" not in repr(attempt)
        assert attempt.login_url not in repr(attempt) and attempt.relay_token not in repr(attempt)

        session = relay.login_complete(attempt.attempt_id, CODE)
        assert session.state == "active"
        assert session.expires_at_ist == EXPIRY
        saved = store.load()
        assert saved is not None and saved.token == attempt.relay_token
        assert saved.expires_at_ist == EXPIRY

        payload = bars(relay)
        assert payload.body == BARS_BODY
        assert payload.sha256 == sha(BARS_BODY)
        assert payload.upstream_api == "breeze-historicalcharts-v2"
        assert payload.upstream_interval == "1day"
        assert payload.empty is False
        assert payload.breeze_status == 200
        assert payload.contract == CONTRACT
        assert payload.route == "/v1/bars/daily"
        assert dict(payload.query) == {
            "stock_code": "RELIND",
            "from": "2025-06-02",
            "to": "2025-06-13",
            "exchange": "NSE",
        }
        assert payload.upstream_request["stock_code"] == "RELIND"
        assert payload.upstream_request["from_date"] == "2025-06-02T00:00:00.000Z"
        assert payload.fetched_at_utc == datetime(2026, 10, 21, 4, 30, tzinfo=timezone.utc)
        prov = payload.provenance()
        assert prov["source"] == "breeze_relay"
        assert prov["fetched_at_utc"] == "2026-10-21T04:30:00.000Z"
        assert prov["sha256"] == payload.sha256 and prov["request_id"] == payload.request_id
        assert prov["route"] == "/v1/bars/daily" and prov["contract"] == CONTRACT
        assert set(prov) == {
            "source", "contract", "route", "query", "upstream_api", "upstream_request",
            "fetched_at_utc", "sha256", "request_id",
        }

        assert relay.revoke().state == "none"
        assert store.load() is None
        with pytest.raises(NoSession) as info:
            bars(relay)
        assert info.value.http_status == 503
        assert len(info.value.request_id) == 16 and int(info.value.request_id, 16) >= 0


def test_data_call_without_a_token_is_unauthorized_and_the_saved_token_works(stack_fixture: Stack):
    stack = stack_fixture
    with RelayClient(http=stack.client) as first:
        attempt = first.login_arm()
        first.login_complete(attempt.attempt_id, CODE)

    anonymous = RelayClient(http=stack.new_client())
    with pytest.raises(Unauthorized) as info:
        bars(anonymous)
    assert info.value.http_status == 401
    assert len(info.value.request_id) == 16

    store = MemoryTokenStore()
    store.save(attempt.relay_token, EXPIRY)
    with RelayClient(http=stack.new_client(), token_store=store) as holder:
        assert bars(holder).body == BARS_BODY
        holder.revoke()
        assert store.load() is None
        with pytest.raises(NoSession):
            bars(holder)


@pytest.fixture()
def stack_fixture() -> Stack:
    return Stack()


# ----------------------------------------------------- headers and tampering


@pytest.mark.parametrize("value", [None, "growin-relay/2", "", "growin-relay/1 "])
def test_a_missing_or_different_contract_header_fails_closed(value):
    # the body is a perfectly good health answer: only the header is wrong
    headers = base_headers()
    if value is None:
        headers.pop(client_mod.HDR_CONTRACT)
    else:
        headers[client_mod.HDR_CONTRACT] = value
    response = httpx.Response(200, headers=headers, content=json.dumps(HEALTH_JSON).encode())
    with pytest.raises(RelayContractMismatch) as info:
        one_response(response).health()
    assert info.value.reason == "contract_header"


@pytest.mark.parametrize("value", [None, "growin-relay/2"])
def test_upstream_bytes_with_a_wrong_contract_header_are_refused_even_if_the_hash_matches(value):
    good = bars_response()
    headers = httpx.Headers(good.headers)
    del headers[client_mod.HDR_CONTRACT]
    if value is not None:
        headers[client_mod.HDR_CONTRACT] = value
    response = httpx.Response(200, headers=headers, content=BARS_BODY)
    with pytest.raises(RelayContractMismatch) as info:
        bars(one_response(response))
    assert info.value.reason == "contract_header"


def test_the_contract_header_is_checked_before_the_status_and_body():
    headers = base_headers()
    headers[client_mod.HDR_CONTRACT] = "growin-relay/2"
    # a well-formed error body must not be mapped when the header says another version
    body = {"contract": CONTRACT, "error": {"code": "NO_SESSION", "reason": None,
                                            "breeze_status": None, "retryable": False,
                                            "retry_after_s": None, "fields": None}}
    response = httpx.Response(503, headers=headers, content=json.dumps(body).encode())
    with pytest.raises(RelayContractMismatch):
        one_response(response).budget()


def test_a_json_body_with_another_contract_fails_closed():
    with pytest.raises(RelayContractMismatch):
        one_response(json_response({**HEALTH_JSON, "contract": "growin-relay/2"})).health()


def test_a_body_changed_by_one_byte_is_a_hash_mismatch():
    tampered = bytearray(BARS_BODY)
    tampered[10] ^= 0x01
    response = bars_response(bytes(tampered), **{client_mod.HDR_UP_SHA256: sha(BARS_BODY)})
    with pytest.raises(RelayHashMismatch):
        bars(one_response(response))


def test_the_hash_is_compared_in_constant_time(monkeypatch):
    calls = []
    real = client_mod.hmac.compare_digest

    def spy(left, right):
        calls.append((left, right))
        return real(left, right)

    monkeypatch.setattr(client_mod.hmac, "compare_digest", spy)
    bars(one_response(bars_response()))
    assert (sha(BARS_BODY).encode(), sha(BARS_BODY).encode()) in calls


@pytest.mark.parametrize(
    "name",
    [
        client_mod.HDR_UP_SHA256,
        client_mod.HDR_UP_FETCHED_AT,
        client_mod.HDR_UP_API,
        client_mod.HDR_UP_REQUEST,
        client_mod.HDR_UP_INTERVAL,
        client_mod.HDR_BREEZE_STATUS,
        client_mod.HDR_EMPTY,
    ],
)
def test_every_c4_header_is_required_for_bars(name):
    with pytest.raises(RelayProtocolError):
        bars(one_response(bars_response(drop=(name,))))


@pytest.mark.parametrize(
    "name,value",
    [
        (client_mod.HDR_UP_SHA256, sha(BARS_BODY).upper()),
        (client_mod.HDR_UP_SHA256, "abc"),
        (client_mod.HDR_UP_FETCHED_AT, "2026-10-21 04:30:00"),
        (client_mod.HDR_UP_FETCHED_AT, "2026-10-21T04:30:00Z"),
        (client_mod.HDR_UP_API, "breeze-quotes-v1"),
        (client_mod.HDR_UP_REQUEST, "not json"),
        (client_mod.HDR_UP_REQUEST, "[1]"),
        (client_mod.HDR_UP_REQUEST, '{"quantity": 1}'),
        (client_mod.HDR_UP_INTERVAL, "5minute"),
        (client_mod.HDR_BREEZE_STATUS, "500"),
        (client_mod.HDR_EMPTY, "maybe"),
        ("Content-Type", "text/plain"),
    ],
)
def test_a_malformed_c4_header_fails_closed(name, value):
    with pytest.raises(RelayProtocolError):
        bars(one_response(bars_response(**{name: value})))


def test_the_empty_flag_is_carried_not_interpreted():
    payload = bars(one_response(bars_response(**{client_mod.HDR_EMPTY: "true"})))
    assert payload.empty is True


def test_a_redirect_is_refused_and_never_followed():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(302, headers={**base_headers(), "Location": "/elsewhere"})

    with pytest.raises(RelayProtocolError) as info:
        bars(relay_with(handler))
    assert info.value.reason == "redirect"
    assert seen == ["/v1/bars/daily"]


def test_a_body_over_the_cap_stops_reading(monkeypatch):
    monkeypatch.setattr(client_mod, "MAX_BODY_BYTES", 1024)
    yielded: list[int] = []

    class Endless(httpx.SyncByteStream):
        def __iter__(self):
            for index in range(10_000):
                yielded.append(index)
                yield b"x" * 512

    response = httpx.Response(200, headers=base_headers(), stream=Endless())
    with pytest.raises(RelayProtocolError) as info:
        one_response(response).health()
    assert info.value.reason == "body_too_large"
    assert len(yielded) <= 4  # it did not read the rest


def test_a_declared_length_over_the_cap_is_refused_up_front(monkeypatch):
    monkeypatch.setattr(client_mod, "MAX_BODY_BYTES", 1024)
    headers = {**base_headers(), "Content-Length": "2048"}
    response = httpx.Response(200, headers=headers, stream=httpx.ByteStream(b"y" * 10))
    with pytest.raises(RelayProtocolError) as info:
        one_response(response).health()
    assert info.value.reason == "body_too_large"


def test_a_missing_or_malformed_request_id_fails_closed():
    for rid in (None, "XYZ", RID.upper(), RID + "0"):
        headers = base_headers()
        headers.pop(client_mod.HDR_REQUEST_ID)
        if rid is not None:
            headers[client_mod.HDR_REQUEST_ID] = rid
        response = httpx.Response(200, headers=headers, content=json.dumps(HEALTH_JSON).encode())
        with pytest.raises(RelayProtocolError):
            one_response(response).health()


def test_non_json_and_non_object_json_fail_closed():
    for content in (b"not json", b"[]", b'"x"'):
        response = httpx.Response(200, headers=base_headers(), content=content)
        with pytest.raises(RelayProtocolError):
            one_response(response).health()


def test_a_json_route_must_answer_json():
    headers = base_headers(ctype="text/html")
    response = httpx.Response(200, headers=headers, content=json.dumps(HEALTH_JSON).encode())
    with pytest.raises(RelayProtocolError):
        one_response(response).health()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("state"),
        lambda d: d.update(state="weird"),
        lambda d: d.update(login_pending="no"),
        lambda d: d.update(version=7),
        lambda d: d.update(server_time_utc="2026-10-21T04:30:00.000"),  # naive
        lambda d: d.update(server_time_utc="yesterday"),
        lambda d: d.update(egress={"ok": "yes", "checked_at_utc": None}),
        lambda d: d.update(egress=None),
    ],
)
def test_health_fields_are_checked_strictly(mutate):
    payload = json.loads(json.dumps(HEALTH_JSON))
    mutate(payload)
    with pytest.raises(RelayProtocolError):
        one_response(json_response(payload)).health()


def test_health_parses_a_full_answer():
    payload = {
        **HEALTH_JSON,
        "state": "active",
        "login_pending": True,
        "expires_at_ist": EXPIRY.isoformat(),
        "egress": {"ok": True, "checked_at_utc": FETCHED},
    }
    health = one_response(json_response(payload)).health()
    assert health.state == "active" and health.login_pending is True
    assert health.expires_at_ist == EXPIRY
    assert health.egress_ok is True
    assert health.egress_checked_at_utc == datetime(2026, 10, 21, 4, 30, tzinfo=timezone.utc)
    assert health.version == "61.0.0"


def test_budget_parses_and_rejects_bad_numbers():
    budget = one_response(json_response(BUDGET_JSON)).budget()
    assert (budget.calls_last_minute, budget.calls_today_ist) == (1, 20)
    assert (budget.per_minute_limit, budget.per_day_limit, budget.remaining_today) == (80, 4500, 4480)
    assert budget.ist_date == date(2026, 10, 21)
    for key, value in (("per_day_limit", "4500"), ("remaining_today", True), ("ist_date", "21-10-2026")):
        with pytest.raises(RelayProtocolError):
            one_response(json_response({**BUDGET_JSON, key: value})).budget()


@pytest.mark.parametrize(
    "key,value",
    [
        ("relay_token", "short"),
        ("relay_token", "bad token with spaces and the right length 1234"),
        ("relay_token", None),
        ("attempt_id", "x"),
        ("login_url", "http://login.example.invalid/"),
        ("login_url", "file:///etc/passwd"),
        ("expires_at_utc", "soon"),
    ],
)
def test_the_arm_answer_is_validated_before_anything_uses_it(key, value):
    relay = one_response(json_response({**ARM_JSON, key: value}))
    with pytest.raises(RelayProtocolError):
        relay.login_arm()
    assert relay._attempt_token is None


def test_complete_requires_an_active_session_with_an_expiry():
    for payload in (
        {**SESSION_JSON, "state": "none"},
        {**SESSION_JSON, "expires_at_ist": None},
        {**SESSION_JSON, "expires_at_ist": "2026-10-22T00:00:00"},
    ):
        store = MemoryTokenStore()
        calls = Canned()

        def handler(request, payload=payload, calls=calls):
            if request.url.path == "/v1/login/complete":
                return json_response(payload)
            return calls(request)

        relay = relay_with(handler, store=store)
        attempt = relay.login_arm()
        with pytest.raises(RelayProtocolError):
            relay.login_complete(attempt.attempt_id, CODE)
        assert store.load() is None


# ------------------------------------------------------------ error codes

C6 = {
    "UNAUTHORIZED": (401, Unauthorized),
    "NO_SESSION": (503, NoSession),
    "SESSION_EXPIRED": (503, SessionExpired),
    "EGRESS_MISMATCH": (503, EgressMismatch),
    "BUDGET_UNAVAILABLE": (503, BudgetUnavailable),
    "BUDGET_EXHAUSTED": (429, BudgetExhausted),
    "UPSTREAM_ERROR": (502, UpstreamError),
    "UPSTREAM_TIMEOUT": (504, UpstreamTimeout),
    "VALIDATION": (422, RelayValidationError),
    "LOGIN_ATTEMPT_INVALID": (409, LoginAttemptInvalid),
    "IDENTITY_MISMATCH": (403, IdentityMismatch),
    "FORBIDDEN_CLIENT": (403, ForbiddenClient),
    "CONTRACT_MISMATCH": (400, ContractMismatch),
    "NOT_FOUND": (404, NotFound),
    "METHOD_NOT_ALLOWED": (405, MethodNotAllowed),
    "INTERNAL": (500, InternalRelayError),
}


def test_every_c6_code_has_exactly_one_subclass():
    assert set(CODE_TO_CLASS) == set(C6)
    classes = list(CODE_TO_CLASS.values())
    for first in classes:
        for second in classes:
            if first is not second:
                assert not issubclass(first, second), (first, second)  # no catch-by-accident
    assert len(set(CODE_TO_CLASS.values())) == len(C6)
    for code, (_, cls) in C6.items():
        assert CODE_TO_CLASS[code] is cls and cls.code == code


@pytest.mark.parametrize("code", sorted(C6))
def test_each_c6_code_maps_to_its_subclass_and_carries_the_fields(code):
    status, cls = C6[code]
    response = error_response(
        code, status, reason="budget_minute", breeze_status=500, retryable=True, retry_after_s=7,
        fields=["stock_code"],
    )
    with pytest.raises(cls) as info:
        bars(one_response(response))
    err = info.value
    assert type(err) is cls
    assert (err.code, err.http_status, err.reason) == (code, status, "budget_minute")
    assert (err.breeze_status, err.retryable, err.retry_after_s) == (500, True, 7)
    assert err.request_id == RID
    assert str(err) == f"{code} (request {RID})"


@pytest.mark.parametrize(
    "response",
    [
        error_response("NOT_A_CODE", 500),
        error_response("UNAUTHORIZED", 401, reason="Has Spaces"),
        error_response("UNAUTHORIZED", 401, retryable="yes"),
        error_response("UNAUTHORIZED", 401, retry_after_s="soon"),
        error_response("UNAUTHORIZED", 401, breeze_status="500"),
        json_response({"contract": CONTRACT, "error": "boom"}, 500),
        json_response({"contract": CONTRACT}, 500),
        httpx.Response(500, headers=base_headers(), content=b"<html>boom</html>"),
        httpx.Response(204, headers=base_headers(), content=b""),
    ],
)
def test_an_error_body_that_is_not_a_contract_error_is_a_protocol_error(response):
    with pytest.raises(RelayProtocolError):
        bars(one_response(response))


def test_an_error_body_with_another_contract_is_a_contract_mismatch():
    body = {"contract": "growin-relay/2", "error": {"code": "NO_SESSION"}}
    with pytest.raises(RelayContractMismatch):
        bars(one_response(json_response(body, 503)))


def test_error_text_holds_only_the_code_and_the_request_id():
    canary = "CANARY-SHOULD-NOT-APPEAR"
    response = error_response("UPSTREAM_ERROR", 502, reason="no_data", fields=[canary])
    relay = one_response(response)
    with pytest.raises(UpstreamError) as info:
        relay.daily_bars("RELIND", date(2025, 6, 2), date(2025, 6, 13))
    text = f"{info.value} {info.value!r} {info.value.args}"
    for forbidden in ("RELIND", "2025", "NSE", canary, "no_data"):
        assert forbidden not in text
    assert str(info.value) == f"UPSTREAM_ERROR (request {RID})"
    for cls in (RelayContractMismatch, RelayHashMismatch, RelayProtocolError, RelayUnavailable):
        err = cls(reason="x", request_id=RID)
        assert str(err) == f"{cls.code} (request {RID})"
        assert isinstance(err, RelayError)


# ---------------------------------------------- what the client sends


def test_requests_carry_the_contract_header_and_the_c3_query_names():
    seen = Canned()
    relay = relay_with(seen)
    relay.health()
    relay.budget()
    attempt = relay.login_arm()
    relay.login_complete(attempt.attempt_id, CODE)
    relay.daily_bars("RELIND", date(2025, 6, 2), date(2025, 6, 13))
    relay.quote("RELIND")
    relay.preview_order("RELIND", action="buy", quantity=35, price=Decimal("1402.3"))
    relay.security_master()
    relay.revoke()

    assert [(r.method, r.url.path) for r in seen.requests] == [
        ("GET", "/v1/health"),
        ("GET", "/v1/budget"),
        ("POST", "/v1/login/arm"),
        ("POST", "/v1/login/complete"),
        ("GET", "/v1/bars/daily"),
        ("GET", "/v1/quotes"),
        ("GET", "/v1/preview-order"),
        ("GET", "/v1/security-master"),
        ("POST", "/v1/session/revoke"),
    ]
    for request in seen.requests:
        assert request.headers[client_mod.HDR_CONTRACT] == CONTRACT
        assert request.url.host == "127.0.0.1"
        if request.method == "POST":
            assert request.headers["content-type"] == "application/json"
            assert json.loads(request.content) is not None
    queries = {r.url.path: dict(r.url.params) for r in seen.requests}
    assert queries["/v1/health"] == queries["/v1/budget"] == queries["/v1/security-master"] == {}
    assert queries["/v1/bars/daily"] == {
        "stock_code": "RELIND", "from": "2025-06-02", "to": "2025-06-13", "exchange": "NSE",
    }
    assert queries["/v1/quotes"] == {"stock_code": "RELIND", "exchange": "NSE"}
    assert queries["/v1/preview-order"] == {
        "stock_code": "RELIND", "action": "buy", "quantity": "35", "price": "1402.30",
        "exchange": "NSE",
    }
    bodies = {r.url.path: json.loads(r.content) for r in seen.requests if r.method == "POST"}
    assert bodies["/v1/login/arm"] == {} and bodies["/v1/session/revoke"] == {}
    assert bodies["/v1/login/complete"] == {"attempt_id": ATTEMPT, "apisession": CODE}


def test_bearer_goes_only_where_the_contract_wants_it():
    seen = Canned()
    relay = relay_with(seen)

    def auth(index: int):
        return seen.requests[index].headers.get("authorization")

    relay.health()
    assert auth(0) is None
    relay.budget()  # holds no token: nothing is sent
    assert auth(1) is None
    attempt = relay.login_arm()  # no session token held: arm sends none
    assert auth(2) is None
    relay.login_complete(attempt.attempt_id, CODE)
    assert auth(3) == f"Bearer {TOKEN}"  # the arm's token
    relay.health()
    assert auth(4) is None  # health never sends one, even now
    relay.budget()
    relay.daily_bars("RELIND", date(2025, 6, 2), date(2025, 6, 13))
    relay.quote("RELIND")
    relay.preview_order("RELIND", action="sell", quantity=1, price=Decimal("10"))
    relay.security_master()
    for index in range(5, 10):
        assert auth(index) == f"Bearer {TOKEN}", seen.requests[index].url.path
    relay.login_arm()  # holds a session token: arm sends it
    assert auth(10) == f"Bearer {TOKEN}"
    relay.revoke()
    assert auth(11) == f"Bearer {TOKEN}"
    relay.budget()  # revoke dropped the token
    assert auth(12) is None


def test_a_token_found_in_the_store_is_sent():
    store = MemoryTokenStore()
    store.save(OTHER_TOKEN, EXPIRY)
    seen = Canned()
    relay_with(seen, store=store).budget()
    assert seen.requests[0].headers["authorization"] == f"Bearer {OTHER_TOKEN}"


@pytest.mark.parametrize(
    "code,status", [("UNAUTHORIZED", 401), ("NO_SESSION", 503), ("SESSION_EXPIRED", 503)]
)
def test_a_dead_session_response_clears_the_token_this_client_sent(code, status):
    store = MemoryTokenStore()
    store.save(TOKEN, EXPIRY)
    relay = relay_with(lambda request: error_response(code, status), store=store)
    with pytest.raises(RelayError):
        bars(relay)
    assert store.load() is None
    assert relay._session_token is None


@pytest.mark.parametrize(
    "code,status", [("UNAUTHORIZED", 401), ("NO_SESSION", 503), ("SESSION_EXPIRED", 503)]
)
def test_the_stored_token_survives_when_it_is_no_longer_the_one_sent(code, status):
    store = MemoryTokenStore()
    store.save(TOKEN, EXPIRY)

    def handler(request: httpx.Request) -> httpx.Response:
        store.save(OTHER_TOKEN, EXPIRY)  # another login replaced it while we waited
        return error_response(code, status)

    with pytest.raises(RelayError):
        bars(relay_with(handler, store=store))
    kept = store.load()
    assert kept is not None and kept.token == OTHER_TOKEN


@pytest.mark.parametrize("code,status", [("UPSTREAM_ERROR", 502), ("BUDGET_EXHAUSTED", 429), ("VALIDATION", 422)])
def test_other_errors_keep_the_token(code, status):
    store = MemoryTokenStore()
    store.save(TOKEN, EXPIRY)
    with pytest.raises(RelayError):
        bars(relay_with(lambda request: error_response(code, status), store=store))
    assert store.load() is not None


def test_no_text_or_log_ever_holds_the_token_or_the_login_url(caplog, capsys):
    caplog.set_level(logging.DEBUG)
    store = MemoryTokenStore()
    relay = relay_with(Canned(), store=store)
    attempt = relay.login_arm()
    relay.login_complete(attempt.attempt_id, CODE)
    texts = [repr(attempt), str(attempt), repr(store.load()), str(store.load()), repr(relay)]

    for code, status in (("UNAUTHORIZED", 401), ("LOGIN_ATTEMPT_INVALID", 409), ("NO_SESSION", 503)):
        failing = relay_with(lambda request, c=code, s=status: error_response(c, s), store=store)
        failing._session_token = TOKEN
        with pytest.raises(RelayError) as info:
            failing.login_complete(ATTEMPT, CODE)
        texts += [str(info.value), repr(info.value), repr(info.value.args)]

    out = capsys.readouterr()
    haystack = "\n".join(texts) + caplog.text + out.out + out.err
    for secret in (TOKEN, LOGIN_URL, "CANARY-LOGIN-URL", CODE):
        assert secret not in haystack


# ------------------------------------------------------------ arguments


@pytest.mark.parametrize(
    "call",
    [
        lambda r: r.daily_bars("reliance", date(2025, 6, 2), date(2025, 6, 13)),
        lambda r: r.daily_bars("RELIND", date(2025, 6, 13), date(2025, 6, 2)),
        lambda r: r.daily_bars("RELIND", date(2000, 1, 1), date(2025, 6, 13)),
        lambda r: r.daily_bars("RELIND", "2025-06-02", date(2025, 6, 13)),
        lambda r: r.daily_bars("RELIND", datetime(2025, 6, 2), date(2025, 6, 13)),
        lambda r: r.daily_bars("RELIND", date(2025, 6, 2), date(2025, 6, 13), exchange="BSE"),
        lambda r: r.quote("TOOLONGSYMBOL"),
        lambda r: r.preview_order("RELIND", action="short", quantity=1, price=Decimal("1")),
        lambda r: r.preview_order("RELIND", action="buy", quantity=0, price=Decimal("1")),
        lambda r: r.preview_order("RELIND", action="buy", quantity=100_001, price=Decimal("1")),
        lambda r: r.preview_order("RELIND", action="buy", quantity=True, price=Decimal("1")),
        lambda r: r.preview_order("RELIND", action="buy", quantity=1, price=Decimal("0")),
        lambda r: r.preview_order("RELIND", action="buy", quantity=1, price=Decimal("1.234")),
        lambda r: r.preview_order("RELIND", action="buy", quantity=1, price=Decimal("1000000.01")),
        lambda r: r.preview_order("RELIND", action="buy", quantity=1, price=Decimal("NaN")),
        lambda r: r.preview_order("RELIND", action="buy", quantity=1, price=1209.0),
        lambda r: r.login_complete("short", CODE),
        lambda r: r.login_complete(ATTEMPT, "12 34"),
    ],
)
def test_bad_arguments_are_refused_before_anything_is_sent(call):
    seen = Canned()
    with pytest.raises(RelayArgumentError) as info:
        call(relay_with(seen))
    assert seen.requests == []
    assert "RELIND" not in str(info.value) and "2025" not in str(info.value)


def test_a_range_of_exactly_3000_days_is_allowed():
    seen = Canned()
    start = date(2015, 1, 1)
    relay_with(seen).daily_bars("RELIND", start, start + timedelta(days=3000))
    assert len(seen.requests) == 1


def test_price_formats_without_an_exponent():
    seen = Canned()
    relay_with(seen).preview_order("RELIND", action="buy", quantity=1, price=Decimal("1E+3"))
    assert seen.requests[0].url.params["price"] == "1000.00"


# ------------------------------------------------------- the other routes


def test_quote_preview_and_master_return_verified_payloads():
    relay = relay_with(Canned())
    quote = relay.quote("RELIND")
    assert quote.upstream_api == "breeze-quotes-v1" and quote.upstream_interval is None
    preview = relay.preview_order("RELIND", action="buy", quantity=1, price=Decimal("1209"))
    assert preview.upstream_api == "breeze-preview-order-v1"
    master = relay.security_master()
    assert master.body == ZIP_BODY and master.upstream_api == "icici-security-master-zip"
    assert master.breeze_status is None and master.empty is False
    assert master.upstream_last_modified is None


def test_security_master_copies_last_modified_and_checks_the_media_type():
    seen = Canned()

    def handler(request: httpx.Request) -> httpx.Response:
        response = seen(request)
        response.headers[client_mod.HDR_UP_LAST_MODIFIED] = "Tue, 20 Oct 2026 10:00:00 GMT"
        return response

    assert relay_with(handler).security_master().upstream_last_modified == "Tue, 20 Oct 2026 10:00:00 GMT"

    def wrong_type(request: httpx.Request) -> httpx.Response:
        response = seen(request)
        response.headers["content-type"] = "application/json"
        return response

    with pytest.raises(RelayProtocolError):
        relay_with(wrong_type).security_master()


# ------------------------------------------------- reuse and concurrency


def test_the_client_is_bounded_to_four_requests_in_flight():
    active = 0
    peak = 0
    lock = threading.Lock()
    release = threading.Event()
    four_in = threading.Event()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active >= 4:
                four_in.set()
        release.wait(timeout=10)
        with lock:
            active -= 1
        return json_response(HEALTH_JSON)

    relay = relay_with(handler)
    assert isinstance(relay._slots, type(threading.BoundedSemaphore(1)))
    results: list[object] = []
    threads = [
        threading.Thread(target=lambda: results.append(relay.health()), daemon=True)
        for _ in range(6)
    ]
    for thread in threads:
        thread.start()
    assert four_in.wait(timeout=10)
    time.sleep(0.3)  # a fifth and sixth call would have arrived by now
    with lock:
        assert active == 4
    release.set()
    for thread in threads:
        thread.join(timeout=10)
    assert len(results) == 6
    assert peak == 4


class _FakeHandle:
    def __init__(self, port: int = 50123, alive: bool = True) -> None:
        self.local_port = port
        self._alive = alive

    def alive(self) -> bool:
        return self._alive


def test_the_tunnel_client_is_built_for_reuse_on_the_handles_loopback_port():
    relay = RelayClient(tunnel=_FakeHandle(50123))
    http = relay._http
    assert str(http.base_url) == "http://127.0.0.1:50123"
    assert http.follow_redirects is False
    assert http.trust_env is False
    pool = http._transport._pool
    assert pool._max_connections == 4
    assert pool._max_keepalive_connections == 4
    assert http.timeout == httpx.Timeout(connect=5.0, read=150.0, write=10.0, pool=5.0)
    relay.close()


def test_the_client_needs_exactly_one_of_tunnel_or_http():
    with pytest.raises(TypeError):
        RelayClient()
    with pytest.raises(TypeError):
        RelayClient(tunnel=_FakeHandle(), http=mock_http(Canned()))


def test_a_closed_client_refuses_to_send():
    relay = relay_with(Canned())
    relay.close()
    relay.close()  # idempotent
    with pytest.raises(RelayUnavailable):
        relay.health()


def test_a_transport_failure_is_relay_unavailable_and_is_not_resent():
    attempts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        raise httpx.ConnectError("boom with CANARY-HOST", request=request)

    with pytest.raises(RelayUnavailable) as info:
        bars(relay_with(handler))
    assert attempts == ["/v1/bars/daily"]
    assert "CANARY" not in str(info.value) and "CANARY" not in repr(info.value.__cause__)
    assert info.value.reason == "connect_error"

    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(RelayUnavailable) as info:
        relay_with(slow).health()
    assert info.value.reason == "timeout"


# ------------------------------------------------------------------- CLI


class _Tunnel:
    """A fake Tunnel for `cli tunnel`."""

    def __init__(self, port: int = 50123, alive_after_wait: bool = True) -> None:
        self.port = port
        self.alive_after_wait = alive_after_wait
        self.handle = _FakeHandle(port)
        self.stopped = 0

    def start(self):
        outer = self

        class Handle(_FakeHandle):
            def stop(self_inner) -> None:
                outer.stopped += 1
                self_inner._alive = False

        self.handle = Handle(self.port)
        return self.handle


def run_cli(args, *, handler=None, store=None, isatty=True, **kwargs):
    """Returns (exit code, text, handler). The client is built on the Canned relay."""
    handler = handler if handler is not None else Canned()
    out = io.StringIO()
    kwargs.setdefault("client_factory", lambda: relay_with(handler, store=store))
    code = cli_mod.main(args, isatty=lambda: isatty, out=out, **kwargs)
    return code, out.getvalue(), handler


def test_health_prints_one_key_value_line_per_field_in_order():
    code, text, _ = run_cli(["health"])
    assert code == 0
    assert text.splitlines() == [
        "state none", "login_pending false", "expires_at_ist none", "egress_ok unknown",
    ]
    active = {
        **HEALTH_JSON, "state": "active", "login_pending": True,
        "expires_at_ist": EXPIRY.isoformat(), "egress": {"ok": True, "checked_at_utc": FETCHED},
    }
    code, text, _ = run_cli(["health"], handler=lambda request: json_response(active))
    assert text.splitlines() == [
        "state active", "login_pending true", f"expires_at_ist {EXPIRY.isoformat()}", "egress_ok true",
    ]
    failing = {**HEALTH_JSON, "egress": {"ok": False, "checked_at_utc": FETCHED}}
    assert "egress_ok false" in run_cli(["health"], handler=lambda r: json_response(failing))[1]


def test_budget_and_revoke_print_their_fields():
    code, text, _ = run_cli(["budget"])
    assert code == 0 and text.splitlines() == [
        "calls_last_minute 1", "calls_today_ist 20", "per_minute_limit 80", "per_day_limit 4500",
        "remaining_today 4480", "ist_date 2026-10-21",
    ]
    code, text, _ = run_cli(["revoke"])
    assert code == 0 and text.splitlines() == ["state none"]


def test_login_paste_completes_over_the_relay_and_prints_no_secret():
    prompts = []
    opened = []

    def ask(prompt):
        prompts.append(prompt)
        return f" {CODE} "

    store = MemoryTokenStore()
    code, text, handler = run_cli(
        ["login", "--paste", "--no-open"], store=store, getpass_fn=ask, opener=opened.append
    )
    assert code == 0
    assert text.splitlines()[-2:] == ["state active", f"expires_at_ist {EXPIRY.isoformat()}"]
    assert "ICICI API portal" in text
    assert opened == [] and len(prompts) == 1
    for secret in (CODE, LOGIN_URL, "CANARY-LOGIN-URL", TOKEN):
        assert secret not in text
    complete = [r for r in handler.requests if r.url.path == "/v1/login/complete"]
    assert json.loads(complete[0].content) == {"attempt_id": ATTEMPT, "apisession": CODE}
    assert complete[0].headers["authorization"] == f"Bearer {TOKEN}"
    saved = store.load()
    assert saved is not None and saved.token == TOKEN


def test_login_without_no_open_hands_the_url_only_to_the_opener():
    opened = []
    code, text, _ = run_cli(
        ["login", "--paste"], getpass_fn=lambda prompt: CODE, opener=opened.append
    )
    assert code == 0 and opened == [LOGIN_URL]
    assert LOGIN_URL not in text and CODE not in text and TOKEN not in text


def test_login_survives_an_opener_that_fails_without_printing_the_url():
    def broken(url):
        raise OSError("no browser")

    code, text, _ = run_cli(["login", "--paste"], getpass_fn=lambda p: CODE, opener=broken)
    assert code == 0 and "could not open a browser" in text
    assert LOGIN_URL not in text


def test_login_without_a_terminal_returns_2_before_arming():
    def never():
        raise AssertionError("the relay must not be touched")

    asked = []
    code, text, _ = run_cli(
        ["login", "--paste"], isatty=False, client_factory=never,
        getpass_fn=lambda p: asked.append(p) or CODE,
    )
    assert code == 2 and asked == []
    assert CODE not in text


def test_login_without_paste_is_a_usage_error():
    code, text, handler = run_cli(["login"], getpass_fn=lambda p: CODE)
    assert code == 2 and handler.requests == []


@pytest.mark.parametrize("typed", ["", "   ", "12 34", "abc"])
def test_a_bad_code_is_refused_before_it_is_sent_and_never_echoed(typed):
    code, text, handler = run_cli(["login", "--paste", "--no-open"], getpass_fn=lambda p: typed)
    assert code == 2
    assert all(r.url.path != "/v1/login/complete" for r in handler.requests)
    assert typed.strip() == "" or typed not in text


def test_a_rejected_code_prints_only_the_code_and_the_request_id():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/login/complete":
            return error_response("LOGIN_ATTEMPT_INVALID", 409)
        return Canned()(request)

    code, text, _ = run_cli(["login", "--paste", "--no-open"], handler=handler,
                            getpass_fn=lambda p: CODE)
    assert code == 1
    assert text.splitlines()[-1] == f"LOGIN_ATTEMPT_INVALID (request {RID})"
    assert CODE not in text and TOKEN not in text


def test_the_hidden_prompt_is_getpass_by_default():
    import inspect

    assert inspect.signature(cli_mod.main).parameters["getpass_fn"].default is getpass.getpass


def test_probe_daily_reports_and_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.iterdir())
    code, text, handler = run_cli(
        ["probe-daily", "--stock-code", "RELIND", "--from", "2025-06-02", "--to", "2025-06-13"]
    )
    assert code == 0
    assert text.splitlines() == [f"request_id {RID}", "rows 1", "empty false", "sha256 verified"]
    assert set(tmp_path.iterdir()) == before
    assert dict(handler.requests[0].url.params)["stock_code"] == "RELIND"


def test_probe_daily_counts_rows_and_reports_unknown_for_other_shapes():
    many = json.dumps({"Success": [{}, {}, {}], "Status": 200, "Error": None}).encode()
    args = ["probe-daily", "--stock-code", "RELIND", "--from", "2025-06-02", "--to", "2025-06-13"]
    assert "rows 3" in run_cli(args, handler=lambda r: bars_response(many))[1]
    odd = b'{"Success": "text"}'
    text = run_cli(args, handler=lambda r: bars_response(odd))[1]
    assert "rows unknown" in text and text.splitlines()[-1] == "sha256 verified"
    empty = b'{"Success": []}'
    assert "empty true" in run_cli(args, handler=lambda r: bars_response(empty, **{client_mod.HDR_EMPTY: "true"}))[1]


def test_probe_daily_rejects_bad_dates_and_codes():
    assert run_cli(["probe-daily", "--stock-code", "RELIND", "--from", "x", "--to", "2025-06-13"])[0] == 2
    code, text, handler = run_cli(
        ["probe-daily", "--stock-code", "reliance", "--from", "2025-06-02", "--to", "2025-06-13"]
    )
    assert code == 2 and handler.requests == []
    assert "reliance" not in text


def test_tunnel_prints_the_url_and_a_note_then_stops_on_interrupt():
    fake = _Tunnel(port=50321)

    def interrupt(handle):
        raise KeyboardInterrupt

    out = io.StringIO()
    code = cli_mod.main(
        ["tunnel"], tunnel_factory=lambda cfg, verbose: fake, config_loader=lambda: object(),
        wait=interrupt, out=out,
    )
    assert code == 0 and fake.stopped == 1
    assert out.getvalue().splitlines() == [
        "RELAY_URL=http://127.0.0.1:50321",
        "for manual inspection only; the relay token is not printed",
    ]


def test_tunnel_verbose_reaches_the_factory_and_a_dead_child_is_exit_4():
    seen = []
    fake = _Tunnel()

    def factory(cfg, verbose):
        seen.append(verbose)
        return fake

    def child_dies(handle):
        handle._alive = False

    out = io.StringIO()
    code = cli_mod.main(["tunnel", "--verbose"], tunnel_factory=factory,
                        config_loader=lambda: object(), wait=child_dies, out=out)
    assert code == 4 and seen == [True] and fake.stopped == 1
    assert "tunnel_exited" in out.getvalue()


def test_exit_codes_for_relay_config_and_tunnel_errors():
    code, text, _ = run_cli(["budget"], handler=lambda r: error_response("NO_SESSION", 503))
    assert code == 1 and text.strip() == f"NO_SESSION (request {RID})"

    def bad_config():
        raise ConfigError("bad_mode", "file")

    out = io.StringIO()
    assert cli_mod.main(["tunnel"], config_loader=bad_config, out=out) == 3
    assert "bad_mode" in out.getvalue()

    def no_tunnel():
        raise TunnelError("gcloud_not_found")

    out = io.StringIO()
    assert cli_mod.main(["health"], client_factory=no_tunnel, out=out) == 4
    assert out.getvalue().strip() == "tunnel_error gcloud_not_found"

    def bad_config_for_client():
        raise ConfigError("unreadable", "file")

    assert cli_mod.main(["health"], client_factory=bad_config_for_client, out=io.StringIO()) == 3
    assert cli_mod.main([], out=io.StringIO()) == 2
    assert cli_mod.main(["nonsense"], out=io.StringIO()) == 2


def test_cli_prints_a_relay_error_without_its_parameters():
    def handler(request: httpx.Request) -> httpx.Response:
        return error_response("UPSTREAM_ERROR", 502, reason="no_data", fields=["stock_code"])

    code, text, _ = run_cli(
        ["probe-daily", "--stock-code", "RELIND", "--from", "2025-06-02", "--to", "2025-06-13"],
        handler=handler,
    )
    assert code == 1 and text.strip() == f"UPSTREAM_ERROR (request {RID})"


# ------------------------------------------------------- contract parity

PENDING_ROUTES = {
    "/v1/budget", "/v1/quotes", "/v1/preview-order", "/v1/security-master",
}  # served by the VM from the plan after the relay core; the client already speaks them


def test_contract_string_and_header_names_equal_the_vm():
    assert CONTRACT == client_mod.CONTRACT == vm.CONTRACT == "growin-relay/1"
    pairs = {
        client_mod.HDR_CONTRACT: vm.HDR_CONTRACT,
        client_mod.HDR_REQUEST_ID: vm.HDR_REQUEST_ID,
        client_mod.HDR_CACHE_CONTROL: vm.HDR_CACHE_CONTROL,
        client_mod.HDR_UP_SHA256: vm.HDR_UP_SHA256,
        client_mod.HDR_UP_FETCHED_AT: vm.HDR_UP_FETCHED_AT,
        client_mod.HDR_UP_API: vm.HDR_UP_API,
        client_mod.HDR_UP_REQUEST: vm.HDR_UP_REQUEST,
        client_mod.HDR_UP_INTERVAL: vm.HDR_UP_INTERVAL,
        client_mod.HDR_BREEZE_STATUS: vm.HDR_BREEZE_STATUS,
        client_mod.HDR_EMPTY: vm.HDR_EMPTY,
        client_mod.HDR_AUTHORIZATION: vm.HDR_AUTHORIZATION,
    }
    for mine, theirs in pairs.items():
        assert mine == theirs
    assert len(set(pairs)) == len(pairs)


def test_route_set_equals_the_vm_apart_from_the_routes_still_to_come():
    assert vm.ROUTES <= client_mod.ROUTES
    assert client_mod.ROUTES - vm.ROUTES <= PENDING_ROUTES
    assert {client_mod.ROUTE_HEALTH, client_mod.ROUTE_LOGIN_ARM, client_mod.ROUTE_LOGIN_COMPLETE,
            client_mod.ROUTE_SESSION_REVOKE, client_mod.ROUTE_BARS_DAILY} <= vm.ROUTES


def test_error_codes_and_statuses_equal_the_vm():
    assert set(CODE_TO_CLASS) == set(vm.ERRORS)
    assert {code: status for code, (status, _) in C6.items()} == {
        code: status for code, (status, _) in vm.ERRORS.items()
    }
    for code, (_, retryable) in vm.ERRORS.items():
        assert isinstance(retryable, bool) and code in CODE_TO_CLASS


def test_upstream_names_patterns_and_limits_equal_the_vm():
    assert client_mod.API_BARS_V2 == vm.API_BARS_V2
    assert client_mod.API_QUOTES_V1 == vm.API_QUOTES_V1
    assert client_mod.API_PREVIEW_V1 == vm.API_PREVIEW_V1
    assert client_mod.API_SECURITY_MASTER == vm.API_SECURITY_MASTER
    assert client_mod.DAILY_INTERVAL == vm.DAILY_INTERVAL_SEMANTIC
    assert client_mod.MAX_RANGE_DAYS == vm.MAX_RANGE_DAYS
    for name in ("REQUEST_ID_RE", "ATTEMPT_ID_RE", "STOCK_CODE_RE"):
        assert getattr(client_mod, name).pattern == getattr(vm, name).pattern
    token = vm.BEARER_RE.pattern.removeprefix("^Bearer (").removesuffix(")$")
    assert client_mod.RELAY_TOKEN_RE.pattern.strip("^$") == token
    # the client's code filter is a superset of the VM's, so the VM is the one that decides
    for sample in ("123456", "A1b2C3d4", "9" * 64, "ab" * 32):
        assert vm.API_SESSION_RE.fullmatch(sample) and client_mod.API_SESSION_RE.fullmatch(sample)


# ------------------------------------------------------------ RELAY-API.md

DOC = (ROOT / "gateway" / "RELAY-API.md").read_text()


def test_the_doc_carries_every_contract_heading_and_the_observed_section():
    for number in range(1, 10):
        assert re.search(rf"^### C{number}\. ", DOC, re.M), f"C{number}"
    assert "## Observed upstream behaviour" in DOC
    assert "## Consumers" in DOC and "## Daily use" in DOC


def test_the_doc_names_every_route_header_and_code_the_code_uses():
    for name in [*vm.ROUTES, *client_mod.ROUTES, *vm.ERRORS]:
        assert name in DOC, name
    for header in (client_mod.HDR_CONTRACT, client_mod.HDR_REQUEST_ID, client_mod.HDR_UP_SHA256,
                   client_mod.HDR_UP_FETCHED_AT, client_mod.HDR_UP_API, client_mod.HDR_UP_REQUEST,
                   client_mod.HDR_UP_INTERVAL, client_mod.HDR_BREEZE_STATUS, client_mod.HDR_EMPTY,
                   client_mod.HDR_UP_LAST_MODIFIED):
        assert header in DOC, header
    assert "growin-relay/1" in DOC


def test_the_observed_section_records_the_smoke_findings():
    observed = DOC.split("## Observed upstream behaviour", 1)[1].split("\n## Consumers", 1)[0]
    for needle in (
        "YYYY-MM-DD HH:MM:SS", "00:00:00", "RAW", "NEWEST", "1000", "Boundary days are included",
        "HTTP 200 with a `Status` 500 body is an upstream error", "NIFBEE", "1day",
        "DD-Mon-YYYY HH:MM:SS", "IST",
    ):
        assert needle in observed, needle


def test_the_doc_holds_no_account_value():
    assert not re.search(r"[\w.+-]+@[\w-]+\.[A-Za-z]{2,}", DOC)  # no email
    assert set(re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", DOC)) <= {
        "127.0.0.1",
        "35.235.240.0",
        "0.0.0.0",  # named in C1 as an address the relay refuses
    }
    assert not re.search(r"[A-Za-z0-9_-]{40,}", DOC)  # no token or key shaped run
    ssh_line = "gcloud compute " + "ssh"
    internal_flag = "--internal" + "-ip"
    assert not re.search(
        rf"(?i){ssh_line}|{internal_flag}|\.planning|api_key=[A-Za-z0-9]", DOC
    )


def test_the_daily_use_block_runs_only_the_cli_and_every_command_exists():
    block = DOC.split("## Daily use", 1)[1].split("```")[1]
    commands = [line for line in block.splitlines() if line and line != "export PYTHONPATH=backend"]
    assert commands and all(
        line.startswith("uv run --project backend --no-sync python -m brokers.breeze_gateway.cli ")
        for line in commands
    )
    known = set(cli_mod._parser()._subparsers._group_actions[0].choices)
    assert {line.split("breeze_gateway.cli ")[1].split()[0] for line in commands} == known
