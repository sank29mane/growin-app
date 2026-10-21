"""GET /v1/bars/intraday through the real FastAPI app (64-02 Task 2, design 5.1).

Only the network is faked, as in test_gateway_relay_app.py. No order of any kind, no real
credentials, nothing written to disk.
"""

from __future__ import annotations

import builtins
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from test_gateway_relay_app import (
    APP_KEY,
    BREEZE_TOKEN,
    CONTRACT_HDR,
    BudgetDouble,
    HttpResponse,
    Stack,
    assert_envelope,
    auth,
    code_of,
    contract,
)
from gateway_vm.session import BudgetExceeded

QUERY = "stock_code=RELIND&date=2026-10-21&interval=1minute"
ROUTE = "/v1/bars/intraday"
URL = f"{ROUTE}?{QUERY}"


def minute_rows(count: int) -> list[dict]:
    return [
        {
            "datetime": f"2026-10-21 {9 + (15 + n) // 60:02d}:{(15 + n) % 60:02d}:00",
            "stock_code": "RELIND",
            "exchange_code": "NSE",
            "open": 1400.5,
            "high": 1401.0,
            "low": 1400.0,
            "close": 1400.75,
            "volume": 100 + n,
        }
        for n in range(count)
    ]


def body_of(rows: list[dict]) -> bytes:
    return json.dumps({"Success": rows, "Status": 200, "Error": None}, separators=(",", ":")).encode()


def get(stack: Stack, token: str | None, url: str = URL):
    return stack.client.get(url, headers={**CONTRACT_HDR, **auth(token)})


@pytest.fixture()
def stack() -> Stack:
    return Stack()


def test_tracer_headers_and_exact_bytes(stack: Stack):
    token = stack.login()
    rows = body_of(minute_rows(375))
    stack.transport.bars = HttpResponse(200, {}, rows)
    resp = get(stack, token)
    assert resp.status_code == 200
    assert resp.content == rows  # the exact upstream bytes, not re-encoded
    assert resp.headers[contract.HDR_UP_SHA256] == hashlib.sha256(rows).hexdigest()
    assert resp.headers[contract.HDR_UP_API] == "breeze-historicalcharts-v2"
    assert resp.headers[contract.HDR_UP_INTERVAL] == "1minute"
    assert resp.headers[contract.HDR_BREEZE_STATUS] == "200"
    assert resp.headers[contract.HDR_EMPTY] == "false"
    assert resp.headers[contract.HDR_UP_FETCHED_AT] == "2026-10-21T04:30:00.000Z"
    assert_envelope(resp)


def test_upstream_request_is_v2_cash_one_minute_whole_day(stack: Stack):
    token = stack.login()
    resp = get(stack, token)
    method, url, headers, body = stack.transport.bars_requests()[-1]
    sent = dict(parse_qsl(urlsplit(url).query))
    assert method == "GET" and body is None
    assert urlsplit(url).hostname == "breezeapi.icicidirect.com"  # no new upstream host
    assert sent == {
        "exch_code": "NSE",
        "from_date": "2026-10-21T00:00:00.000Z",
        "interval": "1minute",
        "product_type": "cash",
        "stock_code": "RELIND",
        "to_date": "2026-10-21T23:59:59.000Z",
    }
    assert json.loads(resp.headers[contract.HDR_UP_REQUEST]) == sent
    assert headers["apikey"] == APP_KEY and headers["X-SessionToken"] == BREEZE_TOKEN
    assert APP_KEY not in resp.headers[contract.HDR_UP_REQUEST]
    assert BREEZE_TOKEN not in resp.headers[contract.HDR_UP_REQUEST]


def test_empty_answer_is_flagged_not_padded(stack: Stack):
    token = stack.login()
    stack.transport.bars = HttpResponse(200, {}, body_of([]))
    resp = get(stack, token)
    assert resp.status_code == 200
    assert resp.headers[contract.HDR_EMPTY] == "true"
    assert json.loads(resp.content)["Success"] == []


def test_capped_answer_passes_through_unpadded(stack: Stack):
    token = stack.login()
    rows = body_of(minute_rows(1000))
    stack.transport.bars = HttpResponse(200, {}, rows)
    resp = get(stack, token)
    assert resp.content == rows and len(json.loads(resp.content)["Success"]) == 1000
    assert resp.headers[contract.HDR_EMPTY] == "false"


# ------------------------------------------------------- order of checks (401, 422, 503)


def test_401_without_a_token_before_anything_else(stack: Stack):
    stack.login()
    n = len(stack.transport.requests)
    charged = len(stack.budget.kinds)
    resp = get(stack, None, f"{ROUTE}?stock_code=bad")  # a bad query too: 401 wins
    assert resp.status_code == 401 and code_of(resp) == "UNAUTHORIZED"
    assert len(stack.transport.requests) == n and len(stack.budget.kinds) == charged


