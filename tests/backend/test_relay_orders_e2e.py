"""63-05 Task 3: the Mac SHADOW venue end to end over the real VM relay app.

See ``relay_orders_support`` for what is real and what is faked. Breeze is always a fake
transport, and every test using the ``world`` fixture ends with the standing assertions: zero
non-GET requests to Breeze (the order endpoint included) and zero non-GET requests to the
metadata fake. The signing key is the TEST ONLY vector key; no network, Keychain or secret.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import shutil
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import india_limits_support as ils
import relay_orders_support as ros
from regime_testkit import bound_admit
from brokers.breeze_gateway import cli as relay_cli
from brokers.breeze_gateway.orders import OrdersClient, match_vm_audit
from execution import (
    AdmissionDecision,
    ApprovalConflict,
    ApprovalVerificationError,
    BrokerOutcomeUnknownError,
    ExecutionConflictError,
    ExecutionDisabledError,
    OrderMode,
    OrderNotPlacedError,
)
from execution.approval import RelayChallengeRefused
from execution.relay_dispatcher import RELAY_FORWARD_DISABLED, RelayDispatcher
from execution.venue import (
    LIVE_DISABLED,
    PRICE_SOURCE_OPERATOR_RECORDED,
    PRICE_SOURCE_RELAY_QUOTE,
    VENUE_BREEZE_RELAY,
    execution_mode_label,
    intent_refusal,
)
from test_gateway_orders_pipeline import assert_no_order_path, find_order_paths
from gateway_vm.transport import HttpResponse
from test_gateway_orders_routes import MetadataFake, OTHER, VECTORS
from test_gateway_relay_app import breeze_body

# A cold `server` import is slow enough to age replay quotes past the 5s session
# window. Import it before any fixture stamps those quotes.
from server import app

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def metadata():
    server = MetadataFake()
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest_asyncio.fixture
async def world(tmp_path, metadata, monkeypatch):
    built = ros.ShadowWorld(tmp_path, metadata, monkeypatch)
    await built.start_replay()
    built.enroll_signer()
    try:
        yield built
        ros.standing_assertions(built)
    finally:
        await built.app.close_market_data()
        built.close()


async def prepared(world):
    proposal, admission = world.prepare()
    assert admission.decision is AdmissionDecision.ADMITTED, admission.reason_code
    return proposal["proposal_id"]


def event_types(world, proposal_id):
    return [event.event_type for event in world.ledger.list_events(proposal_id)]


def relay_events(world, proposal_id):
    return [
        event.payload
        for event in world.ledger.list_events(proposal_id)
        if event.event_type in {"RELAY_CHALLENGED", "RELAY_AUTHORIZE_RESULT"}
    ]


def vm_entries(world):
    return OrdersClient(world.client).audit_all()


# ---------------------------------------------------------------- the tracer


@pytest.mark.asyncio
async def test_tracer_signed_shadow_order_is_verified_by_the_vm_and_not_placed(world):
    pid = await prepared(world)
    proposal = world.service.get_proposal(pid)
    assert proposal["mode"] == "SHADOW" and proposal["broker"] == VENUE_BREEZE_RELAY
    assert proposal["limit_price"] == "100.00"  # the relay LTP floored to the tick

    challenge = world.challenge(pid)
    signed = json.loads(challenge.signed_payload)
    assert signed["purpose"] == "growin.relay.order"  # the VM's bytes, not a Mac-built payload
    assert signed["intent"]["stock_code"] == ros.SYMBOL
    assert signed["limits_sha256"] == VECTORS["limits_sha256"]  # the Mac and the VM share limits
    assert world.http.count("POST", "/v1/orders/intents") == 1
    assert world.http.count("POST", "/v1/orders/authorize") == 0

    with pytest.raises(OrderNotPlacedError) as outcome:
        await world.approve(pid, challenge)
    assert outcome.value.reason_code == RELAY_FORWARD_DISABLED
    assert outcome.value.rejected is False  # a definite non-placement, not a rejection

    assert world.http.count("POST", "/v1/orders/authorize") == 1
    order = world.ledger.get_order(pid)
    assert order.state == "FAILED"  # nothing was placed: the order never reaches ACKNOWLEDGED
    assert world.ledger.get_reservation(pid).state != "ACTIVE"  # the reserved buying power is back
    types = event_types(world, pid)
    assert "DISPATCH_CLAIMED" in types and "DISPATCH_FAILED" in types
    # The same signature the Mac verified and claimed is what the VM verified (VM audit seq 2).
    decisions = [entry.decision for entry in vm_entries(world)]
    assert decisions == ["CHALLENGED", "VERIFIED_NOT_FORWARDED"]
    assert world.vm.transport.order_endpoint_non_get() == []


@pytest.mark.asyncio
async def test_a_signed_order_is_forwarded_exactly_once_and_never_retried(world):
    pid = await prepared(world)
    challenge = world.challenge(pid)
    with pytest.raises(OrderNotPlacedError):
        await world.approve(pid, challenge)
    with pytest.raises(ExecutionConflictError, match="already consumed"):
        await world.approve(pid, challenge)
    assert world.http.count("POST", "/v1/orders/authorize") == 1


@pytest.mark.asyncio
async def test_the_plain_dispatch_of_an_unsigned_order_always_refuses(world):
    intent = ils.make_intent(
        "unsigned-1", account=ros.SHADOW_ACCOUNT, broker=VENUE_BREEZE_RELAY, mode="SHADOW",
        ticker=ros.TICKER,
    )
    with pytest.raises(Exception) as refused:
        await world.dispatcher.dispatch(intent)
    assert getattr(refused.value, "reason_code", None) == "SIGNED_DISPATCH_REQUIRED"
    assert world.http.calls == []  # nothing was sent


def test_shadow_is_an_order_mode_and_the_ledger_reports_shadow_not_live(world):
    assert OrderMode.SHADOW.value == "SHADOW"
    assert world.app.execution_mode == "shadow"
    binding = world.ledger.venue_binding
    assert execution_mode_label(True, binding) == "shadow"
    assert execution_mode_label(False, binding) == "disabled"


@pytest.mark.asyncio
async def test_live_stays_refused_on_the_shadow_venue(world):
    binding = world.ledger.venue_binding
    assert intent_refusal("LIVE", VENUE_BREEZE_RELAY, ros.SHADOW_ACCOUNT, binding) == LIVE_DISABLED
    assert intent_refusal("PAPER", "paper", "paper", binding) is not None
    assert intent_refusal("SHADOW", VENUE_BREEZE_RELAY, ros.SHADOW_ACCOUNT, binding) is None
    # A LIVE intent cannot even be registered into the SHADOW ledger as an approvable order.
    live = ils.make_intent(
        "live-1", account=ros.SHADOW_ACCOUNT, broker=VENUE_BREEZE_RELAY, mode="LIVE",
        ticker=ros.TICKER,
    )
    admission = world.service.admit(
        live, currency="INR", price="100.00", price_divisor="1", price_source="relay-quote",
        simulator_evidence={"simulated_fill_price": "100.00"},
        risk_evidence={"scaled_size": "1"}, **bound_admit(),
        india_quote=world.app.india_quote_from_relay(ros.SYMBOL),
    )
    assert admission.decision is AdmissionDecision.DENIED
    # approve_signed itself still raises for LIVE, before any claim and before any send.
    with pytest.raises(ExecutionDisabledError, match="Live execution remains disabled"):
        await world.service.approve_signed(
            "live-1", "0" * 36, b"\x30\x00", workspace="india"
        )
    assert world.http.order_calls() == []


@pytest.mark.asyncio
async def test_a_shadow_binding_admits_only_the_relay_quote_source(world):
    # Opus G3 (S1b): the UK practice source is not admissible on the India SHADOW venue.
    assert world.service.admissible_price_sources == frozenset({PRICE_SOURCE_RELAY_QUOTE})
    shadow = ils.make_intent(
        "src-1", account=ros.SHADOW_ACCOUNT, broker=VENUE_BREEZE_RELAY, mode="SHADOW",
        ticker=ros.TICKER,
    )
    admission = world.service.admit(
        shadow, currency="INR", price="100.00", price_divisor="1",
        price_source=PRICE_SOURCE_OPERATOR_RECORDED,
        simulator_evidence={"simulated_fill_price": "100.00"},
        risk_evidence={"scaled_size": "1"}, **bound_admit(),
        india_quote=world.app.india_quote_from_relay(ros.SYMBOL),
    )
    assert admission.decision is AdmissionDecision.DENIED
    assert admission.reason_code == "PRICE_SOURCE_NOT_ADMISSIBLE"
    assert world.ledger.get_reservation("src-1") is None
    assert world.http.order_calls() == []


# ---------------------------------------------------- Mac-denied orders never reach the VM


@pytest.mark.asyncio
async def test_an_over_cap_admission_is_denied_on_the_mac_and_the_vm_is_never_asked(world):
    proposal, admission = world.prepare(quantity="200")  # 200 x 100.00 is over the 17500 cap
    assert admission.decision is AdmissionDecision.DENIED
    assert admission.reason_code == "per_position_cap"
    assert world.http.order_calls() == []  # transport count zero for /v1/orders/*
    assert vm_entries(world) == ()
    with pytest.raises(ApprovalConflict):
        world.challenge(proposal["proposal_id"])
    assert world.http.order_calls() == []


@pytest.mark.asyncio
async def test_a_missing_relay_quote_denies_the_admission_and_asks_the_vm_for_no_order(world):
    del world.vm.transport.quote_book[ros.SYMBOL]
    proposal, admission = world.prepare()
    assert admission.decision is AdmissionDecision.DENIED
    assert "limit_price" not in proposal  # no trusted quote, so no limit was invented
    assert world.http.order_calls() == []


@pytest.mark.asyncio
async def test_a_mac_recheck_denial_at_challenge_time_never_reaches_the_vm(world):
    pid = await prepared(world)
    # The market gaps up after admission: the Mac's own fresh-quote recheck denies the challenge.
    world.vm.transport.quote_book[ros.SYMBOL]["ltp"] = 109.0
    world.vm.transport.quote_book[ros.SYMBOL]["best_bid_price"] = 108.9
    world.vm.transport.quote_book[ros.SYMBOL]["best_offer_price"] = 109.0
    with pytest.raises(ApprovalConflict):
        world.challenge(pid)
    assert world.http.count("POST", "/v1/orders/intents") == 0
    assert vm_entries(world) == ()


@pytest.mark.asyncio
async def test_a_shadow_sell_with_no_mac_holding_is_refused_on_the_mac(world):
    sell = ils.make_intent(
        "sell-1", side="SELL", account=ros.SHADOW_ACCOUNT, broker=VENUE_BREEZE_RELAY,
        mode="SHADOW", ticker=ros.TICKER,
    )
    admission = world.service.admit(
        sell, currency="INR", price="100.00", price_divisor="1", price_source="relay-quote",
        simulator_evidence={"simulated_fill_price": "100.00"},
        risk_evidence={"scaled_size": "1"}, **bound_admit(),
        india_quote=world.app.india_quote_from_relay(ros.SYMBOL),
    )
    assert admission.decision is AdmissionDecision.DENIED
    assert admission.reason_code == "sell_exceeds_holding"  # SHADOW fills never reach the Mac ledger
    assert world.http.order_calls() == []


# ----------------------------------------- the Mac refuses bytes that are not its own intent


def _canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def _mutate(edit):
    def tamper(challenge_id: str, signed: bytes):
        payload = json.loads(signed)
        edit(payload)
        return challenge_id, _canonical(payload)

    return tamper


def _raw(edit):
    return lambda challenge_id, signed: (challenge_id, edit(signed))


TAMPERS = {
    "intent_quantity": _mutate(lambda p: p["intent"].__setitem__("quantity", 11)),
    "intent_limit_price": _mutate(lambda p: p["intent"].__setitem__("limit_price", "101.00")),
    "intent_side": _mutate(lambda p: p["intent"].__setitem__("side", "sell")),
    "intent_stock_code": _mutate(lambda p: p["intent"].__setitem__("stock_code", "ABC")),
    "intent_isin": _mutate(lambda p: p["intent"].__setitem__("isin", "INE111B01023")),
    "intent_key_id": _mutate(lambda p: p["intent"].__setitem__("key_id", "0" * 64)),
    "intent_limits_sha256": _mutate(lambda p: p["intent"].__setitem__("limits_sha256", "1" * 64)),
    "top_level_limits_sha256": _mutate(lambda p: p.__setitem__("limits_sha256", "1" * 64)),
    "top_level_key_id": _mutate(lambda p: p.__setitem__("key_id", "2" * 64)),
    "purpose": _mutate(lambda p: p.__setitem__("purpose", "growin.execution.dispatch")),
    "extra_key": _mutate(lambda p: p.__setitem__("extra", 1)),
    "extra_intent_key": _mutate(lambda p: p["intent"].__setitem__("extra", 1)),
    "missing_nonce": _mutate(lambda p: p.pop("nonce")),
    "lifetime_too_long": _mutate(lambda p: p.__setitem__("expires_at", p["expires_at"] + 3600)),
    "float_value": _raw(lambda b: b.replace(b'"version":1', b'"version":1.0', 1)),
    "not_canonical": _raw(lambda b: json.dumps(json.loads(b)).encode()),
    "duplicate_key": _raw(lambda b: b.replace(b'"version":1', b'"version":1,"version":1', 1)),
    "not_json": _raw(lambda b: b"not json"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(TAMPERS))
async def test_the_mac_refuses_vm_bytes_that_differ_from_its_own_intent(world, monkeypatch, name):
    pid = await prepared(world)
    real_mint = OrdersClient.mint
    tamper = TAMPERS[name]

    def mint(self, intent):
        return tamper(*real_mint(self, intent))

    monkeypatch.setattr(OrdersClient, "mint", mint)
    with pytest.raises(RelayChallengeRefused) as refused:
        world.challenge(pid)
    assert refused.value.reason_code.startswith("mint_")
    # Nothing was stored as a local challenge, so there is nothing to sign and nothing to send.
    assert world.ledger.get_order(pid).state == "PENDING"
    assert world.ledger.get_reservation(pid).state == "ACTIVE"
    assert "APPROVAL_CHALLENGE_CREATED" not in event_types(world, pid)
    refusal = [e for e in relay_events(world, pid) if e["decision"] == "REFUSED"]
    assert len(refusal) == 1 and refusal[0]["reason_code"] == refused.value.reason_code
    assert world.http.count("POST", "/v1/orders/authorize") == 0


# The claim is the second wall. It compares the VM's bytes with the intent the Mac stored beside
# the challenge (``RELAY_INTENT_EXPECTED``, written from the Mac's own build, never from the VM's
# bytes) and, separately, with the durable order and its admission. limits_sha256 is checked at
# mint (the TAMPERS cases) and by the VM itself; the ledger holds no copy of the limits.
NOT_EXPECTED = "is not the intent the Mac expected"
NOT_ADMITTED = "does not match the admitted order"
NOT_CHALLENGE = "does not match the challenge"
NEW_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"


def _intent_edit(field, value):
    return lambda p: p["intent"].__setitem__(field, value)


# Every intent field, then the extra field: the VM bytes differ from what the Mac expected.
INTENT_FIELDS = {
    "intent_id": "i" + "a" * 40,
    "proposal_id": "p-other",
    "workspace": "uk",
    "broker": "trading212",
    "mode": "PAPER",
    "exchange": "BSE",
    "product": "margin",
    "order_type": "market",
    "validity": "ioc",
    "side": "sell",
    "stock_code": "ABC",
    "isin": "INE111B01023",
    "quantity": 11,
    "limit_price": "100.05",
    "reason": "stop",
    "batch_id": "batch-abcdef",
    "limits_sha256": "1" * 64,
    "params_sha256": "2" * 64,
    "key_id": "3" * 64,
}
CLAIM_TAMPERS = {f"intent_{name}": _intent_edit(name, value) for name, value in INTENT_FIELDS.items()}
CLAIM_TAMPERS["intent_extra_field"] = _intent_edit("extra", 1)
CLAIM_TAMPERS["intent_field_dropped"] = lambda p: p["intent"].pop("reason")


async def _claim_forged(world, pid, edit, *, expected="genuine", stored_expires_delta=0, stored_issued_delta=0, stored_id=NEW_ID):
    """Store bytes the VM would never have minted straight in the ledger, then try to claim them.

    ``expected`` is the Mac's stored intent: "genuine" (what it built), None (no record), or a
    callable that edits a copy of the genuine one (the Mac's own record disagreeing with the order).
    """

    challenge = world.challenge(pid)
    genuine = json.loads(challenge.signed_payload)
    forged_payload = json.loads(challenge.signed_payload)
    forged_payload["challenge_id"] = stored_id
    edit(forged_payload)
    forged = _canonical(forged_payload)
    if expected == "genuine":
        recorded = genuine["intent"]
    elif expected is None:
        recorded = None
    else:
        recorded = json.loads(_canonical(genuine["intent"]))
        expected(recorded)
    order = world.ledger.get_order(pid)
    key = world.ledger.get_approval_key(workspace="india")
    stored = world.ledger.store_approval_challenge(
        challenge_id=stored_id,
        proposal_id=pid,
        key_id=key.key_id,
        intent_hash=order.intent_hash,
        signed_payload=forged,
        issued_at_epoch=genuine["issued_at"] + stored_issued_delta,
        expires_at_epoch=genuine["expires_at"] + stored_expires_delta,
        expected_intent=recorded,
    )
    claim = world.service.approve_signed(
        pid, stored.challenge_id, world.signature(forged), workspace="india",
        india_quote=world.app.india_quote_from_relay(ros.SYMBOL),
    )
    return claim


async def _assert_claim_refused(world, pid, claim, message):
    with pytest.raises(ExecutionConflictError, match=message):
        await claim
    assert world.http.count("POST", "/v1/orders/authorize") == 0
    assert world.ledger.get_order(pid).state == "PENDING"
    assert world.ledger.get_reservation(pid).state == "ACTIVE"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(CLAIM_TAMPERS))
async def test_the_claim_refuses_relay_bytes_that_differ_from_the_intent_the_mac_stored(world, name):
    """Every intent field, even though the enrolled key signed the bytes and the order is intact."""

    pid = await prepared(world)
    claim = await _claim_forged(world, pid, CLAIM_TAMPERS[name])
    await _assert_claim_refused(world, pid, claim, NOT_EXPECTED)


# The Mac's own stored record is wrong too (a bad build, or a record written to match forged
# bytes). The second check, against the durable order and its admission, still refuses.
ORDER_DISAGREES = {
    "side": ("side", "sell"),
    "proposal_id": ("proposal_id", "p-other"),
    "stock_code": ("stock_code", "ABC"),
    "workspace": ("workspace", "uk"),
    "mode": ("mode", "PAPER"),
    "broker": ("broker", "trading212"),
    "exchange": ("exchange", "BSE"),
    "product": ("product", "margin"),
    "order_type": ("order_type", "market"),
    "validity": ("validity", "ioc"),
    "quantity": ("quantity", 11),
    "limit_price": ("limit_price", "100.05"),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(ORDER_DISAGREES))
async def test_the_claim_refuses_an_intent_that_disagrees_with_the_order_even_if_the_record_agrees(
    world, name
):
    field, value = ORDER_DISAGREES[name]
    edit = _intent_edit(field, value)
    pid = await prepared(world)
    claim = await _claim_forged(world, pid, edit, expected=lambda recorded: recorded.__setitem__(field, value))
    await _assert_claim_refused(world, pid, claim, NOT_ADMITTED)


@pytest.mark.asyncio
async def test_the_claim_refuses_a_boolean_quantity_even_when_the_record_agrees(world):
    # Mutant M10: Decimal(True) == Decimal(1), so only the explicit bool check refuses this.
    proposal, admission = world.prepare(quantity="1")
    assert admission.decision is AdmissionDecision.ADMITTED
    pid = proposal["proposal_id"]
    edit = _intent_edit("quantity", True)
    claim = await _claim_forged(world, pid, edit, expected=lambda recorded: recorded.__setitem__("quantity", True))
    await _assert_claim_refused(world, pid, claim, NOT_ADMITTED)


@pytest.mark.asyncio
async def test_the_claim_refuses_a_float_quantity_even_when_the_record_agrees(world):
    # Decimal(1.0) == Decimal(1) and 1.0 is not a bool, so only isinstance(quantity, int) refuses this.
    proposal, admission = world.prepare(quantity="1")
    assert admission.decision is AdmissionDecision.ADMITTED
    pid = proposal["proposal_id"]
    edit = _intent_edit("quantity", 1.0)
    claim = await _claim_forged(world, pid, edit, expected=lambda recorded: recorded.__setitem__("quantity", 1.0))
    await _assert_claim_refused(world, pid, claim, NOT_ADMITTED)


@pytest.mark.asyncio
async def test_the_claim_refuses_a_boolean_quantity_the_mac_did_not_store(world):
    proposal, admission = world.prepare(quantity="1")
    pid = proposal["proposal_id"]
    claim = await _claim_forged(world, pid, _intent_edit("quantity", True))
    await _assert_claim_refused(world, pid, claim, NOT_EXPECTED)


@pytest.mark.asyncio
async def test_the_claim_refuses_relay_bytes_the_mac_never_recorded_an_intent_for(world):
    pid = await prepared(world)
    claim = await _claim_forged(world, pid, lambda p: None, expected=None)
    await _assert_claim_refused(world, pid, claim, NOT_EXPECTED)


# The envelope: the part of the signed payload outside the intent.
ENVELOPE_TAMPERS = {
    "top_level_key_id": (lambda p: p.__setitem__("key_id", "2" * 64), {}, NOT_CHALLENGE),
    "extra_top_level_key": (lambda p: p.__setitem__("extra", 1), {}, NOT_CHALLENGE),
    "version": (lambda p: p.__setitem__("version", 2), {}, NOT_CHALLENGE),
    "challenge_id": (lambda p: p.__setitem__("challenge_id", "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"), {}, NOT_CHALLENGE),
    "expires_at": (lambda p: None, {"stored_expires_delta": 1}, NOT_CHALLENGE),
    "issued_at": (lambda p: None, {"stored_issued_delta": -1}, NOT_CHALLENGE),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(ENVELOPE_TAMPERS))
async def test_the_claim_refuses_a_relay_envelope_that_differs_from_the_stored_challenge(world, name):
    edit, options, message = ENVELOPE_TAMPERS[name]
    pid = await prepared(world)
    claim = await _claim_forged(world, pid, edit, **options)
    await _assert_claim_refused(world, pid, claim, message)


@pytest.mark.asyncio
async def test_a_relay_claim_needs_a_venue_bound_ledger(world):
    """The ledger refuses relay-purpose bytes when it has no venue binding (defence in depth).

    The service refuses earlier for an unbound ledger, so this goes to the ledger claim itself.
    """

    pid = await prepared(world)
    challenge = world.challenge(pid)
    key = world.ledger.get_approval_key(workspace="india")
    real = world.ledger.venue_binding
    world.ledger.venue_binding = None
    try:
        with pytest.raises(ApprovalConflict, match=NOT_CHALLENGE):
            world.ledger.claim_with_approval(
                proposal_id=pid,
                challenge_id=challenge.challenge_id,
                key_id=key.key_id,
                signature_der=world.signature(challenge.signed_payload),
                verified_payload_hash=hashlib.sha256(challenge.signed_payload).hexdigest(),
                now_epoch=challenge.issued_at_epoch,
            )
    finally:
        world.ledger.venue_binding = real
    assert world.ledger.get_order(pid).state == "PENDING"


@pytest.mark.asyncio
async def test_the_mac_refuses_vm_bytes_with_a_boolean_quantity_where_it_expected_one(world, monkeypatch):
    # Opus G1: ``True == 1`` made a != on the parsed intents pass these bytes. The canonical bytes
    # differ ("true" against "1"), so the mint is refused.
    proposal, admission = world.prepare(quantity="1")
    pid = proposal["proposal_id"]
    real_mint = OrdersClient.mint

    def mint(self, intent):
        return _mutate(_intent_edit("quantity", True))(*real_mint(self, intent))

    monkeypatch.setattr(OrdersClient, "mint", mint)
    with pytest.raises(RelayChallengeRefused) as refused:
        world.challenge(pid)
    assert refused.value.reason_code == "mint_intent"
    assert "APPROVAL_CHALLENGE_CREATED" not in event_types(world, pid)


@pytest.mark.asyncio
async def test_the_mac_refuses_a_challenge_id_that_differs_from_the_bytes(world, monkeypatch):
    pid = await prepared(world)
    real_mint = OrdersClient.mint
    monkeypatch.setattr(
        OrdersClient,
        "mint",
        lambda self, intent: ("00000000-0000-4000-8000-000000000000", real_mint(self, intent)[1]),
    )
    with pytest.raises(RelayChallengeRefused) as refused:
        world.challenge(pid)
    assert refused.value.reason_code.startswith("mint_")


@pytest.mark.asyncio
async def test_a_vm_key_that_is_not_the_enrolled_key_is_refused_at_mint(tmp_path, metadata, monkeypatch):
    other = ros.ShadowWorld(tmp_path, metadata, monkeypatch, signer=OTHER)
    await other.start_replay()
    other.enroll_signer()  # the Mac signs with OTHER, the VM only knows PRIMARY
    try:
        proposal, admission = other.prepare()
        assert admission.decision is AdmissionDecision.ADMITTED
        with pytest.raises(RelayChallengeRefused) as refused:
            other.challenge(proposal["proposal_id"])
        assert refused.value.reason_code == "key_mismatch"
        assert other.ledger.get_order(proposal["proposal_id"]).state == "PENDING"
        ros.standing_assertions(other)
    finally:
        await other.app.close_market_data()
        other.close()


@pytest.mark.asyncio
async def test_a_mac_limits_file_that_differs_from_the_vm_is_refused_at_mint(
    tmp_path, metadata, monkeypatch
):
    world = ros.ShadowWorld(tmp_path, metadata, monkeypatch, capital_cap="49000")
    await world.start_replay()
    world.enroll_signer()
    try:
        proposal, admission = world.prepare()
        assert admission.decision is AdmissionDecision.ADMITTED
        with pytest.raises(RelayChallengeRefused) as refused:
            world.challenge(proposal["proposal_id"])
        assert refused.value.reason_code == "limits_hash_mismatch"
        ros.standing_assertions(world)
    finally:
        await world.app.close_market_data()
        world.close()


@pytest.mark.asyncio
async def test_a_second_challenge_while_one_is_outstanding_is_refused_by_the_vm(world):
    pid = await prepared(world)
    first = world.challenge(pid)
    with pytest.raises(RelayChallengeRefused) as refused:
        world.challenge(pid)
    assert refused.value.reason_code == "challenge_outstanding"
    # The first challenge is untouched and still completes.
    with pytest.raises(OrderNotPlacedError):
        await world.approve(pid, first)


# --------------------------------------------------- VM refusals map to the O6 code


@pytest.mark.asyncio
async def test_a_vm_halt_between_mint_and_authorize_is_rejected_with_the_o6_code(world):
    pid = await prepared(world)
    challenge = world.challenge(pid)
    assert world.vm.post("/v1/orders/halt", {}, world.token).status_code == 200
    with pytest.raises(OrderNotPlacedError) as refused:
        await world.approve(pid, challenge)
    assert refused.value.rejected is True
    assert refused.value.reason_code == "mac_halt"
    assert world.ledger.get_order(pid).state == "FAILED"
    failed = [e for e in world.ledger.list_events(pid) if e.event_type == "DISPATCH_FAILED"]
    assert failed[-1].payload["reason_code"] == "mac_halt"
    # The VM wrote CHALLENGED at mint, HALTED when the Mac halted it, REFUSED at authorize.
    assert [e.decision for e in vm_entries(world)] == ["CHALLENGED", "HALTED", "REFUSED"]


@pytest.mark.asyncio
async def test_a_kill_switch_flip_between_mint_and_authorize_is_rejected_with_kill_switch(world):
    pid = await prepared(world)
    challenge = world.challenge(pid)
    world.metadata.body = b"disabled"
    with pytest.raises(OrderNotPlacedError) as refused:
        await world.approve(pid, challenge)
    assert (refused.value.rejected, refused.value.reason_code) == (True, "kill_switch")


@pytest.mark.asyncio
async def test_a_vm_quote_that_moved_out_of_the_collar_is_rejected_with_collar(world):
    pid = await prepared(world)
    challenge = world.challenge(pid)
    stale = world.app.india_quote_from_relay(ros.SYMBOL)  # the Mac still holds the old quote
    row = world.vm.transport.quote_book[ros.SYMBOL]
    row["ltp"] = 105.0
    with pytest.raises(OrderNotPlacedError) as refused:
        await world.approve(pid, challenge, quote=stale)
    assert (refused.value.rejected, refused.value.reason_code) == (True, "collar")


@pytest.mark.asyncio
async def test_a_signature_by_another_key_never_reaches_the_vm(world):
    pid = await prepared(world)
    challenge = world.challenge(pid)
    forged = world.signature(challenge.signed_payload, key=OTHER)
    with pytest.raises(ApprovalVerificationError):
        await world.approve(pid, challenge, signature=forged)
    assert world.http.count("POST", "/v1/orders/authorize") == 0
    assert world.ledger.get_order(pid).state == "PENDING"


@pytest.mark.asyncio
async def test_an_expired_challenge_is_refused_on_the_mac_before_anything_is_sent(world):
    pid = await prepared(world)
    challenge = world.challenge(pid)
    world.vm.clock.advance(61)
    with pytest.raises(ExecutionConflictError):
        await world.approve(pid, challenge, quote=None)
    assert world.http.count("POST", "/v1/orders/authorize") == 0


@pytest.mark.asyncio
async def test_a_transport_failure_at_authorize_is_unknown_because_the_mac_cannot_tell(world):
    pid = await prepared(world)
    challenge = world.challenge(pid)
    world.http.unreachable.add(("POST", "/v1/orders/authorize"))
    with pytest.raises(BrokerOutcomeUnknownError):
        await world.approve(pid, challenge)
    # A connect error and a read error look alike from here: the Mac never guesses "not sent".
    assert world.ledger.get_order(pid).state == "UNKNOWN"
    assert world.http.count("POST", "/v1/orders/authorize") == 1


@pytest.mark.asyncio
async def test_a_client_closed_after_the_send_is_unknown_not_a_definite_refusal(world):
    # Fix r1 (Codex 1): the VM verified (VERIFIED_NOT_FORWARDED) and then the client closed under
    # the request. "closed" does not prove nothing was sent, so the order is UNKNOWN, not FAILED.
    pid = await prepared(world)
    challenge = world.challenge(pid)

    def retire() -> None:
        world.client._pool.retired = True

    world.http.closed_after_send[("POST", "/v1/orders/authorize")] = retire
    with pytest.raises(BrokerOutcomeUnknownError):
        await world.approve(pid, challenge)
    world.client._pool.retired = False  # the test's own reads go through the same client
    assert world.ledger.get_order(pid).state == "UNKNOWN"
    assert world.ledger.get_reservation(pid).state == "ACTIVE"  # still held: it may have been placed
    assert [e.decision for e in vm_entries(world)] == ["CHALLENGED", "VERIFIED_NOT_FORWARDED"]
    assert world.http.count("POST", "/v1/orders/authorize") == 1


# --------------------------------------------- a timeout after send is UNKNOWN, never retried


@pytest.mark.asyncio
async def test_a_timeout_after_send_is_unknown_and_is_not_retried(world):
    pid = await prepared(world)
    challenge = world.challenge(pid)
    world.http.timeout_after_send.add(("POST", "/v1/orders/authorize"))
    with pytest.raises(BrokerOutcomeUnknownError):
        await world.approve(pid, challenge)
    assert world.http.count("POST", "/v1/orders/authorize") == 1  # exactly one send
    assert world.ledger.get_order(pid).state == "UNKNOWN"
    assert world.ledger.get_reservation(pid).state == "ACTIVE"  # still held: it may have been placed
    # The VM did verify (its audit shows it) while the Mac holds UNKNOWN: the mismatch is visible.
    assert [e.decision for e in vm_entries(world)] == ["CHALLENGED", "VERIFIED_NOT_FORWARDED"]
    with pytest.raises(ExecutionConflictError, match="already consumed"):
        await world.approve(pid, challenge)  # an UNKNOWN order is never sent again
    assert world.http.count("POST", "/v1/orders/authorize") == 1


# --------------------------------------------------------- halt on engage, no clear anywhere


@pytest.mark.asyncio
async def test_engaging_the_workspace_control_also_halts_the_vm(world):
    world.service.engage_workspace_control("MANUAL_KILL", workspace="india")
    assert world.http.count("POST", "/v1/orders/halt") == 1
    assert "mac_halt" in world.vm.state(world.token)["latches"]
    assert world.vm.mint(world.token).status_code == 423  # the VM now refuses every mint


@pytest.mark.asyncio
async def test_a_failed_halt_call_never_undoes_the_local_control(world):
    world.http.unreachable.add(("POST", "/v1/orders/halt"))
    control = world.service.engage_workspace_control("MANUAL_KILL", workspace="india")
    assert control is not None
    assert world.ledger.get_workspace_control(workspace="india").engaged is True
    assert world.http.count("POST", "/v1/orders/halt") == 1


def test_the_mac_has_no_clear_reset_place_modify_or_cancel(world):
    forbidden = {"clear", "reset", "place", "modify", "cancel", "delete", "put", "patch"}
    for owner in (RelayDispatcher, OrdersClient):
        names = [n for n in dir(owner) if not n.startswith("__")]
        assert [n for n in names if forbidden & set(n.lower().split("_"))] == [], owner


# --------------------------------------------------------------- audit matching and the CLI


@pytest.mark.asyncio
async def test_mac_audit_entries_match_the_vm_audit_by_intent_sha256_and_decision(world):
    pid = await prepared(world)
    challenge = world.challenge(pid)
    with pytest.raises(OrderNotPlacedError):
        await world.approve(pid, challenge)
    result = match_vm_audit(relay_events(world, pid), vm_entries(world))
    assert len(result["matched"]) == 2
    assert result["mac_only"] == [] and result["vm_only"] == []
    sha = result["matched"][0][1].intent_sha256
    assert all(entry.intent_sha256 == sha for _event, entry in result["matched"])


@pytest.mark.asyncio
async def test_a_vm_entry_with_no_mac_event_and_a_mac_event_with_no_vm_entry_both_show(
    world, monkeypatch
):
    pid = await prepared(world)
    real_mint = OrdersClient.mint
    monkeypatch.setattr(
        OrdersClient, "mint", lambda self, intent: TAMPERS["intent_quantity"](*real_mint(self, intent))
    )
    with pytest.raises(RelayChallengeRefused):
        world.challenge(pid)
    result = match_vm_audit(relay_events(world, pid), vm_entries(world))
    # The VM minted (CHALLENGED) while the Mac recorded REFUSED: neither pairs up.
    assert [e["decision"] for e in result["mac_only"]] == ["REFUSED"]
    assert [e.decision for e in result["vm_only"]] == ["CHALLENGED"]


def run_cli(world, *argv):
    out = io.StringIO()
    code = relay_cli.main(list(argv), client_factory=world.cli_client, out=out)
    return code, out.getvalue()


@pytest.mark.asyncio
async def test_cli_orders_state_and_audit_print_numbers_and_ids_only(world):
    pid = await prepared(world)
    challenge = world.challenge(pid)
    with pytest.raises(OrderNotPlacedError):
        await world.approve(pid, challenge)

    code, state_out = run_cli(world, "orders-state")
    assert code == 0
    assert "kill enabled" in state_out and "latches none" in state_out
    assert "alerts_logged 0" in state_out

    code, audit_out = run_cli(world, "orders-audit")
    assert code == 0
    lines = audit_out.strip().splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("seq 1 decision CHALLENGED intent_id i")
    assert lines[1].startswith("seq 2 decision VERIFIED_NOT_FORWARDED intent_id i")
    for text in (state_out, audit_out):
        for secret in (ros.SYMBOL, "100.00", "INE000A01012", world.token, ros.SHADOW_ACCOUNT, "10 "):
            assert secret not in text
    code, after = run_cli(world, "orders-audit", "--after-seq", "1")
    assert code == 0 and after.strip().splitlines()[0].startswith("seq 2 ")
    code, bad = run_cli(world, "orders-audit", "--after-seq", "-1")
    assert code != 0


# ------------------------------------------------------------------------- the routes


@pytest_asyncio.fixture
async def api(world, monkeypatch):
    # An in-process ASGI client keeps every route on the test's own event loop thread, which is
    # also where the regime policy tables (an in-memory SQLite connection) were built.
    import routes.ai_routes as ai_routes
    import routes.market_data_routes as market_routes

    monkeypatch.setattr(ai_routes, "state", world.app)
    monkeypatch.setattr(market_routes, "state", world.app)
    # The world fixture stamps quotes before this fixture. Restamp after it so a
    # slow import cannot push them past the 5s session window. start_replay
    # refuses an active session, so close first.
    await world.app.close_market_data()
    await world.start_replay()
    async with AsyncClient(
        transport=ASGITransport(app=app, client=("127.0.0.1", 50000)), base_url="http://test"
    ) as client:
        yield client


PREP = {"confirmation": "PREPARE_INDIA_PAPER", "symbol": ros.SYMBOL, "quantity": "10"}


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode()


@pytest.mark.asyncio
async def test_routes_prepare_challenge_and_complete_a_shadow_order_through_the_relay(world, api):
    prepared_ = await api.post("/api/market-data/paper-preparations", json=PREP)
    assert prepared_.status_code == 201, prepared_.text
    body = prepared_.json()
    assert body["admission"]["decision"] == "ADMITTED"
    pid = body["proposal_id"]
    assert world.service.get_proposal(pid)["limit_price"] == "100.00"

    challenge = await api.post("/api/ai/trade/approval/challenge", json={"proposal_id": pid, "workspace": "india"})
    assert challenge.status_code == 200, challenge.text
    payload = base64.b64decode(challenge.json()["signed_payload_b64"])
    assert json.loads(payload)["purpose"] == "growin.relay.order"

    done = await api.post(
        "/api/ai/trade/approval/complete",
        json={
            "proposal_id": pid,
            "challenge_id": challenge.json()["challenge_id"],
            "signature_der_b64": _b64(world.signature(payload)),
            "workspace": "india",
        },
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "not_placed"
    assert done.json()["reason_code"] == RELAY_FORWARD_DISABLED
    assert world.ledger.get_order(pid).state == "FAILED"


@pytest.mark.asyncio
async def test_the_challenge_route_maps_a_vm_refusal_to_409_challenge_refused(world, api):
    pid = (await api.post("/api/market-data/paper-preparations", json=PREP)).json()["proposal_id"]
    assert world.vm.post("/v1/orders/halt", {}, world.token).status_code == 200
    resp = await api.post("/api/ai/trade/approval/challenge", json={"proposal_id": pid, "workspace": "india"})
    assert resp.status_code == 409
    assert resp.json()["detail"] == {"code": "CHALLENGE_REFUSED", "reason_code": "mac_halt"}


@pytest.mark.asyncio
async def test_the_complete_route_maps_a_vm_refusal_to_409_rejected_with_the_code(world, api):
    pid = (await api.post("/api/market-data/paper-preparations", json=PREP)).json()["proposal_id"]
    challenge = await api.post("/api/ai/trade/approval/challenge", json={"proposal_id": pid, "workspace": "india"})
    payload = base64.b64decode(challenge.json()["signed_payload_b64"])
    world.metadata.body = b"disabled"
    done = await api.post(
        "/api/ai/trade/approval/complete",
        json={
            "proposal_id": pid,
            "challenge_id": challenge.json()["challenge_id"],
            "signature_der_b64": _b64(world.signature(payload)),
            "workspace": "india",
        },
    )
    assert done.status_code == 409
    assert done.json()["detail"] == {"status": "REJECTED", "reason_code": "kill_switch"}


@pytest.mark.asyncio
async def test_paper_preparation_supplies_a_relay_limit_price_and_quote(world, api):
    resp = await api.post("/api/market-data/paper-preparations", json={**PREP, "quantity": "3"})
    assert resp.status_code == 201
    stored = world.service.get_proposal(resp.json()["proposal_id"])
    assert (stored["order_type"], stored["limit_price"]) == ("LIMIT", "100.00")
    quote_reads = [r for r in world.vm.transport.requests if r[1].endswith("/quotes")]
    assert quote_reads and {r[0] for r in quote_reads} == {"GET"}  # the quote came from a GET


@pytest.mark.asyncio
async def test_uat_proposal_creates_one_shadow_proposal_of_one_share_through_admission(world, api):
    resp = await api.post("/api/ai/trade/approval/uat-proposal", json={"workspace": "india", "symbol": ros.SYMBOL})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["ticker"], body["action"], body["quantity"]) == (ros.TICKER, "BUY", 1.0)
    stored = world.service.get_proposal(body["proposal_id"])
    assert (stored["mode"], stored["limit_price"], stored["quantity"]) == ("SHADOW", "100.00", "1")
    admission = world.ledger.get_admission(body["proposal_id"])
    assert admission.decision is AdmissionDecision.ADMITTED  # normal admission, not a bypass


@pytest.mark.asyncio
async def test_uat_proposal_needs_an_operator_named_symbol(world, api):
    resp = await api.post("/api/ai/trade/approval/uat-proposal", json={"workspace": "india"})
    assert resp.status_code == 422
    resp = await api.post(
        "/api/ai/trade/approval/uat-proposal", json={"workspace": "india", "symbol": "bad symbol"}
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_uat_proposal_refuses_a_denied_admission_with_its_reason(world, api):
    del world.vm.transport.quote_book[ros.SYMBOL]
    resp = await api.post("/api/ai/trade/approval/uat-proposal", json={"workspace": "india", "symbol": ros.SYMBOL})
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "ADMISSION_DENIED"


@pytest.mark.asyncio
async def test_uat_proposal_refuses_every_india_mode_but_shadow(tmp_path, monkeypatch):
    import routes.ai_routes as ai_routes
    from app_context import AppState
    from server import app

    monkeypatch.setenv("GROWIN_WORKSPACE", "india")
    private = ils.india_private_dir(tmp_path)  # venue paper: the old India PAPER ledger
    paper = AppState()
    assert paper.start_execution(
        tmp_path / "india.sqlite3", workspace="india", private_dir=private, india_clock=lambda: ils.NOW
    )
    monkeypatch.setattr(ai_routes, "state", paper)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app, client=("127.0.0.1", 50000)), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/ai/trade/approval/uat-proposal", json={"workspace": "india", "symbol": "TCS"}
            )
        assert resp.status_code == 409
        assert "SHADOW" in str(resp.json()["detail"])
    finally:
        paper.close_execution()


# ------------------------------------------------------- the session feed from relay bars


def feed_reads(world):
    """Daily-bar reads for the session feed (October dates), not the September tick reference."""

    return [r for r in world.vm.transport.bars_requests() if "from_date=2026-10" in r[1]]


@pytest.mark.asyncio
async def test_the_first_india_admission_of_a_session_feeds_option_b_from_relay_bars(world):
    guard = world.service.india_guard
    ils.seed_position(world.ledger, ros.TICKER, 10, "1000.00", guard=guard)
    today = world.mac_now().date()
    bars = world.vm.transport.bar_book[ros.SYMBOL]
    for offset, close in ((7, 99.0), (6, 99.5), (5, 100.0), (4, 100.5), (1, 101.0)):
        bars[today - timedelta(days=offset)] = close
    before = guard.store.load().last_session

    proposal, admission = world.prepare(quantity="1")
    after = guard.store.load().last_session
    assert before is None
    assert after is not None and after < today
    reads = feed_reads(world)
    assert reads and {r[0] for r in reads} == {"GET"}
    # A second admission on the same IST day does not read the session bars again.
    world.prepare(quantity="1")
    assert len(feed_reads(world)) == len(reads)


@pytest.mark.asyncio
async def test_a_held_ticker_with_no_trusted_close_denies_the_admission(world):
    guard = world.service.india_guard
    ils.seed_position(world.ledger, ros.TICKER, 10, "1000.00", guard=guard)
    real = world.vm.transport.request

    def request(method, url, **kwargs):
        if "historicalcharts" in url and "from_date=2026-10" in url:
            return HttpResponse(200, {}, breeze_body(None, status=500, error="bars down"))
        return real(method, url, **kwargs)

    world.vm.transport.request = request  # the session bars fail; the quote reads still work
    proposal, admission = world.prepare(quantity="1")
    assert admission.decision is AdmissionDecision.DENIED
    assert admission.reason_code == "SESSION_EVALUATION_FAILED"
    assert world.http.order_calls() == []


@pytest.mark.asyncio
async def test_a_held_ticker_with_no_closes_in_the_window_denies_and_is_not_marked_fed(world):
    # Fix r1 (Codex 2): the bars call succeeds but returns no October closes at all. That used
    # to read as "no sessions to evaluate": ADMITTED, last_session None, the day cached as fed.
    guard = world.service.india_guard
    ils.seed_position(world.ledger, ros.TICKER, 10, "1000.00", guard=guard)
    assert not any(date(2026, 10, 1) <= d for d in world.vm.transport.bar_book[ros.SYMBOL])
    proposal, admission = world.prepare(quantity="1")
    assert admission.decision is AdmissionDecision.DENIED
    assert admission.reason_code == "SESSION_CLOSES_MISSING"
    assert guard.store.load().last_session is None
    assert world.app._india_session_fed is None  # a later admission feeds again
    assert world.http.order_calls() == []


@pytest.mark.asyncio
async def test_a_ragged_session_window_writes_no_session_and_denies(world):
    # Two held tickers, one with a close on a day the other lacks: nothing is evaluated, not
    # even the days both cover.
    guard = world.service.india_guard
    ils.seed_position(world.ledger, ros.TICKER, 10, "1000.00", guard=guard)
    other = "NSE:CASH:OTHER"
    ils.seed_position(world.ledger, other, 5, "500.00", guard=guard)
    today = world.mac_now().date()
    book = world.vm.transport.bar_book
    book[ros.SYMBOL].update({today - timedelta(days=2): 100.0, today - timedelta(days=1): 101.0})
    book["OTHER"] = {today - timedelta(days=2): 50.0}  # no close for the later day
    proposal, admission = world.prepare(quantity="1")
    assert admission.decision is AdmissionDecision.DENIED
    assert admission.reason_code == "SESSION_CLOSES_MISSING"
    assert guard.store.load().last_session is None
    assert world.app._india_session_fed is None


@pytest.mark.asyncio
async def test_the_session_feed_runs_once_per_ist_day_and_again_the_next_day(world):
    # Fix r1 (Opus F2): pin the once-per-IST-day cache on the feed itself. A successful feed
    # moves ``last_session`` forward, so only the "nothing held yet" day shows the cache: a
    # position that lands later the same day is not fed until the next IST day.
    guard = world.service.india_guard
    bars = world.vm.transport.bar_book[ros.SYMBOL]
    for offset, close in ((3, 99.0), (2, 100.0), (1, 101.0)):
        bars[date(2026, 10, 21) - timedelta(days=offset)] = close
    morning = datetime(2026, 10, 21, 4, 0, tzinfo=timezone.utc)  # 09:30 IST
    evening = datetime(2026, 10, 21, 16, 0, tzinfo=timezone.utc)  # 21:30 IST, same IST day
    next_day = datetime(2026, 10, 22, 4, 0, tzinfo=timezone.utc)
    assert world.app.feed_india_session_from_relay(now=morning) == []  # nothing held: no reads
    assert feed_reads(world) == []
    ils.seed_position(world.ledger, ros.TICKER, 10, "1000.00", guard=guard)
    assert world.app.feed_india_session_from_relay(now=evening) == []
    assert feed_reads(world) == []  # the same IST day reads nothing, whatever is held now
    assert world.app.feed_india_session_from_relay(now=next_day)  # the next IST day feeds
    assert feed_reads(world)


@pytest.mark.asyncio
async def test_the_session_feed_takes_its_day_from_the_injected_clock(world, monkeypatch):
    # A wall clock on another day must not choose the window. The bars sit on the
    # injected clock's previous session only.
    guard = world.service.india_guard
    ils.seed_position(world.ledger, ros.TICKER, 10, "1000.00", guard=guard)
    today = world.mac_now().date()
    world.vm.transport.bar_book[ros.SYMBOL][today - timedelta(days=1)] = 101.0

    class _WallClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2020, 1, 15, tzinfo=timezone.utc)

    monkeypatch.setattr("app_context.datetime", _WallClock)
    assert world.app.feed_india_session_from_relay()
    assert world.app._india_session_fed == today
    assert guard.store.load().last_session == today - timedelta(days=1)


# -------------------------------------------------------- the AST no-order guard, Mac side

MAC_FILES = [
    "backend/brokers/breeze_gateway/orders.py",
    "backend/brokers/breeze_gateway/india_quote.py",
    "backend/brokers/breeze_gateway/client.py",
    "backend/brokers/breeze_gateway/errors.py",
    "backend/brokers/breeze_gateway/cli.py",
    "backend/execution/relay_dispatcher.py",
    "backend/execution/service.py",
    "backend/execution/approval.py",
    "backend/execution/ledger.py",
    "backend/execution/venue.py",
    "backend/app_context.py",
    "backend/routes/ai_routes.py",
    "backend/routes/market_data_routes.py",
]
PLANT = (
    "\n\nimport http.client\n"
    "def _planted_order():\n"
    "    c = http.client.HTTPSConnection('api.icicidirect.com')\n"
    "    c.request('POST', '/breezeapi/api/v1/order', body=b'{}')\n"
)
VM_PLANT_TARGET = "gateway/vm/gateway_vm/breeze_client.py"


def test_the_ast_guard_finds_no_order_path_in_the_vm_tree_or_the_mac_relay_files():
    assert_no_order_path([ROOT / "gateway" / "vm" / "gateway_vm"])
    assert_no_order_path([ROOT / rel for rel in MAC_FILES])


@pytest.mark.parametrize(
    "rel",
    [VM_PLANT_TARGET, "backend/execution/relay_dispatcher.py", "backend/routes/ai_routes.py"],
)
def test_a_planted_order_call_in_a_real_file_turns_the_ast_guard_red(tmp_path, rel):
    source = ROOT / rel
    copy = tmp_path / source.name
    shutil.copyfile(source, copy)
    assert find_order_paths([copy]) == []  # the unmodified file is green
    copy.write_text(copy.read_text(encoding="utf-8") + PLANT, encoding="utf-8")
    assert find_order_paths([copy])
    with pytest.raises(AssertionError):
        assert_no_order_path([copy])
