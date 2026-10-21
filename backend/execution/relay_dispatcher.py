"""The India SHADOW relay: mint a VM challenge, then ask the VM to verify the signed order (63-05).

Nothing here places, modifies or cancels an order. The VM's forward step is a constant refusal
(``VERIFIED_NOT_FORWARDED``), so the best outcome of ``dispatch_signed`` is a verified order that
was not placed. ``RelayDispatcher`` has no method that could send anything else: no cancel, no
clear, no direct dispatch. Its plain ``dispatch`` always refuses, because an unsigned order is
never sent.

Outcomes (``RelayOutcome.kind``):
  NOT_FORWARDED  the VM verified the signature and refused to forward: a definite non-placement.
  REFUSED        the VM answered with a refusal (an O6 code): a definite non-placement.
  UNKNOWN        the answer is missing or untrusted after the request may have left. Never retried.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Optional

from brokers.breeze_gateway.errors import (
    EgressMismatch,
    BudgetExhausted,
    BudgetUnavailable,
    ContractMismatch,
    ForbiddenClient,
    IdentityMismatch,
    MethodNotAllowed,
    NoSession,
    NotFound,
    OrderError,
    RelayError,
    RelayUnavailable,
    RelayValidationError,
    SessionExpired,
    Unauthorized,
)
from brokers.breeze_gateway.india_quote import QuoteUnavailable, RelayQuoteSource
from brokers.breeze_gateway.orders import (
    DECISION_NOT_FORWARDED,
    MintMismatch,
    RelayLink,
    check_minted_bytes,
    intent_body,
    intent_sha256,
)

from .approval import RelayChallengeRefused, RelayMint
from .models import OrderAck, OrderIntent

OUTCOME_NOT_FORWARDED = "NOT_FORWARDED"
OUTCOME_REFUSED = "REFUSED"
OUTCOME_UNKNOWN = "UNKNOWN"
RELAY_FORWARD_DISABLED = "RELAY_FORWARD_DISABLED"

_REASON_OK = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_TICKER_PREFIX = "NSE:CASH:"
# Server answers that are a refusal and mean the VM did nothing with the order.
_DEFINITE = (
    OrderError,
    Unauthorized,
    NoSession,
    SessionExpired,
    EgressMismatch,
    BudgetUnavailable,
    BudgetExhausted,
    RelayValidationError,
    ForbiddenClient,
    IdentityMismatch,
    ContractMismatch,
    NotFound,
    MethodNotAllowed,
)
# RelayUnavailable reasons the client raises only while it is still getting a tunnel, so the request
# provably never left the process. "closed" and "tunnel_restarted" are NOT here: the client raises
# them both before a send and from the RuntimeError httpx throws after one, so they prove nothing.
# Anything not listed is UNKNOWN and never retried (the #585 pattern).
_NOT_SENT = frozenset({"tunnel_down", "tunnel_restart_backoff", "tunnel_restart_failed"})


def reason_code(text: object, default: str) -> str:
    """A short closed code for the ledger: letters, digits, ``_``, ``.`` and ``-`` only."""

    if not isinstance(text, str):
        return default
    cleaned = text.replace(":", ".")
    return cleaned if _REASON_OK.fullmatch(cleaned) else default


@dataclass(frozen=True)
class RelayOutcome:
    kind: str
    reason_code: str
    decision: str
    intent_sha256: Optional[str] = None
    audit_seq: Optional[int] = None
    request_id: Optional[str] = None

    @property
    def placed(self) -> bool:
        """Never true: the VM forwards nothing in this phase."""

        return False


class RelayDispatcher:
    """Sends one signed SHADOW order to ``/v1/orders/authorize``. Once, never retried."""

    # Execution is ready once the process can reach the relay when it needs to. The link opens
    # lazily, so a process that never approves a SHADOW order never opens a tunnel.
    execution_ready = True

    def __init__(self, link: RelayLink) -> None:
        self._link = link

    @property
    def link(self) -> RelayLink:
        return self._link

    def halt(self) -> bool:
        """Set the VM's ``mac_halt`` (O2 ``POST /v1/orders/halt``). The Mac has no clear call."""

        self._link.orders().halt()
        return True

    async def dispatch(self, intent: OrderIntent) -> OrderAck:
        """An unsigned order is never sent. The signed path is ``dispatch_signed``."""

        raise _NotSigned()

    async def dispatch_signed(
        self,
        intent: OrderIntent,
        *,
        challenge_id: str,
        signature_der: bytes,
        signed_payload: bytes,
    ) -> RelayOutcome:
        sha = _embedded_intent_sha256(signed_payload)
        try:
            orders = await asyncio.to_thread(self._link.orders)
        except Exception:  # noqa: BLE001 - nothing was sent: the tunnel or config would not open
            return RelayOutcome(OUTCOME_REFUSED, "RELAY_UNAVAILABLE", "REFUSED", sha)
        try:
            result = await asyncio.to_thread(orders.authorize, challenge_id, signature_der)
        except RelayUnavailable as exc:
            if exc.reason in _NOT_SENT:
                return RelayOutcome(OUTCOME_REFUSED, "RELAY_UNAVAILABLE", "REFUSED", sha)
            return RelayOutcome(
                OUTCOME_UNKNOWN, "BROKER_TIMEOUT", "UNKNOWN", sha, request_id=exc.request_id
            )
        except _DEFINITE as exc:
            code = reason_code(exc.reason, reason_code(exc.code.lower(), "RELAY_REFUSED"))
            return RelayOutcome(
                OUTCOME_REFUSED, code, "REFUSED", sha, request_id=exc.request_id
            )
        except RelayError as exc:
            # A 5xx, a protocol break, a hash or contract mismatch: the VM may have acted.
            return RelayOutcome(
                OUTCOME_UNKNOWN, "RELAY_UNTRUSTED", "UNKNOWN", sha, request_id=exc.request_id
            )
        except Exception:  # noqa: BLE001 - the request may have left; never guess, never retry
            return RelayOutcome(OUTCOME_UNKNOWN, "RELAY_UNTRUSTED", "UNKNOWN", sha)
        if result.decision != DECISION_NOT_FORWARDED:
            return RelayOutcome(
                OUTCOME_UNKNOWN,
                "RELAY_UNEXPECTED_DECISION",
                "UNKNOWN",
                sha,
                audit_seq=result.audit_seq,
            )
        return RelayOutcome(
            OUTCOME_NOT_FORWARDED,
            RELAY_FORWARD_DISABLED,
            DECISION_NOT_FORWARDED,
            sha,
            audit_seq=result.audit_seq,
        )


