"""growin-relay/1 as Phase 59 copied it: parity with Phase 61's client, payload re-validation,
gating and failure classification. The fake relay here is also used by the backfill tests.

Everything runs through Phase 61's real RelayClient over httpx.MockTransport. Nothing here
reaches a network, a tunnel or a broker.
"""

import ast
import dataclasses
import hashlib
import inspect
import json
import shutil
import sys
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

import brokers.breeze_gateway as gw
from brokers.breeze_gateway import client as client_mod
from brokers.breeze_gateway import errors as errors_mod
from brokers.breeze_gateway import (
    BudgetExhausted,
    BudgetUnavailable,
    ContractMismatch,
    EgressMismatch,
    ForbiddenClient,
    IdentityMismatch,
    InternalRelayError,
    LoginAttemptInvalid,
    MethodNotAllowed,
    NoSession,
    NotFound,
    RelayBudget,
    RelayClient,
    RelayContractMismatch,
    RelayError,
    RelayHashMismatch,
    RelayHealth,
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

from pilot_data import relay_contract as rc
from pilot_data.core import PilotDataError
from pilot_data.sessions import ensure_fetch_log
from pilot_data.store import PilotDataStore

import pilot_data_testkit as kit

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "gateway" / "vm") not in sys.path:
    sys.path.insert(0, str(ROOT / "gateway" / "vm"))
from gateway_vm import contract as vm  # noqa: E402

BASE = "http://127.0.0.1:8443"
CONTRACT = "growin-relay/1"
FETCHED = "2026-10-21T04:30:00.000Z"
TODAY = date(2026, 10, 21)
LATER = datetime(2030, 1, 1, 12, 0, tzinfo=timezone.utc)
CALENDAR_START = date(2021, 9, 1)
CALENDAR_END = date(2026, 10, 20)
HOLIDAYS = frozenset(
    {date(2022, 1, 26), date(2024, 1, 26), date(2025, 3, 14), date(2026, 1, 26), date(2026, 10, 2)}
)


def is_session(day: date) -> bool:
    return day.weekday() < 5 and day not in HOLIDAYS


def sessions(start: date, end: date) -> list[date]:
    out, day = [], start
    while day <= end:
        if is_session(day):
            out.append(day)
        day += timedelta(days=1)
    return out


# ----------------------------------------------------------------- calendar template


@pytest.fixture(scope="session")
def calendar_template(tmp_path_factory):
    """A store root whose NSE calendar is known for 2021-09-01 to 2026-10-20 (copied per test)."""
    root = tmp_path_factory.mktemp("calendar_template") / "pilot"
    with PilotDataStore(root, workspace="india") as store:
        ensure_fetch_log(store)
        rows = []

        def add(day: date, kind: str, outcome: str) -> None:
            attempt = uuid.uuid4().hex
            rows.append(
                {
                    "attempt_id": attempt, "trade_date": day, "file_kind": kind, "outcome": outcome,
                    "http_status": 200 if outcome == "ingested" else 404, "error_code": None,
                    "source_sha256": None, "url": "u", "attempted_at_utc": LATER.replace(tzinfo=None),
                    "row_sha256": hashlib.sha256(attempt.encode()).hexdigest(),
                }
            )

        day = CALENDAR_START
        while day <= CALENDAR_END:
            if is_session(day):
                add(day, "udiff", "ingested")
            elif day.weekday() >= 5:
                add(day, "pr", "no_file")
            else:
                for kind in ("pr", "udiff", "cm_legacy"):
                    add(day, kind, "no_file")
            day += timedelta(days=1)
        store.append_rows("nse_fetch_log", rows, check="nse_fetch_log")
    return root


@pytest.fixture
def store(calendar_template, tmp_path):
    root = tmp_path / "pilot"
    shutil.copytree(calendar_template, root)
    with PilotDataStore(root, workspace="india") as opened:
        yield opened


# ----------------------------------------------------------------------- fake relay


