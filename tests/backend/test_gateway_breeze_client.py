"""BreezeClient: documented checksum, exact requests, fail-closed normalization,
and the structural guarantee that no order code path exists.

No network. Everything runs against a recording fake transport.
"""

from __future__ import annotations

import ast
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "gateway" / "vm") not in sys.path:
    sys.path.insert(0, str(ROOT / "gateway" / "vm"))

from gateway_vm import breeze_client as bc  # noqa: E402
from gateway_vm.transport import (  # noqa: E402
    DEFAULT_ALLOWED_HOSTS,
    HttpResponse,
    Timeouts,
    TransportError,
)

PKG = ROOT / "gateway" / "vm" / "gateway_vm"
NOW = datetime(2026, 10, 1, 10, 23, 56, 999000, tzinfo=timezone.utc)


class Clock:
    def monotonic(self) -> float:
        return 0.0

    def now_utc(self) -> datetime:
        return NOW


class Fake:
    def __init__(self, response: HttpResponse | Exception) -> None:
        self.response = response
        self.requests: list[tuple[str, str, dict, bytes | None]] = []

    def request(self, method, url, *, headers, body, timeouts, max_bytes):
        self.requests.append((method, url, dict(headers), body))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def client_for(response) -> tuple[bc.BreezeClient, Fake]:
    fake = Fake(response)
    return bc.BreezeClient(fake, clock=Clock()), fake


def body(success, status=200, error=None) -> bytes:
    return json.dumps({"Success": success, "Status": status, "Error": error}).encode()


GOOD_CUSTOMER = {
    "idirect_userid": "FAKE1",
    "session_token": "RkFLRTE6MTIzNA==",
    "segments_allowed": {"Trading": "Y", "Equity": "Y", "Extra": "Y"},
    "exg_trade_date": {"NSE": "01-Oct-2026"},
}

CALLS = {
    "customer_details": lambda c: c.customer_details(app_key="K", api_session="S"),
    "daily_bars_v2": lambda c: c.daily_bars_v2(
        app_key="K", session_token="T", stock_code="RELIND", exch_code="NSE",
        from_iso="2025-06-09T00:00:00.000Z", to_iso="2025-06-20T23:59:59.000Z",
    ),
    "quote": lambda c: c.quote(
        app_key="K", secret_key="X", session_token="T", stock_code="RELIND", exchange_code="NSE"
    ),
    "preview_order": lambda c: c.preview_order(
        app_key="K", secret_key="X", session_token="T", stock_code="RELIND",
        exchange_code="NSE", action="buy", quantity=1, price="1402.30",
    ),
}
RAW_CALLS = {
    "daily_bars_v2": lambda c: c.daily_bars_v2_raw(
        app_key="K", session_token="T", stock_code="RELIND", exch_code="NSE",
        from_iso="2025-06-09T00:00:00.000Z", to_iso="2025-06-20T23:59:59.000Z",
    ),
    "quote": lambda c: c.quote_raw(
        app_key="K", secret_key="X", session_token="T", stock_code="RELIND", exchange_code="NSE"
    ),
    "preview_order": lambda c: c.preview_order_raw(
        app_key="K", secret_key="X", session_token="T", stock_code="RELIND",
        exchange_code="NSE", action="sell", quantity=3, price="1402.30",
    ),
    "customer_details": lambda c: c.customer_details_raw(app_key="K", api_session="S"),
}


# ---------------------------------------------------------------- checksum


def test_checksum_vector():
    assert (
        bc.checksum("2026-10-01T10:23:56.000Z", "{}", "EXAMPLE_SECRET")
        == "c6cd10b3ed73c49fb1ee40dc3137cc1026a2876526001a1617b4335152a937ae"
    )


def test_timestamp_truncates_never_rounds():
    assert bc.breeze_timestamp(NOW) == "2026-10-01T10:23:56.000Z"


def test_timestamp_needs_tz_aware():
    with pytest.raises(ValueError):
        bc.breeze_timestamp(datetime(2026, 10, 1, 10, 23, 56))