def test_401_on_a_good_query_without_a_token_reaches_nothing(stack: Stack):
    stack.login()
    n = len(stack.transport.requests)
    charged = len(stack.budget.kinds)
    resp = get(stack, None)  # a valid query: only the missing token refuses it
    assert resp.status_code == 401 and code_of(resp) == "UNAUTHORIZED"
    assert len(stack.transport.requests) == n and len(stack.budget.kinds) == charged
    assert "RELIND" not in resp.text


@pytest.mark.parametrize("presented", ["not-the-relay-token", "x" * 64, "", "A" * 43, "-_" * 21 + "-"])  # the last two are well formed
def test_401_with_a_wrong_token_on_a_good_query_reaches_nothing(stack: Stack, presented):
    token = stack.login()
    assert presented != token
    n = len(stack.transport.requests)
    charged = len(stack.budget.kinds)
    resp = get(stack, presented)
    assert resp.status_code == 401 and code_of(resp) == "UNAUTHORIZED"
    assert len(stack.transport.requests) == n and len(stack.budget.kinds) == charged
    assert presented not in resp.text or presented == ""


def test_401_with_a_non_bearer_scheme_reaches_nothing(stack: Stack):
    token = stack.login()
    n = len(stack.transport.requests)
    resp = stack.client.get(URL, headers={**CONTRACT_HDR, "Authorization": f"Basic {token}"})
    assert resp.status_code == 401
    assert len(stack.transport.requests) == n


def test_401_for_a_token_from_an_earlier_login(stack: Stack):
    old = stack.login()
    stack.revoke(old)
    fresh = stack.login()
    assert fresh != old
    n = len(stack.transport.requests)
    assert get(stack, old).status_code == 401  # the old relay token is dead, not just the session
    assert len(stack.transport.requests) == n
    assert get(stack, fresh).status_code == 200


@pytest.mark.parametrize(
    "query,fields",
    [
        ("date=2026-10-21&interval=1minute", ["stock_code"]),
        ("stock_code=relind&date=2026-10-21&interval=1minute", ["stock_code"]),
        ("stock_code=RELIND&interval=1minute", ["date"]),
        ("stock_code=RELIND&date=2026-10-21", ["interval"]),
        ("stock_code=RELIND&date=2026-10-21&interval=1day", ["interval"]),
        ("stock_code=RELIND&date=2026-10-21&interval=5minute", ["interval"]),
        ("stock_code=RELIND&date=2026-10-21&interval=1Minute", ["interval"]),
        ("stock_code=RELIND&date=2026-10-22&interval=1minute", ["date"]),  # after today IST
        ("stock_code=RELIND&date=2026-13-01&interval=1minute", ["date"]),
        ("stock_code=RELIND&date=2026-1-5&interval=1minute", ["date"]),
        ("stock_code=RELIND&date=2026-10-21&interval=1minute&exchange=BSE", ["exchange"]),
        ("stock_code=RELIND&stock_code=TCS&date=2026-10-21&interval=1minute", ["stock_code"]),
        ("stock_code=RELIND&date=2026-10-21&interval=1minute&from=2026-10-20", ["unknown"]),
        ("stock_code=RELIND&date=2026-10-21&interval=1minute&product_type=margin", ["unknown"]),
    ],
)
def test_422_names_only_and_no_breeze_call(stack: Stack, query, fields):
    token = stack.login()
    n = len(stack.transport.requests)
    charged = len(stack.budget.kinds)
    resp = get(stack, token, f"{ROUTE}?{query}")
    assert resp.status_code == 422 and code_of(resp) == "VALIDATION"
    assert resp.json()["error"]["fields"] == fields
    assert len(stack.transport.requests) == n  # nothing reached Breeze
    assert len(stack.budget.kinds) == charged  # and nothing was charged
    for value in ("relind", "1day", "5minute", "BSE", "margin", "2026-13-01"):
        assert value not in resp.text


def test_today_ist_is_accepted_and_the_day_is_read_from_the_clock_not_the_host(stack: Stack):
    token = stack.login()
    assert get(stack, token, f"{ROUTE}?stock_code=RELIND&date=2026-10-21&interval=1minute").status_code == 200
    stack.clock.advance(20 * 3600)  # 2026-10-22 00:00 IST has passed
    # the clock moved to 2026-10-22 IST, so that date is no longer "after today"; the session
    # itself has expired at midnight, which is a 503, not a 422
    moved = get(stack, token, f"{ROUTE}?stock_code=RELIND&date=2026-10-22&interval=1minute")
    assert moved.status_code != 422