def sha(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def err_response(
    status: int, code: str, reason: str | None = None, *, retryable: bool = False, retry_after: int | None = None
) -> httpx.Response:
    body = {
        "contract": CONTRACT,
        "error": {
            "code": code, "reason": reason, "breeze_status": None, "retryable": retryable,
            "retry_after_s": retry_after, "fields": None,
        },
    }
    return httpx.Response(
        status,
        headers={
            "X-Growin-Relay-Contract": CONTRACT, "X-Growin-Relay-Request-Id": "00000000000000ee",
            "Cache-Control": "no-store", "Content-Type": "application/json",
        },
        content=json.dumps(body).encode(),
    )


def default_bars_body(params: dict[str, str], cutoff: date | None = None) -> bytes:
    """What the real upstream returns: sessions only, 00:00:00 stamps, raw prices, newest 1000 kept."""
    start, end = date.fromisoformat(params["from"]), date.fromisoformat(params["to"])
    days = sessions(start, end)
    if cutoff is not None:
        days = [day for day in days if day >= cutoff]
    days = days[-1000:]
    rows = []
    for day in days:
        base = 100 + day.toordinal() % 37
        rows.append(
            kit.breeze_row(
                params["stock_code"], f"{day.isoformat()} 00:00:00", f"{base}.00", f"{base + 2}.00",
                f"{base - 1}.00", f"{base + 1}.00", volume=1000 + day.toordinal() % 91,
            )
        )
    return kit.breeze_v2_json(rows)


def upstream_request_header(params: dict[str, str]) -> str:
    return json.dumps(
        {
            "exch_code": "NSE", "from_date": f"{params['from']}T00:00:00.000Z", "interval": "1day",
            "product_type": "cash", "stock_code": params["stock_code"], "to_date": f"{params['to']}T23:59:59.000Z",
        },
        sort_keys=True, separators=(",", ":"),
    )


class FakeRelay:
    """A growin-relay/1 server for httpx.MockTransport. Scripts override single answers."""

    def __init__(self) -> None:
        self.rid = 0
        self.health_state = "active"
        self.egress_ok = True
        self.ist_date = TODAY.isoformat()
        self.remaining = 4000
        self.budget_response: httpx.Response | None = None
        self.health_response: httpx.Response | None = None
        self.fetched_at = FETCHED
        self.bars_requests: list[dict[str, str]] = []
        self.requests: list[str] = []
        self.bars_script = None  # (index, params) -> None | bytes | httpx.Response | Exception
        self.master_script = None
        self.master_rows = list(kit.MASTER_SAMPLE_ROWS)
        self.on_bars = None
        self.depth_cutoff: date | None = None

    def next_rid(self) -> str:
        self.rid += 1
        return f"{self.rid:016x}"

    def headers(self, ctype: str = "application/json") -> dict[str, str]:
        return {
            "X-Growin-Relay-Contract": CONTRACT, "X-Growin-Relay-Request-Id": self.next_rid(),
            "Cache-Control": "no-store", "Content-Type": ctype,
        }

    def json_ok(self, payload: dict) -> httpx.Response:
        return httpx.Response(200, headers=self.headers(), content=json.dumps({"contract": CONTRACT, **payload}).encode())

    def bars_ok(self, params: dict[str, str], body: bytes, **override: str) -> httpx.Response:
        try:
            empty = json.loads(body)["Success"] == []
        except (ValueError, KeyError, TypeError):  # a malformed body, served on purpose by a test
            empty = False
        headers = {
            **self.headers(),
            "X-Growin-Upstream-Sha256": sha(body), "X-Growin-Upstream-Fetched-At": self.fetched_at,
            "X-Growin-Upstream-Api": "breeze-historicalcharts-v2",
            "X-Growin-Upstream-Request": upstream_request_header(params),
            "X-Growin-Upstream-Interval": "1day", "X-Growin-Breeze-Status": "200",
            "X-Growin-Empty": "true" if empty else "false",
            **override,
        }
        return httpx.Response(200, headers=headers, content=body)

    def master_ok(self, body: bytes, **override: str) -> httpx.Response:
        headers = {
            **self.headers("application/zip"),
            "X-Growin-Upstream-Sha256": sha(body), "X-Growin-Upstream-Fetched-At": self.fetched_at,
            "X-Growin-Upstream-Api": "icici-security-master-zip",
            "X-Growin-Upstream-Request": json.dumps({"url": "https://example.invalid/master"}, sort_keys=True),
            **override,
        }
        return httpx.Response(200, headers=headers, content=body)

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = dict(request.url.params)
        self.requests.append(path)
        if path == "/v1/health":
            if self.health_response is not None:
                return self.health_response
            return self.json_ok(
                {
                    "state": self.health_state, "login_pending": False,
                    "expires_at_ist": "2026-10-22T00:00:00+05:30" if self.health_state == "active" else None,
                    "egress": {"ok": self.egress_ok, "checked_at_utc": "2026-10-21T04:29:00+00:00"},
                    "server_time_utc": "2026-10-21T04:30:00+00:00", "version": "test",
                }
            )
        if path == "/v1/budget":
            if self.budget_response is not None:
                return self.budget_response
            return self.json_ok(
                {
                    "calls_last_minute": 0, "calls_today_ist": 100, "per_minute_limit": 80, "per_day_limit": 4500,
                    "remaining_today": self.remaining, "ist_date": self.ist_date,
                }
            )
        if path == "/v1/bars/daily":
            index = len(self.bars_requests)
            self.bars_requests.append(params)
            if self.on_bars is not None:
                self.on_bars(params)
            result = self.bars_script(index, params) if self.bars_script is not None else None
            if isinstance(result, Exception):
                raise result
            if isinstance(result, httpx.Response):
                return result
            return self.bars_ok(params, result if result is not None else default_bars_body(params, self.depth_cutoff))
        if path == "/v1/security-master":
            result = self.master_script() if self.master_script is not None else None
            if isinstance(result, Exception):
                raise result
            if isinstance(result, httpx.Response):
                return result
            return self.master_ok(result if result is not None else kit.security_master_zip(self.master_rows))
        return err_response(404, "NOT_FOUND")

    def client(self) -> RelayClient:
        return RelayClient(
            http=httpx.Client(transport=httpx.MockTransport(self.handler), base_url=BASE)
        )


class TamperSource:
    """Wraps a real client and rewrites payloads after the client's own checks passed."""

    def __init__(self, inner, *, bars=None, master=None):
        self.inner, self.bars, self.master = inner, bars, master

    def health(self):
        return self.inner.health()

    def budget(self):
        return self.inner.budget()

    def daily_bars(self, *args, **kwargs):
        payload = self.inner.daily_bars(*args, **kwargs)
        return self.bars(payload) if self.bars else payload

    def security_master(self):
        payload = self.inner.security_master()
        return self.master(payload) if self.master else payload


@pytest.fixture
def relay():
    return FakeRelay()


def bars_payload(relay: FakeRelay, code="RELIND", start=date(2021, 9, 28), end=date(2024, 4, 3)) -> UpstreamPayload:
    return relay.client().daily_bars(code, start, end)


# ------------------------------------------------------------------------- parity

UPPER_STR_CONSTANTS = {
    value
    for name, value in vars(client_mod).items()
    if name.isupper() and isinstance(value, str)
}


def test_contract_parity():
    assert gw.CONTRACT == client_mod.CONTRACT == RelayClient.CONTRACT == rc.EXPECTED_CONTRACT_VERSION == "growin-relay/1"
    assert vm.CONTRACT == rc.EXPECTED_CONTRACT_VERSION
    assert len(rc.COPIED_HEADERS) == 10 and len(set(rc.COPIED_HEADERS)) == 10
    for header in rc.COPIED_HEADERS:
        assert header in UPPER_STR_CONSTANTS, header
    for route in rc.COPIED_ROUTES:
        assert route in UPPER_STR_CONSTANTS, route
    for value in (rc.API_DAILY_BARS, rc.API_SECURITY_MASTER, rc.INTERVAL_DAILY):
        assert value in UPPER_STR_CONSTANTS, value
    assert rc.API_SECURITY_MASTER == vm.API_SECURITY_MASTER and rc.INTERVAL_DAILY == vm.DAILY_INTERVAL_SEMANTIC
    assert rc.REQUEST_ID_RE == client_mod.REQUEST_ID_RE.pattern == vm.REQUEST_ID_RE.pattern
    assert rc.SHA256_RE == client_mod.SHA256_RE.pattern
    assert rc.STOCK_CODE_RE == client_mod.STOCK_CODE_RE.pattern == vm.STOCK_CODE_RE.pattern

    def field_names(cls):
        return {field.name for field in dataclasses.fields(cls)}

    assert set(rc.COPIED_PAYLOAD_FIELDS) <= field_names(UpstreamPayload)
    assert set(rc.COPIED_HEALTH_FIELDS) <= field_names(RelayHealth)
    assert set(rc.COPIED_BUDGET_FIELDS) <= field_names(RelayBudget)

    assert set(rc.COPIED_ERROR_CODES) == set(errors_mod.CODE_TO_CLASS)
    for code, name in rc.COPIED_ERROR_CODES.items():
        cls = errors_mod.CODE_TO_CLASS[code]
        assert cls.__name__ == name and issubclass(cls, RelayError)
        assert getattr(gw, name) is cls
    for name in ("RelayContractMismatch", "RelayHashMismatch", "RelayProtocolError", "RelayUnavailable", "TunnelError"):
        assert hasattr(gw, name)

    for method in ("open", "health", "budget", "daily_bars", "security_master", "close", "__enter__", "__exit__"):
        assert callable(getattr(RelayClient, method)), method
    assert {"stock_code", "from_date", "to_date", "exchange"} <= set(inspect.signature(RelayClient.daily_bars).parameters)


def test_failure_policy_reasons_are_reasons_the_relay_can_send():
    assert rc.COPIED_POLICY_REASONS <= vm.RELAY_REASONS


def test_row_cap_matches_the_smoke_fact():
    assert rc.BREEZE_ROW_CAP == 1000


# ------------------------------------------------------------ through the real client


def test_bars_payload_becomes_a_source_descriptor(relay):
    payload = bars_payload(relay)
    descriptor = rc.validate_payload(payload, expected_api=rc.API_DAILY_BARS, kind="breeze_v2_daily")
    assert descriptor.source == "breeze_relay" and descriptor.kind == "breeze_v2_daily"
    assert descriptor.fetched_at == payload.fetched_at_utc
    assert descriptor.locator == (
        "relay:/v1/bars/daily?exchange=NSE&from=2021-09-28&stock_code=RELIND&to=2024-04-03"
    )


def test_security_master_payload_becomes_a_source_descriptor(relay):
    payload = relay.client().security_master()
    descriptor = rc.validate_payload(payload, expected_api=rc.API_SECURITY_MASTER, kind="security_master_zip")
    assert descriptor.kind == "security_master_zip" and descriptor.locator == "relay:/v1/security-master"
    assert descriptor.source == "breeze_relay"


ECHO = {
    "exch_code": "NSE", "from_date": "2021-09-28T00:00:00.000Z", "to_date": "2024-04-03T23:59:59.000Z",
    "interval": "1day", "product_type": "cash", "stock_code": "RELIND",
}


def good(relay=None) -> UpstreamPayload:
    return bars_payload(relay or FakeRelay())


def check(payload, **kw):
    return rc.validate_payload(payload, expected_api=rc.API_DAILY_BARS, kind="breeze_v2_daily", **kw)


def code_of(payload, **kw) -> str:
    with pytest.raises(PilotDataError) as info:
        check(payload, **kw)
    return info.value.code


def test_a_good_payload_passes_with_a_matching_expected_query():
    payload = good()
    check(payload, expected_query={"stock_code": "RELIND", "from": "2021-09-28", "to": "2024-04-03", "exchange": "NSE"})


def test_body_edit_is_a_hash_mismatch():
    payload = good()
    assert code_of(dataclasses.replace(payload, body=payload.body + b" ")) == "relay_hash_mismatch"


def test_claimed_hash_edit_is_a_hash_mismatch():
    payload = good()
    assert code_of(dataclasses.replace(payload, sha256="0" * 64)) == "relay_hash_mismatch"
    assert code_of(dataclasses.replace(payload, sha256=payload.sha256.upper())) == "relay_hash_mismatch"


def test_hash_is_compared_in_constant_time():
    tree = ast.parse(Path(rc.__file__).read_text())
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "compare_digest" in called


def test_other_contract_version_is_a_contract_mismatch():
    assert code_of(dataclasses.replace(good(), contract="growin-relay/2")) == "relay_contract_mismatch"
    assert code_of(dataclasses.replace(good(), contract="")) == "relay_contract_mismatch"


@pytest.mark.parametrize(
    "change",
    [
        {"fetched_at_utc": datetime(2026, 10, 21, 4, 30)},
        {"fetched_at_utc": datetime(2026, 10, 21, 10, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))},
        {"request_id": "0123456789ABCDEF"},
        {"request_id": "0123456789abcde"},
        {"request_id": "0123456789abcdef0"},
        {"request_id": None},
        {"upstream_api": "breeze-quotes-v1"},
        {"upstream_api": "icici-security-master-zip"},
        {"upstream_interval": "1minute"},
        {"upstream_interval": None},
        {"breeze_status": 500},
        {"breeze_status": None},
        {"route": "/v1/quotes"},
        {"query": {"stock_code": "RELIND", "from": "2021-09-28", "to": "2024-04-03", "exchange": "NSE", "x": "1"}},
        {"query": {"stock_code": "reliance", "from": "2021-09-28", "to": "2024-04-03"}},
        {"query": {"stock_code": "RELIND", "from": "2024-04-03", "to": "2021-09-28"}},
        {"query": {"stock_code": "RELIND", "from": "2021-09-28", "to": "2024-04-03", "exchange": "BSE"}},
        {"query": {"stock_code": "RELIND", "from": "20210928", "to": "2024-04-03"}},
        {"query": {"stock_code": "RELIND", "from": "2021-09-28"}},
        {"upstream_request": {"stock_code": "OTHER", "from_date": "2021-09-28T00:00:00.000Z",
                              "to_date": "2024-04-03T23:59:59.000Z", "interval": "1day", "product_type": "cash"}},
        {"upstream_request": {"stock_code": "RELIND", "interval": "1day", "product_type": "cash"}},
        {"upstream_request": {"stock_code": "RELIND", "from_date": "2019-01-01T00:00:00.000Z",
                              "to_date": "2024-04-03T23:59:59.000Z", "interval": "1day", "product_type": "cash"}},
        {"upstream_request": {"stock_code": "RELIND", "from_date": "2021-09-28T00:00:00.000Z",
                              "to_date": "2024-04-03T23:59:59.000Z", "interval": "1week", "product_type": "cash"}},
    ] + [
        {"upstream_request": {**ECHO, **change}}
        for change in (
            {"from_date": "2021-09-28NOT_A_TIMESTAMP"},
            {"to_date": "2024-04-03NOT_A_TIMESTAMP"},
            {"from_date": "2021-09-28"},
            {"from_date": "2021-09-28T00:00:00Z"},
            {"from_date": "2021-09-28T09:15:00.000Z"},
            {"to_date": "2024-04-03T00:00:00.000Z"},
            {"from_date": "2021-02-30T00:00:00.000Z"},
            {"from_date": "2021-09-27T00:00:00.000Z"},  # widened a day: the VM pads 0
            {"to_date": "2024-04-04T23:59:59.000Z"},
            {"from_date": "2021-09-29T00:00:00.000Z"},
            {"exch_code": "BSE"},
            {"exch_code": "nse"},
            {"product_type": "futures"},
            {"product_type": "options"},
            {"interval": "1minute"},
            {"expiry_date": "2024-04-25T06:00:00.000Z"},
            {"right": "call"},
            {"strike_price": "100"},
            {"exchange_code": "NSE"},  # both exchange parameter names at once
            {"stock_code": "RELIND ", },
        )
    ] + [
        {"upstream_request": {key: value for key, value in ECHO.items() if key != missing}}
        for missing in ("exch_code", "from_date", "to_date", "interval", "product_type", "stock_code")
    ] + [
        {"upstream_request": {**{k: v for k, v in ECHO.items() if k != "exch_code"}, "exchange_code": "BSE"}},
    ],
    ids=lambda change: next(iter(change)) + "-" + str(abs(hash(str(change))) % 1000),
)
def test_every_other_defect_is_a_response_invalid(change):
    assert code_of(dataclasses.replace(good(), **change)) == "relay_response_invalid"


