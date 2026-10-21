"""The growin-orders/1 routes through the real relay app (Phase 63-05, Task 1).

Only the network is faked. The real BreezeClient, SessionManager, EventLog, middleware,
StateStore, AuditLog, RuleGuard, Breeze read ports and OrderPipeline run over:

  * OrderTransport: a Breeze fake that serves the GET-only account, quote, bars and
    security-master reads, and records every request. Breeze is ALWAYS a fake here.
  * a loopback metadata fake that answers GET only and records any other method.

Every test ends with the same two assertions: no non-GET request ever reached the Breeze
order endpoint, and the metadata fake saw no write. The signing key is the committed TEST
ONLY vector key, pinned with allow_test_key. No real network, secret, account id or IP.
"""

from __future__ import annotations

import base64
import io
import json
import os
import sys
import threading
import zipfile
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "gateway" / "vm") not in sys.path:
    sys.path.insert(0, str(ROOT / "gateway" / "vm"))

from cryptography.hazmat.primitives import hashes  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from test_gateway_relay_app import (  # noqa: E402
    APP_KEY,
    BASE,
    BudgetDouble,
    CONTRACT_HDR,
    EXPECTED_IP,
    FakeClock,
    FakeTransport,
    SECRET_KEY,
    USERID,
    Stack,
    T0,
    auth,
    breeze_body,
    secret_forms,
)

from gateway_vm import app as relay_app  # noqa: E402
from gateway_vm import contract  # noqa: E402
from gateway_vm.breeze_client import BreezeClient  # noqa: E402
from gateway_vm.egress import EgressChecker  # noqa: E402
from gateway_vm.events import EventLog  # noqa: E402
from gateway_vm.session import BudgetExceeded, Credentials, SessionManager  # noqa: E402
from gateway_vm.orders import (  # noqa: E402
    CONTRACT as ORDERS_CONTRACT,
    ORDER_ROUTES,
    ORDERS_PATH_PREFIX,
)
from gateway_vm.orders.alerts import FileAlertPort  # noqa: E402
from gateway_vm.orders.risk import StopExitAlert  # noqa: E402
from gateway_vm.orders.routes import OrderSettings, build_order_service  # noqa: E402
from gateway_vm.orders.store import StateStore  # noqa: E402
from gateway_vm.transport import HttpResponse  # noqa: E402

FIXTURES = ROOT / "tests" / "backend" / "fixtures" / "relay_orders"
VECTORS = json.loads((FIXTURES / "signing_vectors.json").read_text(encoding="utf-8"))
PRIMARY = VECTORS["keys"]["primary"]
OTHER = VECTORS["keys"]["other"]
BASE_INTENT = dict(VECTORS["rows"][0]["payload"]["intent"])
ISIN_A = "INE000A01012"  # TESTCO
ISIN_B = "INE111B01023"  # ABC
MASTER_URL = "https://directlink.icicidirect.com/NewSecurityMaster/SecurityMaster.zip"
ORDERS_HDR = {contract.HDR_CONTRACT: ORDERS_CONTRACT}
KILL_PATH = "/computeMetadata/v1/instance/attributes/growin-order-relay"
IST_OFFSET = timedelta(hours=5, minutes=30)


# ---------------------------------------------------------------- helpers


def sign(key: dict, message: bytes) -> str:
    private = ec.derive_private_key(int(key["private_scalar_hex"], 16), ec.SECP256R1())
    return base64.b64encode(private.sign(message, ec.ECDSA(hashes.SHA256()))).decode("ascii")


def intent(**overrides) -> dict:
    body = dict(BASE_INTENT)
    body.update(overrides)
    return body


def master_zip(rows: list[tuple[str, str, str, str]]) -> bytes:
    """A security master zip: (token, short name, series, ISIN) rows. Invented values."""
    lines = ["Token,ShortName,Series,ISINCode"] + [",".join(row) for row in rows]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("NSEScripMaster.txt", "\n".join(lines) + "\n")
    return buffer.getvalue()


DEFAULT_MASTER = [
    ("1001", "TESTCO", "EQ", ISIN_A),
    ("1002", "ABC", "EQ", ISIN_B),
    ("0", "NIFTY", "EQ", "INE999Z99999"),  # an index line, never bound
]


def quote_row(code: str, ltp: float, low: float, high: float, previous: float) -> dict:
    return {
        "stock_code": code,
        "exchange_code": "NSE",
        "ltp": ltp,
        "lower_circuit": low,
        "upper_circuit": high,
        "previous_close": previous,
        "ltt": "21-Oct-2026 09:59:30",
    }


class OrderTransport(FakeTransport):
    """FakeTransport plus the account, quote and bars reads the order guard makes."""

    def __init__(self) -> None:
        super().__init__()
        self.master = HttpResponse(200, {}, master_zip(DEFAULT_MASTER))
        self.quote_book: dict[str, dict] = {
            "TESTCO": quote_row("TESTCO", 100.0, 90.0, 110.0, 99.8),
            "ABC": quote_row("ABC", 440.0, 400.0, 480.0, 440.0),
            "ZZZZ": quote_row("ZZZZ", 100.0, 90.0, 110.0, 99.8),
        }
        # code -> {date: close}. September 2026 closes feed the tick reference.
        self.bar_book: dict[str, dict[date, float]] = {
            "TESTCO": {date(2026, 9, 30): 99.8},
            "ABC": {date(2026, 9, 30): 500.0},
            "ZZZZ": {date(2026, 9, 30): 99.8},
        }
        self.holdings: HttpResponse | Exception = HttpResponse(200, {}, breeze_body([]))
        self.trades: HttpResponse | Exception = HttpResponse(200, {}, breeze_body([]))
        self.orders: HttpResponse | Exception = HttpResponse(200, {}, breeze_body([]))
        # Called with the URL path of every Breeze read as it is served, so a test can
        # change the world (revoke the session) while a request is in flight.
        self.on_read = None

    def request(self, method, url, *, headers, body, timeouts, max_bytes):
        parts = urlsplit(url)
        host, path = parts.hostname, parts.path
        if self.on_read is not None and host in ("api.icicidirect.com", "breezeapi.icicidirect.com"):
            self.on_read(path)
        if host == "api.icicidirect.com" and path.endswith("/quotes"):
            with self.lock:
                self.requests.append((method, url, dict(headers), body))
            code = json.loads(body)["stock_code"]
            row = self.quote_book.get(code)
            if row is None:
                return HttpResponse(200, {}, breeze_body(None, status=500, error="no quote"))
            return HttpResponse(200, {}, breeze_body([row]))
        for name, attr in (("dematholdings", "holdings"), ("trades", "trades"), ("order", "orders")):
            if host == "api.icicidirect.com" and path.endswith("/" + name):
                with self.lock:
                    self.requests.append((method, url, dict(headers), body))
                return self._reply(getattr(self, attr))
        if host == "breezeapi.icicidirect.com":
            with self.lock:
                self.requests.append((method, url, dict(headers), body))
            query = dict(parse_qsl(parts.query))
            first = date.fromisoformat(query["from_date"][:10])
            last = date.fromisoformat(query["to_date"][:10])
            book = self.bar_book.get(query["stock_code"], {})
            rows = [
                {
                    "datetime": f"{day.isoformat()} 00:00:00",
                    "stock_code": query["stock_code"],
                    "exchange_code": "NSE",
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": 10,
                }
                for day, close in sorted(book.items())
                if first <= day <= last
            ]
            return HttpResponse(200, {}, breeze_body(rows))
        return super().request(
            method, url, headers=headers, body=body, timeouts=timeouts, max_bytes=max_bytes
        )

    def non_get(self) -> list[tuple[str, str]]:
        return [(m, u) for m, u, _h, _b in self.requests if m != "GET"]

    def order_endpoint_non_get(self) -> list[tuple[str, str]]:
        return [(m, u) for m, u in self.non_get() if urlsplit(u).path.rstrip("/").endswith("/order")]

    def order_endpoint_hits(self) -> int:
        return sum(1 for _m, u, _h, _b in self.requests if urlsplit(u).path.rstrip("/").endswith("/order"))


