"""The Mac client for growin-orders/1 (O1 to O8), and the checks the Mac runs on what the VM sends.

Everything here rides on ``RelayClient``: the same tunnel, the same Host rule, the same session
token and the same in-flight semaphore. Only the contract header differs. The client never
retries, never reads or prints a token, and has no method that places, modifies, cancels or
clears anything: the VM has no such route either. An order refusal arrives as one of the O6
error classes in ``errors``.

``check_minted_bytes`` is the Mac's half of P-20. The VM mints the bytes the operator signs; the
Mac signs nothing it has not read. Before any approval challenge exists the Mac compares the
embedded intent, ``limits_sha256`` and ``key_id`` with its own and refuses on any difference.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import threading
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from .client import ORDERS_CONTRACT, RelayClient
from .errors import RelayProtocolError

ROUTE_INTENTS = "/v1/orders/intents"
ROUTE_AUTHORIZE = "/v1/orders/authorize"
ROUTE_HALT = "/v1/orders/halt"
ROUTE_STATE = "/v1/orders/state"
ROUTE_AUDIT = "/v1/orders/audit"

ORDER_PURPOSE = "growin.relay.order"
SIGNED_VERSION = 1
DECISION_NOT_FORWARDED = "VERIFIED_NOT_FORWARDED"
BROKER = "icici-breeze"
MODE = "SHADOW"

INTENT_FIELDS: tuple[str, ...] = (
    "intent_id",
    "proposal_id",
    "workspace",
    "broker",
    "mode",
    "exchange",
    "product",
    "order_type",
    "validity",
    "side",
    "stock_code",
    "isin",
    "quantity",
    "limit_price",
    "reason",
    "batch_id",
    "limits_sha256",
    "params_sha256",
    "key_id",
)
REASONS = frozenset({"entry", "exit", "halve", "flatten", "stop"})

_INTENT_ID = re.compile(r"^[A-Za-z0-9_-]{8,96}$")
_PROPOSAL_ID = re.compile(r"^[A-Za-z0-9_-]{1,96}$")
_STOCK_CODE = re.compile(r"^[A-Z0-9]{1,10}$")
_ISIN = re.compile(r"^IN[A-Z0-9]{9}[0-9]$")
_LIMIT_PRICE = re.compile(r"^[0-9]{1,7}(\.[0-9]{1,2})?$")
_BATCH_ID = re.compile(r"^[a-z0-9-]{8,64}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_CHALLENGE_ID = re.compile(r"^[A-Za-z0-9-]{1,64}$")
_NONCE = re.compile(r"^[A-Za-z0-9_-]{43}$")
_AFTER_SEQ_MAX = 10**12
MAX_CHALLENGE_LIFETIME_S = 300  # the VM's is 60; anything longer than five minutes is not its bytes


class OrdersProtocolError(RelayProtocolError):
    """The VM answered, but not in the shape growin-orders/1 defines."""


class MintMismatch(ValueError):
    """The bytes the VM minted are not the order the Mac asked for. Nothing is stored or shown.

    ``reason`` is a short closed word and never a value.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# ------------------------------------------------------------------ intent


def canonical_bytes(value: Any) -> bytes:
    """Sorted keys, compact separators, ASCII only. Floats and NaN are refused."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def intent_sha256(intent: Mapping[str, Any]) -> str:
    """The VM's ``intent_sha256`` (O7): sha256 of the canonical bytes of the O3 intent."""
    return hashlib.sha256(canonical_bytes(dict(intent))).hexdigest()