def test_503_after_revoke_and_no_breeze_call(stack: Stack):
    token = stack.login()
    stack.revoke(token)
    n = len(stack.transport.requests)
    charged = len(stack.budget.kinds)
    resp = get(stack, token)
    assert resp.status_code == 503 and code_of(resp) == "NO_SESSION"
    assert len(stack.transport.requests) == n and len(stack.budget.kinds) == charged


def test_validation_precedes_the_session_check(stack: Stack):
    token = stack.login()
    stack.revoke(token)
    resp = get(stack, token, f"{ROUTE}?stock_code=bad")
    assert resp.status_code == 422  # same order as /v1/bars/daily: 401, 422, then 503


# ------------------------------------------------------------------ the day counter


def test_one_charge_per_request_before_the_breeze_call(stack: Stack):
    token = stack.login()
    charged = len(stack.budget.kinds)
    for _ in range(3):
        assert get(stack, token).status_code == 200
    assert len(stack.budget.kinds) == charged + 3
    assert len(stack.transport.bars_requests()) == 3


def test_budget_refusal_stops_before_breeze(stack: Stack):
    token = stack.login()
    n = len(stack.transport.bars_requests())
    stack.budget.raises = BudgetExceeded("budget_minute", 30)
    resp = get(stack, token)
    assert resp.status_code == 429
    assert len(stack.transport.bars_requests()) == n


def test_charge_precedes_the_breeze_call(stack: Stack):
    token = stack.login()
    order: list[str] = []
    real_charge = stack.budget.charge

    def charge(kind: str) -> None:
        order.append("charge")
        real_charge(kind)

    stack.budget.charge = charge  # type: ignore[method-assign]
    original = stack.transport.request

    def request(method, url, **kwargs):
        if urlsplit(url).hostname == "breezeapi.icicidirect.com":
            order.append("breeze")
        return original(method, url, **kwargs)

    stack.transport.request = request  # type: ignore[method-assign]
    assert get(stack, token).status_code == 200
    assert order == ["charge", "breeze"]


def test_budget_double_is_the_shared_one():
    assert BudgetDouble().snapshot() == {}  # the harness double the tests above rely on


# ------------------------------------------------------------------------ nothing on disk


def test_route_writes_nothing_to_disk(stack: Stack, tmp_path: Path, monkeypatch):
    token = stack.login()
    monkeypatch.chdir(tmp_path)
    real_open = builtins.open

    def guarded_open(file, mode="r", *args, **kwargs):
        if set(mode) & set("wax+"):
            raise AssertionError("the intraday route tried to open a file for writing")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    for name in ("write", "replace", "rename", "mkdir", "makedirs"):
        monkeypatch.setattr(os, name, _forbid(name))
    assert get(stack, token).status_code == 200
    monkeypatch.undo()
    assert list(tmp_path.iterdir()) == []


def _forbid(name: str):
    def refuse(*args, **kwargs):
        raise AssertionError(f"os.{name} called by the intraday route")

    return refuse


# --------------------------------------------------------------------------- upstream errors


@pytest.mark.parametrize("status", [401, 429, 500, 503])
def test_upstream_errors_map_like_the_daily_route(stack: Stack, status):
    token = stack.login()
    stack.transport.bars = HttpResponse(status, {}, b'{"Success":null,"Status":%d,"Error":"x"}' % status)
    daily = stack.bars(token)
    intraday = get(stack, token)
    assert intraday.status_code == daily.status_code
    assert code_of(intraday) == code_of(daily)
    assert_envelope(intraday)


def test_null_success_is_an_error_never_an_empty_answer(stack: Stack):
    token = stack.login()
    stack.transport.bars = HttpResponse(200, {}, b'{"Success":null,"Status":200,"Error":null}')
    resp = get(stack, token)
    assert resp.status_code >= 400
    assert resp.headers.get(contract.HDR_EMPTY) is None


# ------------------------------------------------------------------------------- contract


def test_route_is_in_the_contract_and_the_query_allowlist():
    assert contract.ROUTE_BARS_INTRADAY == ROUTE
    assert ROUTE in contract.ROUTES
    assert contract.ALLOWED_QUERY_BY_ROUTE[ROUTE] == ("stock_code", "date", "interval", "exchange")
    assert contract.INTRADAY_INTERVALS == ("1minute",)
    assert contract.CONTRACT == "growin-relay/1"  # additive route, no version bump (C8)


def test_event_log_names_the_route_and_holds_no_query_values(stack: Stack):
    token = stack.login()
    get(stack, token)
    lines = [e for e in stack.lines() if e["event"] == "request" and e.get("route") == ROUTE]
    assert lines and lines[-1]["result"] == "ok"
    blob = stack.sink.getvalue()
    for value in (APP_KEY, BREEZE_TOKEN, "2026-10-21T00:00:00"):
        assert value not in blob