class MetadataFake(ThreadingHTTPServer):
    """A loopback metadata server. GET only: any other method is recorded, never served."""

    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _MetadataHandler)
        self.body: bytes | None = b"enabled"  # None answers 404
        self.methods: list[str] = []

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _MetadataHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_):
        pass

    def _record(self) -> None:
        self.server.methods.append(self.command)  # type: ignore[attr-defined]

    def do_GET(self):  # noqa: N802
        self._record()
        body = self.server.body  # type: ignore[attr-defined]
        status = 404 if body is None or self.path != KILL_PATH else 200
        data = b"not found" if status == 404 else body
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _refuse(self) -> None:
        self._record()
        self.send_response(405)
        self.send_header("Content-Length", "0")
        self.end_headers()

    do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _refuse  # noqa: N815


def write_private(path: Path, text: str, mode: int = 0o600) -> Path:
    path.write_text(text, encoding="ascii")
    os.chmod(path, mode)
    return path


class OrderStack(Stack):
    """The 61-05 Stack with the order routes mounted over an OrderTransport."""

    def __init__(
        self,
        tmp_path: Path,
        metadata: MetadataFake,
        *,
        limits: bool | int = True,
        key: bool | int = True,
        state_dir: bool = True,
        holidays: frozenset[date] | None = None,
    ) -> None:
        self.holidays = holidays
        self.tmp = tmp_path
        self.metadata = metadata
        self.state_dir = tmp_path / "state"
        if state_dir:
            self.state_dir.mkdir(mode=0o700)
        self.limits_path = tmp_path / "limits.json"
        self.key_path = tmp_path / "order-key.json"
        if limits:
            write_private(
                self.limits_path,
                json.dumps(VECTORS["limits"]),
                0o600 if limits is True else limits,
            )
        if key:
            write_private(
                self.key_path,
                json.dumps(
                    {"key_id": PRIMARY["key_id"], "public_key_x963_hex": PRIMARY["public_key_x963_hex"]}
                ),
                0o600 if key is True else key,
            )
        self.clock = FakeClock()
        self.transport = OrderTransport()
        self.sink = io.StringIO()
        self.events = EventLog(self.sink, self.clock)
        self.budget = BudgetDouble()
        self.credentials = Credentials(app_key=APP_KEY, secret_key=SECRET_KEY)
        self.breeze = BreezeClient(self.transport, clock=self.clock)
        self.egress = EgressChecker(
            self.transport, expected_ip=EXPECTED_IP, echo_url="https://api.ipify.org", clock=self.clock
        )
        self.session = SessionManager(
            clock=self.clock,
            egress=self.egress,
            breeze=self.breeze,
            credentials=self.credentials,
            expected_userid=USERID,
            events=self.events,
            budget=self.budget,
        )
        self.restart()

    def make_app(self):
        return relay_app.create_app(
            session=self.session,
            breeze=self.breeze,
            credentials=self.credentials,
            budget=self.budget,
            events=self.events,
            clock=self.clock,
            security_master_url=MASTER_URL,
            orders=self.service,
        )

    def build_orders(self):
        return build_order_service(
            OrderSettings(
                state_dir=self.state_dir,
                limits_path=str(self.limits_path),
                key_path=str(self.key_path),
                owner_uid=os.getuid(),
                allow_test_key=True,
                kill_base_url=self.metadata.base,
                holidays=self.holidays,
            ),
            session=self.session,
            breeze=self.breeze,
            credentials=self.credentials,
            budget=self.budget,
            events=self.events,
            clock=self.clock,
            security_master_url=MASTER_URL,
        )

    def restart(self) -> None:
        """A new service and app over the same files and the same live Breeze session."""
        self.service = self.build_orders()
        self.app = self.make_app()
        self.client = TestClient(self.app, base_url=BASE)

    # -- calls

    def post(self, path: str, body, token: str, *, headers: dict | None = None):
        data = body if isinstance(body, (bytes, str)) else json.dumps(body)
        return self.client.post(
            path,
            content=data,
            headers={"Content-Type": "application/json", **(ORDERS_HDR if headers is None else headers), **auth(token)},
        )

    def get(self, path: str, token: str, *, headers: dict | None = None):
        return self.client.get(path, headers={**(ORDERS_HDR if headers is None else headers), **auth(token)})

    def mint(self, token: str, **overrides):
        return self.post("/v1/orders/intents", intent(**overrides), token)

    def authorize(self, token: str, challenge_id: str, signature_b64: str):
        return self.post(
            "/v1/orders/authorize",
            {"challenge_id": challenge_id, "signature_der_b64": signature_b64},
            token,
        )

    def signed(self, token: str, key: dict = PRIMARY, **overrides) -> tuple[str, str]:
        """Mint, sign the returned bytes, return (challenge_id, signature)."""
        minted = self.mint(token, **overrides)
        assert minted.status_code == 200, minted.text
        body = minted.json()
        return body["challenge_id"], sign(key, base64.b64decode(body["signed_bytes_b64"]))

    def verify_ok(self, token: str, **overrides):
        challenge_id, signature = self.signed(token, **overrides)
        return self.authorize(token, challenge_id, signature)

    def audit(self, token: str) -> list[dict]:
        resp = self.get("/v1/orders/audit", token)
        assert resp.status_code == 200, resp.text
        return resp.json()["entries"]

    def state(self, token: str) -> dict:
        resp = self.get("/v1/orders/state", token)
        assert resp.status_code == 200, resp.text
        return resp.json()

    def edit_state(self, mutate) -> None:
        store = StateStore(self.state_dir)
        state = store.load()
        mutate(state)
        store.save(state)


# ---------------------------------------------------------------- fixtures


@pytest.fixture()
def metadata():
    server = MetadataFake()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture()
def stack(tmp_path, metadata):
    built = OrderStack(tmp_path, metadata)
    yield built
    # The two standing assertions of every test in this file.
    assert built.transport.order_endpoint_non_get() == []
    assert built.transport.non_get() == []
    assert set(metadata.methods) <= {"GET"}


@pytest.fixture()
def token(stack) -> str:
    return stack.login()


def code_of(resp) -> tuple[int, str, str]:
    err = resp.json()["error"]
    return resp.status_code, err["code"], err["reason"]


def assert_orders_envelope(resp) -> None:
    assert resp.headers[contract.HDR_CONTRACT] == ORDERS_CONTRACT
    assert contract.REQUEST_ID_RE.fullmatch(resp.headers[contract.HDR_REQUEST_ID])
    assert resp.headers["cache-control"] == "no-store"


# ---------------------------------------------------------------- tracer


