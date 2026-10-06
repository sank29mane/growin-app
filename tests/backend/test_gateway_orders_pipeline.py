"""growin-orders/1 pipeline (Phase 63-01): mint, sign, authorize, refuse.

Everything here runs on fakes and tmp dirs. No broker, no network, no GCP
metadata. The forward port is a constant refusal that counts calls, and each
refusal test asserts the count is still zero.

The TEST ONLY P-256 keys come from fixtures/relay_orders/signing_vectors.json.
"""

from __future__ import annotations

import base64
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "gateway" / "vm") not in sys.path:
    sys.path.insert(0, str(ROOT / "gateway" / "vm"))

from gateway_vm.orders import OrderRefusal  # noqa: E402
from gateway_vm.orders.audit import AuditLog  # noqa: E402
from gateway_vm.orders.challenge import SecretIds, canonical_bytes  # noqa: E402
from gateway_vm.orders.intent import FIELDS, parse_intent  # noqa: E402
from gateway_vm.orders.pipeline import (  # noqa: E402
    GuardResult,
    OrderPipeline,
    RefusalForward,
)
from gateway_vm.orders.verify import PinnedKey  # noqa: E402

FIXTURES = ROOT / "tests" / "backend" / "fixtures" / "relay_orders"
VECTORS = json.loads((FIXTURES / "signing_vectors.json").read_text(encoding="utf-8"))
PRIMARY = VECTORS["keys"]["primary"]
OTHER = VECTORS["keys"]["other"]
LIMITS_SHA = VECTORS["limits_sha256"]
BASE_INTENT = dict(VECTORS["rows"][0]["payload"]["intent"])

T0 = datetime(2026, 10, 8, 4, 30, 0, tzinfo=timezone.utc)  # 10:00 IST, a Thursday


# --------------------------------------------------------------------- fakes


class FakeClock:
    def __init__(self, now: datetime = T0) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class SeqIds(SecretIds):
    def __init__(self) -> None:
        self.n = 0

    def challenge_id(self) -> str:
        self.n += 1
        return f"00000000-0000-4000-8000-{self.n:012d}"

    def nonce(self) -> str:
        return ("n" * 43)


