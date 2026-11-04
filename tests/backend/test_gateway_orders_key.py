"""GET /v1/orders/key: the authenticated, read-only current-key field (64-10, D-36).

The VM half runs on the real in-process OrderStack (committed TEST ONLY keys pinned with
allow_test_key, fake Breeze, loopback metadata fake). The Mac half runs a real RelayClient
over either that in-process VM or httpx.MockTransport canned replies. No tunnel, gcloud, real
key, account id, IP or non-loopback socket.

K2 (route-set pin), K7 (config_invalid) and the bearer and unknown-path pins live in
test_gateway_orders_routes.py, where the tables they extend are.
"""

from __future__ import annotations

import ast
import hashlib
import io
import json
import os
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "gateway" / "vm") not in sys.path:
    sys.path.insert(0, str(ROOT / "gateway" / "vm"))

from test_gateway_orders_routes import (  # noqa: E402
    ORDERS_HDR,
    OTHER,
    PRIMARY,
    MetadataFake,
    OrderStack,
    assert_orders_envelope,
    code_of,
    write_private,
)
from test_gateway_relay_app import (  # noqa: E402
    APP_KEY,
    CONTRACT_HDR,
    EXPECTED_IP,
    OTHER_IP,
    SECRET_KEY,
    auth,
    secret_forms,
)

from brokers.breeze_gateway import cli as cli_mod  # noqa: E402
from brokers.breeze_gateway import orders as mac_orders  # noqa: E402
from brokers.breeze_gateway.client import ORDERS_CONTRACT as MAC_ORDERS_CONTRACT  # noqa: E402
from brokers.breeze_gateway.client import RelayClient  # noqa: E402
from brokers.breeze_gateway.errors import (  # noqa: E402
    MethodNotAllowed,
    NotFound,
    OrdersKeyRouteUnavailable,
    OrdersUnavailable,
    RelayContractMismatch,
    RelayProtocolError,
    Unauthorized,
)
from brokers.breeze_gateway.orders import OrdersClient, OrdersProtocolError  # noqa: E402
from brokers.breeze_gateway.token_store import MemoryTokenStore  # noqa: E402
from gateway_vm import orders as vm_orders  # noqa: E402
from gateway_vm import contract as vm_contract  # noqa: E402
from gateway_vm.session import BudgetExceeded  # noqa: E402
from gateway_vm.orders.verify import PinnedKey  # noqa: E402

KEY_PATH = "/v1/orders/key"
VM_ROUTES_FILE = ROOT / "gateway" / "vm" / "gateway_vm" / "orders" / "routes.py"
HEX_A = "a" * 64
RID = "0123456789abcdef"
MAC_TOKEN = "mac-token-CANARY-0001"


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
    # The standing assertions of every order test: no Breeze write, no metadata write.
    assert built.transport.order_endpoint_non_get() == []
    assert built.transport.non_get() == []
    assert set(metadata.methods) <= {"GET"}


@pytest.fixture()
def token(stack) -> str:
    return stack.login()


def key_read(stack, bearer, **kwargs):
    return stack.get(KEY_PATH, bearer, **kwargs)


def assert_refusal(resp, status: int, code: str, reason: str | None = None) -> None:
    """A refusal that carries no id. The id check comes first, so a leak is the red assertion."""
    assert PRIMARY["key_id"] not in resp.text and OTHER["key_id"] not in resp.text, "the key id leaked"
    assert resp.status_code == status, (resp.status_code, status)
    error = resp.json()["error"]
    assert error["code"] == code
    if reason is not None:
        assert error["reason"] == reason


def assert_key_body(resp, expected: str = PRIMARY["key_id"]) -> None:
    assert resp.status_code == 200, resp.text
    assert_orders_envelope(resp)
    assert resp.json() == {"contract": vm_orders.CONTRACT, "key_id": expected}


def swap_key(stack, key: dict) -> None:
    """Replace the PinnedKey the pipeline holds. The route and the mint check read the same one."""
    stack.service.pipeline._key = PinnedKey(bytes.fromhex(key["public_key_x963_hex"]), allow_test_key=True)


# ---------------------------------------------------------------- K1


