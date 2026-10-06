"""ECDSA P-256 verification against one pinned key (T-63-01, T-63-09).

Only DER signatures over the VM-held bytes verify. A raw r||s signature, a DER
blob with trailing bytes, or anything that does not re-encode to itself is
refused. A high-S signature verifies (the library accepts it); that is safe
because replay protection never keys on signature bytes, only on the
challenge and the intent id.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)

# Public halves of the committed TEST ONLY keys (tests/backend/fixtures/
# relay_orders/signing_vectors.json). A pin equal to either is refused unless a
# test passes allow_test_key=True. The production loader never does.
TEST_ONLY_KEY_X963_HEX = (
    "043ca059121d88096540cdef44df066a6de1e6f807008e3178824ec4d9b6b6cc6d"
    "dde26b3c22626a41041c5300e929a684c4cbeeaa46cb27c2b35ca5aff18b80c2",
    "04d7854e7887be815c0a5308d2fdd98db35ae4bce196cf184e5c7f94676a9929e4"
    "a76134501a05d507658b266fdd19d1863f4b1bc337d574848fdaf132489497db",
)

_MAX_DER = 72
_MIN_DER = 8
_MAX_KEY_FILE = 4096


class KeyConfigError(Exception):
    """The pinned order key is missing, malformed, unsafe or a committed test key."""


class PinnedKey:
    def __init__(self, x963: bytes, *, allow_test_key: bool = False) -> None:
        if len(x963) != 65 or x963[0] != 4:
            raise KeyConfigError("order key must be 65-byte uncompressed X9.63")
        if x963.hex() in TEST_ONLY_KEY_X963_HEX and not allow_test_key:
            raise KeyConfigError("the committed TEST ONLY key cannot be a production pin")
        try:
            self._key = ec.EllipticCurvePublicKey.from_encoded_point(
                ec.SECP256R1(), bytes(x963)
            )
        except ValueError as exc:
            raise KeyConfigError("order key is not a valid P-256 point") from exc
        self.x963 = bytes(x963)
        self.key_id = hashlib.sha256(self.x963).hexdigest()

    def verify_der(self, signature: bytes, message: bytes) -> bool:
        """True only for a canonical-DER ECDSA/SHA-256 signature over message."""
        sig = bytes(signature)
        if not _MIN_DER <= len(sig) <= _MAX_DER:
            return False
        try:
            r, s = decode_dss_signature(sig)
            if encode_dss_signature(r, s) != sig:
                return False
            self._key.verify(sig, bytes(message), ec.ECDSA(hashes.SHA256()))
        except (InvalidSignature, ValueError):
            return False
        return True


def load_order_key(
    path: str | Path,
    *,
    expected_owner_uid: int | None = 0,
    allow_test_key: bool = False,
) -> PinnedKey:
    """Read /etc/growin-gateway/order-key.json: {"key_id", "public_key_x963_hex"}.

    Refused: symlink, not a regular file, group- or other-writable, wrong owner,
    unknown or missing keys, a key_id that is not sha256 of the key, and the
    committed test key. Callers answer 503 config_invalid on KeyConfigError.
    """
    target = Path(path)
    try:
        st = os.lstat(target)
    except OSError as exc:
        raise KeyConfigError("order key file is unreadable") from exc
    if not stat.S_ISREG(st.st_mode):
        raise KeyConfigError("order key file must be a regular file")
    if st.st_mode & 0o022:
        raise KeyConfigError("order key file is writable by group or other")
    if expected_owner_uid is not None and st.st_uid != expected_owner_uid:
        raise KeyConfigError("order key file has the wrong owner")
    if st.st_size > _MAX_KEY_FILE:
        raise KeyConfigError("order key file is too large")
    try:
        parsed = json.loads(target.read_text(encoding="ascii"))
    except (OSError, ValueError) as exc:
        raise KeyConfigError("order key file is not valid JSON") from exc
    if not isinstance(parsed, dict) or set(parsed) != {"key_id", "public_key_x963_hex"}:
        raise KeyConfigError("order key file has the wrong keys")
    hex_key = parsed["public_key_x963_hex"]
    if not isinstance(hex_key, str) or not isinstance(parsed["key_id"], str):
        raise KeyConfigError("order key file values must be strings")
    try:
        key = PinnedKey(bytes.fromhex(hex_key), allow_test_key=allow_test_key)
    except ValueError as exc:
        raise KeyConfigError("order key is not hex") from exc
    if key.key_id != parsed["key_id"]:
        raise KeyConfigError("order key_id does not match the key")
    return key