# ---------------------------------------------------------- request shapes


@pytest.mark.parametrize("name", ["quote", "preview_order"])
def test_checksummed_get_sends_exact_hashed_body(name):
    client, fake = client_for(HttpResponse(200, {}, body({"ltp": 1})))
    CALLS[name](client)
    method, url, headers, sent = fake.requests[0]
    assert method == "GET"
    assert url == bc.V1_BASE + ("quotes" if name == "quote" else "preview_order")
    text = sent.decode()
    assert text == bc.compact_json(json.loads(text))  # compact, nothing re-encoded
    ts = "2026-10-01T10:23:56.000Z"
    assert headers["X-Timestamp"] == ts
    assert headers["X-Checksum"] == "token " + bc.checksum(ts, text, "X")
    assert headers["X-AppKey"] == "K"
    assert headers["X-SessionToken"] == "T"
    assert headers["Content-Type"] == "application/json"
    assert headers["User-Agent"] == bc.SDK_USER_AGENT
    assert "X" not in headers.values()  # the secret key is never sent


def test_quote_body_fields():
    client, fake = client_for(HttpResponse(200, {}, body({"ltp": 1})))
    CALLS["quote"](client)
    assert json.loads(fake.requests[0][3]) == {
        "stock_code": "RELIND", "exchange_code": "NSE", "expiry_date": "",
        "product_type": "cash", "right": "", "strike_price": "",
    }


def test_preview_order_body_fields():
    client, fake = client_for(HttpResponse(200, {}, body({"brokerage": 1})))
    CALLS["preview_order"](client)
    assert json.loads(fake.requests[0][3]) == {
        "stock_code": "RELIND", "exchange_code": "NSE", "product": "cash",
        "order_type": "limit", "price": "1402.30", "action": "buy", "quantity": "1",
        "specialflag": "N",
    }


@pytest.mark.parametrize(
    "kwargs",
    [
        {"action": "hold"},
        {"quantity": 0},
        {"quantity": -2},
        {"quantity": True},
        {"quantity": 1.5},
        {"price": "1402.305"},
        {"price": "abc"},
        {"price": "0.00"},
        {"price": 1402.3},
    ],
)
def test_preview_order_validates_before_any_request(kwargs):
    client, fake = client_for(HttpResponse(200, {}, body({})))
    args = dict(
        app_key="K", secret_key="X", session_token="T", stock_code="RELIND",
        exchange_code="NSE", action="buy", quantity=1, price="1402.30",
    )
    args.update(kwargs)
    with pytest.raises(ValueError):
        client.preview_order(**args)
    assert fake.requests == []
    assert client.calls == 0


def test_daily_bars_v2_request_shape():
    client, fake = client_for(HttpResponse(200, {}, body([])))
    CALLS["daily_bars_v2"](client)
    method, url, headers, sent = fake.requests[0]
    assert method == "GET"
    assert sent is None
    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == bc.V2_HISTORICAL_URL
    assert "%3A" not in parts.query  # colons stay literal
    assert dict(parse_qsl(parts.query)) == {
        "stock_code": "RELIND", "exch_code": "NSE",
        "from_date": "2025-06-09T00:00:00.000Z", "to_date": "2025-06-20T23:59:59.000Z",
        "interval": "1day", "product_type": "cash",
    }
    assert headers["apikey"] == "K"
    assert headers["X-SessionToken"] == "T"
    assert "X-Checksum" not in headers


def test_customer_details_request_has_no_checksum():
    client, fake = client_for(HttpResponse(200, {}, body(GOOD_CUSTOMER)))
    details = CALLS["customer_details"](client)
    method, url, headers, sent = fake.requests[0]
    assert (method, url) == ("GET", bc.V1_BASE + "customerdetails")
    assert sent == bc.compact_json({"SessionToken": "S", "AppKey": "K"}).encode()
    assert "X-Checksum" not in headers
    assert details.segments == {"Trading": "Y", "Equity": "Y"}  # unknown keys dropped
    assert details.exg_trade_date_nse == "01-Oct-2026"
    assert "FAKE1" not in repr(details) and "RkFLRTE6" not in repr(details)