def test_k1_shape_is_exactly_contract_and_key_id_from_the_pipelines_own_key(stack, token):
    resp = key_read(stack, token)
    assert resp.status_code == 200
    assert_orders_envelope(resp)
    assert resp.content == (
        b'{"contract":"growin-orders/1","key_id":"' + PRIMARY["key_id"].encode("ascii") + b'"}'
    )
    body = resp.json()
    assert set(body) == {"contract", "key_id"}
    assert re.fullmatch(r"[0-9a-f]{64}", body["key_id"])
    pinned_file = json.loads(stack.key_path.read_text(encoding="ascii"))
    assert body["key_id"] == hashlib.sha256(bytes.fromhex(pinned_file["public_key_x963_hex"])).hexdigest()
    assert body["key_id"] == stack.service.pipeline.key_id == stack.service.pipeline._key.key_id
    # The id is the public fingerprint. The key bytes are not in the answer.
    assert PRIMARY["public_key_x963_hex"] not in resp.text


# ---------------------------------------------------------------- K3


def test_k3_contract_and_host_rules_run_before_the_route(stack, token):
    relay_contract = key_read(stack, token, headers=CONTRACT_HDR)
    assert_refusal(relay_contract, 400, "CONTRACT_MISMATCH")
    assert relay_contract.headers[vm_contract.HDR_CONTRACT] == vm_orders.CONTRACT
    missing = key_read(stack, token, headers={})
    assert missing.status_code == 403 and PRIMARY["key_id"] not in missing.text
    for host in ("evil.example:8443", "10.0.0.2", "localhost.evil.example"):
        resp = stack.client.get(KEY_PATH, headers={**ORDERS_HDR, **auth(token), "Host": host})
        assert resp.status_code == 403, host
        assert PRIMARY["key_id"] not in resp.text


# ---------------------------------------------------------------- K4


@pytest.mark.parametrize(
    "query",
    ["x=1", f"key_id={PRIMARY['key_id']}", "after_seq=1", "a=1&a=2"],
    ids=["unknown", "key_id", "after_seq", "repeated"],
)
def test_k4_any_query_string_is_a_422_with_no_key(stack, token, query):
    resp = stack.get(f"{KEY_PATH}?{query}", token)
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "VALIDATION" and error["fields"] == ["unknown"]
    body_without_query = resp.text.replace(f"key_id={PRIMARY['key_id']}", "")
    assert PRIMARY["key_id"] not in body_without_query


# ---------------------------------------------------------------- K5


def relogin(stack, old_token: str) -> str:
    armed = stack.arm(old_token)
    assert armed.status_code == 200, armed.text
    fresh = armed.json()
    assert stack.complete(fresh["attempt_id"], fresh["relay_token"]).status_code == 200
    return fresh["relay_token"]


def test_k5_an_active_session_still_needs_the_current_bearer(stack, token):
    assert_key_body(key_read(stack, token))
    previous = token
    current = relogin(stack, previous)
    for bearer in (None, "wrong-bearer", previous):
        resp = key_read(stack, bearer)
        assert_refusal(resp, 401, "UNAUTHORIZED")
        assert resp.headers[vm_contract.HDR_CONTRACT] == vm_orders.CONTRACT
    assert_key_body(key_read(stack, current))


@pytest.mark.parametrize("bearer", [None, "never-bound"], ids=["no_bearer", "any_bearer"])
def test_k5_before_any_login_the_answer_is_no_session_not_the_id(stack, bearer):
    assert_refusal(key_read(stack, bearer), 503, "NO_SESSION")


def test_k5_a_revoked_session_answers_no_session_even_with_the_old_bearer(stack, token):
    assert stack.revoke(token).status_code == 200
    for bearer in (token, None, "wrong"):
        assert_refusal(key_read(stack, bearer), 503, "NO_SESSION")


def test_k5_past_the_deadline_the_answer_is_session_expired_not_the_id(stack, token):
    stack.clock.advance(15 * 3600)  # login 10:00 IST, midnight IST is the deadline
    for bearer in (token, None):
        assert_refusal(key_read(stack, bearer), 503, "SESSION_EXPIRED")
    # The expiry is the existing one-shot event, not a new trace.
    assert len(stack.events_named("session_expired")) == 1


def test_k5_a_suspended_session_answers_egress_mismatch_not_the_id(stack, token):
    stack.clock.advance(301)
    stack.transport.echo_ip = OTHER_IP
    assert code_of(stack.bars(token))[:2] == (503, "EGRESS_MISMATCH")  # a data route suspends it
    for bearer in (token, None):
        assert_refusal(key_read(stack, bearer), 503, "EGRESS_MISMATCH")


# ---------------------------------------------------------------- K6


def _kill_absent(stack):
    stack.metadata.body = None


