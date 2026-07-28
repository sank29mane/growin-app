"""Pure, local-paper adaptive limit candidate policy.

This module is deliberately not a broker adapter.  It receives an immutable
local evidence snapshot and produces a Decimal limit candidate.  Persisting,
approval, dispatch, cancellation, and replacement are separate boundaries.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Mapping

from .ledger import ExecutionLedger, LedgerRequote, canonical_json
from .models import OrderIntent, OrderMode, OrderSide, OrderType, ReconciliationStatus


class RequoteValidationError(ValueError):
    """Raised when local evidence cannot safely produce a candidate."""


@dataclass(frozen=True)
class LocalPaperVenue:
    """The only Phase 52 capability declaration accepted by the evaluator."""

    mode: str = "LOCAL_PAPER"
    external_network: bool = False
    supports_limit: bool = True
    supports_replace: bool = False
    supports_cancel: bool = False

    def __post_init__(self) -> None:
        if self.mode != "LOCAL_PAPER":
            raise RequoteValidationError("only local-paper capability is supported")
        if self.external_network:
            raise RequoteValidationError("local-paper venue must not have network access")
        if not self.supports_limit:
            raise RequoteValidationError("local-paper venue must support limit candidates")
        if self.supports_replace or self.supports_cancel:
            raise RequoteValidationError("local-paper venue cannot mutate orders")


@dataclass(frozen=True)
class QuoteEvidence:
    """Validated local inputs for one deterministic policy evaluation."""

    bid: Decimal
    ask: Decimal
    volatility: Decimal
    cost: Decimal
    tick_size: Decimal
    regime_id: int
    observed_at: datetime
    source: str

    def __post_init__(self) -> None:
        for field_name in ("bid", "ask", "volatility", "cost", "tick_size"):
            value = _finite_decimal(getattr(self, field_name), field_name.replace("_", " "))
            object.__setattr__(self, field_name, value)
        if self.bid <= 0:
            raise RequoteValidationError("bid must be positive")
        if self.ask <= 0 or self.ask < self.bid:
            raise RequoteValidationError("ask must be positive and not below bid")
        if self.volatility < 0:
            raise RequoteValidationError("volatility must not be negative")
        if self.cost < 0:
            raise RequoteValidationError("cost must not be negative")
        if self.tick_size <= 0:
            raise RequoteValidationError("tick size must be positive")
        if not isinstance(self.regime_id, int):
            raise RequoteValidationError("regime id must be an integer")
        if self.observed_at.tzinfo is None:
            raise RequoteValidationError("evidence timestamp must be timezone-aware")
        if not self.source.strip():
            raise RequoteValidationError("evidence source is required")


@dataclass(frozen=True)
class RequotePolicy:
    """Versioned pure policy inputs supplied by the caller."""

    regime_multipliers: Mapping[int, Decimal] = field(
        default_factory=lambda: {0: Decimal("1"), 1: Decimal("1.5"), 2: Decimal("2")}
    )
    max_age_seconds: int = 30
    version: str = "local-paper-v1"

    def __post_init__(self) -> None:
        if self.max_age_seconds < 0:
            raise RequoteValidationError("maximum evidence age must not be negative")
        if not self.version.strip():
            raise RequoteValidationError("policy version is required")
        normalised: dict[int, Decimal] = {}
        for regime_id, multiplier in self.regime_multipliers.items():
            if not isinstance(regime_id, int):
                raise RequoteValidationError("regime ids must be integers")
            decimal = _finite_decimal(multiplier, "regime multiplier")
            if decimal <= 0:
                raise RequoteValidationError("regime multiplier must be positive")
            normalised[regime_id] = decimal
        object.__setattr__(self, "regime_multipliers", normalised)


@dataclass(frozen=True)
class RequoteCandidate:
    """An immutable, non-executable local limit recommendation."""

    side: OrderSide
    limit_price: Decimal
    lower_bound: Decimal
    upper_bound: Decimal
    policy_version: str
    snapshot_hash: str
    expires_at: datetime


@dataclass(frozen=True)
class RequoteEvaluation:
    """One persisted candidate and the pure policy result that produced it."""

    candidate: RequoteCandidate
    record: LedgerRequote


@dataclass(frozen=True)
class ReplacementPreparation:
    """A fresh pending LIMIT intent that still needs the normal approval flow."""

    record: LedgerRequote
    intent: OrderIntent


def evaluate_requote(
    *,
    side: OrderSide,
    evidence: QuoteEvidence,
    policy: RequotePolicy,
    venue: LocalPaperVenue,
    now: datetime | None = None,
) -> RequoteCandidate:
    """Return a bounded, tick-aligned local BUY limit candidate or reject."""

    if not isinstance(venue, LocalPaperVenue):
        raise RequoteValidationError("only local-paper capability is supported")
    if side is not OrderSide.BUY:
        raise RequoteValidationError("only BUY candidates are supported")
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise RequoteValidationError("evaluation timestamp must be timezone-aware")
    age_seconds = (current - evidence.observed_at).total_seconds()
    if age_seconds < 0 or age_seconds > policy.max_age_seconds:
        raise RequoteValidationError("evidence timestamp is stale or future-dated")
    try:
        multiplier = policy.regime_multipliers[evidence.regime_id]
    except KeyError as exc:
        raise RequoteValidationError("regime is unsupported") from exc

    midpoint = (evidence.bid + evidence.ask) / Decimal("2")
    collar = (evidence.volatility + evidence.cost) * multiplier
    lower_bound = evidence.bid
    upper_bound = evidence.ask
    raw_price = min(upper_bound, max(lower_bound, midpoint + collar))
    limit_price = _round_down_to_tick(raw_price, evidence.tick_size)
    if limit_price < lower_bound:
        limit_price = _round_up_to_tick(lower_bound, evidence.tick_size)
    if limit_price > upper_bound:
        raise RequoteValidationError("candidate is outside the permitted collar")

    snapshot_hash = hashlib.sha256(
        canonical_json(
            {
                "side": side.value,
                "bid": str(evidence.bid),
                "ask": str(evidence.ask),
                "volatility": str(evidence.volatility),
                "cost": str(evidence.cost),
                "tick_size": str(evidence.tick_size),
                "regime_id": evidence.regime_id,
                "observed_at": evidence.observed_at.isoformat(),
                "source": evidence.source,
                "policy_version": policy.version,
                "regime_multiplier": str(multiplier),
                "limit_price": str(limit_price),
            }
        ).encode("utf-8")
    ).hexdigest()
    return RequoteCandidate(
        side=side,
        limit_price=limit_price,
        lower_bound=lower_bound,
        upper_bound=upper_bound,
        policy_version=policy.version,
        snapshot_hash=snapshot_hash,
        expires_at=evidence.observed_at + timedelta(seconds=policy.max_age_seconds),
    )


class RequoteCoordinator:
    """Persist a local candidate and stop at the no-mutation capability gate."""

    def __init__(self, ledger: ExecutionLedger) -> None:
        self._ledger = ledger

    def evaluate_and_record(
        self,
        *,
        proposal_id: str,
        side: OrderSide,
        evidence: QuoteEvidence,
        policy: RequotePolicy,
        venue: LocalPaperVenue,
        now: datetime | None = None,
    ) -> RequoteEvaluation:
        """Evaluate a parent and durably block the non-executable candidate.

        The ledger validates parent identity, acknowledgement, reservation,
        workspace control, and state.  This coordinator intentionally does not
        receive a dispatcher or any external client.
        """

        parent = self._ledger.get_order(proposal_id)
        if parent is None:
            raise RequoteValidationError("parent order was not found")
        if parent.acknowledgment is None:
            raise RequoteValidationError("parent acknowledgement is required")
        candidate = evaluate_requote(
            side=side,
            evidence=evidence,
            policy=policy,
            venue=venue,
            now=now,
        )
        idempotency_key = f"{proposal_id}:{candidate.snapshot_hash}"
        requote_id = "rq-" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]
        record = self._ledger.record_requote_intent(
            requote_id=requote_id,
            proposal_id=proposal_id,
            parent_intent_hash=parent.intent_hash,
            parent_reconciliation_fingerprint=(
                f"ack:{parent.acknowledgment.broker_order_id}"
            ),
            idempotency_key=idempotency_key,
            snapshot_hash=candidate.snapshot_hash,
            candidate={
                "side": candidate.side.value,
                "limit_price": str(candidate.limit_price),
                "lower_bound": str(candidate.lower_bound),
                "upper_bound": str(candidate.upper_bound),
                "policy_version": candidate.policy_version,
                "expires_at": candidate.expires_at.isoformat(),
                "evidence": {
                    "bid": str(evidence.bid), "ask": str(evidence.ask),
                    "volatility": str(evidence.volatility), "cost": str(evidence.cost),
                    "tick_size": str(evidence.tick_size), "regime_id": evidence.regime_id,
                    "observed_at": evidence.observed_at.isoformat(), "source": evidence.source,
                },
            },
        )
        if record.state == "EVALUATED":
            record = self._ledger.block_requote(record.requote_id, "NO_MUTATION_CAPABILITY")
        return RequoteEvaluation(candidate=candidate, record=record)

    def prepare_replacement(
        self, *, requote_id: str, replacement_proposal_id: str, now: datetime | None = None
    ) -> ReplacementPreparation:
        """Create a fresh pending PAPER LIMIT intent after reconciled cancellation.

        This does not cancel, dispatch, reserve, or approve anything.  It
        merely makes the replacement eligible for the existing fresh
        admission → reservation → signature path.
        """

        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise RequoteValidationError("preparation timestamp must be timezone-aware")
        record = self._ledger.get_requote(requote_id)
        if record is None:
            raise RequoteValidationError("re-quote candidate was not found")
        if record.state == "REPLACEMENT_PREPARED":
            if record.replacement_proposal_id != replacement_proposal_id:
                raise RequoteValidationError("re-quote already prepared another replacement")
            order = self._ledger.get_order(replacement_proposal_id)
            if order is None:
                raise RequoteValidationError("prepared replacement was not found")
            return ReplacementPreparation(record=record, intent=OrderIntent.model_validate(order.intent))
        if record.state != "BLOCKED_NO_MUTATION_CAPABILITY":
            raise RequoteValidationError("re-quote is not eligible for replacement preparation")
        candidate = dict(record.candidate)
        try:
            expires_at = datetime.fromisoformat(str(candidate["expires_at"]))
            limit_price = _finite_decimal(candidate["limit_price"], "candidate limit price")
            evidence = candidate["evidence"]
        except (KeyError, TypeError, ValueError) as exc:
            raise RequoteValidationError("candidate lacks complete immutable evidence") from exc
        if expires_at.tzinfo is None or current >= expires_at:
            raise RequoteValidationError("candidate evidence is expired")
        if not isinstance(evidence, Mapping) or str(evidence.get("source", "")).strip() == "":
            raise RequoteValidationError("candidate evidence is malformed")
        parent = self._ledger.get_order(record.proposal_id)
        latest = self._ledger.get_latest_reconciliation(record.proposal_id)
        if parent is None or parent.state != "CANCELLED":
            raise RequoteValidationError("parent must be reconciled as CANCELLED")
        if latest is None or latest.status is not ReconciliationStatus.CANCELLED:
            raise RequoteValidationError("cancelled reconciliation evidence is required")
        if parent.acknowledgment is None or latest.broker_order_id != parent.acknowledgment.broker_order_id:
            raise RequoteValidationError("cancelled evidence does not match parent acknowledgement")
        remaining = _finite_decimal(parent.intent["quantity"], "parent quantity") - latest.cumulative_quantity
        if remaining <= 0:
            raise RequoteValidationError("parent has no remaining quantity to replace")
        intent = OrderIntent(
            proposal_id=replacement_proposal_id,
            client_order_id=f"growin-{replacement_proposal_id}",
            intent_version=int(parent.intent.get("intent_version", 1)) + 1,
            workspace=str(parent.intent["workspace"]), account=str(parent.intent["account"]),
            broker="paper", mode=OrderMode.PAPER, ticker=str(parent.intent["ticker"]),
            side=OrderSide.BUY, quantity=remaining, order_type=OrderType.LIMIT,
            limit_price=limit_price, replaces_proposal_id=record.proposal_id, requote_id=requote_id,
        )
        self._ledger.register_intent(intent)
        record = self._ledger.mark_requote_replacement_prepared(requote_id, replacement_proposal_id)
        return ReplacementPreparation(record=record, intent=intent)


def _finite_decimal(value: object, name: str) -> Decimal:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise RequoteValidationError(f"{name} must be a finite Decimal") from exc
    if not decimal.is_finite() or math.isnan(float(decimal)) or math.isinf(float(decimal)):
        raise RequoteValidationError(f"{name} must be a finite Decimal")
    return decimal


def _round_down_to_tick(value: Decimal, tick_size: Decimal) -> Decimal:
    return (value / tick_size).to_integral_value(rounding=ROUND_FLOOR) * tick_size


def _round_up_to_tick(value: Decimal, tick_size: Decimal) -> Decimal:
    rounded = _round_down_to_tick(value, tick_size)
    return rounded if rounded == value else rounded + tick_size
