"""Swift signatures against the Python verifiers (Phase 63-03).

`swift_signature.json` holds DER signatures made by Swift (CryptoKit, the app's
signer code path with an injected TEST ONLY key) over the golden bytes in
`signing_vectors.json`. Both verifiers must accept them: the VM verifier
(gateway_vm, DER-only, canonical) and the backend loader (OpenSSL via
cryptography). A one-byte flip in the message or the signature must fail both.

The `se_uat` tests read the operator's real Secure Enclave signature, written by
`SecureEnclaveSignerTests/testOperatorSecureEnclaveUAT`. They skip unless that file
exists. Nothing here ever prints key material.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "gateway" / "vm") not in sys.path:
    sys.path.insert(0, str(ROOT / "gateway" / "vm"))

from backend.execution import approval as backend_approval  # noqa: E402
from gateway_vm.orders.verify import PinnedKey  # noqa: E402

FIXTURES = ROOT / "tests" / "backend" / "fixtures" / "relay_orders"
VECTORS = json.loads((FIXTURES / "signing_vectors.json").read_text(encoding="utf-8"))
SWIFT = json.loads((FIXTURES / "swift_signature.json").read_text(encoding="utf-8"))
PRIMARY = VECTORS["keys"]["primary"]
GOLDEN = {row["name"]: row for row in VECTORS["rows"]}
UAT_FILE = Path(
    os.environ.get("GROWIN_SE_UAT_FILE")
    or Path.home() / ".config" / "growin" / "uat" / "63-se-signature.json"
)


def _backend_verifies(x963: bytes, signature: bytes, message: bytes) -> bool:
    public = backend_approval._load_public_key(x963)
    try:
        public.verify(signature, message, ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, ValueError):
        return False
    return True


def _flip(data: bytes, index: int) -> bytes:
    out = bytearray(data)
    out[index] ^= 0x01
    return bytes(out)


def test_swift_vector_is_labelled_test_only_and_uses_the_primary_test_key():
    assert SWIFT["test_only"] is True
    assert SWIFT["signer"] == "primary"
    assert SWIFT["key_id"] == PRIMARY["key_id"]
    assert SWIFT["public_key_x963_hex"] == PRIMARY["public_key_x963_hex"]


def test_swift_vector_covers_every_golden_row():
    assert [r["name"] for r in SWIFT["rows"]] == [r["name"] for r in VECTORS["rows"]]


@pytest.mark.parametrize("row", SWIFT["rows"], ids=[r["name"] for r in SWIFT["rows"]])
def test_swift_der_verifies_in_the_vm_and_backend_verifiers(row):
    golden = GOLDEN[row["name"]]
    message = base64.b64decode(golden["canonical_b64"])
    assert hashlib.sha256(message).hexdigest() == row["canonical_sha256"] == golden["canonical_sha256"]
    signature = base64.b64decode(row["signature_der_b64"])
    x963 = bytes.fromhex(PRIMARY["public_key_x963_hex"])
    pin = PinnedKey(x963, allow_test_key=True)
    assert pin.verify_der(signature, message) is True
    assert _backend_verifies(x963, signature, message) is True


@pytest.mark.parametrize("row", SWIFT["rows"], ids=[r["name"] for r in SWIFT["rows"]])
def test_one_byte_flip_fails_both_verifiers(row):
    golden = GOLDEN[row["name"]]
    message = base64.b64decode(golden["canonical_b64"])
    signature = base64.b64decode(row["signature_der_b64"])
    x963 = bytes.fromhex(PRIMARY["public_key_x963_hex"])
    pin = PinnedKey(x963, allow_test_key=True)
    for index in (0, len(message) // 2, len(message) - 1):
        bad = _flip(message, index)
        assert pin.verify_der(signature, bad) is False
        assert _backend_verifies(x963, signature, bad) is False
    for index in range(len(signature)):
        bad_sig = _flip(signature, index)
        assert pin.verify_der(bad_sig, message) is False
        assert _backend_verifies(x963, bad_sig, message) is False


def test_swift_signature_does_not_verify_under_the_other_test_key():
    other = bytes.fromhex(VECTORS["keys"]["other"]["public_key_x963_hex"])
    pin = PinnedKey(other, allow_test_key=True)
    row = SWIFT["rows"][0]
    message = base64.b64decode(GOLDEN[row["name"]]["canonical_b64"])
    assert pin.verify_der(base64.b64decode(row["signature_der_b64"]), message) is False


# --- Operator UAT: one real Secure Enclave signature ----------------------------

_needs_uat = pytest.mark.skipif(
    not UAT_FILE.exists(), reason="operator has not run testOperatorSecureEnclaveUAT yet"
)


def _uat() -> dict:
    return json.loads(UAT_FILE.read_text(encoding="utf-8"))


def _uat_message(uat: dict) -> bytes:
    """Rebuild the signed bytes independently: golden row 0 with the real key_id swapped in."""
    golden = GOLDEN[uat["golden_row"]]
    text = base64.b64decode(golden["canonical_b64"]).decode("ascii")
    return text.replace(PRIMARY["key_id"], uat["key_id"]).encode("ascii")


@_needs_uat
def test_se_uat_file_is_private_and_names_a_real_key():
    mode = stat.S_IMODE(os.stat(UAT_FILE).st_mode)
    assert mode == 0o600, "UAT file must be mode 0600"
    uat = _uat()
    x963 = bytes.fromhex(uat["public_key_x963_hex"])
    assert len(x963) == 65 and x963[0] == 4
    assert hashlib.sha256(x963).hexdigest() == uat["key_id"]
    committed = {k["public_key_x963_hex"] for k in VECTORS["keys"].values()}
    assert uat["public_key_x963_hex"] not in committed, "the UAT must use the real India key"


@_needs_uat
def test_se_uat_signature_verifies_in_the_vm_and_backend_verifiers():
    uat = _uat()
    x963 = bytes.fromhex(uat["public_key_x963_hex"])
    message = base64.b64decode(uat["signed_bytes_b64"])
    assert message == _uat_message(uat)
    assert hashlib.sha256(message).hexdigest() == uat["signed_bytes_sha256"]
    signature = base64.b64decode(uat["signature_der_b64"])
    # A real key: no allow_test_key escape hatch.
    pin = PinnedKey(x963)
    assert pin.key_id == uat["key_id"]
    assert pin.verify_der(signature, message) is True
    assert _backend_verifies(x963, signature, message) is True


@_needs_uat
def test_se_uat_flipped_byte_fails_both_verifiers():
    uat = _uat()
    x963 = bytes.fromhex(uat["public_key_x963_hex"])
    message = base64.b64decode(uat["signed_bytes_b64"])
    signature = base64.b64decode(uat["signature_der_b64"])
    pin = PinnedKey(x963)
    for index in (0, len(message) // 2, len(message) - 1):
        bad = _flip(message, index)
        assert pin.verify_der(signature, bad) is False
        assert _backend_verifies(x963, signature, bad) is False
    for index in range(len(signature)):
        bad_sig = _flip(signature, index)
        assert pin.verify_der(bad_sig, message) is False
        assert _backend_verifies(x963, bad_sig, message) is False