def _kill_blocked(stack):
    stack.metadata.body = b"blocked"


def _egress_evidence_aged_out(stack):
    stack.clock.advance(301)  # past EGRESS_MAX_AGE_S, still inside the session


def _budget_exhausted(stack):
    stack.budget.raises = BudgetExceeded("budget_minute", 30)


@pytest.mark.parametrize(
    "setup",
    [_kill_absent, _kill_blocked, _egress_evidence_aged_out, _budget_exhausted],
    ids=["kill_absent", "kill_blocked", "egress_aged_out", "budget_exhausted"],
)
def test_k6_the_read_needs_no_kill_flag_egress_check_or_budget(stack, token, setup):
    setup(stack)
    requests = list(stack.transport.requests)
    methods = list(stack.metadata.methods)
    kinds = list(stack.budget.kinds)
    assert_key_body(key_read(stack, token))
    assert stack.transport.requests == requests  # no Breeze, metadata-echo or egress request
    assert stack.metadata.methods == methods  # no kill read
    assert stack.budget.kinds == kinds  # no charge


# ---------------------------------------------------------------- K8


def test_k8_the_field_and_the_mint_check_read_the_same_object(stack, token):
    assert_key_body(key_read(stack, token))
    swap_key(stack, OTHER)
    assert_key_body(key_read(stack, token), OTHER["key_id"])
    refused = stack.mint(token, intent_id="intent-test-0002")  # BASE_INTENT carries PRIMARY's id
    assert code_of(refused) == (409, "LIMIT_REJECTED", "key_mismatch")
    passed = stack.mint(token, intent_id="intent-test-0003", key_id=OTHER["key_id"])
    assert passed.status_code == 200, passed.text
    assert [e["decision"] for e in stack.audit(token)] == ["REFUSED", "CHALLENGED"]
    swap_key(stack, PRIMARY)
    assert_key_body(key_read(stack, token))
    back = stack.mint(token, intent_id="intent-test-0004")
    assert back.status_code == 200, back.text
    assert code_of(stack.mint(token, intent_id="intent-test-0005", key_id=OTHER["key_id"])) == (
        409,
        "LIMIT_REJECTED",
        "key_mismatch",
    )


# ---------------------------------------------------------------- K9


def rewrite_key(stack, key: dict, mode: int = 0o600) -> None:
    write_private(
        stack.key_path,
        json.dumps({"key_id": key["key_id"], "public_key_x963_hex": key["public_key_x963_hex"]}),
        mode,
    )
    os.chmod(stack.key_path, mode)


def test_k9_a_rotation_takes_a_restart_and_moves_the_field_and_the_mint_together(stack, token):
    rewrite_key(stack, OTHER)  # the disk changes, the process does not
    assert_key_body(key_read(stack, token))
    assert code_of(stack.mint(token, intent_id="intent-test-0002", key_id=OTHER["key_id"])) == (
        409,
        "LIMIT_REJECTED",
        "key_mismatch",
    )
    assert stack.mint(token, intent_id="intent-test-0003").status_code == 200

    stack.cold_restart()  # as production: a new SessionManager as well as a new pipeline
    assert_refusal(key_read(stack, token), 503, "NO_SESSION")
    fresh = stack.login()
    assert_key_body(key_read(stack, fresh), OTHER["key_id"])
    assert stack.mint(fresh, intent_id="intent-test-0004", key_id=OTHER["key_id"]).status_code == 200
    assert code_of(stack.mint(fresh, intent_id="intent-test-0005")) == (409, "LIMIT_REJECTED", "key_mismatch")

    rewrite_key(stack, PRIMARY, 0o666)  # group- and other-writable: the loader refuses it
    stack.cold_restart()
    fresh = stack.login()
    assert_refusal(key_read(stack, fresh), 503, "ORDERS_UNAVAILABLE", "config_invalid")
    assert code_of(stack.mint(fresh, intent_id="intent-test-0006")) == (
        503,
        "ORDERS_UNAVAILABLE",
        "config_invalid",
    )


# ---------------------------------------------------------------- K10


