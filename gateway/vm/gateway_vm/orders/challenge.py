"""VM-minted challenge bytes (O4) and the outstanding-challenge table (P-03).

The VM authors every field of the signed bytes and keeps a copy; it verifies
against its own copy and nobody re-serializes. The Mac never supplies bytes.
Outstanding challenges live in memory only: at most four, 60 seconds, VM clock.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
import uuid
from dataclasses import dataclass
from typing import Any

from . import (
    CHALLENGE_TTL_SECONDS,
    MAX_OUTSTANDING_CHALLENGES,
    ORDER_PURPOSE,
    SIGNED_VERSION,
    refusal,
)
from .intent import Intent


def canonical_bytes(value: Any) -> bytes:
    """Sorted keys, compact separators, ASCII only. Floats and NaN are refused."""
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def intent_sha256(intent: Intent) -> str:
    return hashlib.sha256(canonical_bytes(intent.as_dict())).hexdigest()


def build_payload(
    *,
    challenge_id: str,
    nonce: str,
    issued_at: int,
    expires_at: int,
    key_id: str,
    limits_sha256: str,
    intent: Intent,
) -> dict[str, Any]:
    return {
        "version": SIGNED_VERSION,
        "purpose": ORDER_PURPOSE,
        "challenge_id": challenge_id,
        "nonce": nonce,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "key_id": key_id,
        "limits_sha256": limits_sha256,
        "intent": intent.as_dict(),
    }


class SecretIds:
    """Source of challenge ids and nonces. Tests inject a deterministic one."""

    def challenge_id(self) -> str:
        return str(uuid.uuid4())

    def nonce(self) -> str:
        return secrets.token_urlsafe(32)  # 43 url-safe characters


@dataclass(frozen=True)
class Challenge:
    challenge_id: str
    signed_bytes: bytes
    intent: Intent
    issued_at: int
    expires_at: int


class ChallengeStore:
    """In-memory, single-use challenges. Callers hold the pipeline lock for
    multi-step flows; every method is also safe on its own."""

    def __init__(self) -> None:
        self._items: dict[str, Challenge] = {}
        self._mutex = threading.Lock()

    def _purge(self, now_epoch: int) -> None:
        for key in [k for k, c in self._items.items() if now_epoch >= c.expires_at]:
            del self._items[key]

    def live_count(self, now_epoch: int) -> int:
        with self._mutex:
            self._purge(now_epoch)
            return len(self._items)

    def has_live_for_intent(self, intent_id: str, now_epoch: int) -> bool:
        with self._mutex:
            self._purge(now_epoch)
            return any(c.intent.intent_id == intent_id for c in self._items.values())

    def mint(
        self,
        intent: Intent,
        *,
        now_epoch: int,
        key_id: str,
        limits_sha256: str,
        ids: SecretIds,
    ) -> Challenge:
        with self._mutex:
            self._purge(now_epoch)
            if any(c.intent.intent_id == intent.intent_id for c in self._items.values()):
                raise refusal("challenge_outstanding")
            if len(self._items) >= MAX_OUTSTANDING_CHALLENGES:
                raise refusal("challenge_capacity")
            challenge_id = ids.challenge_id()
            payload = build_payload(
                challenge_id=challenge_id,
                nonce=ids.nonce(),
                issued_at=now_epoch,
                expires_at=now_epoch + CHALLENGE_TTL_SECONDS,
                key_id=key_id,
                limits_sha256=limits_sha256,
                intent=intent,
            )
            challenge = Challenge(
                challenge_id=challenge_id,
                signed_bytes=canonical_bytes(payload),
                intent=intent,
                issued_at=now_epoch,
                expires_at=now_epoch + CHALLENGE_TTL_SECONDS,
            )
            self._items[challenge_id] = challenge
            return challenge

    def peek(self, challenge_id: str, now_epoch: int) -> Challenge | None:
        with self._mutex:
            self._purge(now_epoch)
            return self._items.get(challenge_id)

    def take(self, challenge_id: str) -> Challenge:
        """Remove and return a challenge: single use, gone before anyone verifies.

        Unknown (never minted, already consumed, or purged) is challenge_unknown.
        Expiry is the caller's check against the VM clock, after removal, so an
        expired challenge cannot be tried again either.
        """
        with self._mutex:
            challenge = self._items.pop(challenge_id, None)
            if challenge is None:
                raise refusal("challenge_unknown")
            return challenge