def test_tracer_mint_sign_authorize_over_raw_http(stack, token, metadata):
    minted = stack.mint(token)
    assert minted.status_code == 200, minted.text
    assert_orders_envelope(minted)
    body = minted.json()
    assert body["contract"] == ORDERS_CONTRACT
    signed = base64.b64decode(body["signed_bytes_b64"])
    done = stack.authorize(token, body["challenge_id"], sign(PRIMARY, signed))
    assert done.status_code == 200, done.text
    assert_orders_envelope(done)
    result = done.json()
    assert result["decision"] == "VERIFIED_NOT_FORWARDED"
    assert result["intent_id"] == BASE_INTENT["intent_id"]
    assert [e["decision"] for e in stack.audit(token)] == ["CHALLENGED", "VERIFIED_NOT_FORWARDED"]
    # The only `order` traffic is the GET order-list read: one per check, two checks.
    assert stack.transport.order_endpoint_hits() == 2
    assert metadata.methods == ["GET", "GET"]  # one kill read per check, nothing else


# ---------------------------------------------------- fixture A: the signature

REFUSED = "REFUSED"


def test_unsigned_authorize_is_signature_invalid_and_burns_the_challenge(stack, token):
    challenge_id, signature = stack.signed(token)
    assert code_of(stack.authorize(token, challenge_id, "")) == (403, "SIGNATURE_INVALID", "signature_invalid")
    # The bad attempt consumed the challenge: the right signature no longer helps.
    assert code_of(stack.authorize(token, challenge_id, signature)) == (409, "REPLAY", "challenge_unknown")


@pytest.mark.parametrize(
    "body",
    [
        {"challenge_id": "c"},
        {"signature_der_b64": "AA=="},
        {"challenge_id": "c", "signature_der_b64": "AA==", "extra": 1},
        {"challenge_id": 7, "signature_der_b64": "AA=="},
        {"challenge_id": "c", "signature_der_b64": "not base64!"},
        b"[]",
        b"7",
        b"{",
        b'{"challenge_id":"a","challenge_id":"b","signature_der_b64":"AA=="}',
    ],
)
def test_malformed_authorize_bodies_are_422(stack, token, body):
    resp = stack.post("/v1/orders/authorize", body, token)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "INTENT_INVALID"


def test_wrong_key_is_signature_invalid_and_burns_the_challenge(stack, token):
    challenge_id, signature = stack.signed(token, key=OTHER)
    assert code_of(stack.authorize(token, challenge_id, signature)) == (403, "SIGNATURE_INVALID", "signature_invalid")
    good = sign(PRIMARY, b"irrelevant")
    assert code_of(stack.authorize(token, challenge_id, good)) == (409, "REPLAY", "challenge_unknown")


