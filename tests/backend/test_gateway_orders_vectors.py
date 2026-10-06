"""Golden vectors for growin-orders/1 (Phase 63-01).

The same files are read by the Mac suite (63-02) and the Swift suite (63-03),
so all three check the same canonical bytes, signatures and limit decisions.
gateway_vm cannot import backend; this file is where the two implementations
are made to agree.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "gateway" / "vm") not in sys.path:
    sys.path.insert(0, str(ROOT / "gateway" / "vm"))

from backend.execution import approval as backend_approval  # noqa: E402
from backend.execution.ledger import canonical_json as backend_canonical_json  # noqa: E402
from gateway_vm.orders import verify as vm_verify  # noqa: E402
from gateway_vm.orders.challenge import canonical_bytes  # noqa: E402
from gateway_vm.orders.intent import parse_intent  # noqa: E402
from gateway_vm.orders.verify import KeyConfigError, PinnedKey  # noqa: E402

FIXTURES = ROOT / "tests" / "backend" / "fixtures" / "relay_orders"
VECTORS = json.loads((FIXTURES / "signing_vectors.json").read_text(encoding="utf-8"))
ROWS = VECTORS["rows"]
NEGATIVES = VECTORS["negatives"]
PRIMARY = VECTORS["keys"]["primary"]
OTHER = VECTORS["keys"]["other"]


def _pin(key: dict) -> PinnedKey:
    return PinnedKey(bytes.fromhex(key["public_key_x963_hex"]), allow_test_key=True)


def _backend_verifies(key: dict, signature: bytes, message: bytes) -> bool:
    public = backend_approval._load_public_key(bytes.fromhex(key["public_key_x963_hex"]))
    try:
        public.verify(signature, message, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError):
        return False
    return True


def test_vectors_are_labelled_test_only():
    assert VECTORS["test_only"] is True
    assert all(k["test_only"] is True for k in VECTORS["keys"].values())


def test_key_ids_and_private_scalars_are_consistent():
    for key in VECTORS["keys"].values():
        x963 = bytes.fromhex(key["public_key_x963_hex"])
        assert hashlib.sha256(x963).hexdigest() == key["key_id"]
        private = ec.derive_private_key(int(key["private_scalar_hex"], 16), ec.SECP256R1())
        public = private.public_key().public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint,
        )
        assert public == x963


@pytest.mark.parametrize("row", ROWS, ids=[r["name"] for r in ROWS])
def test_canonical_bytes_agree_between_backend_and_vm(row):
    payload = row["payload"]
    committed = base64.b64decode(row["canonical_b64"])
    assert canonical_bytes(payload) == committed
    assert backend_canonical_json(payload).encode("utf-8") == committed
    assert hashlib.sha256(committed).hexdigest() == row["canonical_sha256"]


@pytest.mark.parametrize("row", ROWS, ids=[r["name"] for r in ROWS])
def test_vector_intents_parse_strictly(row):
    intent = row["payload"]["intent"]
    assert parse_intent(json.dumps(intent)).as_dict() == intent


@pytest.mark.parametrize("row", ROWS, ids=[r["name"] for r in ROWS])
def test_signing_rows_agree_between_backend_and_vm(row):
    signature = base64.b64decode(row["signature_der_b64"])
    message = base64.b64decode(row["canonical_b64"])
    key = VECTORS["keys"][row["signer"]]
    assert _pin(key).verify_der(signature, message) is row["expect_valid"] is True
    assert _backend_verifies(key, signature, message) is True


@pytest.mark.parametrize("row", NEGATIVES, ids=[r["name"] for r in NEGATIVES])
def test_negative_rows_agree_between_backend_and_vm(row):
    signature = base64.b64decode(row["signature_der_b64"])
    message = base64.b64decode(row["message_b64"])
    vm = _pin(PRIMARY).verify_der(signature, message)
    assert vm is row["expect_valid"]
    # The backend verifier (OpenSSL via cryptography) is never more permissive
    # than the VM's. Where a row is invalid the VM must refuse it; where the
    # backend also refuses, the two agree outright.
    backend = _backend_verifies(PRIMARY, signature, message)
    if row["expect_valid"]:
        assert backend is True
    else:
        assert vm is False
        assert backend is False


def test_wrong_key_row_verifies_only_under_the_other_key():
    row = next(n for n in NEGATIVES if n["name"] == "wrong_key")
    signature = base64.b64decode(row["signature_der_b64"])
    message = base64.b64decode(row["message_b64"])
    assert _pin(OTHER).verify_der(signature, message) is True
    assert _pin(PRIMARY).verify_der(signature, message) is False


def test_vm_mint_reproduces_every_committed_byte_string(tmp_path):
    """The VM's own minting path produces exactly the vector bytes."""
    from datetime import datetime, timezone

    from gateway_vm.orders.audit import AuditLog
    from gateway_vm.orders.challenge import SecretIds
    from gateway_vm.orders.pipeline import GuardResult, OrderPipeline, RefusalForward

    class Allow:
        def check(self, intent, now):
            return GuardResult()

        def set_mac_halt(self):
            return ()

    class Mem:
        def is_consumed(self, intent_id):
            return False

        def consume(self, intent_id):
            pass

    for row in ROWS:
        payload = row["payload"]

        class Ids(SecretIds):
            def challenge_id(self):
                return payload["challenge_id"]

            def nonce(self):
                return payload["nonce"]

        now = datetime.fromtimestamp(payload["issued_at"], timezone.utc)
        pipeline = OrderPipeline(
            key=_pin(PRIMARY),
            limits_sha256=VECTORS["limits_sha256"],
            intents=Mem(),
            guard=Allow(),
            audit=AuditLog(tmp_path / f"{row['name']}.jsonl"),
            clock=lambda now=now: now,
            forward=RefusalForward(),
            ids=Ids(),
        )
        minted = pipeline.mint(json.dumps(payload["intent"]))
        assert minted.signed_bytes == base64.b64decode(row["canonical_b64"])