def intent_body(
    *,
    intent_id: str,
    proposal_id: str,
    side: str,
    stock_code: str,
    isin: str,
    quantity: int,
    limit_price: str,
    reason: str,
    batch_id: str | None,
    limits_sha256: str,
    params_sha256: str,
    key_id: str,
) -> dict[str, Any]:
    """The O3 intent, field by field, refused here if any field would be refused by the VM.

    The Mac never sends the VM a body its own pattern checks reject, so a malformed order is a
    local refusal and costs no round trip.
    """

    if side not in ("buy", "sell"):
        raise MintMismatch("side")
    if reason not in REASONS:
        raise MintMismatch("reason")
    if isinstance(quantity, bool) or not isinstance(quantity, int) or not 1 <= quantity <= 100_000:
        raise MintMismatch("quantity")
    if not isinstance(limit_price, str) or _LIMIT_PRICE.fullmatch(limit_price) is None:
        raise MintMismatch("limit_price")
    if not Decimal(limit_price) > 0:
        raise MintMismatch("limit_price")
    for name, value, pattern in (
        ("intent_id", intent_id, _INTENT_ID),
        ("proposal_id", proposal_id, _PROPOSAL_ID),
        ("stock_code", stock_code, _STOCK_CODE),
        ("isin", isin, _ISIN),
        ("limits_sha256", limits_sha256, _HEX64),
        ("params_sha256", params_sha256, _HEX64),
        ("key_id", key_id, _HEX64),
    ):
        if not isinstance(value, str) or pattern.fullmatch(value) is None:
            raise MintMismatch(name)
    if batch_id is not None and (not isinstance(batch_id, str) or _BATCH_ID.fullmatch(batch_id) is None):
        raise MintMismatch("batch_id")
    return {
        "intent_id": intent_id,
        "proposal_id": proposal_id,
        "workspace": "india",
        "broker": BROKER,
        "mode": MODE,
        "exchange": "NSE",
        "product": "cash",
        "order_type": "limit",
        "validity": "day",
        "side": side,
        "stock_code": stock_code,
        "isin": isin,
        "quantity": quantity,
        "limit_price": limit_price,
        "reason": reason,
        "batch_id": batch_id,
        "limits_sha256": limits_sha256,
        "params_sha256": params_sha256,
        "key_id": key_id,
    }


# --------------------------------------------------------- minted bytes


@dataclass(frozen=True)
class MintedChallenge:
    """What ``POST /v1/orders/intents`` answered, after the Mac's own checks have passed."""

    challenge_id: str
    signed_bytes: bytes
    issued_at_epoch: int
    expires_at_epoch: int


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise MintMismatch("duplicate_key")
        out[key] = value
    return out


def _no_float(_text: str) -> Any:
    raise MintMismatch("float")


def check_minted_bytes(
    signed_bytes: bytes,
    *,
    challenge_id: str,
    expected_intent: Mapping[str, Any],
    limits_sha256: str,
    key_id: str,
) -> MintedChallenge:
    """P-20: refuse bytes whose embedded intent, ``limits_sha256`` or ``key_id`` are not the Mac's.

    The check parses the bytes strictly, then requires them to be exactly the canonical form
    of what was parsed, so no field the parser skipped can survive into the signature. The
    purpose must be the relay order purpose (never the local dispatch or control-clear one).
    """

    try:
        text = bytes(signed_bytes).decode("ascii")
        payload = json.loads(
            text,
            object_pairs_hook=_no_duplicates,
            parse_float=_no_float,
            parse_constant=_no_float,
        )
    except MintMismatch:
        raise
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise MintMismatch("bad_bytes") from None
    if not isinstance(payload, dict) or canonical_bytes(payload) != bytes(signed_bytes):
        raise MintMismatch("not_canonical")
    if set(payload) != {
        "version",
        "purpose",
        "challenge_id",
        "nonce",
        "issued_at",
        "expires_at",
        "key_id",
        "limits_sha256",
        "intent",
    }:
        raise MintMismatch("keys")
    if payload["version"] != SIGNED_VERSION or isinstance(payload["version"], bool):
        raise MintMismatch("version")
    if payload["purpose"] != ORDER_PURPOSE:
        raise MintMismatch("purpose")
    if (
        not isinstance(challenge_id, str)
        or _CHALLENGE_ID.fullmatch(challenge_id) is None
        or payload["challenge_id"] != challenge_id
    ):
        raise MintMismatch("challenge_id")
    nonce = payload["nonce"]
    if not isinstance(nonce, str) or _NONCE.fullmatch(nonce) is None:
        raise MintMismatch("nonce")
    issued, expires = payload["issued_at"], payload["expires_at"]
    for value in (issued, expires):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise MintMismatch("times")
    if not 0 < expires - issued <= MAX_CHALLENGE_LIFETIME_S:
        raise MintMismatch("times")
    if payload["key_id"] != key_id:
        raise MintMismatch("key_id")
    if payload["limits_sha256"] != limits_sha256:
        raise MintMismatch("limits_sha256")
    # Compare the canonical bytes, not the parsed values: ``True == 1`` in Python, so a VM that
    # minted ``"quantity": true`` for an expected ``1`` would pass a ``!=`` on the objects.
    try:
        same_intent = canonical_bytes(payload["intent"]) == canonical_bytes(dict(expected_intent))
    except (TypeError, ValueError):
        same_intent = False
    if not same_intent:
        raise MintMismatch("intent")
    return MintedChallenge(
        challenge_id=challenge_id,
        signed_bytes=bytes(signed_bytes),
        issued_at_epoch=issued,
        expires_at_epoch=expires,
    )


# ------------------------------------------------------------- replies


