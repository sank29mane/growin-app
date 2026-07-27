"""Touch ID/Secure Enclave approval primitives for immutable order intents."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec

from .ledger import (
    ApprovalConflict,
    ClaimResult,
    ExecutionLedger,
    LedgerApprovalKey,
    OrderNotFound,
    canonical_json,
)


APPROVAL_PURPOSE = "growin.execution.dispatch"
CONTROL_CLEAR_PURPOSE = "growin.execution.control.clear"
APPROVAL_VERSION = 1
Clock = Callable[[], datetime]


class ApprovalError(RuntimeError):
    """Base class for signed-approval failures."""


class EnrollmentError(ApprovalError):
    """Raised when local approval-key enrollment is unauthorized or invalid."""


class ApprovalVerificationError(ApprovalError):
    """Raised when P-256 approval evidence cannot be verified."""


@dataclass(frozen=True)
class ApprovalChallenge:
    challenge_id: str
    proposal_id: str
    key_id: str
    intent_hash: str
    signed_payload: bytes
    issued_at_epoch: int
    expires_at_epoch: int


@dataclass(frozen=True)
class ControlChallenge:
    challenge_id: str
    workspace: str
    version: int
    key_id: str
    signed_payload: bytes
    issued_at_epoch: int
    expires_at_epoch: int


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ApprovalService:
    """Create and verify short-lived approvals using one workspace-bound key."""

    def __init__(self, ledger: ExecutionLedger, *, clock: Clock = _utc_now) -> None:
        self._ledger = ledger
        self._clock = clock
        self.ensure_enrollment_token_file()

    @property
    def enrollment_token_path(self) -> Path:
        return self._ledger.path.with_name(
            f"{self._ledger.path.name}.enrollment-token"
        )

    def ensure_enrollment_token_file(self) -> Path:
        """Create one private bootstrap token when the workspace has no key."""

        token_path = self.enrollment_token_path
        if self._ledger.get_approval_key() is not None:
            return token_path
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(token_path, flags, 0o600)
        except FileExistsError:
            return token_path
        except OSError as exc:
            raise EnrollmentError("failed to create approval enrollment token") from exc
        try:
            token = secrets.token_urlsafe(32).encode("ascii")
            written = os.write(fd, token)
            if written != len(token):
                raise EnrollmentError("failed to write approval enrollment token")
            os.fsync(fd)
        except BaseException:
            try:
                token_path.unlink()
            except OSError:
                pass
            raise
        finally:
            os.close(fd)
        return token_path

    def enroll_key(
        self, public_key_x963: bytes, enrollment_token: str | bytes
    ) -> LedgerApprovalKey:
        """Enroll exactly one key, authorized by a private one-time token file."""

        _load_public_key(public_key_x963)
        normalized = bytes(public_key_x963)
        key_id = hashlib.sha256(normalized).hexdigest()
        existing = self._ledger.get_approval_key()
        if existing is not None:
            if existing.key_id == key_id and hmac.compare_digest(
                existing.public_key_x963, normalized
            ):
                return existing
            raise EnrollmentError("approval key rotation is not enabled")

        supplied = (
            enrollment_token.encode("utf-8")
            if isinstance(enrollment_token, str)
            else bytes(enrollment_token)
        )
        token_path = self.enrollment_token_path
        flags = os.O_RDONLY
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(token_path, flags)
        except OSError as exc:
            raise EnrollmentError("approval enrollment token is unavailable") from exc
        try:
            token_stat = os.fstat(fd)
            if not stat.S_ISREG(token_stat.st_mode):
                raise EnrollmentError("approval enrollment token must be a regular file")
            if stat.S_IMODE(token_stat.st_mode) != 0o600:
                raise EnrollmentError("approval enrollment token must use mode 0600")
            if token_stat.st_uid != os.getuid():
                raise EnrollmentError("approval enrollment token has the wrong owner")
            stored = os.read(fd, 4097)
            if len(stored) > 4096 or not stored:
                raise EnrollmentError("approval enrollment token is invalid")
            if not hmac.compare_digest(stored, supplied):
                raise EnrollmentError("approval enrollment token does not match")
        finally:
            os.close(fd)

        enrolled = self._ledger.register_approval_key(key_id, normalized)
        try:
            token_path.unlink()
        except OSError as exc:
            raise EnrollmentError("failed to consume approval enrollment token") from exc
        return enrolled

    def create_challenge(
        self, proposal_id: str, *, ttl_seconds: int = 60
    ) -> ApprovalChallenge:
        if not 5 <= ttl_seconds <= 300:
            raise ValueError("approval challenge TTL must be between 5 and 300 seconds")
        order = self._ledger.get_order(proposal_id)
        if order is None:
            raise OrderNotFound(f"order {proposal_id!r} was not found")
        intent = dict(order.intent)
        if str(intent.get("workspace")) != self._ledger.workspace:
            raise ApprovalConflict("order workspace does not match ledger workspace")
        if str(intent.get("mode", "")).upper() != "PAPER":
            raise ApprovalConflict("live execution remains disabled")
        admission = self._ledger.get_admission(proposal_id)
        if admission is None or admission.decision.value != "ADMITTED":
            raise ApprovalConflict("admitted evidence is required before approval")
        reservation = self._ledger.get_reservation(proposal_id)
        if reservation is None or reservation.state != "ACTIVE":
            raise ApprovalConflict("active paper reservation is required before approval")
        key = self._ledger.get_approval_key()
        if key is None:
            raise ApprovalConflict("approval signer is not enrolled")

        issued_at = _epoch(self._clock())
        expires_at = issued_at + ttl_seconds
        challenge_id = str(uuid.uuid4())
        payload = {
            "version": APPROVAL_VERSION,
            "purpose": APPROVAL_PURPOSE,
            "challenge_id": challenge_id,
            "proposal_id": order.proposal_id,
            "client_order_id": order.client_order_id,
            "intent_hash": order.intent_hash,
            "workspace": intent["workspace"],
            "account": intent["account"],
            "broker": intent["broker"],
            "mode": intent["mode"],
            "ticker": intent["ticker"],
            "side": intent["side"],
            "quantity": intent["quantity"],
            "admitted_quantity": str(admission.final_quantity),
            "currency": admission.currency,
            "price": str(admission.price),
            "notional": str(admission.notional),
            "evidence_hash": admission.evidence_hash,
            "nonce": secrets.token_urlsafe(32),
            "issued_at": issued_at,
            "expires_at": expires_at,
            "key_id": key.key_id,
        }
        signed_payload = canonical_json(payload).encode("utf-8")
        stored = self._ledger.store_approval_challenge(
            challenge_id=challenge_id,
            proposal_id=proposal_id,
            key_id=key.key_id,
            intent_hash=order.intent_hash,
            signed_payload=signed_payload,
            issued_at_epoch=issued_at,
            expires_at_epoch=expires_at,
        )
        return ApprovalChallenge(
            challenge_id=stored.challenge_id,
            proposal_id=stored.proposal_id,
            key_id=stored.key_id,
            intent_hash=stored.intent_hash,
            signed_payload=stored.signed_payload,
            issued_at_epoch=stored.issued_at_epoch,
            expires_at_epoch=stored.expires_at_epoch,
        )

    def approve_signed(
        self, proposal_id: str, challenge_id: str, signature_der: bytes
    ) -> ClaimResult:
        """Verify outside SQLite, then atomically consume and claim in the ledger."""

        challenge = self._ledger.get_approval_challenge(challenge_id)
        if challenge is None or challenge.proposal_id != proposal_id:
            raise ApprovalConflict("approval challenge was not found")
        key = self._ledger.get_approval_key()
        if key is None or key.key_id != challenge.key_id:
            raise ApprovalConflict("approval signer is not enrolled")
        public_key = _load_public_key(key.public_key_x963)
        try:
            public_key.verify(
                bytes(signature_der),
                challenge.signed_payload,
                ec.ECDSA(hashes.SHA256()),
            )
        except (InvalidSignature, ValueError) as exc:
            raise ApprovalVerificationError("approval signature is invalid") from exc
        payload_hash = hashlib.sha256(challenge.signed_payload).hexdigest()
        return self._ledger.claim_with_approval(
            proposal_id=proposal_id,
            challenge_id=challenge_id,
            key_id=key.key_id,
            signature_der=bytes(signature_der),
            verified_payload_hash=payload_hash,
            now_epoch=_epoch(self._clock()),
        )

    def create_control_challenge(self, *, ttl_seconds: int = 60) -> ControlChallenge:
        if not 5 <= ttl_seconds <= 300:
            raise ValueError("control challenge TTL must be between 5 and 300 seconds")
        control = self._ledger.get_workspace_control()
        if not control.engaged:
            raise ApprovalConflict("workspace control is not engaged")
        key = self._ledger.get_approval_key()
        if key is None:
            raise ApprovalConflict("approval signer is not enrolled")
        issued_at = _epoch(self._clock())
        challenge_id = str(uuid.uuid4())
        payload = {
            "version": 1,
            "purpose": CONTROL_CLEAR_PURPOSE,
            "challenge_id": challenge_id,
            "workspace": self._ledger.workspace,
            "control_version": control.version,
            "nonce": secrets.token_urlsafe(32),
            "issued_at": issued_at,
            "expires_at": issued_at + ttl_seconds,
            "key_id": key.key_id,
        }
        signed_payload = canonical_json(payload).encode("utf-8")
        return ControlChallenge(
            challenge_id=challenge_id,
            workspace=self._ledger.workspace,
            version=control.version,
            key_id=key.key_id,
            signed_payload=signed_payload,
            issued_at_epoch=issued_at,
            expires_at_epoch=issued_at + ttl_seconds,
        )

    def clear_workspace_control(
        self, challenge: ControlChallenge, signature_der: bytes
    ) -> None:
        key = self._ledger.get_approval_key()
        if key is None or key.key_id != challenge.key_id:
            raise ApprovalConflict("approval signer is not enrolled")
        public_key = _load_public_key(key.public_key_x963)
        try:
            public_key.verify(bytes(signature_der), challenge.signed_payload, ec.ECDSA(hashes.SHA256()))
        except (InvalidSignature, ValueError) as exc:
            raise ApprovalVerificationError("control-clear signature is invalid") from exc
        try:
            payload = json.loads(challenge.signed_payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApprovalConflict("control challenge is malformed") from exc
        if (
            payload.get("purpose") != CONTROL_CLEAR_PURPOSE
            or payload.get("workspace") != self._ledger.workspace
            or payload.get("control_version") != challenge.version
            or payload.get("key_id") != key.key_id
            or _epoch(self._clock()) >= int(payload.get("expires_at", 0))
        ):
            raise ApprovalConflict("control challenge is invalid or expired")
        self._ledger.clear_workspace_control(
            version=challenge.version,
            evidence_id=challenge.challenge_id,
            purpose=CONTROL_CLEAR_PURPOSE,
        )


def _load_public_key(public_key_x963: bytes) -> ec.EllipticCurvePublicKey:
    if len(public_key_x963) != 65:
        raise EnrollmentError("approval public key must be 65-byte X9.63 data")
    try:
        return ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(), bytes(public_key_x963)
        )
    except ValueError as exc:
        raise EnrollmentError("approval public key is not valid P-256 data") from exc


def _epoch(value: datetime) -> int:
    if value.tzinfo is None:
        raise ValueError("approval clock must return a timezone-aware datetime")
    return int(value.timestamp())


__all__ = [
    "APPROVAL_PURPOSE",
    "APPROVAL_VERSION",
    "CONTROL_CLEAR_PURPOSE",
    "ApprovalChallenge",
    "ApprovalError",
    "ApprovalService",
    "ApprovalVerificationError",
    "ControlChallenge",
    "EnrollmentError",
]
