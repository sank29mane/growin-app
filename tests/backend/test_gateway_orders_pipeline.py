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