def tree_snapshot(root: Path) -> dict[str, tuple[int, int, str]]:
    snap: dict[str, tuple[int, int, str]] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            st = path.lstat()
            snap[path.relative_to(root).as_posix()] = (
                st.st_size,
                st.st_mtime_ns,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
    return snap


def test_k10_twenty_reads_change_nothing_and_the_open_challenge_still_authorizes(stack, token, tmp_path):
    challenge_id, signature = stack.signed(token)
    state_before = stack.state(token)
    entries_before = stack.audit(token)
    # Everything below is copied after the last observing call, so the observers do not count.
    files = tree_snapshot(tmp_path)
    kinds = list(stack.budget.kinds)
    requests = list(stack.transport.requests)
    methods = list(stack.metadata.methods)
    live = len(stack.service.pipeline._challenges._items)
    assert live == 1

    for _ in range(20):
        assert_key_body(key_read(stack, token))

    assert tree_snapshot(tmp_path) == files
    assert stack.budget.kinds == kinds
    assert stack.transport.requests == requests
    assert stack.metadata.methods == methods
    assert len(stack.service.pipeline._challenges._items) == live
    assert stack.state(token) == state_before
    entries_after = stack.audit(token)
    assert (len(entries_after), entries_after[-1]["entry_sha256"]) == (
        len(entries_before),
        entries_before[-1]["entry_sha256"],
    )
    done = stack.authorize(token, challenge_id, signature)
    assert done.status_code == 200 and done.json()["decision"] == "VERIFIED_NOT_FORWARDED"


# ---------------------------------------------------------------- K11

HANDLER_ALLOWED_CALLS = {"session.authorize", "pipeline", "JSONResponse"}
HANDLER_ALLOWED_NAMES = {"session", "token", "authorization", "pipeline", "JSONResponse", "CONTRACT"}
HANDLER_ALLOWED_ATTRS = {"authorize", "failure", "key_id"}


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ast.dump(node)


def key_handler_violations(source: str) -> list[str]:
    """Why the key handler is more than bearer, failure gate and one in-memory read. Empty is clean."""
    tree = ast.parse(source)
    mount = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "mount_order_routes"
    )
    handlers = [
        n
        for n in ast.walk(mount)
        if isinstance(n, ast.FunctionDef)
        and any(
            isinstance(d, ast.Call)
            and isinstance(d.func, ast.Attribute)
            and d.func.attr == "get"
            and d.args
            and isinstance(d.args[0], ast.Name)
            and d.args[0].id == "ROUTE_KEY"
            for d in n.decorator_list
        )
    ]
    if len(handlers) != 1:
        return [f"expected one key handler, found {len(handlers)}"]
    found: list[str] = []
    for node in (n for stmt in handlers[0].body for n in ast.walk(stmt)):
        if isinstance(node, ast.Call) and _dotted(node.func) not in HANDLER_ALLOWED_CALLS:
            found.append("call " + _dotted(node.func))
        if isinstance(node, ast.Attribute) and node.attr not in HANDLER_ALLOWED_ATTRS:
            found.append("attribute " + node.attr)
        if isinstance(node, ast.Name) and node.id not in HANDLER_ALLOWED_NAMES:
            found.append("name " + node.id)
        if isinstance(node, (ast.With, ast.AsyncWith, ast.Await, ast.Try)):
            found.append(type(node).__name__)
    return found


def test_k11_the_key_handler_calls_only_authorize_pipeline_and_the_response():
    source = VM_ROUTES_FILE.read_text(encoding="utf-8")
    assert key_handler_violations(source) == []


ANCHOR = '        return JSONResponse({"contract": CONTRACT, "key_id": pipeline().key_id})'


@pytest.mark.parametrize(
    "plant",
    [
        "        session.require_active(authorization)\n",
        "        session.require_egress(token)\n",
        "        service.kill.read()\n",
        "        charge_budget(None, None)\n",
        "        service.audit.append({})\n",
        "        load_order_key('x')\n",
        "        with service.store.lock():\n            pass\n",
    ],
    ids=["require_active", "require_egress", "kill_read", "charge_budget", "audit_append", "load_order_key", "store_lock"],
)
def test_k11_each_planted_side_effect_turns_the_scan_red(plant):
    source = VM_ROUTES_FILE.read_text(encoding="utf-8")
    assert source.count(ANCHOR) == 1
    planted = source.replace(ANCHOR, plant + ANCHOR)
    assert key_handler_violations(planted) != []


# ---------------------------------------------------------------- K12


def request_events(stack) -> list[dict]:
    return stack.events_named("request")