def test_both_documented_exchange_parameter_names_pass():
    echo = {k: v for k, v in ECHO.items() if k != "exch_code"}
    check(dataclasses.replace(good(), upstream_request={**echo, "exchange_code": "NSE"}))
    check(dataclasses.replace(good(), upstream_request=ECHO))


def test_the_upstream_request_rules_match_the_vm_contract():
    from gateway_vm import breeze_client as vm_client

    assert rc.BARS_FROM_PAD_DAYS == vm.BARS_FROM_PAD_DAYS and rc.BARS_TO_PAD_DAYS == vm.BARS_TO_PAD_DAYS
    assert rc.UPSTREAM_EXCHANGE_KEYS == vm_client.V2_EXCHANGE_PARAM_NAMES
    assert rc.INTERVAL_DAILY == vm_client.V2_DAILY_INTERVAL
    # the header the real VM builds is the header the fake relay serves, so the rule accepts the real thing
    assert json.loads(upstream_request_header({"stock_code": "RELIND", "from": "2021-09-28", "to": "2024-04-03"})) == ECHO


def test_request_echo_must_match_the_request_the_caller_sent():
    payload = good()
    wrong_window = {"stock_code": "RELIND", "from": "2021-09-28", "to": "2024-04-02", "exchange": "NSE"}
    wrong_stock = {"stock_code": "NIFBEE", "from": "2021-09-28", "to": "2024-04-03", "exchange": "NSE"}
    assert code_of(payload, expected_query=wrong_window) == "relay_response_invalid"
    assert code_of(payload, expected_query=wrong_stock) == "relay_response_invalid"