class _NotSigned(RuntimeError):
    """Raised by the plain ``dispatch``: the relay takes signed orders only."""

    reason_code = "SIGNED_DISPATCH_REQUIRED"

    def __str__(self) -> str:
        return "the relay venue takes signed orders only"


def _embedded_intent_sha256(signed_payload: bytes) -> Optional[str]:
    try:
        payload = json.loads(bytes(signed_payload))
        return intent_sha256(payload["intent"])
    except (ValueError, KeyError, TypeError, RecursionError):
        return None


class OrderRelayMinter:
    """Builds the O3 intent for one admitted SHADOW order and has the VM mint its challenge."""

    def __init__(
        self,
        link: RelayLink,
        source: RelayQuoteSource,
        *,
        ledger: Any,
        limits_sha256: str,
        params_sha256: str,
    ) -> None:
        self._link = link
        self._source = source
        self._ledger = ledger
        self._limits_sha256 = limits_sha256
        self._params_sha256 = params_sha256

    @staticmethod
    def intent_id_for(proposal_id: str) -> str:
        """One stable intent id per proposal, so a lapsed challenge can be minted again."""

        return "i" + hashlib.sha256(proposal_id.encode("utf-8")).hexdigest()[:40]

    def build_intent(self, *, order: Any, admission: Any, key_id: str) -> dict[str, Any]:
        stored: Mapping[str, Any] = order.intent
        ticker = str(stored.get("ticker", ""))
        if not ticker.startswith(_TICKER_PREFIX) or stored.get("mode") != "SHADOW":
            raise RelayChallengeRefused("instrument_unsupported")
        stock_code = ticker[len(_TICKER_PREFIX) :]
        side = str(stored.get("side", "")).lower()
        try:
            isin = self._source.isin(stock_code)
        except QuoteUnavailable as exc:
            raise RelayChallengeRefused(reason_code(exc.reason, "isin_unbound")) from None
        except Exception:  # noqa: BLE001 - the relay read failed: refuse, never guess an ISIN
            raise RelayChallengeRefused("relay_unavailable") from None
        batch = self._ledger.get_exit_batch(order.proposal_id) if side == "sell" else None
        reason = "entry" if side == "buy" else (batch["reason"] if batch else "exit")
        batch_id = batch["batch_id"] if batch else None
        try:
            limit = Decimal(str(stored.get("limit_price")))
            quantized = limit.quantize(Decimal("0.01"))
            if limit != quantized:
                raise MintMismatch("limit_price")
            return intent_body(
                intent_id=self.intent_id_for(order.proposal_id),
                proposal_id=order.proposal_id,
                side=side,
                stock_code=stock_code,
                isin=isin,
                quantity=int(admission.final_quantity),
                limit_price=format(quantized, "f"),
                reason=reason,
                batch_id=batch_id,
                limits_sha256=self._limits_sha256,
                params_sha256=self._params_sha256,
                key_id=key_id,
            )
        except (MintMismatch, ArithmeticError, ValueError, TypeError) as exc:
            reason_word = exc.reason if isinstance(exc, MintMismatch) else "intent_invalid"
            raise RelayChallengeRefused(reason_code("mint_" + reason_word, "mint_mismatch")) from None

    def mint(self, *, order: Any, admission: Any, key_id: str) -> RelayMint:
        body = self.build_intent(order=order, admission=admission, key_id=key_id)
        sha = intent_sha256(body)
        try:
            orders = self._link.orders()
            challenge_id, signed = orders.mint(body)
        except OrderError as exc:
            raise RelayChallengeRefused(
                reason_code(exc.reason, reason_code(exc.code.lower(), "relay_refused")), sha
            ) from None
        except RelayError as exc:
            raise RelayChallengeRefused(reason_code(exc.code.lower(), "relay_unavailable"), sha) from None
        except Exception:  # noqa: BLE001 - tunnel or config failure: refuse, mint nothing
            raise RelayChallengeRefused("relay_unavailable", sha) from None
        try:
            checked = check_minted_bytes(
                signed,
                challenge_id=challenge_id,
                expected_intent=body,
                limits_sha256=self._limits_sha256,
                key_id=key_id,
            )
        except MintMismatch as mismatch:
            raise RelayChallengeRefused(
                reason_code("mint_" + mismatch.reason, "mint_mismatch"), sha
            ) from None
        return RelayMint(
            challenge_id=checked.challenge_id,
            signed_payload=checked.signed_bytes,
            issued_at_epoch=checked.issued_at_epoch,
            expires_at_epoch=checked.expires_at_epoch,
            intent_sha256=sha,
            intent=dict(body),
        )


__all__ = [
    "OUTCOME_NOT_FORWARDED",
    "OUTCOME_REFUSED",
    "OUTCOME_UNKNOWN",
    "OrderRelayMinter",
    "RELAY_FORWARD_DISABLED",
    "RelayDispatcher",
    "RelayOutcome",
    "reason_code",
]