@dataclass(frozen=True)
class AuthorizeResult:
    decision: str
    intent_id: str
    audit_seq: int
    audit_sha256: str


@dataclass(frozen=True)
class OrdersState:
    """The O2 state: names and numbers only."""

    kill: str
    latches: tuple[str, ...]
    drawdown: str
    peak_date: str | None
    last_evaluated_session: str | None
    alerts_logged: int


@dataclass(frozen=True)
class VmAuditEntry:
    """The O7 fields the Mac matches on, plus the rest as the VM wrote them."""

    seq: int
    entry_sha256: str
    intent_id: str | None
    intent_sha256: str | None
    decision: str
    codes: tuple[str, ...]
    raw: Mapping[str, Any]


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _fail(reason: str, rid: str | None = None) -> OrdersProtocolError:
    return OrdersProtocolError(reason=reason, request_id=rid)


class OrdersClient:
    """The five O2 routes. The only way the Mac reaches ``/v1/orders/*``."""

    CONTRACT = ORDERS_CONTRACT

    def __init__(self, relay: RelayClient) -> None:
        self._relay = relay

    def _call(
        self,
        method: str,
        path: str,
        *,
        body: Mapping[str, Any] | None = None,
        params: list[tuple[str, str]] | None = None,
    ) -> tuple[dict, str]:
        return self._relay._json(  # noqa: SLF001 - same package, same transport
            method, path, auth="session", body=body, params=params, contract=ORDERS_CONTRACT
        )

    def mint(self, intent: Mapping[str, Any]) -> tuple[str, bytes]:
        """O2 ``POST /v1/orders/intents``. Returns ``(challenge_id, signed_bytes)`` unchecked.

        The caller must pass both through ``check_minted_bytes`` before anything is stored.
        """

        data, rid = self._call("POST", ROUTE_INTENTS, body=intent)
        challenge_id = data.get("challenge_id")
        encoded = data.get("signed_bytes_b64")
        if (
            set(data) != {"contract", "challenge_id", "signed_bytes_b64", "expires_at_utc"}
            or not isinstance(challenge_id, str)
            or not isinstance(encoded, str)
            or not isinstance(data.get("expires_at_utc"), str)
        ):
            raise _fail("bad_field", rid)
        try:
            signed = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError):
            raise _fail("bad_field", rid) from None
        return challenge_id, signed

    def authorize(self, challenge_id: str, signature_der: bytes) -> AuthorizeResult:
        """O2 ``POST /v1/orders/authorize``: the same signature the Mac verified locally.

        One request, never repeated by this client. A timeout after the request left is the
        caller's UNKNOWN.
        """

        data, rid = self._call(
            "POST",
            ROUTE_AUTHORIZE,
            body={
                "challenge_id": challenge_id,
                "signature_der_b64": base64.b64encode(bytes(signature_der)).decode("ascii"),
            },
        )
        decision = data.get("decision")
        intent_id = data.get("intent_id")
        seq = data.get("audit_seq")
        digest = data.get("audit_sha256")
        if (
            set(data) != {"contract", "decision", "intent_id", "audit_seq", "audit_sha256"}
            or not isinstance(decision, str)
            or not isinstance(intent_id, str)
            or not _is_int(seq)
            or not isinstance(digest, str)
            or _HEX64.fullmatch(digest) is None
        ):
            raise _fail("bad_field", rid)
        return AuthorizeResult(decision, intent_id, seq, digest)

    def halt(self) -> None:
        """O2 ``POST /v1/orders/halt``: the Mac may set ``mac_halt`` and never clear it."""

        data, rid = self._call("POST", ROUTE_HALT, body={})
        if data.get("mac_halt") is not True or set(data) != {"contract", "mac_halt"}:
            raise _fail("bad_field", rid)

    def state(self) -> OrdersState:
        """O2 ``GET /v1/orders/state``."""

        data, rid = self._call("GET", ROUTE_STATE)
        latches = data.get("latches")
        alerts = data.get("alerts_logged")
        if (
            not isinstance(data.get("kill"), str)
            or not isinstance(latches, list)
            or not all(isinstance(item, str) for item in latches)
            or not isinstance(data.get("drawdown"), str)
            or not _is_int(alerts)
            or data.get("peak_date") is not None and not isinstance(data.get("peak_date"), str)
            or data.get("last_evaluated_session") is not None
            and not isinstance(data.get("last_evaluated_session"), str)
        ):
            raise _fail("bad_field", rid)
        return OrdersState(
            kill=data["kill"],
            latches=tuple(latches),
            drawdown=data["drawdown"],
            peak_date=data.get("peak_date"),
            last_evaluated_session=data.get("last_evaluated_session"),
            alerts_logged=alerts,
        )

    def audit(self, after_seq: int = 0) -> tuple[VmAuditEntry, ...]:
        """O2 ``GET /v1/orders/audit?after_seq=N``: at most 200 entries."""

        if not _is_int(after_seq) or not 0 <= after_seq < _AFTER_SEQ_MAX:
            raise ValueError("after_seq")
        data, rid = self._call(
            "GET", ROUTE_AUDIT, params=[("after_seq", str(after_seq))] if after_seq else None
        )
        entries = data.get("entries")
        if not isinstance(entries, list) or len(entries) > 200:
            raise _fail("bad_field", rid)
        out: list[VmAuditEntry] = []
        for entry in entries:
            if not isinstance(entry, dict) or not _is_int(entry.get("seq")):
                raise _fail("bad_field", rid)
            codes = entry.get("codes")
            decision = entry.get("decision")
            if not isinstance(decision, str) or not isinstance(codes, list):
                raise _fail("bad_field", rid)
            out.append(
                VmAuditEntry(
                    seq=entry["seq"],
                    entry_sha256=str(entry.get("entry_sha256")),
                    intent_id=entry.get("intent_id"),
                    intent_sha256=entry.get("intent_sha256"),
                    decision=decision,
                    codes=tuple(str(code) for code in codes),
                    raw=dict(entry),
                )
            )
        return tuple(out)

    def audit_all(self) -> tuple[VmAuditEntry, ...]:
        """Every VM audit entry, page by page."""

        after = 0
        out: list[VmAuditEntry] = []
        while True:
            page = self.audit(after)
            out.extend(page)
            if len(page) < 200:
                return tuple(out)
            after = page[-1].seq