def envelope_payload(relay, body: bytes, **override: str) -> UpstreamPayload:
    relay.bars_script = lambda index, params: relay.bars_ok(params, body, **override)
    return relay.client().daily_bars("RELIND", date(2021, 9, 28), date(2024, 4, 3))


def envelope_code(relay, body: bytes, **override: str) -> str | None:
    payload = envelope_payload(relay, body, **override)
    try:
        rc.check_bars_envelope(payload)
    except PilotDataError as exc:
        return exc.code
    return None


ROW = kit.breeze_row("RELIND", "2024-01-02 00:00:00", "1", "2", "0.5", "1.5")


def test_a_status_500_envelope_is_an_upstream_error_even_with_an_empty_header_and_list(relay):
    # HTTP 200, Status 500, Success [] and X-Growin-Empty: true: the shape that reads as "no data" if unchecked
    body = kit.breeze_v2_json([], status=500, error="Something went wrong")
    assert envelope_payload(relay, body).empty is True
    assert envelope_code(relay, body) == "breeze_upstream_error"


@pytest.mark.parametrize(
    "body",
    [
        kit.breeze_v2_json([ROW], status=500, error=None),
        kit.breeze_v2_json([ROW], status=200, error="Something went wrong"),
        kit.breeze_v2_json([ROW], status=200, error=""),
        kit.breeze_v2_json([ROW], status="200", error=None),
        kit.breeze_v2_json([], status=0, error=None),
    ],
)
def test_any_non_200_status_or_error_text_is_an_upstream_error(relay, body):
    assert envelope_code(relay, body) == "breeze_upstream_error"