def test_k12_one_request_event_per_call_and_nothing_secret_anywhere(stack, token):
    before = len(request_events(stack))
    ok = key_read(stack, token)
    events = request_events(stack)
    assert len(events) == before + 1
    assert (events[-1]["route"], events[-1]["result"], events[-1]["http"]) == (KEY_PATH, "ok", 200)
    refused = key_read(stack, "wrong-bearer")
    events = request_events(stack)
    assert len(events) == before + 2
    assert (events[-1]["route"], events[-1]["http"], events[-1]["reason"]) == (KEY_PATH, 401, "unauthorized")

    secrets = [token, "wrong-bearer", PRIMARY["public_key_x963_hex"], APP_KEY, SECRET_KEY, EXPECTED_IP, *secret_forms()]
    for text in (stack.sink.getvalue(), ok.text, refused.text):
        for secret in secrets:
            assert secret not in text
    assert PRIMARY["key_id"] not in refused.text


# ---------------------------------------------------------------- Mac helpers


def orders_reply(payload, status: int = 200, *, contract: str = MAC_ORDERS_CONTRACT, ctype: str = "application/json"):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return httpx.Response(
        status,
        headers={
            vm_contract.HDR_CONTRACT: contract,
            vm_contract.HDR_REQUEST_ID: RID,
            "Cache-Control": "no-store",
            "Content-Type": ctype,
        },
        content=body,
    )


def orders_error(code: str, status: int, reason: str | None = None):
    error = {
        "code": code,
        "reason": reason,
        "breeze_status": None,
        "retryable": False,
        "retry_after_s": None,
        "fields": None,
    }
    return orders_reply({"contract": MAC_ORDERS_CONTRACT, "error": error}, status)


def mac_store(token: str = MAC_TOKEN) -> MemoryTokenStore:
    store = MemoryTokenStore()
    store.save(token, datetime(2099, 1, 1, tzinfo=timezone(timedelta(hours=5, minutes=30))))
    return store


class Recorder:
    """Wraps an httpx.Client: records (method, path) and delegates. Never a socket."""

    def __init__(self, inner: httpx.Client) -> None:
        self._inner = inner
        self.calls: list[tuple[str, str]] = []

    def stream(self, method, path, **kwargs):
        self.calls.append((method, path))
        return self._inner.stream(method, path, **kwargs)

    def close(self) -> None:
        """The owner closes the inner client."""