# --------------------------------------------------------------- the link


class RelayLink:
    """A lazily opened ``RelayClient`` shared by the dispatcher, the minter and the quote source.

    Building the production link opens nothing: no tunnel, no process, no network. The first
    ``client()`` call opens it. A test hands in an open client with ``RelayLink.of``.
    """

    def __init__(self, opener: Callable[[], RelayClient]) -> None:
        self._opener = opener
        self._client: RelayClient | None = None
        self._lock = threading.Lock()

    @classmethod
    def of(cls, client: RelayClient) -> "RelayLink":
        link = cls(lambda: client)
        link._client = client
        return link

    @classmethod
    def production(cls) -> "RelayLink":
        return cls(RelayClient.open)

    def client(self) -> RelayClient:
        with self._lock:
            if self._client is None:
                self._client = self._opener()
            return self._client

    def orders(self) -> OrdersClient:
        return OrdersClient(self.client())

    def close(self) -> None:
        with self._lock:
            client, self._client = self._client, None
        if client is not None:
            client.close()


def match_vm_audit(
    mac_events: Sequence[Mapping[str, Any]], vm_entries: Sequence[VmAuditEntry]
) -> dict[str, list[Any]]:
    """Match the Mac's relay events with the VM's audit by ``(intent_sha256, decision)``.

    Each Mac event is a mapping with ``intent_sha256`` and ``decision`` (``CHALLENGED`` after a
    mint, then ``VERIFIED_NOT_FORWARDED`` or ``REFUSED`` after an authorize). ``matched`` holds
    the pairs found; ``mac_only`` and ``vm_only`` are the entries with no partner. A Mac event
    the VM never recorded (or the reverse) is a reconciliation anomaly for the operator.
    """

    remaining = list(vm_entries)
    matched: list[tuple[Mapping[str, Any], VmAuditEntry]] = []
    mac_only: list[Mapping[str, Any]] = []
    for event in mac_events:
        for index, entry in enumerate(remaining):
            if (
                entry.intent_sha256 == event.get("intent_sha256")
                and entry.decision == event.get("decision")
            ):
                matched.append((event, remaining.pop(index)))
                break
        else:
            mac_only.append(event)
    return {"matched": list(matched), "mac_only": mac_only, "vm_only": remaining}


__all__ = [
    "BROKER",
    "DECISION_NOT_FORWARDED",
    "INTENT_FIELDS",
    "MODE",
    "ORDER_PURPOSE",
    "REASONS",
    "AuthorizeResult",
    "MintMismatch",
    "MintedChallenge",
    "OrdersClient",
    "OrdersProtocolError",
    "OrdersState",
    "RelayLink",
    "VmAuditEntry",
    "canonical_bytes",
    "check_minted_bytes",
    "intent_body",
    "intent_sha256",
    "match_vm_audit",
]