def test_good_and_empty_envelopes_pass_the_check(relay):
    assert envelope_code(relay, kit.breeze_v2_json([ROW])) is None
    assert envelope_code(relay, kit.breeze_v2_json([])) is None


def test_the_empty_header_must_agree_with_the_body(relay):
    assert envelope_code(relay, kit.breeze_v2_json([ROW]), **{"X-Growin-Empty": "true"}) == "relay_response_invalid"
    assert envelope_code(relay, kit.breeze_v2_json([]), **{"X-Growin-Empty": "false"}) == "relay_response_invalid"


@pytest.mark.parametrize(
    "body",
    [b"not json", b"[]", b'{"Success": [], "Status": 200}', b'{"Success": {}, "Status": 200, "Error": null}'],
)
def test_a_malformed_envelope_is_a_schema_mismatch(relay, body):
    assert envelope_code(relay, body, **{"X-Growin-Empty": "false"}) == "breeze_schema_mismatch"


def test_security_master_payload_must_have_an_empty_query_and_the_master_api(relay):
    payload = relay.client().security_master()
    ok = dict(expected_api=rc.API_SECURITY_MASTER, kind="security_master_zip")
    rc.validate_payload(payload, **ok)
    with pytest.raises(PilotDataError):
        rc.validate_payload(dataclasses.replace(payload, query={"stock_code": "RELIND"}), **ok)
    with pytest.raises(PilotDataError):
        rc.validate_payload(dataclasses.replace(payload, upstream_api=rc.API_DAILY_BARS), **ok)
    with pytest.raises(PilotDataError):
        rc.validate_payload(dataclasses.replace(payload, route="/v1/bars/daily"), **ok)


def test_unknown_kind_and_foreign_objects_are_refused():
    with pytest.raises(PilotDataError) as info:
        rc.validate_payload(good(), expected_api=rc.API_DAILY_BARS, kind="quotes")
    assert info.value.code == "relay_kind_invalid"
    with pytest.raises(PilotDataError) as info:
        rc.validate_payload(object(), expected_api=rc.API_DAILY_BARS, kind="breeze_v2_daily")  # type: ignore[arg-type]
    assert info.value.code == "relay_response_invalid"