def canned_client(reply) -> tuple[OrdersClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return reply(request) if callable(reply) else reply

    http = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://127.0.0.1:8443")
    return OrdersClient(RelayClient(http=http, token_store=mac_store())), seen


# ---------------------------------------------------------------- K13


def test_k13_a_good_reply_returns_the_id_after_one_get(stack):
    client, seen = canned_client(orders_reply({"contract": MAC_ORDERS_CONTRACT, "key_id": HEX_A}))
    assert client.current_key() == HEX_A
    assert [(r.method, r.url.path, r.url.query) for r in seen] == [("GET", KEY_PATH, b"")]
    assert seen[0].headers[vm_contract.HDR_CONTRACT] == MAC_ORDERS_CONTRACT
    assert seen[0].content == b""


BAD_FIELD_BODIES = {
    "missing_key_id": {"contract": MAC_ORDERS_CONTRACT},
    "extra_key": {"contract": MAC_ORDERS_CONTRACT, "key_id": HEX_A, "note": "x"},
    "uppercase": {"contract": MAC_ORDERS_CONTRACT, "key_id": "A" * 64},
    "mixed_case": {"contract": MAC_ORDERS_CONTRACT, "key_id": "a" * 63 + "B"},
    "len_63": {"contract": MAC_ORDERS_CONTRACT, "key_id": "a" * 63},
    "len_65": {"contract": MAC_ORDERS_CONTRACT, "key_id": "a" * 65},
    "trailing_newline": {"contract": MAC_ORDERS_CONTRACT, "key_id": HEX_A + "\n"},
    "non_hex": {"contract": MAC_ORDERS_CONTRACT, "key_id": "a" * 63 + "g"},
    "int": {"contract": MAC_ORDERS_CONTRACT, "key_id": 7},
    "null": {"contract": MAC_ORDERS_CONTRACT, "key_id": None},
    "bool": {"contract": MAC_ORDERS_CONTRACT, "key_id": True},
    "list": {"contract": MAC_ORDERS_CONTRACT, "key_id": [HEX_A]},
}


@pytest.mark.parametrize("body", BAD_FIELD_BODIES.values(), ids=BAD_FIELD_BODIES.keys())
def test_k13_any_other_shape_is_bad_field(body):
    client, seen = canned_client(orders_reply(body))
    with pytest.raises(OrdersProtocolError) as caught:
        client.current_key()
    assert caught.value.reason == "bad_field"
    assert len(seen) == 1


@pytest.mark.parametrize(
    "reply",
    [
        orders_reply({"key_id": HEX_A}),
        orders_reply({"contract": "growin-relay/1", "key_id": HEX_A}),
    ],
    ids=["no_contract", "other_contract"],
)
def test_k13_a_missing_or_other_body_contract_is_a_contract_mismatch(reply):
    client, _ = canned_client(reply)
    with pytest.raises(RelayContractMismatch):
        client.current_key()


@pytest.mark.parametrize(
    "reply",
    [
        orders_reply(b"not json"),
        orders_reply([HEX_A]),
        orders_reply({"contract": MAC_ORDERS_CONTRACT, "key_id": HEX_A}, ctype="text/plain"),
    ],
    ids=["not_json", "array", "content_type"],
)
def test_k13_non_json_arrays_and_wrong_content_types_are_protocol_errors(reply):
    client, _ = canned_client(reply)
    with pytest.raises(RelayProtocolError):
        client.current_key()


# ---------------------------------------------------------------- K14


def test_k14_a_404_from_an_older_vm_is_route_unavailable_with_no_audit_fallback(stack, token):
    stack.app.router.routes[:] = [r for r in stack.app.router.routes if getattr(r, "path", "") != KEY_PATH]
    recorder = Recorder(stack.client)
    client = OrdersClient(RelayClient(http=recorder, token_store=mac_store(token)))
    with pytest.raises(OrdersKeyRouteUnavailable) as caught:
        client.current_key()
    assert isinstance(caught.value, NotFound)
    assert caught.value.reason == "route_unavailable" and caught.value.http_status == 404
    assert recorder.calls == [("GET", KEY_PATH)]
    assert [c for c in recorder.calls if c[1] == "/v1/orders/audit"] == []
    assert all(e["route"] != "/v1/orders/audit" for e in request_events(stack))


@pytest.mark.parametrize(
    "reply,expected",
    [
        (orders_error("METHOD_NOT_ALLOWED", 405), MethodNotAllowed),
        (orders_error("ORDERS_UNAVAILABLE", 503, "config_invalid"), OrdersUnavailable),
        (orders_error("UNAUTHORIZED", 401), Unauthorized),
    ],
    ids=["405", "503", "401"],
)
def test_k14_every_other_error_passes_through_unchanged(reply, expected):
    client, seen = canned_client(reply)
    with pytest.raises(expected) as caught:
        client.current_key()
    assert not isinstance(caught.value, (OrdersKeyRouteUnavailable, NotFound))
    assert [(r.method, r.url.path) for r in seen] == [("GET", KEY_PATH)]  # never retried, never audit


# ---------------------------------------------------------------- K15


def test_k15_the_two_route_constants_match_and_the_mac_reads_the_pipelines_key(stack, token):
    assert mac_orders.ROUTE_KEY == vm_orders.ROUTE_KEY == KEY_PATH
    client = OrdersClient(RelayClient(http=stack.client, token_store=mac_store(token)))
    assert client.current_key() == stack.service.pipeline.key_id == PRIMARY["key_id"]
    swap_key(stack, OTHER)
    assert client.current_key() == stack.service.pipeline.key_id == OTHER["key_id"]


# ---------------------------------------------------------------- K16


def run_cli(client_factory) -> tuple[int, str]:
    out = io.StringIO()
    code = cli_mod.main(["orders-key"], client_factory=client_factory, out=out)
    return code, out.getvalue()


def test_k16_cli_prints_one_line_and_exits_zero(stack, token):
    code, text = run_cli(lambda: RelayClient(http=Recorder(stack.client), token_store=mac_store(token)))
    assert code == 0
    assert text == f"key_id {PRIMARY['key_id']}\n"
    assert token not in text


def test_k16_cli_on_a_404_exits_one_with_the_code_only():
    http = httpx.Client(
        transport=httpx.MockTransport(lambda request: orders_error("NOT_FOUND", 404)),
        base_url="http://127.0.0.1:8443",
    )
    code, text = run_cli(lambda: RelayClient(http=http, token_store=mac_store()))
    assert code == cli_mod.EXIT_RELAY == 1
    assert text.startswith("NOT_FOUND") and "reason route_unavailable" in text
    assert text.count("\n") == 1
    assert MAC_TOKEN not in text and "key_id" not in text
