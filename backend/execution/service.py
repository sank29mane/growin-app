"""Fail-closed execution service backed by an optional durable ledger."""

import asyncio
import hashlib
import math
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Mapping, Optional, Protocol, Union

from .approval import ApprovalChallenge, ApprovalService
from .ledger import (
    ApprovalConflict,
    ClaimResult,
    ClaimStatus,
    ExecutionLedger,
    IntentConflict,
    InvalidTransition,
    OrderNotFound,
    canonical_json,
    intent_hash,
)
from .models import (
    AdmissionDecision,
    ExecutionAdmission,
    OrderAck,
    OrderIntent,
    OrderMode,
    OrderSide,
    OrderState,
    ReconciliationSnapshot,
)


class ExecutionDisabledError(RuntimeError):
    """Raised when no authorized dispatcher is installed."""


class ExecutionConflictError(RuntimeError):
    """Raised when a proposal is not eligible for approval or rejection."""


class BrokerExecutionError(RuntimeError):
    """Raised for a definite broker rejection or invalid acknowledgement."""


class BrokerOutcomeUnknownError(RuntimeError):
    """Raised when submission may have reached the broker but is unconfirmed."""


class ExecutionDispatcher(Protocol):
    async def dispatch(self, intent: OrderIntent) -> OrderAck: ...


Proposal = Union[str, Dict[str, Any]]