def test_error_messages_carry_codes_and_request_ids_only():
    payload = good()
    for tampered in (
        dataclasses.replace(payload, body=payload.body + b"X"),
        dataclasses.replace(payload, upstream_api="other"),
        dataclasses.replace(payload, contract="growin-relay/9"),
    ):
        with pytest.raises(PilotDataError) as info:
            check(tampered)
        text = str(info.value)
        assert "RELIND" not in text and "2021" not in text and "Success" not in text
        assert payload.sha256 not in text
    with pytest.raises(PilotDataError) as info:
        check(dataclasses.replace(payload, upstream_api="other"))
    assert payload.request_id in str(info.value)


def test_location_never_looks_like_a_credential(relay):
    descriptor = check(good(relay))
    lowered = descriptor.locator.lower()
    assert not any(word in lowered for word in ("session", "token", "apikey", "secret", "password"))


def test_open_relay_source_is_relay_client_open(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(rc.RelayClient, "open", classmethod(lambda cls, *a, **k: sentinel))
    assert rc.open_relay_source() is sentinel


# ------------------------------------------------------------------------- gating


def health(state="active", egress_ok=True) -> RelayHealth:
    return RelayHealth(
        state=state, login_pending=False, expires_at_ist=None, egress_ok=egress_ok, egress_checked_at_utc=None,
        server_time_utc=datetime(2026, 10, 21, 4, 30, tzinfo=timezone.utc), version="t",
    )


def budget(remaining=100, ist_date=TODAY) -> RelayBudget:
    return RelayBudget(
        calls_last_minute=0, calls_today_ist=1, per_minute_limit=80, per_day_limit=4500,
        remaining_today=remaining, ist_date=ist_date,
    )


def test_healthy_active_state_with_ok_egress_passes():
    rc.gate_health(health())
    rc.gate_budget(budget(), today_ist=TODAY)
    rc.gate_budget(budget(remaining=1), today_ist=TODAY)


@pytest.mark.parametrize(
    "state,egress,code",
    [
        ("none", None, "breeze_session_missing"),
        ("none", True, "breeze_session_missing"),
        ("expired", True, "breeze_session_stale"),
        ("suspended_egress", False, "egress_ip_unverified"),
        ("suspended_egress", True, "egress_ip_unverified"),
        ("active", False, "egress_ip_unverified"),
        ("active", None, "egress_ip_unverified"),
        ("surprise", True, "relay_response_invalid"),
    ],
)
def test_gate_health_codes(state, egress, code):
    with pytest.raises(PilotDataError) as info:
        rc.gate_health(health(state, egress))
    assert info.value.code == code


@pytest.mark.parametrize(
    "remaining,ist_date,code",
    [
        (0, TODAY, "relay_budget_day"),
        (-5, TODAY, "relay_budget_day"),
        (100, TODAY - timedelta(days=1), "relay_clock_mismatch"),
        (100, TODAY + timedelta(days=1), "relay_clock_mismatch"),
    ],
)
def test_gate_budget_codes(remaining, ist_date, code):
    with pytest.raises(PilotDataError) as info:
        rc.gate_budget(budget(remaining, ist_date), today_ist=TODAY)
    assert info.value.code == code


# ----------------------------------------------------------------- classification


def make(cls, **kw):
    return cls(request_id="0123456789abcdef", **kw)


POLICY = [
    (make(NoSession), ("stop", "breeze_session_missing", 0.0)),
    (make(SessionExpired, reason="deadline"), ("stop", "breeze_session_stale", 0.0)),
    (make(SessionExpired, reason="invalid_session"), ("stop", "breeze_session_stale", 0.0)),
    (make(SessionExpired, reason="resource_not_available"), ("stop", "breeze_session_stale", 0.0)),
    (make(EgressMismatch, reason="egress_mismatch"), ("stop", "egress_ip_unverified", 0.0)),
    (make(EgressMismatch, reason="egress_unavailable"), ("stop", "egress_ip_unverified", 0.0)),
    (make(EgressMismatch, reason="ip_mismatch"), ("stop", "egress_ip_unverified", 0.0)),
    (make(BudgetExhausted, reason="budget_minute", retry_after_s=20), ("retry_once", "relay_budget_minute", 20.0)),
    (make(BudgetExhausted, reason="budget_minute", retry_after_s=500), ("retry_once", "relay_budget_minute", 60.0)),
    (make(BudgetExhausted, reason="budget_minute", retry_after_s=60), ("retry_once", "relay_budget_minute", 60.0)),
    (make(BudgetExhausted, reason="budget_minute", retry_after_s=0), ("retry_once", "relay_budget_minute", 60.0)),
    (make(BudgetExhausted, reason="budget_minute"), ("retry_once", "relay_budget_minute", 60.0)),
    (make(BudgetExhausted, reason="budget_day"), ("stop", "relay_budget_day", 0.0)),
    (make(BudgetExhausted, reason="security_master_hourly"), ("stop", "relay_security_master_hourly", 0.0)),
    (make(BudgetExhausted, reason="something_new"), ("stop", "relay_budget_exhausted", 0.0)),
    (make(UpstreamError, reason="no_data"), ("empty", "breeze_no_data", 0.0)),
    (make(UpstreamError, reason="upstream_http"), ("fail_window", "upstream_upstream_http", 0.0)),
    (make(UpstreamError, reason="upstream_shape"), ("fail_window", "upstream_upstream_shape", 0.0)),
    (make(UpstreamError, reason="other"), ("fail_window", "upstream_other", 0.0)),
    (make(UpstreamError, reason="invalid_session"), ("stop", "upstream_invalid_session", 0.0)),
    (make(UpstreamError, reason="public_key_missing"), ("stop", "upstream_public_key_missing", 0.0)),
    (make(UpstreamError, reason="something_new"), ("stop", "upstream_unknown", 0.0)),
    (make(UpstreamError), ("stop", "upstream_unknown", 0.0)),
    (make(UpstreamTimeout, reason="upstream_timeout"), ("retry_once", "upstream_timeout", 0.0)),
    (make(UpstreamTimeout, reason="upstream_unreachable"), ("retry_once", "upstream_timeout", 0.0)),
    (make(RelayUnavailable, reason="timeout"), ("retry_then_stop", "relay_unavailable", 2.0)),
    (make(RelayUnavailable, reason="connect_error"), ("retry_then_stop", "relay_unavailable", 2.0)),
    (make(RelayUnavailable, reason="tunnel_restarted"), ("retry_then_stop", "relay_unavailable", 2.0)),
    (make(RelayUnavailable, reason="tunnel_restart_backoff"),
     ("retry_then_stop", "relay_tunnel_restart_backoff", 60.0)),
    (make(RelayUnavailable, reason="closed"), ("stop", "relay_closed", 0.0)),
    (make(RelayUnavailable, reason="tunnel_restart_failed"), ("stop", "relay_tunnel_restart_failed", 0.0)),
    (make(RelayUnavailable, reason="tunnel_down"), ("stop", "relay_tunnel_down", 0.0)),
    (make(RelayUnavailable, reason="something_new"), ("stop", "relay_unavailable_unknown", 0.0)),
    (make(RelayUnavailable), ("stop", "relay_unavailable_unknown", 0.0)),
    (make(Unauthorized), ("stop", "relay_unauthorized", 0.0)),
    (make(BudgetUnavailable, reason="counter_unreadable"), ("stop", "relay_budget_unavailable", 0.0)),
    (make(RelayValidationError), ("stop", "relay_validation", 0.0)),
    (make(ForbiddenClient), ("stop", "relay_forbidden_client", 0.0)),
    (make(ContractMismatch), ("stop", "relay_contract_mismatch", 0.0)),
    (make(IdentityMismatch), ("stop", "relay_identity_mismatch", 0.0)),
    (make(LoginAttemptInvalid), ("stop", "relay_login_attempt_invalid", 0.0)),
    (make(NotFound), ("stop", "relay_not_found", 0.0)),
    (make(MethodNotAllowed), ("stop", "relay_method_not_allowed", 0.0)),
    (make(InternalRelayError), ("stop", "relay_internal", 0.0)),
    (make(RelayContractMismatch), ("stop", "relay_contract_mismatch", 0.0)),
    (make(RelayHashMismatch), ("stop", "relay_hash_mismatch", 0.0)),
    (make(RelayProtocolError, reason="bad_header"), ("stop", "relay_protocol_error", 0.0)),
    (TunnelError("tunnel_start_failed"), ("stop", "relay_tunnel_error", 0.0)),
    (ValueError("anything"), ("stop", "gateway_error_unknown", 0.0)),
    (RuntimeError("x"), ("stop", "gateway_error_unknown", 0.0)),
    (make(RelayError), ("stop", "gateway_error_unknown", 0.0)),
]


@pytest.mark.parametrize("exc,expected", POLICY, ids=[f"{type(e).__name__}-{getattr(e, 'reason', '')}-{i}" for i, (e, _) in enumerate(POLICY)])
def test_failure_policy_table(exc, expected):
    assert rc.classify_gateway_error(exc) == expected


def test_arbitrary_value_error_stops_unknown():
    assert rc.classify_gateway_error(ValueError("x")) == ("stop", "gateway_error_unknown", 0.0)


def test_every_server_error_class_is_covered_by_the_policy_test():
    covered = {type(exc) for exc, _ in POLICY}
    for cls in errors_mod.CODE_TO_CLASS.values():
        assert cls in covered, cls


def test_every_relay_class_ends_in_a_stop_retry_empty_or_fail_window_action():
    for exc, _ in POLICY:
        action, code, sleep = rc.classify_gateway_error(exc)
        assert action in ("stop", "retry_once", "retry_then_stop", "empty", "fail_window")
        assert code and sleep >= 0


@pytest.mark.parametrize(
    "response,expected",
    [
        (err_response(503, "NO_SESSION"), ("stop", "breeze_session_missing", 0.0)),
        (err_response(503, "SESSION_EXPIRED", "deadline"), ("stop", "breeze_session_stale", 0.0)),
        (err_response(503, "EGRESS_MISMATCH", "ip_mismatch"), ("stop", "egress_ip_unverified", 0.0)),
        (err_response(429, "BUDGET_EXHAUSTED", "budget_minute", retryable=True, retry_after=7),
         ("retry_once", "relay_budget_minute", 7.0)),
        (err_response(429, "BUDGET_EXHAUSTED", "budget_day", retryable=True, retry_after=3600),
         ("stop", "relay_budget_day", 0.0)),
        (err_response(502, "UPSTREAM_ERROR", "no_data", retryable=True), ("empty", "breeze_no_data", 0.0)),
        (err_response(502, "UPSTREAM_ERROR", "upstream_shape"), ("fail_window", "upstream_upstream_shape", 0.0)),
        (err_response(504, "UPSTREAM_TIMEOUT", "upstream_timeout", retryable=True),
         ("retry_once", "upstream_timeout", 0.0)),
        (err_response(422, "VALIDATION"), ("stop", "relay_validation", 0.0)),
        (err_response(401, "UNAUTHORIZED"), ("stop", "relay_unauthorized", 0.0)),
        (err_response(503, "BUDGET_UNAVAILABLE", "counter_unreadable"), ("stop", "relay_budget_unavailable", 0.0)),
    ],
)
def test_errors_from_the_real_client_classify_per_the_table(relay, response, expected):
    relay.bars_script = lambda index, params: response
    with pytest.raises(Exception) as info:
        relay.client().daily_bars("RELIND", date(2026, 1, 1), date(2026, 2, 1))
    assert rc.classify_gateway_error(info.value) == expected


def test_connection_failure_through_the_real_client_is_retry_then_stop(relay):
    relay.bars_script = lambda index, params: httpx.ConnectError("boom")
    with pytest.raises(Exception) as info:
        relay.client().daily_bars("RELIND", date(2026, 1, 1), date(2026, 2, 1))
    assert rc.classify_gateway_error(info.value) == ("retry_then_stop", "relay_unavailable", 2.0)
    assert not rc.refused_before_send(info.value)  # a connect error may have reached the relay: it counts


def test_a_closed_client_refuses_before_sending_and_stops(relay):
    client = relay.client()
    client.close()
    with pytest.raises(Exception) as info:
        client.daily_bars("RELIND", date(2026, 1, 1), date(2026, 2, 1))
    assert rc.classify_gateway_error(info.value) == ("stop", "relay_closed", 0.0)
    assert rc.refused_before_send(info.value) and relay.bars_requests == []


@pytest.mark.parametrize(
    "reason,sent",
    [("closed", False), ("tunnel_restart_failed", False), ("tunnel_restart_backoff", False), ("tunnel_down", False),
     ("tunnel_restarted", False), ("timeout", True), ("connect_error", True), ("something_new", True)],
)
def test_only_refused_before_send_reasons_spend_no_budget(reason, sent):
    assert rc.refused_before_send(make(RelayUnavailable, reason=reason)) is not sent
    assert not rc.refused_before_send(ValueError("x")) and not rc.refused_before_send(make(UpstreamTimeout))


def test_header_and_hash_tampering_through_the_real_client_stops(relay):
    relay.bars_script = lambda index, params: relay.bars_ok(
        params, default_bars_body(params), **{"X-Growin-Relay-Contract": "growin-relay/2"}
    )
    with pytest.raises(Exception) as info:
        relay.client().daily_bars("RELIND", date(2026, 1, 1), date(2026, 2, 1))
    assert rc.classify_gateway_error(info.value) == ("stop", "relay_contract_mismatch", 0.0)
    relay.bars_script = lambda index, params: relay.bars_ok(
        params, default_bars_body(params), **{"X-Growin-Upstream-Sha256": "0" * 64}
    )
    with pytest.raises(Exception) as info:
        relay.client().daily_bars("RELIND", date(2026, 1, 1), date(2026, 2, 1))
    assert rc.classify_gateway_error(info.value) == ("stop", "relay_hash_mismatch", 0.0)


# -------------------------------------------------------------------- no second client

FORBIDDEN_IMPORTS = ("httpx", "requests", "urllib.request", "urllib3", "http.client", "aiohttp", "socket", "subprocess")
PACKAGE_FILES = [Path(rc.__file__), Path(rc.__file__).with_name("backfill.py")]


def imported_names(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names |= {f"{node.module}.{alias.name}" for alias in node.names}
        elif isinstance(node, ast.Call) and getattr(node.func, "attr", getattr(node.func, "id", "")) in {
            "import_module", "__import__"
        }:
            names.add("<dynamic>")
    return names


@pytest.mark.parametrize("path", PACKAGE_FILES, ids=lambda p: p.name)
def test_no_second_transport_can_appear(path):
    names = imported_names(path)
    assert "<dynamic>" not in names
    for name in names:
        for banned in FORBIDDEN_IMPORTS:
            assert name != banned and not name.startswith(banned + "."), f"{path.name} imports {name}"


def test_the_ast_scan_catches_a_planted_client(tmp_path):
    planted = tmp_path / "planted.py"
    planted.write_text("import httpx\nfrom urllib import request\nimport urllib.request\n")
    names = imported_names(planted)
    assert "httpx" in names and "urllib.request" in names


@pytest.mark.parametrize("path", PACKAGE_FILES, ids=lambda p: p.name)
def test_modules_take_no_relay_location_or_credential(path):
    text = path.read_text().lower()
    for word in ("--relay-url", "--url", "--host", "--port", "base_url", "localhost", "127.0.0.1", "bearer"):
        assert word not in text, word