def test_committed_test_key_is_refused_as_a_production_pin(tmp_path):
    for key in VECTORS["keys"].values():
        x963 = bytes.fromhex(key["public_key_x963_hex"])
        assert key["public_key_x963_hex"] in vm_verify.TEST_ONLY_KEY_X963_HEX
        with pytest.raises(KeyConfigError):
            PinnedKey(x963)  # default: not allowed
        path = tmp_path / f"order-key-{key['key_id'][:8]}.json"
        path.write_text(
            json.dumps({"key_id": key["key_id"], "public_key_x963_hex": key["public_key_x963_hex"]})
        )
        path.chmod(0o644)
        import os

        with pytest.raises(KeyConfigError):
            vm_verify.load_order_key(path, expected_owner_uid=os.getuid())
    assert set(vm_verify.TEST_ONLY_KEY_X963_HEX) == {
        k["public_key_x963_hex"] for k in VECTORS["keys"].values()
    }


def test_order_key_loader_refusals(tmp_path):
    import os

    fresh = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    good = {"key_id": hashlib.sha256(fresh).hexdigest(), "public_key_x963_hex": fresh.hex()}

    def write(name: str, body, mode: int = 0o644) -> Path:
        path = tmp_path / name
        path.write_text(body if isinstance(body, str) else json.dumps(body))
        path.chmod(mode)
        return path

    uid = os.getuid()
    assert vm_verify.load_order_key(write("ok.json", good), expected_owner_uid=uid).key_id == good["key_id"]
    for name, body, mode in (
        ("group_w.json", good, 0o664),
        ("other_w.json", good, 0o646),
        ("extra.json", {**good, "x": 1}, 0o644),
        ("missing.json", {"key_id": good["key_id"]}, 0o644),
        ("wrong_id.json", {**good, "key_id": "ab" * 32}, 0o644),
        ("not_json.json", "{nope", 0o644),
        ("short_key.json", {"key_id": "ab" * 32, "public_key_x963_hex": "04ab"}, 0o644),
    ):
        with pytest.raises(KeyConfigError):
            vm_verify.load_order_key(write(name, body, mode), expected_owner_uid=uid)
    with pytest.raises(KeyConfigError):
        vm_verify.load_order_key(tmp_path / "absent.json", expected_owner_uid=uid)
    with pytest.raises(KeyConfigError):  # owner must be root in production
        vm_verify.load_order_key(tmp_path / "ok.json", expected_owner_uid=uid + 1)
    link = tmp_path / "link.json"
    link.symlink_to(tmp_path / "ok.json")
    with pytest.raises(KeyConfigError):
        vm_verify.load_order_key(link, expected_owner_uid=uid)