def test_customer_details_requires_userid_and_token():
    for missing in ("idirect_userid", "session_token"):
        data = {k: v for k, v in GOOD_CUSTOMER.items() if k != missing}
        client, _ = client_for(HttpResponse(200, {}, body(data)))
        with pytest.raises(bc.BreezeError) as err:
            CALLS["customer_details"](client)
        assert err.value.code == "UPSTREAM_SHAPE"


def test_security_master_sends_user_agent_only():
    client, fake = client_for(HttpResponse(200, {}, b"PK\x03\x04"))
    raw = client.security_master_raw(
        "https://directlink.icicidirect.com/NewSecurityMaster/SecurityMaster.zip",
        timeouts=Timeouts(), max_bytes=64 * 1024 * 1024,
    )
    assert raw.body == b"PK\x03\x04"
    assert fake.requests[0][2] == {"User-Agent": bc.SDK_USER_AGENT}


# --------------------------------------------------- fail-closed normalizing

BAD_SHAPES = {
    "http200_status5": (200, body(None, 5, "Public Key does not exist."), "public_key_missing"),
    "http401_status5": (401, body(None, 5, "Public Key does not exist."), "public_key_missing"),
    "error_with_status200": (200, body({"a": 1}, 200, "Invalid session."), "invalid_session"),
    "success_null": (200, body(None, 200, None), "other"),
    "success_empty_string": (200, body("", 200, None), "other"),
    "http500_good_body": (500, body({"a": 1}, 200, None), "other"),
    "status_is_string": (200, body({"a": 1}, "200", None), "other"),
}


@pytest.mark.parametrize("method", list(CALLS))
@pytest.mark.parametrize("shape", list(BAD_SHAPES))
def test_every_method_raises_on_bad_shapes(method, shape):
    http, payload, error_class = BAD_SHAPES[shape]
    client, _ = client_for(HttpResponse(http, {}, payload))
    with pytest.raises(bc.BreezeError) as err:
        CALLS[method](client)
    assert err.value.code == "UPSTREAM_ERROR"
    assert err.value.error_class == error_class
    assert err.value.http_status == http


@pytest.mark.parametrize("method", list(CALLS))
def test_non_json_body_is_shape_error(method):
    client, _ = client_for(HttpResponse(200, {}, b"<html>bad gateway</html>"))
    with pytest.raises(bc.BreezeError) as err:
        CALLS[method](client)
    assert err.value.code == "UPSTREAM_SHAPE"


def test_empty_list_success_is_legal_for_bars():
    client, _ = client_for(HttpResponse(200, {}, body([])))
    assert CALLS["daily_bars_v2"](client) == []


def test_bars_success_must_be_a_list():
    client, _ = client_for(HttpResponse(200, {}, body({"not": "a list"})))
    with pytest.raises(bc.BreezeError) as err:
        CALLS["daily_bars_v2"](client)
    assert err.value.code == "UPSTREAM_SHAPE"


def test_breeze_error_never_keeps_the_raw_error_text():
    secret_text = "boom contains-a-private-detail"
    client, _ = client_for(HttpResponse(200, {}, body(None, 5, secret_text)))
    with pytest.raises(bc.BreezeError) as err:
        CALLS["quote"](client)
    assert str(err.value) == "UPSTREAM_ERROR:other"
    assert secret_text not in repr(err.value) and secret_text not in str(err.value.args)
    assert secret_text not in repr(vars(err.value))


@pytest.mark.parametrize("method", list(RAW_CALLS))
@pytest.mark.parametrize("shape", list(BAD_SHAPES))
def test_raw_methods_return_exact_bytes_and_classify(method, shape):
    http, payload, error_class = BAD_SHAPES[shape]
    client, _ = client_for(HttpResponse(http, {}, payload))
    raw = RAW_CALLS[method](client)
    assert raw.http_status == http
    assert raw.body == payload
    assert raw.fetched_at_utc == NOW
    verdict = bc.classify_payload(raw.http_status, raw.body)
    assert verdict.ok is False
    assert verdict.error_class == error_class