class ExecutionService:
    """Own validation, durable claims, dispatch, and terminal transitions."""

    def __init__(
        self,
        dispatcher: Optional[ExecutionDispatcher] = None,
        ledger: Optional[ExecutionLedger] = None,
        *,
        require_approval: bool = False,
        approval_service: Optional[ApprovalService] = None,
        simulator: Any = None,
        risk_gate: Any = None,
        require_runtime_preflight: bool = False,
    ):
        self._dispatcher = dispatcher
        self._ledger = ledger
        self._require_approval = require_approval
        self._approval_service = approval_service
        self._simulator = simulator
        self._risk_gate = risk_gate
        self._require_runtime_preflight = require_runtime_preflight
        if require_approval:
            if ledger is not None:
                ledger.require_approval = True
            if approval_service is None and ledger is not None:
                self._approval_service = ApprovalService(ledger)
        # Local locks improve same-process replay UX. SQLite transactions and
        # constraints remain the authority across services and restarts.
        self._proposal_locks: Dict[str, asyncio.Lock] = {}

    @property
    def execution_enabled(self) -> bool:
        return self._dispatcher is not None

    @property
    def durable(self) -> bool:
        return self._ledger is not None

    def _lock_for(self, proposal_id: str) -> asyncio.Lock:
        lock = self._proposal_locks.get(proposal_id)
        if lock is None:
            lock = asyncio.Lock()
            self._proposal_locks[proposal_id] = lock
        return lock

    def register_proposal(self, proposal: Dict[str, Any]) -> OrderIntent:
        """Validate and durably register the immutable executable fields."""

        intent = _intent_from_proposal(proposal)
        if self._ledger is not None:
            try:
                order = self._ledger.register_intent(intent)
            except IntentConflict as exc:
                raise ExecutionConflictError(str(exc)) from exc
            _sync_projection(proposal, order.state, order.acknowledgment)
        return intent

    def admit(
        self,
        proposal: Proposal,
        *,
        currency: str = "GBP",
        price: object = None,
        simulator_evidence: Optional[Mapping[str, Any]] = None,
        risk_evidence: Optional[Mapping[str, Any]] = None,
        evidence_at: Optional[datetime] = None,
        max_age_seconds: int = 30,
        simulator: Any = None,
        risk_gate: Any = None,
        tick_window: Optional[Dict[str, Any]] = None,
        portfolio_state: Optional[Dict[str, Any]] = None,
        regime_id: Optional[int] = None,
        current_spread_pct: object = None,
        risk_db_connection: Any = None,
        deny_reason: Optional[str] = None,
    ) -> ExecutionAdmission:
        """Run deterministic simulation/risk checks and persist immutable evidence."""

        if self._ledger is None:
            raise ExecutionDisabledError("durable execution admission is unavailable")
        intent = proposal if isinstance(proposal, OrderIntent) else _intent_from_proposal(proposal)
        self._ledger.register_intent(intent)
        if self._ledger.get_workspace_control().engaged:
            raise ExecutionConflictError("workspace execution control is engaged")
        now = datetime.now(timezone.utc)
        observed_at = evidence_at or now
        simulator_evidence = dict(simulator_evidence or {})
        risk_evidence = dict(risk_evidence or {})
        reason = "ADMISSION_UNAVAILABLE"
        decision = AdmissionDecision.DENIED
        simulator_fill = Decimal("0")
        drawdown = Decimal("0")
        risk_quantity = Decimal("0")
        price_decimal = Decimal("0")
        spread_decimal = Decimal("0")
        try:
            if deny_reason:
                raise ValueError(deny_reason)
            if observed_at.tzinfo is None:
                raise ValueError("admission evidence timestamp must be timezone-aware")
            if (now - observed_at).total_seconds() > max_age_seconds or observed_at > now:
                raise ValueError("admission evidence is stale")
            if intent.side is not OrderSide.BUY:
                raise ValueError("SELL admission requires a position reservation")
            selected_simulator = simulator or self._simulator
            selected_gate = risk_gate or self._risk_gate
            if self._require_runtime_preflight:
                if selected_simulator is None or selected_gate is None:
                    raise ValueError("runtime preflight controls are unavailable")
                if tick_window is None or portfolio_state is None or risk_db_connection is None:
                    raise ValueError("runtime preflight context is required")
                if not isinstance(regime_id, int) or isinstance(regime_id, bool):
                    raise ValueError("GMM regime id is required")
                if current_spread_pct is None:
                    raise ValueError("current spread is required")
            if selected_simulator is not None:
                simulator_evidence = dict(
                    selected_simulator.simulate_execution(
                        intent.side.value,
                        float(intent.quantity),
                        tick_window or {},
                        portfolio_state or {},
                    )
                )
            if selected_gate is not None:
                risk_output = selected_gate.evaluate(
                    float(simulator_evidence.get("simulated_fill_price", 0)),
                    float(intent.quantity),
                    0 if regime_id is None else regime_id,
                    0 if current_spread_pct is None else float(current_spread_pct),
                    risk_db_connection,
                )
                risk_evidence = {**risk_evidence, "scaled_size": risk_output}
            if not simulator_evidence or not risk_evidence:
                raise ValueError("simulator and risk evidence are required")
            simulator_fill = _finite_decimal(
                simulator_evidence.get("simulated_fill_price"), "simulator fill price"
            )
            drawdown = _finite_decimal(
                simulator_evidence.get("simulator_drawdown_pct", 0), "simulator drawdown"
            )
            spread_decimal = _finite_decimal(
                0 if current_spread_pct is None else current_spread_pct, "spread"
            )
            risk_value = risk_evidence.get("admitted_quantity", risk_evidence.get("scaled_size"))
            risk_quantity = _finite_decimal(risk_value, "risk quantity")
            price_decimal = _finite_decimal(
                price if price is not None else simulator_fill, "price"
            )
            if simulator_fill <= 0 or price_decimal <= 0 or risk_quantity <= 0:
                raise ValueError("admission quantities and price must be positive")
            if risk_quantity > intent.quantity:
                raise ValueError("risk gate cannot increase quantity")
            if risk_evidence.get("allowed") is False:
                raise ValueError("risk gate denied the intent")
            decision = AdmissionDecision.ADMITTED
            reason = "ADMITTED"
        except Exception as exc:
            reason = _reason_code(str(exc))
            simulator_fill = max(Decimal("0"), simulator_fill)
            drawdown = max(Decimal("0"), drawdown)
            risk_quantity = max(Decimal("0"), risk_quantity)
            price_decimal = max(Decimal("0"), price_decimal)
            spread_decimal = max(Decimal("0"), spread_decimal)
        evidence = {
            "simulator": _json_safe(simulator_evidence),
            "risk": _json_safe(risk_evidence),
            "evidence_at": observed_at.isoformat(),
            "max_age_seconds": max_age_seconds,
            "current_spread_pct": _decimal_text(spread_decimal),
        }
        evidence_hash = hashlib.sha256(canonical_json(evidence).encode("utf-8")).hexdigest()
        final_quantity = risk_quantity if decision is AdmissionDecision.ADMITTED else Decimal("0")
        notional = final_quantity * price_decimal
        admission = ExecutionAdmission(
            proposal_id=intent.proposal_id,
            intent_hash=intent_hash(intent),
            workspace=intent.workspace,
            account=intent.account,
            currency=currency,
            ticker=intent.ticker,
            side=intent.side,
            original_quantity=intent.quantity,
            final_quantity=final_quantity,
            price=price_decimal,
            notional=notional,
            simulator_fill_price=simulator_fill,
            simulator_drawdown_pct=drawdown,
            risk_quantity=risk_quantity,
            current_spread_pct=spread_decimal,
            evidence_at=observed_at,
            evidence_hash=evidence_hash,
            decision=decision,
            reason_code=reason,
            created_at=now,
        )
        stored = self._ledger.record_admission(intent, admission)
        if isinstance(proposal, dict):
            order = self._ledger.get_order(intent.proposal_id)
            if order is not None:
                _sync_projection(proposal, order.state)
        return stored

    def reserve(self, proposal_id: str):
        if self._ledger is None:
            raise ExecutionDisabledError("durable execution reservation is unavailable")
        return self._ledger.reserve_buying_power(proposal_id)

    def prepare(self, proposal: Proposal, **kwargs: Any) -> ExecutionAdmission:
        admission = self.admit(proposal, **kwargs)
        if admission.decision is AdmissionDecision.ADMITTED:
            self.reserve(admission.proposal_id)
        return admission

    def get_proposal(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        """Load the durable executable projection for route compatibility."""

        if self._ledger is None:
            return None
        order = self._ledger.get_order(proposal_id)
        if order is None:
            return None
        intent = dict(order.intent)
        proposal: Dict[str, Any] = {
            "proposal_id": order.proposal_id,
            "client_order_id": order.client_order_id,
            "intent_hash": order.intent_hash,
            "intent_version": intent.get("intent_version", 1),
            "workspace": intent.get("workspace", "uk"),
            "account": intent.get("account", "invest"),
            "broker": intent.get("broker", "paper"),
            "mode": intent.get("mode", OrderMode.PAPER.value),
            "ticker": intent.get("ticker"),
            "action": intent.get("side"),
            "quantity": intent.get("quantity"),
            "order_type": intent.get("order_type"),
            "limit_price": intent.get("limit_price"),
            "replaces_proposal_id": intent.get("replaces_proposal_id", ""),
            "requote_id": intent.get("requote_id", ""),
            "status": order.state,
            "execution_intent": intent,
        }
        if order.acknowledgment is not None:
            proposal["execution_ack"] = order.acknowledgment.model_dump(mode="json")
            proposal["execution_result"] = proposal["execution_ack"]
        return proposal

    async def approve(self, proposal: Proposal) -> OrderAck:
        raise ExecutionDisabledError("Signed approval is required before dispatch")

        mutable = proposal if isinstance(proposal, dict) else None
        if mutable is not None:
            intent = _intent_from_proposal(mutable)
            proposal_id = intent.proposal_id
        else:
            proposal_id = proposal
            durable = self.get_proposal(proposal_id)
            if durable is None:
                raise ExecutionConflictError(f"Trade proposal {proposal_id} was not found")
            intent = _intent_from_proposal(durable)

        async with self._lock_for(proposal_id):
            if self._ledger is not None:
                return await self._approve_durable(intent, mutable)
            if mutable is None:
                raise ExecutionConflictError("Durable proposal storage is unavailable")
            return await self._approve_in_memory(intent, mutable)

    def enroll_approval_key(
        self, public_key_x963: bytes, enrollment_token: str | bytes
    ):
        if self._approval_service is None:
            raise ExecutionDisabledError("Signed approval service is unavailable")
        return self._approval_service.enroll_key(public_key_x963, enrollment_token)

    def approval_key_id(self) -> Optional[str]:
        """Return the enrolled public-key identifier without exposing key material."""

        if self._ledger is None:
            return None
        enrolled = self._ledger.get_approval_key()
        return enrolled.key_id if enrolled is not None else None

    def create_approval_challenge(
        self, proposal_id: str, *, ttl_seconds: int = 60
    ) -> ApprovalChallenge:
        if self._approval_service is None:
            raise ExecutionDisabledError("Signed approval service is unavailable")
        return self._approval_service.create_challenge(
            proposal_id, ttl_seconds=ttl_seconds
        )

    def verify_approval_signature_for_uat(
        self, proposal_id: str, challenge_id: str, signature_der: bytes
    ) -> ApprovalChallenge:
        """Verify local UAT signing evidence without claiming or dispatching."""

        if self._approval_service is None:
            raise ExecutionDisabledError("Signed approval service is unavailable")
        return self._approval_service.verify_signature(
            proposal_id, challenge_id, signature_der
        )

    async def approve_signed(
        self, proposal_id: str, challenge_id: str, signature_der: bytes
    ) -> OrderAck:
        if self._dispatcher is None or self._ledger is None:
            raise ExecutionDisabledError(
                "Broker execution is disabled until mandatory controls are installed"
            )
        if not self._require_approval or self._approval_service is None:
            raise ExecutionDisabledError("Signed approval service is unavailable")
        durable = self.get_proposal(proposal_id)
        if durable is None:
            raise ExecutionConflictError(f"Trade proposal {proposal_id} was not found")
        intent = _intent_from_proposal(durable)
        if intent.mode is not OrderMode.PAPER:
            raise ExecutionDisabledError("Live execution remains disabled")
        async with self._lock_for(proposal_id):
            try:
                claim = self._approval_service.approve_signed(
                    proposal_id, challenge_id, signature_der
                )
            except (ApprovalConflict, InvalidTransition, OrderNotFound) as exc:
                raise ExecutionConflictError(str(exc)) from exc
            return await self._dispatch_durable_claim(intent, None, claim)

    def reconcile(self, snapshot: ReconciliationSnapshot):
        if self._ledger is None:
            raise ExecutionDisabledError("durable reconciliation is unavailable")
        return self._ledger.reconcile(snapshot)

    def engage_workspace_control(self, reason_code: str = "MANUAL_KILL"):
        if self._ledger is None:
            raise ExecutionDisabledError("durable workspace control is unavailable")
        return self._ledger.engage_workspace_control(reason_code)

    def create_control_challenge(self, *, ttl_seconds: int = 60):
        if self._approval_service is None:
            raise ExecutionDisabledError("Signed approval service is unavailable")
        return self._approval_service.create_control_challenge(ttl_seconds=ttl_seconds)

    def clear_workspace_control(self, challenge, signature_der: bytes) -> None:
        if self._approval_service is None:
            raise ExecutionDisabledError("Signed approval service is unavailable")
        self._approval_service.clear_workspace_control(challenge, signature_der)

    async def _approve_durable(
        self, intent: OrderIntent, proposal: Optional[Dict[str, Any]]
    ) -> OrderAck:
        try:
            self._ledger.register_intent(intent)
            claim = self._ledger.claim_intent(intent)
        except IntentConflict as exc:
            raise ExecutionConflictError(str(exc)) from exc
        except OrderNotFound as exc:
            raise ExecutionConflictError(str(exc)) from exc
        except InvalidTransition as exc:
            if "UNKNOWN" in str(exc):
                raise BrokerOutcomeUnknownError(
                    "Previous submission outcome is unknown and requires reconciliation"
                ) from exc
            raise ExecutionConflictError(str(exc)) from exc

        return await self._dispatch_durable_claim(intent, proposal, claim)

    async def _dispatch_durable_claim(
        self,
        intent: OrderIntent,
        proposal: Optional[Dict[str, Any]],
        claim: ClaimResult,
    ) -> OrderAck:

        if claim.status is ClaimStatus.REPLAY:
            ack = claim.order.acknowledgment
            if ack is None:
                raise BrokerOutcomeUnknownError(
                    "Stored acknowledgement is incomplete and requires reconciliation"
                )
            if proposal is not None:
                _sync_projection(proposal, claim.order.state, ack)
            return ack.as_replay()
        if claim.status is ClaimStatus.IN_PROGRESS:
            raise ExecutionConflictError("Trade proposal is already SUBMITTING")

        if proposal is not None:
            _sync_projection(proposal, OrderState.SUBMITTING.value)

        try:
            ack = await self._dispatcher.dispatch(intent)
        except BrokerExecutionError:
            self._ledger.mark_failed(intent.proposal_id, "BROKER_REJECTED")
            if proposal is not None:
                _sync_projection(proposal, OrderState.FAILED.value)
            raise
        except BrokerOutcomeUnknownError:
            self._ledger.mark_unknown(intent.proposal_id, "INCOMPLETE_ACK")
            if proposal is not None:
                _sync_projection(proposal, OrderState.UNKNOWN.value)
            raise
        except (asyncio.TimeoutError, TimeoutError) as exc:
            self._ledger.mark_unknown(intent.proposal_id, "BROKER_TIMEOUT")
            if proposal is not None:
                _sync_projection(proposal, OrderState.UNKNOWN.value)
            raise BrokerOutcomeUnknownError(
                "Broker submission outcome is unknown and requires reconciliation"
            ) from exc
        except RuntimeError as exc:
            if "timeout" in str(exc).lower():
                self._ledger.mark_unknown(intent.proposal_id, "BROKER_TIMEOUT")
                if proposal is not None:
                    _sync_projection(proposal, OrderState.UNKNOWN.value)
                raise BrokerOutcomeUnknownError(
                    "Broker submission outcome is unknown and requires reconciliation"
                ) from exc
            self._ledger.mark_failed(intent.proposal_id, "DISPATCH_RUNTIME_ERROR")
            if proposal is not None:
                _sync_projection(proposal, OrderState.FAILED.value)
            raise BrokerExecutionError("Broker execution failed") from exc
        except Exception as exc:
            self._ledger.mark_failed(intent.proposal_id, "DISPATCH_ERROR")
            if proposal is not None:
                _sync_projection(proposal, OrderState.FAILED.value)
            raise BrokerExecutionError("Broker execution failed") from exc

        stored_ack = self._ledger.finalize(intent.proposal_id, ack)
        if proposal is not None:
            _sync_projection(proposal, OrderState.ACKNOWLEDGED.value, stored_ack)
        return stored_ack

    async def _approve_in_memory(
        self, intent: OrderIntent, proposal: Dict[str, Any]
    ) -> OrderAck:
        intent_snapshot = intent.model_dump(mode="json")
        state = str(proposal.get("status", OrderState.PENDING.value)).upper()
        if state in {OrderState.ACKNOWLEDGED.value, OrderState.APPROVED.value} and proposal.get(
            "execution_ack"
        ):
            if proposal.get("execution_intent") != intent_snapshot:
                raise ExecutionConflictError("Trade proposal changed after broker submission")
            return OrderAck.model_validate(proposal["execution_ack"]).as_replay()
        if state == OrderState.UNKNOWN.value:
            raise BrokerOutcomeUnknownError(
                "Previous submission outcome is unknown and requires reconciliation"
            )
        if state != OrderState.PENDING.value:
            raise ExecutionConflictError(f"Trade proposal is already {state}")

        proposal["status"] = OrderState.SUBMITTING.value
        proposal["execution_intent"] = intent_snapshot
        try:
            ack = await self._dispatcher.dispatch(intent)
        except BrokerExecutionError:
            _sync_projection(proposal, OrderState.FAILED.value)
            proposal["failed_at"] = datetime.now().timestamp()
            raise
        except BrokerOutcomeUnknownError:
            _sync_projection(proposal, OrderState.UNKNOWN.value)
            proposal["unknown_at"] = datetime.now().timestamp()
            raise
        except (asyncio.TimeoutError, TimeoutError) as exc:
            _sync_projection(proposal, OrderState.UNKNOWN.value)
            proposal["unknown_at"] = datetime.now().timestamp()
            raise BrokerOutcomeUnknownError(
                "Broker submission outcome is unknown and requires reconciliation"
            ) from exc
        except RuntimeError as exc:
            if "timeout" in str(exc).lower():
                _sync_projection(proposal, OrderState.UNKNOWN.value)
                proposal["unknown_at"] = datetime.now().timestamp()
                raise BrokerOutcomeUnknownError(
                    "Broker submission outcome is unknown and requires reconciliation"
                ) from exc
            _sync_projection(proposal, OrderState.FAILED.value)
            proposal["failed_at"] = datetime.now().timestamp()
            raise BrokerExecutionError("Broker execution failed") from exc
        except Exception as exc:
            _sync_projection(proposal, OrderState.FAILED.value)
            proposal["failed_at"] = datetime.now().timestamp()
            raise BrokerExecutionError("Broker execution failed") from exc

        _sync_projection(proposal, OrderState.ACKNOWLEDGED.value, ack)
        proposal["executed_at"] = datetime.now().timestamp()
        return ack

    async def reject(self, proposal: Proposal, notes: Optional[str]) -> None:
        mutable = proposal if isinstance(proposal, dict) else None
        proposal_id = str(mutable.get("proposal_id", "")) if mutable is not None else proposal
        async with self._lock_for(proposal_id):
            if self._ledger is not None:
                try:
                    if mutable is not None:
                        self._ledger.register_intent(_intent_from_proposal(mutable))
                    order = self._ledger.reject(proposal_id, notes)
                except (IntentConflict, InvalidTransition, OrderNotFound) as exc:
                    raise ExecutionConflictError(str(exc)) from exc
                if mutable is not None:
                    _sync_projection(mutable, order.state)
                    mutable["rejection_notes"] = notes
                return

            if mutable is None:
                raise ExecutionConflictError("Durable proposal storage is unavailable")
            state = str(mutable.get("status", OrderState.PENDING.value)).upper()
            if state == OrderState.REJECTED.value:
                return
            if state != OrderState.PENDING.value:
                raise ExecutionConflictError(f"Trade proposal is already {state}")
            _sync_projection(mutable, OrderState.REJECTED.value)
            mutable["rejected_at"] = datetime.now().timestamp()
            mutable["rejection_notes"] = notes


def _intent_from_proposal(proposal: Dict[str, Any]) -> OrderIntent:
    mode = str(proposal.get("mode", OrderMode.PAPER.value)).upper()
    default_broker = "paper" if mode == OrderMode.PAPER.value else "trading212"
    return OrderIntent(
        proposal_id=str(proposal.get("proposal_id", "")),
        client_order_id=str(proposal.get("client_order_id", "")),
        intent_version=proposal.get("intent_version", 1),
        workspace=proposal.get("workspace", "uk"),
        account=proposal.get("account", "invest"),
        broker=proposal.get("broker", default_broker),
        mode=mode,
        ticker=proposal.get("ticker"),
        side=str(proposal.get("action", proposal.get("side", ""))).upper(),
        quantity=proposal.get("quantity"),
        order_type=proposal.get("order_type"),
        limit_price=proposal.get("limit_price"),
        replaces_proposal_id=proposal.get("replaces_proposal_id", ""),
        requote_id=proposal.get("requote_id", ""),
    )


def _sync_projection(
    proposal: Dict[str, Any], state: str, acknowledgment: Optional[OrderAck] = None
) -> None:
    proposal["status"] = state
    if acknowledgment is not None:
        proposal["execution_ack"] = acknowledgment.model_dump(mode="json")
        proposal["execution_result"] = proposal["execution_ack"]


def _finite_decimal(value: object, label: str) -> Decimal:
    try:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError(f"{label} is non-finite")
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{label} is not a finite Decimal") from exc
    if not parsed.is_finite():
        raise ValueError(f"{label} is non-finite")
    return parsed


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _json_safe(value: Mapping[str, Any]) -> Mapping[str, Any]:
    safe: Dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, float) and not math.isfinite(item):
            safe[str(key)] = "NON_FINITE"
        elif isinstance(item, Decimal):
            safe[str(key)] = _decimal_text(item)
        elif isinstance(item, (str, int, bool)) or item is None:
            safe[str(key)] = item
        else:
            safe[str(key)] = str(item)
    return safe


def _reason_code(reason: str) -> str:
    text = "_".join("".join(ch if ch.isalnum() else "_" for ch in reason.upper()).split())
    return text[:80] or "ADMISSION_DENIED"