def test_one_altered_byte_of_the_signed_bytes_is_signature_invalid(stack, token):
    minted = stack.mint(token).json()
    signed = bytearray(base64.b64decode(minted["signed_bytes_b64"]))
    signed[len(signed) // 2] ^= 0x01
    forged = sign(PRIMARY, bytes(signed))  # a valid signature over bytes the VM never issued
    assert code_of(stack.authorize(token, minted["challenge_id"], forged)) == (403, "SIGNATURE_INVALID", "signature_invalid")


def test_one_flipped_signature_byte_is_signature_invalid(stack, token):
    minted = stack.mint(token).json()
    signature = bytearray(base64.b64decode(sign(PRIMARY, base64.b64decode(minted["signed_bytes_b64"]))))
    signature[-1] ^= 0x01
    resp = stack.authorize(token, minted["challenge_id"], base64.b64encode(bytes(signature)).decode())
    assert code_of(resp) == (403, "SIGNATURE_INVALID", "signature_invalid")


def test_replayed_challenge_is_unknown(stack, token):
    challenge_id, signature = stack.signed(token)
    assert stack.authorize(token, challenge_id, signature).status_code == 200
    assert code_of(stack.authorize(token, challenge_id, signature)) == (409, "REPLAY", "challenge_unknown")


def test_unknown_challenge_id(stack, token):
    resp = stack.authorize(token, "00000000-0000-4000-8000-000000000000", sign(PRIMARY, b"x"))
    assert code_of(resp) == (409, "REPLAY", "challenge_unknown")


def test_expired_challenge_uses_the_vm_clock(stack, token):
    challenge_id, signature = stack.signed(token)
    stack.clock.advance(61)
    assert code_of(stack.authorize(token, challenge_id, signature)) == (409, "REPLAY", "challenge_expired")


def test_last_valid_second_still_authorizes(stack, token):
    challenge_id, signature = stack.signed(token)
    stack.clock.advance(59)
    assert stack.authorize(token, challenge_id, signature).status_code == 200


def test_second_mint_for_a_live_challenge_is_outstanding(stack, token):
    assert stack.mint(token).status_code == 200
    assert code_of(stack.mint(token)) == (409, "REPLAY", "challenge_outstanding")


def test_second_challenge_for_a_consumed_intent_is_refused_even_after_a_restart(stack, token):
    assert stack.verify_ok(token).status_code == 200
    assert code_of(stack.mint(token)) == (409, "REPLAY", "intent_consumed")
    stack.restart()
    assert code_of(stack.mint(token)) == (409, "REPLAY", "intent_consumed")
    state = stack.state(token)
    assert state["latches"] == []


def test_limits_hash_and_key_id_mismatch_are_refused_at_mint(stack, token):
    assert code_of(stack.mint(token, limits_sha256="0" * 64)) == (409, "LIMIT_REJECTED", "limits_hash_mismatch")
    assert code_of(stack.mint(token, key_id="0" * 64)) == (409, "LIMIT_REJECTED", "key_mismatch")


def test_kill_switch_absent_blocks_mint(stack, token, metadata):
    metadata.body = None
    assert code_of(stack.mint(token)) == (423, "ORDERS_BLOCKED", "kill_switch")
    assert stack.state(token)["kill"] != "enabled"


# --------------------------------------------- fixture B: the rule re-check


def test_kill_flipped_between_mint_and_authorize_blocks_authorize(stack, token, metadata):
    challenge_id, signature = stack.signed(token)
    metadata.body = b"disabled"
    assert code_of(stack.authorize(token, challenge_id, signature)) == (423, "ORDERS_BLOCKED", "kill_switch")
    assert [e["decision"] for e in stack.audit(token)] == ["CHALLENGED", "REFUSED"]


def test_kill_enabled_again_allows_a_new_intent(stack, token, metadata):
    metadata.body = b"disabled"
    assert code_of(stack.mint(token)) == (423, "ORDERS_BLOCKED", "kill_switch")
    metadata.body = b"enabled"
    assert stack.verify_ok(token, intent_id="intent-test-0002").status_code == 200


@pytest.mark.parametrize("body", [b"ENABLED", b"Enabled", b"enabled\n", b" enabled", b""])
def test_only_the_exact_lowercase_enabled_body_passes_the_kill_switch(stack, token, metadata, body):
    metadata.body = body
    assert code_of(stack.mint(token)) == (423, "ORDERS_BLOCKED", "kill_switch")
    metadata.body = b"enabled"
    assert stack.mint(token, intent_id="intent-test-0002").status_code == 200


def test_a_blocked_kill_switch_makes_no_account_read_at_mint_or_authorize(stack, token, metadata):
    challenge_id, signature = stack.signed(token)
    metadata.body = b"disabled"
    reads = len(stack.transport.requests)
    assert code_of(stack.mint(token, intent_id="intent-test-0002")) == (423, "ORDERS_BLOCKED", "kill_switch")
    assert code_of(stack.authorize(token, challenge_id, signature)) == (423, "ORDERS_BLOCKED", "kill_switch")
    assert len(stack.transport.requests) == reads  # not one Breeze GET behind a closed switch


def test_the_option_b_halt_latch_refuses_a_buy_and_survives_a_restart(stack, token):
    stack.edit_state(lambda state: setattr(state, "halt", True))
    assert code_of(stack.mint(token)) == (423, "ORDERS_BLOCKED", "halt_latch")
    stack.restart()
    assert code_of(stack.mint(token, intent_id="intent-test-0002")) == (423, "ORDERS_BLOCKED", "halt_latch")
    assert "halt" in stack.state(token)["latches"]


def test_over_cap_buy_is_refused_with_every_code_audited(stack, token):
    resp = stack.mint(token, quantity=600)  # 600 x 100.05 is over both the capital and the position cap
    status, error, reason = code_of(resp)
    assert (status, error) == (409, "LIMIT_REJECTED") and reason in ("capital_cap", "per_position_cap")
    entry = stack.audit(token)[-1]
    assert entry["decision"] == REFUSED and {"capital_cap", "per_position_cap"} <= set(entry["codes"])


def test_mac_halt_blocks_mint_and_survives_a_restart(stack, token):
    halted = stack.post("/v1/orders/halt", {}, token)
    assert halted.status_code == 200 and halted.json() == {"contract": ORDERS_CONTRACT, "mac_halt": True}
    assert_orders_envelope(halted)
    assert code_of(stack.mint(token)) == (423, "ORDERS_BLOCKED", "mac_halt")
    stack.restart()
    assert code_of(stack.mint(token)) == (423, "ORDERS_BLOCKED", "mac_halt")
    assert "mac_halt" in stack.state(token)["latches"]


def test_halt_between_mint_and_authorize_blocks_authorize(stack, token):
    challenge_id, signature = stack.signed(token)
    assert stack.post("/v1/orders/halt", {}, token).status_code == 200
    assert code_of(stack.authorize(token, challenge_id, signature)) == (423, "ORDERS_BLOCKED", "mac_halt")
    assert stack.audit(token)[-1]["decision"] == REFUSED


def test_halt_needs_an_empty_object_and_no_breeze_session(stack):
    armed = stack.arm().json()  # the relay token exists but the Breeze login never completed
    token = armed["relay_token"]
    assert stack.post("/v1/orders/halt", {"x": 1}, token).status_code == 422
    assert stack.post("/v1/orders/halt", {}, token).status_code == 200


def test_pilot_ended_refuses_a_buy(stack, token):
    stack.edit_state(lambda state: setattr(state, "ended", True))
    assert code_of(stack.mint(token)) == (423, "ORDERS_BLOCKED", "pilot_ended")
    assert "ended" in stack.state(token)["latches"]


def test_session_cutoff_is_15_10_ist(stack, token):
    stack.clock.advance(5 * 3600 + 9 * 60 + 59)  # 15:09:59 IST
    assert stack.verify_ok(token).status_code == 200
    stack.clock.advance(1)  # 15:10:00 IST
    assert code_of(stack.mint(token, intent_id="intent-test-0002")) == (423, "ORDERS_BLOCKED", "session_closed")


def test_session_cutoff_between_mint_and_authorize(stack, token):
    stack.clock.advance(5 * 3600 + 9 * 60 + 30)  # 15:09:30 IST, 30 s left
    challenge_id, signature = stack.signed(token)
    stack.clock.advance(30)  # 15:10:00 IST, challenge itself still valid
    assert code_of(stack.authorize(token, challenge_id, signature)) == (423, "ORDERS_BLOCKED", "session_closed")


def test_live_mode_is_refused_at_mint_before_any_read(stack, token):
    before = len(stack.transport.requests)
    assert code_of(stack.mint(token, mode="LIVE")) == (423, "ORDERS_BLOCKED", "live_disabled")
    assert len(stack.transport.requests) == before  # not even the master or the account was read


def test_isin_not_matching_the_master_is_refused_over_raw_http(stack, token):
    # TESTCO is ISIN_A in the master. The Mac says ISIN_B: refused, never trusted.
    assert code_of(stack.mint(token, isin=ISIN_B)) == (409, "LIMIT_REJECTED", "isin_mismatch")


def test_stock_code_unknown_to_the_master_is_isin_mismatch(stack, token):
    # The quote for ZZZZ exists and the Mac's ISIN is well formed, but the master has no row for it.
    resp = stack.mint(token, stock_code="ZZZZ", isin=ISIN_A)
    assert code_of(resp) == (409, "LIMIT_REJECTED", "isin_mismatch")
    assert stack.audit(token)[-1]["codes"] == ["isin_mismatch"]


def test_master_is_fetched_once_per_ist_day_through_the_budget(stack, token):
    assert stack.verify_ok(token).status_code == 200
    assert stack.verify_ok(token, intent_id="intent-test-0002").status_code == 200
    assert len(stack.transport.master_requests()) == 1
    assert stack.budget.kinds.count("security_master") == 1


# --------------------------------------------------------- account reads


def trade_row(order_id, code, action, quantity, price, day) -> dict:
    return {
        "order_id": order_id,
        "stock_code": code,
        "action": action,
        "quantity": str(quantity),
        "average_cost": str(price),
        "brokerage_amount": "0",
        "total_taxes": "0",
        "trade_date": day,
    }


def holding_row(isin, quantity) -> dict:
    return {"stock_ISIN": isin, "quantity": str(quantity)}


def seed_abc_stopped(stack: OrderStack) -> None:
    """ABC bought 20 at 500 on 6 Oct, closed 440 from 7 Oct on: a -12% stop latch, drawdown -2.4%."""
    t = stack.transport
    t.holdings = HttpResponse(200, {}, breeze_body([holding_row(ISIN_B, 20)]))
    t.trades = HttpResponse(200, {}, breeze_body([trade_row("ord-buy-1", "ABC", "Buy", 20, 500, "06-Oct-2026")]))
    bars = {date(2026, 9, 30): 500.0, date(2026, 10, 6): 500.0}
    day = date(2026, 10, 7)
    while day <= date(2026, 10, 20):
        if day.weekday() < 5:
            bars[day] = 440.0
        day += timedelta(days=1)
    t.bar_book["ABC"] = bars


def test_account_read_error_is_never_an_empty_account(stack, token):
    stack.transport.holdings = HttpResponse(200, {}, breeze_body(None, status=500, error="down"))
    assert code_of(stack.mint(token)) == (503, "ORDERS_UNAVAILABLE", "account_read_failed")
    assert stack.state(token)["latches"] == []  # a read failure is not a latch, just a refusal


@pytest.mark.parametrize(
    "target,response",
    [
        ("holdings", HttpResponse(200, {}, breeze_body(None))),
        ("holdings", HttpResponse(200, {}, b"not json")),
        ("holdings", HttpResponse(200, {}, breeze_body([{"stock_ISIN": ISIN_A, "quantity": "-3"}]))),
        ("holdings", HttpResponse(200, {}, breeze_body([{"stock_ISIN": "bad", "quantity": "3"}]))),
        ("trades", HttpResponse(500, {}, b"")),
        ("trades", HttpResponse(200, {}, breeze_body([trade_row("o1", "NOPE", "Buy", 1, 10, "06-Oct-2026")]))),
        (
            "trades",
            HttpResponse(
                200,
                {},
                breeze_body(
                    [
                        trade_row("o1", "TESTCO", "Buy", 1, 10, "06-Oct-2026"),
                        trade_row("o1", "TESTCO", "Buy", 1, 10, "06-Oct-2026"),
                    ]
                ),
            ),
        ),
        ("orders", HttpResponse(200, {}, breeze_body(None))),
        (
            "orders",
            HttpResponse(
                200,
                {},
                breeze_body(
                    [
                        {
                            "order_id": "w1",
                            "stock_code": "TESTCO",
                            "action": "Buy",
                            "quantity": "5",
                            "price": "0",
                            "pending_quantity": "5",
                            "status": "Ordered",
                        }
                    ]
                ),
            ),
        ),
    ],
)
def test_untrusted_account_shapes_fail_closed(stack, token, target, response):
    setattr(stack.transport, target, response)
    assert code_of(stack.mint(token)) == (503, "ORDERS_UNAVAILABLE", "account_read_failed")


def test_excess_demat_quantity_latches_account_mismatch(stack, token):
    seed_abc_stopped(stack)
    stack.transport.holdings = HttpResponse(200, {}, breeze_body([holding_row(ISIN_B, 25)]))  # ledger says 20
    assert code_of(stack.mint(token)) == (423, "ORDERS_BLOCKED", "account_mismatch")
    assert "account_mismatch" in stack.state(token)["latches"]
    # The latch is durable: correcting the holdings does not clear it, and nothing here can.
    stack.transport.holdings = HttpResponse(200, {}, breeze_body([holding_row(ISIN_B, 20)]))
    assert code_of(stack.mint(token, intent_id="intent-test-0002")) == (423, "ORDERS_BLOCKED", "account_mismatch")
    stack.restart()
    assert "account_mismatch" in stack.state(token)["latches"]


def test_holding_with_no_trade_history_latches_account_mismatch(stack, token):
    stack.transport.holdings = HttpResponse(200, {}, breeze_body([holding_row(ISIN_A, 5)]))
    assert code_of(stack.mint(token)) == (423, "ORDERS_BLOCKED", "account_mismatch")


def test_stop_exit_open_blocks_a_buy_until_the_trade_list_shows_the_exit(stack, token):
    seed_abc_stopped(stack)
    resp = stack.mint(token)  # a BUY of TESTCO while ABC is stopped
    assert code_of(resp) == (423, "ORDERS_BLOCKED", "stop_open")
    assert stack.state(token)["latches"] == ["stop"]
    assert stack.state(token)["last_evaluated_session"] == "2026-10-20"
    # The exit SELL of the stopped ISIN is allowed through.
    sell = stack.verify_ok(
        token, intent_id="intent-test-0002", side="sell", reason="stop", stock_code="ABC",
        isin=ISIN_B, quantity=20, limit_price="440.00",
    )
    assert sell.status_code == 200 and sell.json()["decision"] == "VERIFIED_NOT_FORWARDED"
    assert code_of(stack.mint(token, intent_id="intent-test-0003")) == (423, "ORDERS_BLOCKED", "stop_open")
    # The exit fills: the trade list shows it (today, so the holding still lags a day).
    stack.transport.trades = HttpResponse(
        200,
        {},
        breeze_body(
            [
                trade_row("ord-buy-1", "ABC", "Buy", 20, 500, "06-Oct-2026"),
                trade_row("ord-sell-1", "ABC", "Sell", 20, 440, "21-Oct-2026"),
            ]
        ),
    )
    assert stack.verify_ok(token, intent_id="intent-test-0004").status_code == 200
    assert stack.state(token)["latches"] == []


def test_missed_sessions_are_replayed_and_evaluated_in_order(stack, token):
    seed_abc_stopped(stack)
    stack.mint(token)
    state = stack.state(token)
    assert state["last_evaluated_session"] == "2026-10-20"
    assert Decimal(state["drawdown"]) < 0 and "stop" in state["latches"]


def test_no_trusted_mark_refuses_every_order(stack, token):
    seed_abc_stopped(stack)
    stack.transport.bar_book["ABC"][date(2026, 10, 20)] = 300.0  # the quote's previous_close is 440
    assert code_of(stack.mint(token)) == (503, "ORDERS_UNAVAILABLE", "quote_unavailable")
    assert stack.state(token)["last_evaluated_session"] == "2026-10-19"


def test_quote_from_a_prior_session_is_quote_unavailable(stack, token):
    """P-10: an ltt dated before the current IST session never admits an order."""
    stack.transport.quote_book["TESTCO"]["ltt"] = "20-Oct-2026 15:29:58"
    assert code_of(stack.mint(token)) == (503, "ORDERS_UNAVAILABLE", "quote_unavailable")
    assert stack.transport.non_get() == []


# -------------------------------------------------- audit, state, routes


def test_state_route_shape(stack, token):
    state = stack.state(token)
    assert state["contract"] == ORDERS_CONTRACT
    assert set(state) == {
        "contract", "kill", "latches", "drawdown", "peak_date", "last_evaluated_session", "alerts_logged",
    }
    assert state["kill"] == "enabled" and state["latches"] == [] and state["alerts_logged"] == 0


def test_audit_route_paging_and_validation(stack, token):
    stack.verify_ok(token)
    assert [e["seq"] for e in stack.audit(token)] == [1, 2]
    page = stack.get("/v1/orders/audit?after_seq=1", token).json()["entries"]
    assert [e["seq"] for e in page] == [2]
    for query in ("after_seq=-1", "after_seq=x", "after_seq=1&after_seq=2", "other=1", "after_seq=1234567890123"):
        assert stack.get("/v1/orders/audit?" + query, token).status_code == 422, query


def test_bearer_is_required_on_every_order_route(stack, token):
    for method, path in (
        ("POST", "/v1/orders/intents"),
        ("POST", "/v1/orders/authorize"),
        ("POST", "/v1/orders/halt"),
        ("GET", "/v1/orders/state"),
        ("GET", "/v1/orders/audit"),
    ):
        for bearer in (None, "wrong"):
            resp = (
                stack.post(path, {}, bearer) if method == "POST" else stack.get(path, bearer)
            )
            assert resp.status_code == 401, (path, bearer)
            assert resp.headers[contract.HDR_CONTRACT] == ORDERS_CONTRACT


def test_a_revoked_breeze_session_refuses_mint_and_authorize_but_not_halt_state_or_audit(stack, token):
    challenge_id, signature = stack.signed(token)
    stack.revoke(token)
    reads = len(stack.transport.requests)
    assert code_of(stack.mint(token, intent_id="intent-test-0002"))[:2] == (503, "NO_SESSION")
    assert code_of(stack.authorize(token, challenge_id, signature))[:2] == (503, "NO_SESSION")
    assert len(stack.transport.requests) == reads  # no Breeze read without a session
    assert stack.get("/v1/orders/audit", token).status_code == 200
    assert stack.get("/v1/orders/state", token).status_code == 200
    assert stack.post("/v1/orders/halt", {}, token).status_code == 200


def revoke_on_read(stack: OrderStack, token: str, nth: int) -> list[str]:
    """Revoke the Breeze session while the nth upstream read is in flight. Returns the read log."""
    seen: list[str] = []

    def hook(path: str) -> None:
        seen.append(path)
        if len(seen) == nth:
            stack.session.revoke(token)

    stack.transport.on_read = hook
    return seen


def decisions(stack: OrderStack, token: str) -> list[str]:
    return [entry["decision"] for entry in stack.audit(token)]


def test_a_session_revoked_during_authorize_stops_the_next_read_and_never_verifies(stack, token):
    challenge_id, signature = stack.signed(token)
    seen = revoke_on_read(stack, token, 1)  # the first read (holdings) is the last one allowed
    resp = stack.authorize(token, challenge_id, signature)
    assert code_of(resp)[:2] == (503, "NO_SESSION")
    assert len(seen) == 1  # trades, orders, quote and bars never left on the dead session
    entries = stack.audit(token)
    # The intent was consumed before the check. The refusal is the audit line.
    assert [e["decision"] for e in entries] == ["CHALLENGED", "REFUSED"]
    assert entries[-1]["codes"] == ["no_session"]
    assert entries[-1]["route"] == "authorize"


def test_a_session_revoked_during_mint_stops_the_next_read_and_issues_no_challenge(stack, token):
    seen = revoke_on_read(stack, token, 2)
    assert code_of(stack.mint(token))[:2] == (503, "NO_SESSION")
    assert len(seen) == 2
    assert decisions(stack, token) == []


@pytest.mark.parametrize("route", ["mint", "authorize"])
def test_a_session_revoked_during_the_last_read_is_caught_before_the_decision(stack, token, route):
    challenge_id, signature = stack.signed(token)
    before = len(stack.audit(token))
    seen = revoke_on_read(stack, token, 5)  # holdings, trades, orders, quote, tick-reference bars
    if route == "mint":
        resp = stack.mint(token, intent_id="intent-test-0002")
    else:
        resp = stack.authorize(token, challenge_id, signature)
    assert code_of(resp)[:2] == (503, "NO_SESSION")
    assert len(seen) == 5  # every read ran; only the final decision was left to stop
    entries = stack.audit(token)
    if route == "mint":
        assert len(entries) == before  # the intent was not consumed, so nothing is recorded
    else:
        # Authorize had already consumed the intent. The dead session is audited.
        assert [e["decision"] for e in entries] == ["CHALLENGED", "REFUSED"]
        assert entries[-1]["codes"] == ["no_session"]
        assert "VERIFIED_NOT_FORWARDED" not in [e["decision"] for e in entries]


def call_while_recheck_is_paused(stack: OrderStack, token: str, call, disturb=None):
    """Run `call` on another thread. After its post-read recheck returns, revoke.

    `disturb` replaces the revoke (for example an egress drift).

    require_active has dropped the session lock by the time the recheck returns.
    Pausing there is the window in which a revoke used to be answered 200.
    """
    arrived = threading.Event()
    release = threading.Event()
    original = stack.service.pipeline._recheck
    box: dict = {}

    def hooked() -> None:
        original()
        if arrived.is_set():
            return
        arrived.set()
        if not release.wait(timeout=5):
            raise RuntimeError("recheck pause was not released")

    def run() -> None:
        try:
            box["resp"] = call()
        except Exception as exc:
            box["error"] = exc

    stack.service.pipeline._recheck = hooked
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    try:
        assert arrived.wait(timeout=5), "the post-read recheck never returned"
        if disturb is None:
            stack.session.revoke(token)
        else:
            disturb()
        release.set()
    finally:
        worker.join(timeout=5)
    assert not worker.is_alive(), "authorize or mint did not finish after the revoke"
    assert "error" not in box, box.get("error")
    return box["resp"]


def test_a_session_revoked_after_the_authorize_recheck_returns_is_not_verified(stack, token):
    challenge_id, signature = stack.signed(token)
    resp = call_while_recheck_is_paused(
        stack, token, lambda: stack.authorize(token, challenge_id, signature)
    )
    assert code_of(resp)[:2] == (503, "NO_SESSION")
    entries = stack.audit(token)
    assert [e["decision"] for e in entries] == [
        "CHALLENGED",
        "VERIFIED_NOT_FORWARDED",
        "REFUSED",
    ]
    assert entries[-1]["codes"] == ["no_session"]
    assert entries[-1]["route"] == "authorize"
    assert stack.service.pipeline._forward.calls == 0


def test_a_session_revoked_after_the_mint_recheck_returns_issues_no_challenge(stack, token):
    resp = call_while_recheck_is_paused(stack, token, lambda: stack.mint(token))
    assert code_of(resp)[:2] == (503, "NO_SESSION")
    assert "challenge_id" not in resp.json()
    entries = stack.audit(token)
    assert [e["decision"] for e in entries] == ["CHALLENGED", "REFUSED"]
    assert entries[-1]["codes"] == ["no_session"]
    assert entries[-1]["route"] == "intents"
    epoch = int(stack.clock.now_utc().timestamp())
    assert stack.service.pipeline._challenges.live_count(epoch) == 0
    assert stack.service.pipeline._forward.calls == 0


# ----------------------------------------------- audit carries the failure's own code


def drift_egress(stack: OrderStack) -> None:
    """The echoed egress IP stops matching and the last good check no longer counts.

    The clock is not moved: a clock jump would expire the challenge and the
    deadline codes would win over the egress failure under test.
    """
    stack.transport.echo_ip = "203.0.113.9"
    stack.session._egress_max_age_s = 0


def egress_drift_on_read(stack: OrderStack, nth: int) -> list[str]:
    seen: list[str] = []

    def hook(path: str) -> None:
        seen.append(path)
        if len(seen) == nth:
            drift_egress(stack)

    stack.transport.on_read = hook
    return seen


def assert_chain_verifies(stack: OrderStack, token: str) -> None:
    seq, _head = stack.service.pipeline._audit.verify()
    assert seq == len(stack.audit(token))


def test_budget_refusal_during_the_authorize_check_is_audited_as_budget_exhausted(stack, token):
    challenge_id, signature = stack.signed(token)
    stack.budget.raises = BudgetExceeded("budget_minute", 30)
    reads = len(stack.transport.breeze_requests())
    resp = stack.authorize(token, challenge_id, signature)
    assert code_of(resp)[:2] == (429, "BUDGET_EXHAUSTED")
    assert len(stack.transport.breeze_requests()) == reads  # nothing left on an exhausted budget
    entries = stack.audit(token)
    assert [e["decision"] for e in entries] == ["CHALLENGED", "REFUSED"]
    assert entries[-1]["codes"] == ["budget_exhausted"]
    assert entries[-1]["route"] == "authorize"
    assert_chain_verifies(stack, token)


@pytest.mark.parametrize("nth", [1, 5])
def test_egress_mismatch_during_the_authorize_check_is_audited_as_egress_mismatch(stack, token, nth):
    # nth=1: the first read's successor is refused. nth=5: the last read ran, so it is
    # the post-check recheck that finds the mismatch.
    challenge_id, signature = stack.signed(token)
    seen = egress_drift_on_read(stack, nth)
    resp = stack.authorize(token, challenge_id, signature)
    assert code_of(resp)[:2] == (503, "EGRESS_MISMATCH")
    assert len(seen) == nth
    entries = stack.audit(token)
    assert [e["decision"] for e in entries] == ["CHALLENGED", "REFUSED"]
    assert entries[-1]["codes"] == ["egress_mismatch"]
    assert entries[-1]["route"] == "authorize"
    assert_chain_verifies(stack, token)


def test_egress_mismatch_after_the_authorize_recheck_returns_is_audited_as_egress_mismatch(stack, token):
    challenge_id, signature = stack.signed(token)
    resp = call_while_recheck_is_paused(
        stack,
        token,
        lambda: stack.authorize(token, challenge_id, signature),
        disturb=lambda: drift_egress(stack),
    )
    assert code_of(resp)[:2] == (503, "EGRESS_MISMATCH")
    entries = stack.audit(token)
    assert [e["decision"] for e in entries] == ["CHALLENGED", "VERIFIED_NOT_FORWARDED", "REFUSED"]
    assert entries[-1]["codes"] == ["egress_mismatch"]
    assert stack.service.pipeline._forward.calls == 0
    assert_chain_verifies(stack, token)


def test_egress_mismatch_after_the_mint_recheck_returns_is_audited_as_egress_mismatch(stack, token):
    resp = call_while_recheck_is_paused(
        stack, token, lambda: stack.mint(token), disturb=lambda: drift_egress(stack)
    )
    assert code_of(resp)[:2] == (503, "EGRESS_MISMATCH")
    assert "challenge_id" not in resp.json()
    entries = stack.audit(token)
    assert [e["decision"] for e in entries] == ["CHALLENGED", "REFUSED"]
    assert entries[-1]["codes"] == ["egress_mismatch"]
    assert entries[-1]["route"] == "intents"
    assert_chain_verifies(stack, token)


def test_a_real_session_death_during_the_check_is_still_audited_as_no_session(stack, token):
    challenge_id, signature = stack.signed(token)
    revoke_on_read(stack, token, 1)
    resp = stack.authorize(token, challenge_id, signature)
    assert code_of(resp)[:2] == (503, "NO_SESSION")
    entries = stack.audit(token)
    assert [e["decision"] for e in entries] == ["CHALLENGED", "REFUSED"]
    assert entries[-1]["codes"] == ["no_session"]
    assert_chain_verifies(stack, token)


def test_security_master_fetch_revalidates_the_session_before_the_download(stack, token):
    """The zip GET in security_master_bytes rechecks the session before it leaves.

    The route-level require_active has already passed. Revoke in the gap after
    that call returns. The directlink download must not be made.
    """
    seen = {"n": 0}
    original = stack.session.require_active

    def revoke_after_the_route_check(auth):
        seen["n"] += 1
        token_value = original(auth)
        if seen["n"] == 1:
            stack.session.revoke(token)
        return token_value

    stack.session.require_active = revoke_after_the_route_check
    meta_before = len(stack.metadata.methods)
    resp = stack.mint(token)
    assert code_of(resp)[:2] == (503, "NO_SESSION")
    assert len(stack.metadata.methods) == meta_before + 1  # the kill read: the guard was entered
    assert stack.transport.master_requests() == []
    assert stack.budget.kinds.count("security_master") == 0
    assert decisions(stack, token) == []


def test_contract_mismatch_both_ways(stack, token):
    wrong = {contract.HDR_CONTRACT: contract.CONTRACT}  # the data contract on an order route
    assert stack.post("/v1/orders/intents", intent(), token, headers=wrong).status_code == 400
    assert stack.get("/v1/orders/state", token, headers=wrong).status_code == 400
    # No contract header at all is the existing relay answer, not a mismatch.
    assert stack.get("/v1/orders/state", token, headers={}).status_code == 403
    # An order route's contract on a data route.
    resp = stack.client.get("/v1/budget", headers={**ORDERS_HDR, **auth(token)})
    assert resp.status_code == 400 and resp.headers[contract.HDR_CONTRACT] == contract.CONTRACT
    assert stack.client.get("/v1/budget", headers={**CONTRACT_HDR, **auth(token)}).status_code == 200


@pytest.mark.parametrize(
    "method,path,status",
    [
        ("GET", "/v1/orders/intents", 405),
        ("PUT", "/v1/orders/intents", 405),
        ("DELETE", "/v1/orders/intents", 405),
        ("PATCH", "/v1/orders/halt", 405),
        ("DELETE", "/v1/orders/state", 405),
        ("POST", "/v1/orders/state", 405),
        ("POST", "/v1/orders/audit", 405),
        ("GET", "/v1/orders/clear", 404),
        ("POST", "/v1/orders/reset", 404),
        ("POST", "/v1/orders/place", 404),
        ("POST", "/v1/orders/modify", 404),
        ("POST", "/v1/orders/cancel", 404),
        ("DELETE", "/v1/orders/anything", 404),
        ("POST", "/v1/orders/intents/", 404),
        ("POST", "/v1/orders/intents/extra", 404),
    ],
)
def test_unknown_order_paths_and_methods_are_refused_with_the_orders_contract(stack, token, method, path, status):
    resp = stack.client.request(method, path, headers={**ORDERS_HDR, **auth(token)}, content=b"{}")
    assert resp.status_code == status
    assert resp.headers[contract.HDR_CONTRACT] == ORDERS_CONTRACT


def test_bare_orders_path_stays_under_the_relay_contract_and_404s(stack, token):
    resp = stack.client.post("/v1/orders", headers={**CONTRACT_HDR, **auth(token)}, content=b"{}")
    assert resp.status_code == 404
    assert resp.headers[contract.HDR_CONTRACT] == contract.CONTRACT


def test_oversized_and_non_json_bodies_are_refused_before_any_read(stack, token):
    before = len(stack.transport.requests)
    assert stack.post("/v1/orders/intents", b"x" * 9000, token).status_code == 422
    assert stack.post("/v1/orders/intents", b"[]", token).status_code == 422
    assert stack.post("/v1/orders/intents", b'{"intent_id":1.5}', token).status_code == 422
    resp = stack.client.post(
        "/v1/orders/intents", content=b"{}", headers={**ORDERS_HDR, **auth(token), "Content-Type": "text/plain"}
    )
    assert resp.status_code == 422
    assert stack.client.post(
        "/v1/orders/intents?x=1", content=b"{}", headers={**ORDERS_HDR, **auth(token), "Content-Type": "application/json"}
    ).status_code == 422
    assert len(stack.transport.requests) == before


def test_malformed_intent_is_422_before_the_breeze_session_is_consulted(stack, token):
    stack.revoke(token)
    assert code_of(stack.mint(token, quantity=0))[:2] == (422, "INTENT_INVALID")
    assert code_of(stack.mint(token, mode="LIVE")) == (423, "ORDERS_BLOCKED", "live_disabled")


# ----------------------------------------------- route table and metadata


def route_table(app) -> list[tuple[str, frozenset]]:
    return sorted((r.path, frozenset(r.methods)) for r in app.routes if getattr(r, "methods", None))


DATA_ROUTES = [
    ("/v1/bars/daily", frozenset({"GET"})),
    ("/v1/bars/intraday", frozenset({"GET"})),
    ("/v1/budget", frozenset({"GET"})),
    ("/v1/health", frozenset({"GET"})),
    ("/v1/login/arm", frozenset({"POST"})),
    ("/v1/login/complete", frozenset({"POST"})),
    ("/v1/preview-order", frozenset({"GET"})),
    ("/v1/quotes", frozenset({"GET"})),
    ("/v1/security-master", frozenset({"GET"})),
    ("/v1/session/revoke", frozenset({"POST"})),
]
ORDER_TABLE = [
    ("/v1/orders/audit", frozenset({"GET"})),
    ("/v1/orders/authorize", frozenset({"POST"})),
    ("/v1/orders/halt", frozenset({"POST"})),
    ("/v1/orders/intents", frozenset({"POST"})),
    ("/v1/orders/state", frozenset({"GET"})),
]
FORBIDDEN_WORDS = ("clear", "reset", "place", "modify", "cancel", "delete", "unlock", "enrol", "key", "limits")


def test_route_table_audit(stack):
    table = route_table(stack.app)
    assert [row for row in table if row[0].startswith("/v1/orders")] == ORDER_TABLE
    assert [row for row in table if not row[0].startswith("/v1/orders")] == DATA_ROUTES
    assert set(ORDER_ROUTES) == {path for path, _ in ORDER_TABLE}
    methods = set().union(*(m for _, m in table))
    assert methods <= {"GET", "POST", "HEAD"}  # no PUT, PATCH or DELETE anywhere
    for route in stack.app.routes:
        path = getattr(route, "path", "")
        name = getattr(route, "name", "") or ""
        if path.startswith("/v1/orders"):
            text = (path + " " + name).lower()
            assert not any(word in text for word in FORBIDDEN_WORDS[:6]), (path, name)
            assert path.startswith(ORDERS_PATH_PREFIX)


GATEWAY_DIR = ROOT / "gateway" / "vm" / "gateway_vm"
METADATA_SCAN_FILES = sorted((GATEWAY_DIR / "orders").glob("*.py")) + [GATEWAY_DIR / "app.py"]
_WRITE_VERBS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_HTTP_CALLS = frozenset({"request", "Request", "urlopen"})
_VERB_METHODS = frozenset({"post", "put", "patch", "delete"})
_METADATA_MARKERS = ("computemetadata", "metadata.google.internal", "169.254.169.254")
_PROCESS_ATTRS = frozenset({"system", "popen", "posix_spawn", "execv", "execve", "execvp", "spawnv", "spawnl"})


def metadata_write_violations(source: str) -> list[str]:
    """Why this source could write instance metadata or shell out to gcloud. Empty means clean.

    Anywhere in the file: no subprocess or pty import, no os.system-style call, no
    "gcloud" or "add-metadata" string. In a file that names the metadata server: no
    string that is a write verb, no http call that sets a method other than the constant
    "GET" or sends a body, and no post/put/patch/delete call.
    """
    import ast

    tree = ast.parse(source)
    found: list[str] = []
    constants = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    names_metadata = any(marker in value.lower() for value in constants for marker in _METADATA_MARKERS)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(a.name.split(".")[0] in ("subprocess", "pty") for a in node.names):
            found.append("process import")
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] in ("subprocess", "pty"):
            found.append("process import")
        if isinstance(node, ast.Attribute) and node.attr in _PROCESS_ATTRS:
            found.append("process call " + node.attr)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            lowered = node.value.lower()
            if "gcloud" in lowered or "add-metadata" in lowered:
                found.append("gcloud")
            if names_metadata and node.value.upper() in _WRITE_VERBS:
                found.append("write verb " + node.value)
        if names_metadata and isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name in _VERB_METHODS:
                found.append("verb call " + name)
            if name in _HTTP_CALLS:
                for keyword in node.keywords:
                    if keyword.arg in ("data", "body", "json"):
                        found.append("http body")
                    if keyword.arg == "method" and not (
                        isinstance(keyword.value, ast.Constant) and keyword.value.value == "GET"
                    ):
                        found.append("http method")
    return found