def test_classify_payload_good_and_unparsable():
    good = bc.classify_payload(200, body({"a": 1}))
    assert (good.ok, good.breeze_status, good.error_class) == (True, 200, None)
    bad = bc.classify_payload(200, b"nope")
    assert (bad.ok, bad.breeze_status, bad.error_class) == (False, None, "other")
    assert bc.classify_payload(200, b"[1,2]").ok is False


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Invalid session.", "invalid_session"),
        ("Resource not available.", "resource_not_available"),
        ("Public Key does not exist.", "public_key_missing"),
        ("The IP address used does not match the registered IP", "ip_mismatch"),
        ("No Data Found", "no_data"),
        ("something else", "other"),
        (None, "other"),
        (5, "other"),
    ],
)
def test_classify_error_text(text, expected):
    assert bc.classify_error_text(text) == expected
    assert expected in bc.ERROR_CLASSES


def test_transport_failures_map_to_upstream_codes():
    client, _ = client_for(TransportError("timeout", "api.icicidirect.com"))
    with pytest.raises(bc.BreezeError) as err:
        CALLS["quote"](client)
    assert err.value.code == "UPSTREAM_TIMEOUT"
    for kind in ("connect", "tls", "redirect", "too_large", "protocol"):
        client, _ = client_for(TransportError(kind, "api.icicidirect.com"))
        with pytest.raises(bc.BreezeError) as err:
            RAW_CALLS["quote"](client)
        assert err.value.code == "UPSTREAM_UNREACHABLE"


def test_every_call_is_counted():
    client, _ = client_for(HttpResponse(200, {}, body({"ltp": 1})))
    CALLS["quote"](client)
    RAW_CALLS["quote"](client)
    assert client.calls == 2


# ------------------------------------------------------------ no order path


def _modules():
    return sorted(PKG.glob("*.py"))


def test_no_order_function_names():
    banned = ("place", "cancel", "modify", "squareoff")
    offences = []
    for path in _modules():
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if any(word in node.name.lower() for word in banned):
                    offences.append(f"{path.name}:{node.name}")
    assert offences == []


def test_no_order_endpoint_constants():
    banned = {
        "order", "squareoff", "trades", "funds", "margin", "portfolioholdings",
        "portfoliopositions", "dematholdings", "gttorder",
    }
    offences = []
    for path in _modules():
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value.strip().lower() in banned:
                    offences.append(f"{path.name}:{node.lineno}")
                if "/breezeapi/api/v1/" in node.value and node.value != bc.V1_BASE:
                    offences.append(f"{path.name}:{node.lineno}:v1 url")
    assert offences == []


def test_v1_endpoint_allowlist_is_exact():
    assert bc.V1_ENDPOINTS == frozenset({"customerdetails", "quotes", "preview_order"})


def test_every_public_method_is_get_only_to_allowlisted_paths():
    allowed_paths = {"/breezeapi/api/v1/" + e for e in bc.V1_ENDPOINTS}
    allowed_paths.add("/api/v2/historicalcharts")
    allowed_paths.add("/NewSecurityMaster/SecurityMaster.zip")
    client, fake = client_for(HttpResponse(200, {}, body([{"ltp": 1}])))
    for call in list(CALLS.values()) + list(RAW_CALLS.values()):
        try:
            call(client)
        except bc.BreezeError:
            pass
    client.security_master_raw(
        "https://directlink.icicidirect.com/NewSecurityMaster/SecurityMaster.zip",
        timeouts=Timeouts(), max_bytes=1024,
    )
    assert fake.requests
    for method, url, _headers, _body in fake.requests:
        parts = urlsplit(url)
        assert method == "GET"
        assert parts.hostname in DEFAULT_ALLOWED_HOSTS
        assert parts.path in allowed_paths