class FileIntentLedger:
    """Durable consumed-intent fake: a JSON file, so a new instance 'reloads'."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _read(self) -> list[str]:
        return json.loads(self.path.read_text()) if self.path.exists() else []

    def is_consumed(self, intent_id: str) -> bool:
        return intent_id in self._read()

    def consume(self, intent_id: str) -> None:
        self.path.write_text(json.dumps(self._read() + [intent_id]))


class ScriptedGuard:
    """Allow-all by default; tests set .codes to script a refusal."""

    def __init__(self) -> None:
        self.codes: tuple[str, ...] = ()
        self.checks = 0
        self.mac_halted = False

    def check(self, intent, now):
        self.checks += 1
        return GuardResult(codes=self.codes, kill="enabled", latches=())

    def set_mac_halt(self):
        self.mac_halted = True
        return ("mac_halt",)


def sign(key: dict, message: bytes) -> bytes:
    private = ec.derive_private_key(int(key["private_scalar_hex"], 16), ec.SECP256R1())
    return private.sign(message, ec.ECDSA(hashes.SHA256()))


def intent_body(**overrides) -> bytes:
    body = dict(BASE_INTENT)
    body.update(overrides)
    return json.dumps(body).encode("ascii")


class Rig:
    def __init__(self, tmp_path: Path, *, key: PinnedKey | None = None) -> None:
        self.tmp = tmp_path
        self.clock = FakeClock()
        self.ids = SeqIds()
        self.guard = ScriptedGuard()
        self.forward = RefusalForward()
        self.audit = AuditLog(tmp_path / "audit.jsonl", clock=self.clock)
        self.ledger = FileIntentLedger(tmp_path / "consumed.json")
        self.key = key or PinnedKey(
            bytes.fromhex(PRIMARY["public_key_x963_hex"]), allow_test_key=True
        )
        self.pipeline = self.new_pipeline()

    def new_pipeline(self) -> OrderPipeline:
        return OrderPipeline(
            key=self.key,
            limits_sha256=LIMITS_SHA,
            intents=FileIntentLedger(self.tmp / "consumed.json"),
            guard=self.guard,
            audit=self.audit,
            clock=self.clock,
            forward=self.forward,
            ids=self.ids,
        )

    def mint(self, **overrides):
        return self.pipeline.mint(intent_body(**overrides))

    def signed(self, minted, key=PRIMARY) -> bytes:
        return sign(key, minted.signed_bytes)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def expect(excinfo, status: int, error: str, code: str) -> None:
    exc = excinfo.value
    assert isinstance(exc, OrderRefusal)
    assert (exc.status, exc.error, exc.code) == (status, error, code)


# ----------------------------------------------------------------- happy path


def test_tracer_buy_parse_mint_sign_authorize_audit(rig: Rig):
    minted = rig.mint()
    payload = json.loads(minted.signed_bytes)
    assert payload["purpose"] == "growin.relay.order"
    assert payload["version"] == 1
    assert payload["issued_at"] == int(T0.timestamp())
    assert payload["expires_at"] == payload["issued_at"] + 60
    assert payload["key_id"] == PRIMARY["key_id"]
    assert payload["limits_sha256"] == LIMITS_SHA
    assert payload["intent"] == BASE_INTENT
    assert len(payload["nonce"]) == 43
    assert minted.signed_bytes == canonical_bytes(payload)  # nobody re-serializes differently

    result = rig.pipeline.authorize(minted.challenge_id, rig.signed(minted))

    assert result.decision == "VERIFIED_NOT_FORWARDED"
    assert result.intent_id == BASE_INTENT["intent_id"]
    assert rig.ledger.is_consumed(BASE_INTENT["intent_id"])
    entries = rig.audit.entries_after(0)
    assert [e["decision"] for e in entries] == ["CHALLENGED", "VERIFIED_NOT_FORWARDED"]
    assert entries[-1]["seq"] == result.audit_seq
    assert entries[-1]["entry_sha256"] == result.audit_sha256
    assert rig.audit.verify()[0] == 2
    assert rig.forward.calls == 0


def test_mint_body_shape(rig: Rig):
    body = rig.mint().body()
    assert set(body) == {"contract", "challenge_id", "signed_bytes_b64", "expires_at_utc"}
    assert body["contract"] == "growin-orders/1"
    assert base64.b64decode(body["signed_bytes_b64"])
    assert body["expires_at_utc"].endswith("Z")


def test_forward_port_must_be_the_refusal_object(tmp_path: Path):
    class Sneaky(RefusalForward):
        pass

    with pytest.raises(TypeError):
        OrderPipeline(
            key=PinnedKey(bytes.fromhex(PRIMARY["public_key_x963_hex"]), allow_test_key=True),
            limits_sha256=LIMITS_SHA,
            intents=FileIntentLedger(tmp_path / "c.json"),
            guard=ScriptedGuard(),
            audit=AuditLog(tmp_path / "a.jsonl"),
            clock=FakeClock(),
            forward=Sneaky(),
        )


# --------------------------------------------------------------------- parsing


def raw_with(mutator) -> bytes:
    body = dict(BASE_INTENT)
    mutator(body)
    return json.dumps(body).encode("ascii")


def test_parse_accepts_the_vector_intent():
    assert parse_intent(intent_body()).as_dict() == BASE_INTENT


@pytest.mark.parametrize(
    "name,raw,code",
    [
        ("missing_key", raw_with(lambda b: b.pop("reason")), "missing_key"),
        ("extra_key", raw_with(lambda b: b.update(extra="x")), "extra_key"),
        (
            "duplicate_key",
            json.dumps(BASE_INTENT).replace(
                '"side": "buy"', '"side": "buy", "side": "sell"'
            ).encode(),
            "duplicate_key",
        ),
        ("float_quantity", json.dumps(BASE_INTENT).replace('"quantity": 10', '"quantity": 10.0').encode(), "float_not_allowed"),
        ("exponent_quantity", json.dumps(BASE_INTENT).replace('"quantity": 10', '"quantity": 1e1').encode(), "float_not_allowed"),
        ("bool_quantity", raw_with(lambda b: b.update(quantity=True)), "bad_type"),
        ("string_quantity", raw_with(lambda b: b.update(quantity="10")), "bad_type"),
        ("number_price", raw_with(lambda b: b.update(limit_price=100)), "bad_type"),
        ("zero_quantity", raw_with(lambda b: b.update(quantity=0)), "bad_field:quantity"),
        ("big_quantity", raw_with(lambda b: b.update(quantity=100001)), "bad_field:quantity"),
        ("non_ascii", json.dumps(BASE_INTENT, ensure_ascii=False).replace("TESTCO", "TESTCÖ").encode("utf-8"), "non_ascii"),
        ("not_object", b"[1, 2]", "not_object"),
        ("bad_json", b"{nope", "bad_json"),
        ("nan", json.dumps(BASE_INTENT).replace('"quantity": 10', '"quantity": NaN').encode(), "float_not_allowed"),
        ("intent_id_short", raw_with(lambda b: b.update(intent_id="short")), "bad_field:intent_id"),
        ("intent_id_newline", raw_with(lambda b: b.update(intent_id="intent-test-0001\n")), "bad_field:intent_id"),
        ("proposal_id_empty", raw_with(lambda b: b.update(proposal_id="")), "bad_field:proposal_id"),
        ("workspace", raw_with(lambda b: b.update(workspace="uk")), "bad_field:workspace"),
        ("broker", raw_with(lambda b: b.update(broker="trading212")), "bad_field:broker"),
        ("mode_paper", raw_with(lambda b: b.update(mode="PAPER")), "bad_field:mode"),
        ("exchange", raw_with(lambda b: b.update(exchange="BSE")), "bad_field:exchange"),
        ("product", raw_with(lambda b: b.update(product="margin")), "bad_field:product"),
        ("order_type", raw_with(lambda b: b.update(order_type="market")), "bad_field:order_type"),
        ("validity", raw_with(lambda b: b.update(validity="ioc")), "bad_field:validity"),
        ("side", raw_with(lambda b: b.update(side="BUY")), "bad_field:side"),
        ("stock_code_lower", raw_with(lambda b: b.update(stock_code="testco")), "bad_field:stock_code"),
        ("stock_code_long", raw_with(lambda b: b.update(stock_code="A" * 11)), "bad_field:stock_code"),
        ("isin_shape", raw_with(lambda b: b.update(isin="US0378331005")), "bad_field:isin"),
        ("isin_check_digit", raw_with(lambda b: b.update(isin="INE000A0101X")), "bad_field:isin"),
        ("price_zero", raw_with(lambda b: b.update(limit_price="0")), "bad_field:limit_price"),
        ("price_three_dp", raw_with(lambda b: b.update(limit_price="1.234")), "bad_field:limit_price"),
        ("price_negative", raw_with(lambda b: b.update(limit_price="-1")), "bad_field:limit_price"),
        ("price_exponent", raw_with(lambda b: b.update(limit_price="1e2")), "bad_field:limit_price"),
        ("reason", raw_with(lambda b: b.update(reason="yolo")), "bad_field:reason"),
        ("batch_id", raw_with(lambda b: b.update(batch_id="BAD")), "bad_field:batch_id"),
        ("batch_id_number", raw_with(lambda b: b.update(batch_id=5)), "bad_type"),
        ("limits_sha_upper", raw_with(lambda b: b.update(limits_sha256=LIMITS_SHA.upper())), "bad_field:limits_sha256"),
        ("params_sha_short", raw_with(lambda b: b.update(params_sha256="ab")), "bad_field:params_sha256"),
        ("key_id_short", raw_with(lambda b: b.update(key_id="ab")), "bad_field:key_id"),
    ],
)
def test_parse_refuses(rig: Rig, name, raw, code):
    with pytest.raises(OrderRefusal) as err:
        rig.pipeline.mint(raw)
    expect(err, 422, "INTENT_INVALID", code)
    assert rig.forward.calls == 0
    assert rig.guard.checks == 0
    assert not rig.audit.has_entries()


def test_mode_live_is_423_live_disabled(rig: Rig):
    with pytest.raises(OrderRefusal) as err:
        rig.mint(mode="LIVE")
    expect(err, 423, "ORDERS_BLOCKED", "live_disabled")
    assert rig.forward.calls == 0 and rig.guard.checks == 0


def test_fields_constant_matches_contract():
    assert len(FIELDS) == 19 and len(set(FIELDS)) == 19


# --------------------------------------------------------------- mint refusals


def test_limits_hash_mismatch_refused_at_mint(rig: Rig):
    with pytest.raises(OrderRefusal) as err:
        rig.mint(limits_sha256="0" * 64)
    expect(err, 409, "LIMIT_REJECTED", "limits_hash_mismatch")
    assert rig.audit.entries_after(0)[0]["decision"] == "REFUSED"
    assert rig.forward.calls == 0 and rig.guard.checks == 0


def test_key_mismatch_refused_at_mint(rig: Rig):
    with pytest.raises(OrderRefusal) as err:
        rig.mint(key_id=OTHER["key_id"])
    expect(err, 409, "LIMIT_REJECTED", "key_mismatch")
    assert rig.forward.calls == 0


def test_second_mint_for_live_challenge_is_409(rig: Rig):
    rig.mint()
    with pytest.raises(OrderRefusal) as err:
        rig.mint()
    expect(err, 409, "REPLAY", "challenge_outstanding")
    assert rig.forward.calls == 0


def test_fifth_outstanding_challenge_is_429(rig: Rig):
    for n in range(4):
        rig.mint(intent_id=f"intent-cap-{n:04d}")
    with pytest.raises(OrderRefusal) as err:
        rig.mint(intent_id="intent-cap-0004")
    expect(err, 429, "CHALLENGE_CAPACITY", "challenge_capacity")
    assert rig.forward.calls == 0


def test_expired_challenges_free_capacity(rig: Rig):
    for n in range(4):
        rig.mint(intent_id=f"intent-cap-{n:04d}")
    rig.clock.advance(61)
    assert rig.mint(intent_id="intent-cap-0004").challenge_id


def test_guard_codes_refuse_mint_with_every_code_audited(rig: Rig):
    rig.guard.codes = ("kill_switch", "halt_latch")
    with pytest.raises(OrderRefusal) as err:
        rig.mint()
    expect(err, 423, "ORDERS_BLOCKED", "kill_switch")
    entry = rig.audit.entries_after(0)[0]
    assert entry["decision"] == "REFUSED" and entry["codes"] == ["kill_switch", "halt_latch"]
    assert rig.forward.calls == 0


# ---------------------------------------------------------- authorize refusals


def test_unknown_challenge(rig: Rig):
    with pytest.raises(OrderRefusal) as err:
        rig.pipeline.authorize("99999999-9999-4999-8999-999999999999", b"x" * 70)
    expect(err, 409, "REPLAY", "challenge_unknown")
    assert rig.forward.calls == 0


def test_bad_challenge_id_shape(rig: Rig):
    with pytest.raises(OrderRefusal) as err:
        rig.pipeline.authorize("../etc", b"x")
    expect(err, 422, "INTENT_INVALID", "bad_challenge_id")


def test_expired_challenge_uses_vm_clock(rig: Rig):
    minted = rig.mint()
    sig = rig.signed(minted)
    rig.clock.advance(60)  # now == expires_at: expired
    with pytest.raises(OrderRefusal) as err:
        rig.pipeline.authorize(minted.challenge_id, sig)
    expect(err, 409, "REPLAY", "challenge_expired")
    assert rig.forward.calls == 0
    assert not rig.ledger.is_consumed(BASE_INTENT["intent_id"])


def test_last_second_still_valid(rig: Rig):
    minted = rig.mint()
    sig = rig.signed(minted)
    rig.clock.advance(59)
    assert rig.pipeline.authorize(minted.challenge_id, sig).decision == "VERIFIED_NOT_FORWARDED"


def test_reused_challenge(rig: Rig):
    minted = rig.mint()
    sig = rig.signed(minted)
    rig.pipeline.authorize(minted.challenge_id, sig)
    with pytest.raises(OrderRefusal) as err:
        rig.pipeline.authorize(minted.challenge_id, sig)
    expect(err, 409, "REPLAY", "challenge_unknown")
    assert rig.forward.calls == 0


def test_bad_signature_burns_the_challenge(rig: Rig):
    minted = rig.mint()
    good = rig.signed(minted)
    with pytest.raises(OrderRefusal) as err:
        rig.pipeline.authorize(minted.challenge_id, b"\x30\x06\x02\x01\x01\x02\x01\x01")
    expect(err, 403, "SIGNATURE_INVALID", "signature_invalid")
    with pytest.raises(OrderRefusal) as again:
        rig.pipeline.authorize(minted.challenge_id, good)
    expect(again, 409, "REPLAY", "challenge_unknown")
    assert not rig.ledger.is_consumed(BASE_INTENT["intent_id"])
    assert rig.forward.calls == 0


def test_raw_r_s_signature_refused(rig: Rig):
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

    minted = rig.mint()
    r, s = decode_dss_signature(rig.signed(minted))
    raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
    with pytest.raises(OrderRefusal) as err:
        rig.pipeline.authorize(minted.challenge_id, raw)
    expect(err, 403, "SIGNATURE_INVALID", "signature_invalid")
    assert rig.forward.calls == 0


def test_trailing_der_byte_refused(rig: Rig):
    minted = rig.mint()
    with pytest.raises(OrderRefusal) as err:
        rig.pipeline.authorize(minted.challenge_id, rig.signed(minted) + b"\x00")
    expect(err, 403, "SIGNATURE_INVALID", "signature_invalid")
    assert rig.forward.calls == 0


def test_wrong_key_refused(rig: Rig):
    minted = rig.mint()
    with pytest.raises(OrderRefusal) as err:
        rig.pipeline.authorize(minted.challenge_id, rig.signed(minted, key=OTHER))
    expect(err, 403, "SIGNATURE_INVALID", "signature_invalid")
    assert rig.forward.calls == 0


def test_signature_over_different_bytes_refused(rig: Rig):
    first = rig.mint()
    second = rig.mint(intent_id="intent-test-0009")
    with pytest.raises(OrderRefusal) as err:
        rig.pipeline.authorize(first.challenge_id, rig.signed(second))
    expect(err, 403, "SIGNATURE_INVALID", "signature_invalid")
    assert rig.forward.calls == 0


def test_flipped_byte_in_signed_bytes_refused(rig: Rig):
    minted = rig.mint()
    flipped = bytearray(minted.signed_bytes)
    flipped[len(flipped) // 2] ^= 1
    # A valid signature over bytes the VM did not mint, and the VM's own bytes
    # under the original signature with one byte altered, both fail.
    with pytest.raises(OrderRefusal) as err:
        rig.pipeline.authorize(minted.challenge_id, sign(PRIMARY, bytes(flipped)))
    expect(err, 403, "SIGNATURE_INVALID", "signature_invalid")
    assert rig.forward.calls == 0
    assert not rig.key.verify_der(rig.signed(minted), bytes(flipped))


def test_consumed_intent_cannot_be_challenged_again_even_after_reload(rig: Rig):
    minted = rig.mint()
    rig.pipeline.authorize(minted.challenge_id, rig.signed(minted))
    reloaded = rig.new_pipeline()  # a restart: new pipeline, same files
    with pytest.raises(OrderRefusal) as err:
        reloaded.mint(intent_body())
    expect(err, 409, "REPLAY", "intent_consumed")
    assert rig.forward.calls == 0


def test_intent_consumed_between_mint_and_authorize(rig: Rig):
    minted = rig.mint()
    sig = rig.signed(minted)
    rig.ledger.consume(BASE_INTENT["intent_id"])  # another process won the race
    with pytest.raises(OrderRefusal) as err:
        rig.pipeline.authorize(minted.challenge_id, sig)
    expect(err, 409, "REPLAY", "intent_consumed")
    assert rig.forward.calls == 0


def test_high_s_signature_verifies_but_challenge_is_single_use(rig: Rig):
    row = next(n for n in VECTORS["negatives"] if n["name"] == "high_s_variant_verifies")
    # Build a fresh challenge and flip its own signature to the high-S form.
    from cryptography.hazmat.primitives.asymmetric.utils import (
        decode_dss_signature,
        encode_dss_signature,
    )

    n_order = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
    minted = rig.mint()
    r, s = decode_dss_signature(rig.signed(minted))
    high = encode_dss_signature(r, s if s > n_order // 2 else n_order - s)
    assert row["expect_valid"] is True
    result = rig.pipeline.authorize(minted.challenge_id, high)
    assert result.decision == "VERIFIED_NOT_FORWARDED"
    with pytest.raises(OrderRefusal) as err:
        rig.pipeline.authorize(minted.challenge_id, high)
    expect(err, 409, "REPLAY", "challenge_unknown")
    assert rig.forward.calls == 0


def test_guard_codes_refuse_authorize_and_still_consume_the_intent(rig: Rig):
    minted = rig.mint()
    sig = rig.signed(minted)
    rig.guard.codes = ("halt_latch",)
    with pytest.raises(OrderRefusal) as err:
        rig.pipeline.authorize(minted.challenge_id, sig)
    expect(err, 423, "ORDERS_BLOCKED", "halt_latch")
    assert rig.ledger.is_consumed(BASE_INTENT["intent_id"])  # O5: persisted before re-check
    assert rig.audit.entries_after(0)[-1]["decision"] == "REFUSED"
    assert rig.forward.calls == 0


def test_mac_halt_route_sets_latch_and_audits(rig: Rig):
    assert rig.pipeline.halt() == {"contract": "growin-orders/1", "mac_halt": True}
    assert rig.guard.mac_halted
    assert rig.audit.entries_after(0)[0]["decision"] == "HALTED"


def test_audit_failure_refuses_and_never_returns_success(rig: Rig):
    minted = rig.mint()
    sig = rig.signed(minted)
    rig.audit.path.write_bytes(rig.audit.path.read_bytes() + b"{trunc")
    with pytest.raises(OrderRefusal) as err:
        rig.pipeline.authorize(minted.challenge_id, sig)
    expect(err, 503, "ORDERS_UNAVAILABLE", "audit_broken")
    assert rig.forward.calls == 0


def test_audit_entries_carry_only_the_o7_keys(rig: Rig):
    minted = rig.mint()
    rig.pipeline.authorize(minted.challenge_id, rig.signed(minted))
    for entry in rig.audit.entries_after(0):
        assert set(entry) == {
            "seq", "prev_sha256", "entry_sha256", "at_utc", "route", "intent_id",
            "proposal_id", "intent_sha256", "key_id", "side", "stock_code", "isin",
            "quantity", "limit_price", "reason", "batch_id", "decision", "codes",
            "limits_sha256", "kill", "latches",
        }


def test_audit_chain_detects_edit_and_drop(rig: Rig, tmp_path: Path):
    for n in range(3):
        rig.mint(intent_id=f"intent-aud-{n:04d}")
    lines = rig.audit.path.read_bytes().splitlines(keepends=True)
    assert rig.audit.verify()[0] == 3
    rig.audit.path.write_bytes(lines[0] + lines[2])  # drop the middle entry
    from gateway_vm.orders.audit import AuditBroken

    with pytest.raises(AuditBroken):
        rig.audit.verify()
    rig.audit.path.write_bytes(lines[0].replace(b"CHALLENGED", b"VERIFIED_NOT_FORWARDED") + lines[1] + lines[2])
    with pytest.raises(AuditBroken):
        rig.audit.verify()


def test_audit_rejects_keys_outside_the_allowlist(rig: Rig):
    with pytest.raises(ValueError):
        rig.audit.append({"decision": "REFUSED", "account_id": "123"})


# =============================================================================
# Task 3: the same pipeline on the real modules (evaluator, kill reader, store,
# audit). Port fakes return values that tests swap AFTER mint, so each re-check
# row proves authorize reads everything again. The pipeline is built once per
# test, not rebuilt per row.
# =============================================================================

import ast  # noqa: E402
import os  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402
import threading  # noqa: E402
from dataclasses import dataclass  # noqa: E402
from datetime import date  # noqa: E402
from decimal import Decimal  # noqa: E402

from gateway_vm.orders import risk  # noqa: E402
from gateway_vm.orders.kill import ENABLED, MetadataKillReader  # noqa: E402
from gateway_vm.orders.limits import (  # noqa: E402
    AccountSnapshot,
    Holding,
    Limits,
    OpenOrder,
    Quote,
    Trade,
)
from gateway_vm.orders.pipeline import RuleGuard  # noqa: E402
from gateway_vm.orders.store import StateStore  # noqa: E402

LIMITS = Limits.from_fields(VECTORS["limits"])
ISIN_A = "INE000A01012"  # TESTCO, the BASE_INTENT security
ISIN_B = "INE111B01023"  # ABC
D = Decimal


def quote_for(code: str = "TESTCO", isin: str = ISIN_A, ltp: str = "100.00", **kw) -> Quote:
    base = dict(
        stock_code=code,
        isin=isin,
        series="EQ",
        ltp=D(ltp),
        lower_circuit=D("90.00"),
        upper_circuit=D("110.00"),
        previous_close=D("99.80"),
        session_date=date(2026, 10, 8),
    )
    base.update(kw)
    return Quote(**base)


class FakeKill:
    def __init__(self) -> None:
        self.state = ENABLED
        self.reads = 0

    def read(self):
        self.reads += 1
        return self.state


class FakeAccount:
    def __init__(self) -> None:
        self.snap = AccountSnapshot()
        self.reads = 0
        self.fail = False

    def snapshot(self) -> AccountSnapshot:
        self.reads += 1
        if self.fail:
            raise RuntimeError("breeze down")
        return self.snap


class FakeMarket:
    def __init__(self) -> None:
        self.quotes = {"TESTCO": quote_for(), "ABC": quote_for("ABC", ISIN_B)}
        self.reads = 0
        self.fail = False

    def quote(self, stock_code: str) -> Quote:
        self.reads += 1
        if self.fail:
            raise RuntimeError("quote down")
        return self.quotes[stock_code]


class RealRig:
    def __init__(self, tmp_path: Path, *, kill=None) -> None:
        self.dir = tmp_path / "state"
        self.dir.mkdir(mode=0o700)
        self.clock = FakeClock()
        self.ids = SeqIds()
        self.kill = kill or FakeKill()
        self.account = FakeAccount()
        self.market = FakeMarket()
        self.forward = RefusalForward()
        self.store = StateStore(self.dir)
        self.audit = AuditLog(self.dir / "audit.jsonl", clock=self.clock)
        self.key = PinnedKey(bytes.fromhex(PRIMARY["public_key_x963_hex"]), allow_test_key=True)
        # First start: initialise the state once (peak = capital_cap). The server
        # does this at startup; a missing state file after that is refused.
        self.store.load_or_init(LIMITS, audit_has_entries=False)
        self.pipeline = self.build(self.store)

    def build(self, store: StateStore) -> OrderPipeline:
        guard = RuleGuard(
            limits=LIMITS,
            kill=self.kill,
            store=store,
            account=self.account,
            market=self.market,
            audit=self.audit,
        )
        return OrderPipeline(
            key=self.key,
            limits_sha256=LIMITS.sha256,
            intents=store,
            guard=guard,
            audit=self.audit,
            clock=self.clock,
            forward=self.forward,
            ids=self.ids,
            lock=store.lock(),
        )

    def reads(self) -> tuple[int, int, int]:
        kill_reads = self.kill.reads if isinstance(self.kill, FakeKill) else -1
        return (kill_reads, self.account.reads, self.market.reads)

    def mint(self, **overrides):
        return self.pipeline.mint(intent_body(**overrides))

    def signed(self, minted) -> bytes:
        return sign(PRIMARY, minted.signed_bytes)

    def state(self) -> risk.OrderState:
        return StateStore(self.dir).load()

    def edit_state(self, mutate) -> None:
        state = self.state()
        mutate(state)
        StateStore(self.dir).save(state)

    def decisions(self) -> list[tuple[str, list[str]]]:
        return [(e["decision"], e["codes"]) for e in self.audit.entries_after(0)]

    def refused_authorize(self, minted, code: str, status: int, error: str):
        with pytest.raises(OrderRefusal) as err:
            self.pipeline.authorize(minted.challenge_id, self.signed(minted))
        expect(err, status, error, code)
        assert self.forward.calls == 0
        last = self.audit.entries_after(0)[-1]
        assert last["decision"] == "REFUSED" and last["codes"][0] == code
        assert last["route"] == "authorize"


@pytest.fixture
def real(tmp_path: Path) -> RealRig:
    return RealRig(tmp_path)


def holding_snapshot(*, isin=ISIN_A, qty=100, cost="10000", opens=(), extra_trades=()) -> AccountSnapshot:
    """A holding the ledger explains: the matching buy fill is on the trade list."""
    return AccountSnapshot(
        holdings=(Holding(isin, qty, D(cost)),),
        open_orders=tuple(opens),
        trades=(Trade(f"seed-{isin}", isin, "buy", qty, D(cost) / qty, D("0")), *extra_trades),
    )


# ----------------------------------------------------------- real-stack basics


def test_real_stack_happy_path_reads_everything_once_per_check(real: RealRig):
    minted = real.mint()
    assert real.reads() == (1, 1, 1)
    result = real.pipeline.authorize(minted.challenge_id, real.signed(minted))
    assert result.decision == "VERIFIED_NOT_FORWARDED"
    assert real.reads() == (2, 2, 2)  # fresh reads again at authorize, no cache
    assert real.decisions() == [("CHALLENGED", []), ("VERIFIED_NOT_FORWARDED", [])]
    assert real.state().consumed_intents == [BASE_INTENT["intent_id"]]
    assert real.forward.calls == 0
    entry = real.audit.entries_after(0)[-1]
    assert entry["kill"] == "enabled" and entry["limits_sha256"] == LIMITS.sha256


def test_consumed_intent_survives_a_restart_on_the_real_store(real: RealRig):
    minted = real.mint()
    real.pipeline.authorize(minted.challenge_id, real.signed(minted))
    restarted = real.build(StateStore(real.dir))
    with pytest.raises(OrderRefusal) as err:
        restarted.mint(intent_body())
    expect(err, 409, "REPLAY", "intent_consumed")
    assert real.forward.calls == 0


def test_limits_hash_mismatch_refuses_at_mint_on_the_real_stack(real: RealRig):
    with pytest.raises(OrderRefusal) as err:
        real.mint(limits_sha256="1" * 64)
    expect(err, 409, "LIMIT_REJECTED", "limits_hash_mismatch")
    assert real.reads() == (0, 0, 0)
    assert real.forward.calls == 0


def test_corrupt_state_after_mint_answers_503_at_authorize(real: RealRig):
    minted = real.mint()
    sig = real.signed(minted)
    real.store.path.write_text("{corrupt")
    with pytest.raises(OrderRefusal) as err:
        real.pipeline.authorize(minted.challenge_id, sig)
    expect(err, 503, "ORDERS_UNAVAILABLE", "state_unreadable")
    assert real.forward.calls == 0


def test_broken_audit_blocks_the_next_mint_with_503(real: RealRig):
    real.mint()
    real.audit.path.write_bytes(real.audit.path.read_bytes() + b"{trunc")
    with pytest.raises(OrderRefusal) as err:
        real.mint(intent_id="intent-test-0002")
    expect(err, 503, "ORDERS_UNAVAILABLE", "audit_broken")
    assert real.forward.calls == 0


def test_account_and_quote_read_failures_fail_closed(real: RealRig):
    real.account.fail = True
    with pytest.raises(OrderRefusal) as err:
        real.mint()
    expect(err, 503, "ORDERS_UNAVAILABLE", "account_read_failed")
    real.account.fail = False
    real.market.fail = True
    with pytest.raises(OrderRefusal) as err:
        real.mint()
    expect(err, 503, "ORDERS_UNAVAILABLE", "quote_unavailable")
    assert real.forward.calls == 0


# ---------------------------------------------------- kill switch (real reader)


@pytest.fixture
def metadata_server():
    from test_gateway_orders_kill import Fake  # loopback fake, 127.0.0.1 only

    server = Fake()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def test_kill_blocks_at_mint_with_no_account_or_quote_reads(tmp_path, metadata_server):
    metadata_server.mode["body"] = b"blocked"
    rig = RealRig(tmp_path, kill=MetadataKillReader(metadata_server.base))
    with pytest.raises(OrderRefusal) as err:
        rig.mint()
    expect(err, 423, "ORDERS_BLOCKED", "kill_switch")
    assert (rig.account.reads, rig.market.reads) == (0, 0)
    assert rig.forward.calls == 0
    assert rig.audit.entries_after(0)[0]["codes"] == ["kill_switch"]


def test_kill_blocks_at_authorize_after_a_clean_mint(tmp_path, metadata_server):
    rig = RealRig(tmp_path, kill=MetadataKillReader(metadata_server.base))
    minted = rig.mint()
    metadata_server.mode["body"] = b"enabled\n"  # one stray byte flips it
    rig.refused_authorize(minted, "kill_switch", 423, "ORDERS_BLOCKED")
    assert len(metadata_server.requests) == 2  # one read per check, no cache
    assert rig.account.reads == 1  # not read again once the kill switch said no
    assert rig.state().consumed_intents == [BASE_INTENT["intent_id"]]


def test_kill_flipped_back_to_enabled_allows_a_fresh_order(tmp_path, metadata_server):
    metadata_server.mode["status"] = 503
    rig = RealRig(tmp_path, kill=MetadataKillReader(metadata_server.base))
    with pytest.raises(OrderRefusal):
        rig.mint()
    metadata_server.mode["status"] = 200
    assert rig.mint(intent_id="intent-test-0002").challenge_id


# ------------------------------------------ latches set between mint and authorize


def test_halt_latch_set_between_mint_and_authorize_refuses_a_buy(real: RealRig):
    minted = real.mint()
    real.edit_state(lambda s: setattr(s, "halt", True))
    real.refused_authorize(minted, "halt_latch", 423, "ORDERS_BLOCKED")


def test_mac_halt_route_blocks_even_a_minted_buy_and_a_sell(real: RealRig):
    minted = real.mint()
    real.pipeline.halt()
    real.refused_authorize(minted, "mac_halt", 423, "ORDERS_BLOCKED")
    real.account.snap = holding_snapshot()
    with pytest.raises(OrderRefusal) as err:
        real.mint(intent_id="intent-test-0002", side="sell", reason="exit")
    expect(err, 423, "ORDERS_BLOCKED", "mac_halt")


def test_ended_latch_refuses_buys_but_a_sell_is_verified(real: RealRig):
    real.edit_state(lambda s: (setattr(s, "halt", True), setattr(s, "ended", True)))
    with pytest.raises(OrderRefusal) as err:
        real.mint()
    expect(err, 423, "ORDERS_BLOCKED", "pilot_ended")
    real.account.snap = holding_snapshot()
    sell = real.mint(intent_id="intent-test-0002", side="sell", reason="flatten", quantity=100, limit_price="100.00")
    assert real.pipeline.authorize(sell.challenge_id, real.signed(sell)).decision == "VERIFIED_NOT_FORWARDED"


def test_verified_sell_leaves_the_halt_latch_set(real: RealRig):
    real.account.snap = holding_snapshot()
    real.edit_state(lambda s: setattr(s, "halt", True))
    sell = real.mint(intent_id="intent-test-0002", side="sell", reason="halve", quantity=50, limit_price="100.00")
    assert real.pipeline.authorize(sell.challenge_id, real.signed(sell)).decision == "VERIFIED_NOT_FORWARDED"
    assert real.state().halt is True
    # The sell then fills and shows on the trade list: still no clearing.
    real.account.snap = holding_snapshot(
        qty=50, cost="5000",
        extra_trades=(Trade("sell-1", ISIN_A, "sell", 50, D("100"), D("0")),),
    )
    with pytest.raises(OrderRefusal) as err:
        real.mint(intent_id="intent-test-0003")
    expect(err, 423, "ORDERS_BLOCKED", "halt_latch")
    assert real.state().halt is True


def stopped_on_b(rig: RealRig) -> None:
    """ISIN_B bought 20 at 500, closed at 440: -12% stop latch, drawdown only -2.4%."""

    def seed(state: risk.OrderState) -> None:
        risk.apply_trades(state, [Trade("b-buy", ISIN_B, "buy", 20, D("500"), D("0"))])
        risk.evaluate_session(state, LIMITS, date(2026, 10, 7), {ISIN_B: D("440")})

    rig.edit_state(seed)
    assert list(rig.state().stops) == [ISIN_B]
    rig.account.snap = AccountSnapshot(
        holdings=(Holding(ISIN_B, 20, D("10000")),),
        trades=(Trade("b-buy", ISIN_B, "buy", 20, D("500"), D("0")),),
    )
    rig.market.quotes["ABC"] = quote_for(
        "ABC", ISIN_B, ltp="440.00", lower_circuit=D("400.00"), upper_circuit=D("480.00"), previous_close=D("440.00")
    )


def test_stop_exit_open_refuses_a_buy_on_any_isin_at_mint(real: RealRig):
    stopped_on_b(real)
    with pytest.raises(OrderRefusal) as err:
        real.mint()  # TESTCO, not the stopped ISIN
    expect(err, 423, "ORDERS_BLOCKED", "stop_open")
    with pytest.raises(OrderRefusal) as err:
        real.mint(
            intent_id="intent-test-0002", stock_code="ABC", isin=ISIN_B, limit_price="440.00", quantity=1
        )
    expect(err, 423, "ORDERS_BLOCKED", "stop_open")
    assert real.forward.calls == 0


def test_stop_set_between_mint_and_authorize_refuses_at_authorize(real: RealRig):
    minted = real.mint()
    stopped_on_b(real)
    real.refused_authorize(minted, "stop_open", 423, "ORDERS_BLOCKED")


def test_sell_of_the_stopped_isin_is_verified_and_the_stop_stays_until_the_fill(real: RealRig):
    stopped_on_b(real)
    sell = real.mint(
        intent_id="intent-test-0002", side="sell", reason="stop", stock_code="ABC", isin=ISIN_B,
        quantity=20, limit_price="440.00",
    )
    assert real.pipeline.authorize(sell.challenge_id, real.signed(sell)).decision == "VERIFIED_NOT_FORWARDED"
    assert list(real.state().stops) == [ISIN_B]  # verified is not filled
    with pytest.raises(OrderRefusal) as err:
        real.mint(intent_id="intent-test-0003")
    expect(err, 423, "ORDERS_BLOCKED", "stop_open")
    assert list(real.state().stops) == [ISIN_B]


def test_buy_is_allowed_again_once_the_trade_list_shows_the_exit_fill(real: RealRig):
    stopped_on_b(real)
    # Partial exit fill: still open.
    real.account.snap = AccountSnapshot(
        holdings=(Holding(ISIN_B, 10, D("5000")),),
        trades=(
            Trade("b-buy", ISIN_B, "buy", 20, D("500"), D("0")),
            Trade("b-sell-1", ISIN_B, "sell", 10, D("440"), D("0")),
        ),
    )
    with pytest.raises(OrderRefusal) as err:
        real.mint()
    expect(err, 423, "ORDERS_BLOCKED", "stop_open")
    # The exit completes. No admin reset: the fill evidence clears the latch.
    real.account.snap = AccountSnapshot(
        holdings=(),
        trades=(
            Trade("b-buy", ISIN_B, "buy", 20, D("500"), D("0")),
            Trade("b-sell-1", ISIN_B, "sell", 10, D("440"), D("0")),
            Trade("b-sell-2", ISIN_B, "sell", 10, D("440"), D("0")),
        ),
    )
    minted = real.mint()
    assert real.state().stops == {}
    assert real.pipeline.authorize(minted.challenge_id, real.signed(minted)).decision == "VERIFIED_NOT_FORWARDED"


def test_stop_latch_survives_a_restart_before_the_fill(real: RealRig):
    stopped_on_b(real)
    restarted = real.build(StateStore(real.dir))
    with pytest.raises(OrderRefusal) as err:
        restarted.mint(intent_body())
    expect(err, 423, "ORDERS_BLOCKED", "stop_open")


# ------------------------------------------- fresh re-check after a valid mint


def test_recheck_capital_cap_from_an_open_buy_that_appears_after_mint(real: RealRig):
    minted = real.mint()
    before = real.reads()
    real.account.snap = AccountSnapshot(open_orders=(OpenOrder(ISIN_B, "buy", 500, D("100.00")),))
    real.refused_authorize(minted, "capital_cap", 409, "LIMIT_REJECTED")
    after = real.reads()
    assert all(a > b for a, b in zip(after, before))  # every port read again


def test_recheck_capital_cap_breach_only_from_open_buy_pending_notional(real: RealRig):
    minted = real.mint()
    positions_only = AccountSnapshot(
        holdings=(Holding(ISIN_B, 100, D("20000")),),
        trades=(Trade("b-buy", ISIN_B, "buy", 100, D("200"), D("0")),),
    )
    real.account.snap = positions_only
    # Prove the positions alone are under the cap: another order passes on them.
    probe = real.mint(intent_id="intent-test-0002")
    assert real.pipeline.authorize(probe.challenge_id, real.signed(probe)).decision == "VERIFIED_NOT_FORWARDED"
    real.account.snap = AccountSnapshot(
        holdings=positions_only.holdings,
        trades=positions_only.trades,
        open_orders=(OpenOrder(ISIN_B, "buy", 295, D("100.00")),),  # 29500 pending
    )
    real.refused_authorize(minted, "capital_cap", 409, "LIMIT_REJECTED")


def test_recheck_collar_on_a_buy_when_ltp_falls(real: RealRig):
    minted = real.mint()
    real.market.quotes["TESTCO"] = quote_for(ltp="97.00")  # limit 100.05 is 3.1% above
    real.refused_authorize(minted, "collar", 409, "LIMIT_REJECTED")


def test_recheck_collar_on_a_sell_when_ltp_rises(real: RealRig):
    real.account.snap = holding_snapshot()
    minted = real.mint(intent_id="intent-test-0002", side="sell", reason="exit", quantity=10, limit_price="99.95")
    real.market.quotes["TESTCO"] = quote_for(ltp="103.00")  # limit is 3.0% below
    real.refused_authorize(minted, "collar", 409, "LIMIT_REJECTED")


def test_recheck_session_cutoff_between_1509_59_and_1510_00(real: RealRig):
    real.clock.now = datetime(2026, 10, 8, 9, 39, 59, tzinfo=timezone.utc)  # 15:09:59 IST
    minted = real.mint()
    real.clock.advance(1)  # 15:10:00 IST
    real.refused_authorize(minted, "session_closed", 423, "ORDERS_BLOCKED")


def test_recheck_account_mismatch_unexplained_isin_after_mint(real: RealRig):
    minted = real.mint()
    real.account.snap = AccountSnapshot(holdings=(Holding(ISIN_B, 5, D("500")),))
    real.refused_authorize(minted, "account_mismatch", 423, "ORDERS_BLOCKED")
    assert real.state().account_mismatch is True  # latched, not just refused once
    with pytest.raises(OrderRefusal) as err:
        real.mint(intent_id="intent-test-0002")
    expect(err, 423, "ORDERS_BLOCKED", "account_mismatch")


def test_recheck_account_mismatch_excess_quantity_after_mint(real: RealRig):
    real.account.snap = holding_snapshot(qty=10, cost="1000")
    minted = real.mint(intent_id="intent-test-0002", side="sell", reason="exit", quantity=5, limit_price="100.00")
    real.account.snap = AccountSnapshot(
        holdings=(Holding(ISIN_A, 11, D("1100")),),  # the ledger only explains 10
        trades=real.account.snap.trades,
    )
    real.refused_authorize(minted, "account_mismatch", 423, "ORDERS_BLOCKED")


# --------------------------------------------------- the no-order-path AST guard

NON_GET = {"POST", "PUT", "PATCH", "DELETE"}
NON_GET_ATTRS = {"post", "put", "patch", "delete"}
BANNED_NAMES = {"place_order", "place_market_order"}
# The broker endpoint is the path segment "order" (Breeze .../v1/order). Our own
# relay routes live under /v1/orders/ (plural) and must not match.
ORDER_ENDPOINT = re.compile(r"(?:^|/)order(?:$|[/?#\s])")
VERB_AND_ORDER = re.compile(r"(?i)\b(?:POST|PUT|PATCH|DELETE)\s+\S*/order(?:$|[/?#\s])")


@dataclass(frozen=True)
class OrderPathOffence:
    path: str
    line: int
    kind: str


def _py_files(paths) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        item = Path(raw)
        files.extend(sorted(item.rglob("*.py")) if item.is_dir() else [item])
    return files


def _local_nodes(scope: ast.AST):
    """Nodes of one scope: nested function bodies are their own scopes, but their
    decorators and defaults are evaluated here, so they stay in."""
    stack = [scope]
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)) and node is not scope:
            args = node.args
            stack.extend(list(getattr(node, "decorator_list", [])) + args.defaults + [d for d in args.kw_defaults if d])
            continue
        stack.extend(ast.iter_child_nodes(node))


def _scopes(tree: ast.AST):
    yield tree
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            yield node


def _is_endpoint(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and ORDER_ENDPOINT.search(node.value) is not None
    )


def _scope_offences(scope: ast.AST, module_has_endpoint: bool) -> list[tuple[int, str]]:
    nodes = list(_local_nodes(scope))
    if not (module_has_endpoint or any(_is_endpoint(n) for n in nodes)):
        return []
    found: list[tuple[int, str]] = []
    for n in nodes:
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.strip().upper() in NON_GET:
            found.append((n.lineno, "non-GET method beside an /order endpoint"))
        elif isinstance(n, ast.Attribute) and n.attr in NON_GET_ATTRS:
            found.append((n.lineno, "post/put/patch/delete call beside an /order endpoint"))
        elif isinstance(n, ast.keyword) and n.arg == "method" and not (
            isinstance(n.value, ast.Constant) and str(n.value.value).upper() == "GET"
        ):
            found.append((n.value.lineno, "method= is not GET beside an /order endpoint"))
    return found


def find_order_paths(paths) -> list[OrderPathOffence]:
    """Offending nodes in every .py under paths: any non-GET request shaped at the
    broker /order endpoint, any place_order or place_market_order reference, and
    any forward implementation other than RefusalForward."""
    offences: list[OrderPathOffence] = []
    for file in _py_files(paths):
        tree = ast.parse(file.read_text(encoding="utf-8"))
        seen: set[tuple[int, str]] = set()
        # A module-level constant naming the endpoint counts for every function in the module.
        module_has_endpoint = any(_is_endpoint(n) for n in _local_nodes(tree))
        for scope in _scopes(tree):
            for line, kind in _scope_offences(scope, module_has_endpoint):
                if (line, kind) not in seen:
                    seen.add((line, kind))
                    offences.append(OrderPathOffence(str(file), line, kind))
        for n in ast.walk(tree):
            names: list[str] = []
            if isinstance(n, ast.Name):
                names.append(n.id)
            elif isinstance(n, ast.Attribute):
                names.append(n.attr)
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.append(n.name)
            elif isinstance(n, ast.keyword) and n.arg:
                names.append(n.arg)
            elif isinstance(n, ast.alias):
                names.append(n.name.split(".")[-1])
            elif isinstance(n, ast.Constant) and isinstance(n.value, str):
                if n.value in BANNED_NAMES:
                    names.append(n.value)
                if VERB_AND_ORDER.search(n.value):
                    offences.append(OrderPathOffence(str(file), n.lineno, "verb and /order in one string"))
            for name in names:
                if name in BANNED_NAMES:
                    offences.append(OrderPathOffence(str(file), getattr(n, "lineno", 0), f"reference to {name}"))
            if isinstance(n, ast.ClassDef) and "forward" in n.name.lower() and n.name != "RefusalForward":
                offences.append(OrderPathOffence(str(file), n.lineno, f"forward implementation {n.name}"))
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.lower().startswith("forward"):
                offences.append(OrderPathOffence(str(file), n.lineno, f"forward function {n.name}"))
    return offences


def assert_no_order_path(paths) -> None:
    """Reusable by 63-05 Task 3 and 63-06 Task 3 over wider path sets."""
    offences = find_order_paths(paths)
    assert offences == [], "\n".join(f"{o.path}:{o.line}: {o.kind}" for o in offences)


def test_gateway_vm_tree_has_no_order_path():
    assert_no_order_path([ROOT / "gateway" / "vm" / "gateway_vm"])


def test_forward_implementation_is_only_the_refusal_object():
    tree = ast.parse((ROOT / "gateway/vm/gateway_vm/orders/pipeline.py").read_text())
    classes = [n.name for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and "forward" in n.name.lower()]
    assert classes == ["RefusalForward"]
    refuse = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "refuse")
    # The refusal counts the call and returns a constant; it builds nothing.
    assert [type(s).__name__ for s in refuse.body] == ["AugAssign", "Return"]
    assert RefusalForward().refuse("x") == "VERIFIED_NOT_FORWARDED"


PLANTED = {
    "http_client_post": (
        "import http.client\n"
        "def go():\n"
        "    c = http.client.HTTPSConnection('api.icicidirect.com')\n"
        "    c.request('POST', '/breezeapi/api/v1/order', body=b'{}')\n"
    ),
    "requests_post_fstring": (
        "import requests\n"
        "BASE = 'https://api.icicidirect.com/breezeapi/api/v1'\n"
        "def go():\n"
        "    requests.post(f'{BASE}/order', json={})\n"
    ),
    "delete_method_keyword": (
        "import urllib.request\n"
        "def go():\n"
        "    return urllib.request.Request('https://x/breezeapi/api/v1/order', method='DELETE')\n"
    ),
    "method_variable": (
        "def go(conn):\n"
        "    verb = 'PUT'\n"
        "    conn.request(verb, '/breezeapi/api/v1/order')\n"
    ),
    "verb_and_path_in_one_constant": "ROUTE = 'POST /order'\n",
    "place_order_call": "def go(client):\n    return client.place_order(stock_code='X')\n",
    "place_market_order_name": "def go():\n    place_market_order()\n",
    "module_constant_then_post_in_a_function": (
        "ORDER_URL = 'https://x/breezeapi/api/v1/order'\n"
        "def go(session):\n"
        "    session.post(ORDER_URL)\n"
    ),
    "second_forward_class": "class HttpForward:\n    def send(self):\n        pass\n",
}


@pytest.mark.parametrize("name", sorted(PLANTED))
def test_ast_guard_fails_on_a_planted_order_call(tmp_path, name):
    planted = tmp_path / "planted.py"
    planted.write_text(PLANTED[name])
    assert find_order_paths([planted]), name
    with pytest.raises(AssertionError):
        assert_no_order_path([planted])


def test_ast_guard_allows_reads_and_our_own_plural_routes(tmp_path):
    ok = tmp_path / "ok.py"
    ok.write_text(
        "import http.client\n"
        "ROUTE = '/v1/orders/intents'\n"
        "def read(c):\n"
        "    c.request('GET', '/breezeapi/api/v1/order')\n"
        "def route(app):\n"
        "    @app.post('/v1/orders/authorize')\n"
        "    def authorize():\n"
        "        return {}\n"
    )
    assert find_order_paths([ok]) == []


def test_ast_guard_scans_directories_recursively(tmp_path):
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    (deep / "x.py").write_text(PLANTED["place_order_call"])
    assert find_order_paths([tmp_path])


# ------------------------------------------------------------ contract + safety


def test_orders_api_doc_lists_every_o6_code_and_all_eight_sections():
    from gateway_vm.orders import CODE_TABLE

    text = (ROOT / "gateway" / "ORDERS-API.md").read_text()
    for number in range(1, 9):
        assert re.search(rf"\*\*O{number} ", text), f"O{number} missing"
    for code in CODE_TABLE:
        assert f"`{code}`" in text, f"{code} missing from ORDERS-API.md"
    assert "growin-orders/1" in text and "VERIFIED_NOT_FORWARDED" in text
    assert "## Consumers" in text and "growin-orders/2" in text


def _guard_exit(tmp_path: Path, rel_path: str, reviewed: str = "false") -> int:
    changes = tmp_path / "changes.tsv"
    changes.write_text(f"modified\t{rel_path}\t\n")
    done = subprocess.run(
        ["bash", str(ROOT / ".github/scripts/safety-guard.sh"), str(changes), str(ROOT / ".github/safety-paths.txt")],
        capture_output=True, text=True, env={"PATH": os.environ["PATH"], "REVIEWED": reviewed},
    )
    return done.returncode


@pytest.mark.parametrize(
    "rel_path",
    [
        "gateway/vm/gateway_vm/orders/pipeline.py",
        "gateway/vm/gateway_vm/orders/__init__.py",
        "gateway/vm/gateway_vm/orders/data/nse_cash_tick_sizes.json",
        "gateway/vm/gateway_vm/orders/deeper/still/file.py",
        "backend/risk_india/limits.py",
        "backend/risk_india/sub/deep/state.py",
    ],
)
def test_safety_guard_blocks_unlabelled_changes_to_the_order_trees(tmp_path, rel_path):
    assert _guard_exit(tmp_path, rel_path) == 1


@pytest.mark.parametrize(
    "rel_path",
    ["gateway/vm/gateway_vm/egress.py", "docs/notes.md", "gateway/ORDERS-API.md", "backend/risk_indiana/x.py"],
)
def test_safety_guard_does_not_over_match(tmp_path, rel_path):
    assert _guard_exit(tmp_path, rel_path) == 0


def test_safety_guard_passes_with_the_review_label(tmp_path):
    assert _guard_exit(tmp_path, "gateway/vm/gateway_vm/orders/pipeline.py", reviewed="true") == 0