def test_orders_package_and_relay_app_cannot_write_metadata_or_run_gcloud():
    assert len(METADATA_SCAN_FILES) > 10
    for file in METADATA_SCAN_FILES:
        assert metadata_write_violations(file.read_text(encoding="utf-8")) == [], file.name
    # The scanner has to see the one file that does read the metadata server.
    kill = (GATEWAY_DIR / "orders" / "kill.py").read_text(encoding="utf-8")
    assert any(marker in kill.lower() for marker in _METADATA_MARKERS)


@pytest.mark.parametrize(
    "plant",
    [
        "import subprocess\nsubprocess.run(['gcloud', 'compute', 'instances', 'add-metadata', 'vm', '--metadata=growin-order-relay=enabled'])\n",
        "import os\nos.system('true')\n",
        "from urllib.request import Request, urlopen\n"
        "urlopen(Request('http://metadata.google.internal/computeMetadata/v1/instance/attributes/growin-order-relay', method='PUT'))\n",
        "import http.client\n"
        "http.client.HTTPConnection('metadata.google.internal').request('PUT', '/computeMetadata/v1/instance/attributes/growin-order-relay', body=b'enabled')\n",
        "import http.client\nverb = 'GET'\nhttp.client.HTTPConnection('x').request(verb, '/computeMetadata/v1/', body=b'enabled')\n",
        "import httpx\nhttpx.put('http://metadata.google.internal/computeMetadata/v1/instance/attributes/growin-order-relay')\n",
    ],
    ids=["gcloud_subprocess", "os_system", "request_put", "connection_put_body", "connection_body", "httpx_put"],
)
@pytest.mark.parametrize("target", ["routes.py", "app.py"])
def test_the_metadata_write_scan_catches_a_plant_in_the_order_routes_and_the_relay_app(plant, target):
    path = GATEWAY_DIR / ("orders/routes.py" if target == "routes.py" else "app.py")
    clean = path.read_text(encoding="utf-8")
    assert metadata_write_violations(clean) == []
    assert metadata_write_violations(clean + "\n" + plant) != []


# ----------------------------------------------- config_invalid isolation


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limits": False},
        {"key": False},
        {"limits": 0o666},
        {"key": 0o666},
        {"limits": 0o620},
        {"state_dir": False},
    ],
    ids=["no_limits", "no_key", "limits_world_writable", "key_world_writable", "limits_group_writable", "no_state_dir"],
)
def test_config_invalid_answers_only_the_order_routes(tmp_path, metadata, kwargs):
    stack = OrderStack(tmp_path, metadata, **kwargs)
    token = stack.login()
    for path in ("/v1/orders/intents", "/v1/orders/authorize", "/v1/orders/halt"):
        resp = stack.post(path, {}, token)
        assert code_of(resp) == (503, "ORDERS_UNAVAILABLE", "config_invalid"), path
        assert_orders_envelope(resp)
    for path in ("/v1/orders/state", "/v1/orders/audit"):
        assert code_of(stack.get(path, token)) == (503, "ORDERS_UNAVAILABLE", "config_invalid"), path
    # Health and the data routes keep working, and the alarm event is on the log.
    assert stack.client.get("/v1/health", headers=CONTRACT_HDR).status_code == 200
    assert stack.bars(token).status_code == 200
    alarms = stack.events_named("config_alarm")
    assert alarms and alarms[0]["reason"] == "config_invalid"
    assert stack.transport.non_get() == []
    assert metadata.methods == []  # an unconfigured order service never even asks the kill switch


# ------------------------------------------------------------- on disk


def test_on_disk_state_is_only_the_expected_files_and_holds_no_secret(stack, token, tmp_path):
    assert stack.verify_ok(token).status_code == 200
    assert stack.post("/v1/orders/halt", {}, token).status_code == 200
    FileAlertPort(stack.state_dir).send(StopExitAlert(isin=ISIN_B, session_date=date(2026, 10, 21), quantity=20))
    names = sorted(os.listdir(stack.state_dir))
    assert set(names) <= {"state.json", "audit.jsonl", "audit.jsonl.lock", ".order.lock", "alerts.jsonl"}
    assert {"state.json", "audit.jsonl", "alerts.jsonl"} <= set(names)
    blob = ""
    for name in names:
        path = stack.state_dir / name
        assert os.lstat(path).st_mode & 0o077 == 0, name
        blob += path.read_text(encoding="utf-8")
    for secret in [*secret_forms(), token, EXPECTED_IP, "203.0.113", "Bearer"]:
        assert secret not in blob
    # Nothing was written beside the state directory: the two config files are the only others.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["limits.json", "order-key.json", "state"]
    assert alerts_logged(stack, token) == 1


def alerts_logged(stack, token) -> int:
    return stack.state(token)["alerts_logged"]
