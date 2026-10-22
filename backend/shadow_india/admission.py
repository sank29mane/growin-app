"""The production SHADOW admission adapter and its durable preparation stages (Phase 64-07).

Two parts, both with no network and no broker:

``ShadowAdmissionAdapter`` builds an ``ExecutionService`` with every control the app builds (the
PreFlightSimulator, the RiskSwarmGate, ``require_runtime_preflight``, the Mac's India guard, the
loaded regime model's severity map and the matching policy table) and supplies every runtime input
``admit()`` requires. Nothing is removed or bypassed. The only differences from the app's
construction are the two a simulation needs: no dispatcher (nothing is ever sent) and no approval
service (the simulation account has no signing key).

``ShadowPreparation`` walks one intent through the SL-9 stages (E1 intent, L1 ledger, E2
reservation, E3 mint started, the VM answer, E4, L2) against an evidence port, a mint port and an
audit port. Recovery never POSTs. A mint whose answer was lost is adopted from exactly one verified
audit entry, or it stays INCONCLUSIVE and its session is excluded.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from execution.india_guard import IndiaAdmissionGuard, IndiaQuoteEvidence
from execution.ledger import (
    SHADOW_VM_DECISION_EVENT,
    SIMULATED_BOOKING_EVENT,
    SIMULATION_RUN_DIRECTORY,
    ExecutionLedger,
    IndiaCapExceeded,
    IntentConflict,
    InvalidTransition,
    OrderNotFound,
    SimulatedBooking,
    SimulatedCashInsufficient,
    SimulationInit,
    intent_hash,
    simulated_booking_id,
)
from execution.models import (
    AdmissionDecision,
    ExecutionAdmission,
    OrderIntent,
    OrderMode,
    OrderSide,
    OrderType,
    Workspace,
)
from execution.service import ExecutionService
from execution.venue import PRICE_SOURCE_RELAY_QUOTE, VenueBinding
from market_data import (
    IndiaInstrument,
    MarketDataError,
    MarketDataSession,
    RegimeClassifier,
    ReplayMarketDataProvider,
    TopOfBook,
    build_market_preflight_context,
)
from simulation import PreFlightSimulator, RiskSwarmGate
from simulation.regime_severity import build_scaling_policy_connection

SHADOW_VENUE = "breeze_relay"
MIN_QUOTE_SAMPLES = 3
MAX_QUOTE_SAMPLES = 20
MAX_AGE_SECONDS = 30
VM_CODE_KILL_SWITCH = "kill_switch"
INCONCLUSIVE_NO_FILL = "inconclusive_no_fill_assumed"

# Stable codes the adapter itself raises or records.
REGIME_WINDOW_INSUFFICIENT = "REGIME_WINDOW_INSUFFICIENT"
QUOTE_READINGS_NOT_DISTINCT = "QUOTE_READINGS_NOT_DISTINCT"
QUOTE_INVALID = "QUOTE_INVALID"

# Evidence record kinds.
E_INTENT = "intent"
E_RESERVATION = "reservation"
E_MINT_STARTED = "mint_started"
E_VM_DECISION = "vm_decision"
E_DENIED = "denied"
E_RESERVATION_REFUSED = "reservation_refused"
E_PREP_ABANDONED = "prep_abandoned"
E_VM_AMBIGUOUS = "vm_ambiguous"
E_VM_LATE_ENTRY = "vm_late_entry"
E_ATTEMPT_INCONCLUSIVE = "attempt_inconclusive"
E_SESSION_EXCLUDED = "session_excluded"
E_BOOKING_INTENT = "booking_intent"
E_BOOKING_DONE = "booking_done"
E_BOOKING_REFUSED = "booking_refused"

# A booking the ledger refuses is a deterministic no-op, not a fault: it changes nothing and is
# closed with a refusal row. Any other ledger error stays abort-class.
_BOOKING_REFUSALS = (InvalidTransition, IntentConflict, OrderNotFound, ValueError)

PREP_INTERRUPTED_BEFORE_MINT = "prep_interrupted_before_mint"
MINT_AMBIGUOUS = "mint_ambiguous"

_PREP_CLOSED = frozenset({E_DENIED, E_RESERVATION_REFUSED, E_PREP_ABANDONED})


class ShadowAbort(RuntimeError):
    """Evidence and ledger disagree in a way recovery must not repair. ``code`` is stable.

    The caller stops the run. Nothing is changed when this is raised from a check, and no
    message carries a price, a quantity or an identifier.
    """

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class MintUnresolved(RuntimeError):
    """The mint POST ended without a typed VM answer (timeout, transport error, untyped body)."""


# --- the ports ----------------------------------------------------------------------


class EvidencePort(Protocol):
    """An append-only, durable evidence store. 64-03 implements it; this plan tests a tmp-file one."""

    def append(self, kind: str, payload: Mapping[str, Any]) -> Mapping[str, Any]: ...

    def records(self) -> Sequence[Mapping[str, Any]]: ...


@dataclass(frozen=True)
class MintRequest:
    intent_id: str
    intent_sha256: str
    session_id: str


@dataclass(frozen=True)
class VmDecision:
    """The VM's typed answer to one intent: its audit entry and the refusal codes."""

    intent_id: str
    intent_sha256: str
    codes: tuple[str, ...]
    audit_seq: int
    entry_hash: str


class MintPort(Protocol):
    """The one POST per intent. Raises ``MintUnresolved`` (or ``asyncio.TimeoutError``) on no answer."""

    async def mint(self, request: MintRequest) -> VmDecision: ...


@dataclass(frozen=True)
class AuditEntry:
    seq: int
    intent_id: str
    intent_sha256: str
    codes: tuple[str, ...]
    entry_hash: str


@dataclass(frozen=True)
class AuditPage:
    entries: tuple[AuditEntry, ...]
    chain_verified: bool


class AuditPort(Protocol):
    """Read-only paging of the VM order audit from a base seq, with chain verification."""

    def page(self, from_seq: int) -> AuditPage: ...


# --- quote samples --------------------------------------------------------------------


@dataclass(frozen=True)
class QuoteSample:
    """One relay quote reading, pinned: the raw body hash, when it was fetched, bid and ask.

    ``evidence`` is the India rule input read at the same time (circuits, tick reference, ISIN).
    The adapter builds the replay from ``fetched_at``, ``bid`` and ``ask``, and hands the last
    sample's ``evidence`` to the Mac's India guard.
    """

    raw_sha256: str
    fetched_at: datetime
    bid: Decimal
    ask: Decimal
    evidence: IndiaQuoteEvidence

    def __post_init__(self) -> None:
        digest = self.raw_sha256
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
        ):
            raise ValueError("a quote sample is pinned by a lower-case sha256 of the raw body")
        if self.fetched_at.tzinfo is None:
            raise ValueError("a quote sample time must be timezone-aware")
        for label, price in (("bid", self.bid), ("ask", self.ask)):
            if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
                raise ValueError(f"a quote sample {label} must be a positive finite Decimal")
        if self.bid > self.ask:
            raise ValueError("a quote sample bid cannot exceed its ask")
        if not isinstance(self.evidence, IndiaQuoteEvidence):
            raise TypeError("a quote sample carries IndiaQuoteEvidence")


# --- construction ---------------------------------------------------------------------


def simulation_ledger_path(root: Path, run_id: str) -> Path:
    return Path(root) / SIMULATION_RUN_DIRECTORY / run_id / "ledger.sqlite3"


def simulation_binding(run_id: str) -> VenueBinding:
    return VenueBinding(venue=SHADOW_VENUE, account_id=f"sim-{run_id}", currency="INR")


def open_simulation_ledger(root: Path, run_id: str, seed: Decimal) -> ExecutionLedger:
    """Open (creating on first use) the run's simulation ledger under ``<root>/india-shadow/<run_id>/``."""

    init = SimulationInit(
        run_id=run_id,
        workspace=Workspace.INDIA,
        binding=simulation_binding(run_id),
        seed=seed,
    )
    path = simulation_ledger_path(root, run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    return ExecutionLedger(
        path,
        workspace=Workspace.INDIA,
        require_approval=False,
        venue=init.binding,
        simulation=init,
    )


class ShadowAdmissionAdapter:
    """One admission path for the SHADOW simulation account, with every 63 control live.

    The policy connection is an in-memory SQLite connection, so an adapter must be used on the
    thread that built it (the same rule the app follows for its own connection).
    """

    def __init__(
        self,
        ledger: ExecutionLedger,
        india_guard: IndiaAdmissionGuard,
        *,
        classifier: Optional[RegimeClassifier] = None,
        simulator: Any = None,
        risk_gate: Any = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if not ledger.is_simulation_ledger:
            raise ValueError("the SHADOW adapter needs a simulation ledger")
        self._ledger = ledger
        self._clock = clock
        self._classifier = classifier or RegimeClassifier()
        severity_map = self._classifier.severity_map
        self._policy_connection = build_scaling_policy_connection(severity_map)
        self._service = ExecutionService(
            None,
            ledger,
            require_approval=False,
            simulator=simulator if simulator is not None else PreFlightSimulator(),
            risk_gate=risk_gate if risk_gate is not None else RiskSwarmGate(),
            require_runtime_preflight=True,
            india_guard=india_guard,
            regime_severity_map=severity_map,
        )

    @property
    def service(self) -> ExecutionService:
        return self._service

    @property
    def ledger(self) -> ExecutionLedger:
        return self._ledger

    @property
    def classifier(self) -> RegimeClassifier:
        return self._classifier

    def build_intent(
        self,
        proposal_id: str,
        *,
        symbol: str,
        side: str,
        quantity: int | str | Decimal,
        limit_price: Decimal | str,
    ) -> OrderIntent:
        """A SHADOW LIMIT intent stamped from the ledger's own binding, never from the caller."""

        binding = self._ledger.venue_binding
        if binding is None:
            raise ValueError("the simulation ledger has no venue binding")
        return OrderIntent(
            proposal_id=proposal_id,
            workspace=self._ledger.workspace,
            account=binding.account_id,
            broker=binding.venue,
            mode=OrderMode.SHADOW,
            ticker=IndiaInstrument(symbol=symbol).execution_ticker,
            side=OrderSide(side),
            quantity=Decimal(str(quantity)),
            order_type=OrderType.LIMIT,
            limit_price=Decimal(str(limit_price)),
        )

    def portfolio_state(self) -> dict[str, float]:
        """Equity and peak equity from the simulation account: realised cash plus holdings at cost."""

        cash = self._ledger.simulated_cash_view()
        view = self._ledger.india_account_view()
        held_cost = sum((cost for _ticker, _quantity, cost in view.positions), Decimal("0"))
        equity = cash.seed - cash.net_cash_out + held_cost
        return {"equity": float(equity), "peak_equity": float(max(cash.seed, equity))}

    async def prepare(
        self, intent: OrderIntent, samples: Sequence[QuoteSample]
    ) -> ExecutionAdmission:
        """Admit and, when admitted, reserve one intent. Every refusal is a recorded DENIED admission."""

        if intent.mode is not OrderMode.SHADOW or intent.limit_price is None:
            raise ValueError("a SHADOW admission needs a SHADOW LIMIT intent")
        now = self._clock()
        india_quote = samples[-1].evidence if samples else None
        pins: dict[str, Any] = {
            # D-16: the order is admitted at its own LIMIT, labelled as the relay quote it came from.
            "price": intent.limit_price,
            "price_divisor": Decimal(1),
            "price_source": PRICE_SOURCE_RELAY_QUOTE,
        }
        denial, context = await self._context(intent, samples, now)
        if denial is not None or context is None:
            return self._service.admit(
                intent, currency="INR", deny_reason=denial, india_quote=india_quote, **pins
            )
        kwargs = {**context.execution_kwargs(), **pins}
        return self._service.prepare(
            intent,
            currency="INR",
            portfolio_state=self.portfolio_state(),
            risk_db_connection=self._policy_connection,
            india_quote=india_quote,
            max_age_seconds=MAX_AGE_SECONDS,
            **kwargs,
        )

    async def _context(
        self, intent: OrderIntent, samples: Sequence[QuoteSample], now: datetime
    ):
        if len(samples) < MIN_QUOTE_SAMPLES or len(samples) > MAX_QUOTE_SAMPLES:
            return REGIME_WINDOW_INSUFFICIENT, None
        ordered = sorted(samples, key=lambda sample: sample.fetched_at)
        stamps = [sample.fetched_at for sample in ordered]
        if len(set(stamps)) != len(stamps):
            return QUOTE_READINGS_NOT_DISTINCT, None
        symbol = intent.ticker.rsplit(":", 1)[-1]
        try:
            instrument = IndiaInstrument(symbol=symbol)
            events = tuple(
                TopOfBook(
                    instrument=instrument,
                    source=PRICE_SOURCE_RELAY_QUOTE,
                    observed_at=sample.fetched_at,
                    received_at=sample.fetched_at,
                    bid=sample.bid,
                    ask=sample.ask,
                    sequence=index,
                )
                for index, sample in enumerate(ordered, start=1)
            )
        except ValueError:
            return QUOTE_INVALID, None
        session = MarketDataSession(
            ReplayMarketDataProvider(events, name=PRICE_SOURCE_RELAY_QUOTE),
            max_age_seconds=MAX_AGE_SECONDS,
            clock=lambda: now,
        )
        try:
            await session.start((instrument,))
            while await session.poll_once() is not None:
                pass
            regime = self._classifier.evidence(session, instrument, now=now)
            context = build_market_preflight_context(
                session,
                intent=intent,
                instrument=instrument,
                regime=regime,
                now=now,
                max_regime_age_seconds=float(MAX_AGE_SECONDS),
            )
        except MarketDataError as exc:
            return exc.code, None
        finally:
            await session.stop()
        return None, context


# --- preparation stages (SL-9) and booking across stores (SL-8) -------------------------


def deterministic_proposal_id(run_id: str, session_id: str, signal_id: str, leg: str) -> str:
    text = json.dumps(
        [run_id, session_id, signal_id, leg],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PrepareResult:
    """Where one intent stopped. ``status`` is one of the stage names below."""

    status: str  # denied, reservation_refused, vm_decided or mint_unresolved
    proposal_id: str
    admission: Optional[ExecutionAdmission] = None
    vm_decision: Optional[VmDecision] = None


def _text(value: Decimal) -> str:
    return format(value, "f")


class ShadowPreparation:
    """Durable, recoverable preparation of SHADOW intents for one run."""

    def __init__(
        self,
        *,
        run_id: str,
        adapter: ShadowAdmissionAdapter,
        evidence: EvidencePort,
        mint: MintPort,
        audit: AuditPort,
        audit_base_seq: int = 0,
        mint_timeout: timedelta = timedelta(seconds=10),
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._run_id = run_id
        self._adapter = adapter
        self._ledger = adapter.ledger
        self._evidence = evidence
        self._mint = mint
        self._audit = audit
        self._audit_base_seq = audit_base_seq
        self._mint_timeout = mint_timeout
        self._clock = clock

    def proposal_id(self, session_id: str, signal_id: str, leg: str) -> str:
        return deterministic_proposal_id(self._run_id, session_id, signal_id, leg)

    def _append(self, kind: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._evidence.append(kind, dict(payload))

    # -- the live path ---------------------------------------------------------

    async def prepare(
        self,
        *,
        session_id: str,
        signal_id: str,
        leg: str,
        symbol: str,
        side: str,
        quantity: int | str | Decimal,
        limit_price: Decimal | str,
        samples: Sequence[QuoteSample],
    ) -> PrepareResult:
        proposal_id = self.proposal_id(session_id, signal_id, leg)
        intent = self._adapter.build_intent(
            proposal_id, symbol=symbol, side=side, quantity=quantity, limit_price=limit_price
        )
        digest = intent_hash(intent)
        # E1: the intent is on durable evidence before the ledger knows it.
        self._append(
            E_INTENT,
            {
                "intent_id": proposal_id,
                "session_id": session_id,
                "signal_id": signal_id,
                "leg": leg,
                "intent_sha256": digest,
                "symbol": symbol,
                "side": side,
                "quantity": _text(intent.quantity),
                "limit_price": _text(intent.limit_price),
            },
        )
        # L1: register, admission and reservation, one admission at a time.
        try:
            admission = await self._adapter.prepare(intent, samples)
        except (IndiaCapExceeded, SimulatedCashInsufficient) as exc:
            self._expire_unreserved(proposal_id, "reservation_refused")
            self._append(
                E_RESERVATION_REFUSED,
                {"intent_id": proposal_id, "code": getattr(exc, "code", type(exc).__name__)},
            )
            return PrepareResult("reservation_refused", proposal_id)
        if admission.decision is not AdmissionDecision.ADMITTED:
            self._append(E_DENIED, {"intent_id": proposal_id, "code": admission.reason_code})
            return PrepareResult("denied", proposal_id, admission)
        reservation = self._ledger.get_simulated_reservation(proposal_id)
        if reservation is None:
            raise ShadowAbort("admitted_without_reservation")
        # E2 copies the ledger's created_at (the pinned submission time, P-05).
        self._append(
            E_RESERVATION,
            {"intent_id": proposal_id, "reservation_created_at": reservation[1]},
        )
        started = self._clock()
        self._append(E_MINT_STARTED, {"intent_id": proposal_id, "at": started.isoformat()})
        try:
            decision = await self._mint.mint(MintRequest(proposal_id, digest, session_id))
        except (MintUnresolved, asyncio.TimeoutError):
            # Never retried. Recovery decides, from the VM audit, and never POSTs.
            return PrepareResult("mint_unresolved", proposal_id, admission)
        if self._ambiguous(proposal_id):
            # Recovery already called this mint ambiguous and the attempt is INCONCLUSIVE. The held
            # handler answering now is a late entry, never a decision.
            self._record_late_answer(decision)
            return PrepareResult("mint_unresolved", proposal_id, admission)
        self._record_decision(session_id, decision, recovered=False)
        return PrepareResult("vm_decided", proposal_id, admission, decision)

    def _ambiguous(self, intent_id: str) -> bool:
        return any(
            record.get("kind") == E_VM_AMBIGUOUS and record.get("intent_id") == intent_id
            for record in self._evidence.records()
        )

    def _record_late_answer(self, decision: VmDecision) -> None:
        if tuple(decision.codes) != (VM_CODE_KILL_SWITCH,):
            raise ShadowAbort("vm_codes_unexpected")
        late = [
            record
            for record in self._evidence.records()
            if record.get("kind") == E_VM_LATE_ENTRY and record.get("intent_id") == decision.intent_id
        ]
        if late:
            if int(late[0]["audit_seq"]) != decision.audit_seq or late[0]["entry_hash"] != decision.entry_hash:
                raise ShadowAbort("audit_duplicate_entries")
            return
        self._append(
            E_VM_LATE_ENTRY,
            {
                "intent_id": decision.intent_id,
                "audit_seq": decision.audit_seq,
                "entry_hash": decision.entry_hash,
            },
        )

    def _expire_unreserved(self, proposal_id: str, reason: str) -> None:
        order = self._ledger.get_order(proposal_id)
        if order is not None and order.state == "PENDING":
            self._ledger.expire_simulated_order(proposal_id, f"{reason}:{proposal_id}", reason)

    def _record_decision(self, session_id: str, decision: VmDecision, *, recovered: bool) -> None:
        if tuple(decision.codes) != (VM_CODE_KILL_SWITCH,):
            raise ShadowAbort("vm_codes_unexpected")
        payload = {
            "intent_id": decision.intent_id,
            "audit_seq": decision.audit_seq,
            "entry_hash": decision.entry_hash,
            "intent_sha256": decision.intent_sha256,
            "codes": list(decision.codes),
            "recovered_from_audit": recovered,
        }
        self._append(E_VM_DECISION, payload)  # E4
        self._ledger.record_shadow_vm_decision(  # L2
            decision.intent_id,
            decision.audit_seq,
            {"codes": list(decision.codes), "entry_hash": decision.entry_hash,
             "recovered_from_audit": recovered},
        )

    # -- recovery (never mints) ------------------------------------------------

    def _by_intent(self) -> dict[str, list[Mapping[str, Any]]]:
        grouped: dict[str, list[Mapping[str, Any]]] = {}
        for record in self._evidence.records():
            intent_id = record.get("intent_id")
            if isinstance(intent_id, str):
                grouped.setdefault(intent_id, []).append(record)
        return grouped

    def _audit_entries(self) -> dict[str, list[AuditEntry]]:
        page = self._audit.page(self._audit_base_seq)
        if not page.chain_verified:
            raise ShadowAbort("audit_chain_unverified")
        found: dict[str, list[AuditEntry]] = {}
        for entry in page.entries:
            found.setdefault(entry.intent_id, []).append(entry)
        return found

    def _mark_inconclusive(self, intent_id: str, session_id: str, code: str) -> None:
        self._append(E_ATTEMPT_INCONCLUSIVE, {"intent_id": intent_id, "code": code})
        already = any(
            record.get("kind") == E_SESSION_EXCLUDED and record.get("session_id") == session_id
            for record in self._evidence.records()
        )
        if not already:
            self._append(
                E_SESSION_EXCLUDED,
                {"session_id": session_id, "code": code, "excluded_from_qualification": True},
            )

    def recover(self) -> dict[str, str]:
        """Settle every unfinished intent from evidence and ledger. Returns intent id to outcome.

        Run at the start of decide and of close. It never POSTs to the VM, in any branch.
        """

        grouped = self._by_intent()
        known = set(grouped)
        for order in self._ledger.list_orders():
            if order.proposal_id not in known or not any(
                record.get("kind") == E_INTENT for record in grouped[order.proposal_id]
            ):
                raise ShadowAbort("ledger_order_without_intent_record")
        outcomes: dict[str, str] = {}
        audit: Optional[dict[str, list[AuditEntry]]] = None
        for intent_id, records in grouped.items():
            kinds = {str(record.get("kind")) for record in records}
            first = next((r for r in records if r.get("kind") == E_INTENT), None)
            if first is None:
                raise ShadowAbort("evidence_without_intent_record")
            if kinds & _PREP_CLOSED:
                outcomes[intent_id] = "closed"
                continue
            session_id = str(first["session_id"])
            order = self._ledger.get_order(intent_id)
            if order is None:
                self._append(E_PREP_ABANDONED, {"intent_id": intent_id})
                outcomes[intent_id] = E_PREP_ABANDONED
                continue
            admission = self._ledger.get_admission(intent_id)
            if admission is None:
                self._ledger.reject(intent_id, "prep abandoned")
                self._append(E_PREP_ABANDONED, {"intent_id": intent_id})
                outcomes[intent_id] = E_PREP_ABANDONED
                continue
            if admission.decision is not AdmissionDecision.ADMITTED:
                self._append(E_DENIED, {"intent_id": intent_id, "code": admission.reason_code})
                outcomes[intent_id] = "closed"
                continue
            reservation = self._ledger.get_simulated_reservation(intent_id)
            if reservation is None:
                self._ledger.expire_simulated_order(
                    intent_id, f"prep-abandoned:{intent_id}", E_PREP_ABANDONED
                )
                self._append(E_PREP_ABANDONED, {"intent_id": intent_id})
                outcomes[intent_id] = E_PREP_ABANDONED
                continue
            if E_RESERVATION not in kinds:
                # Never a new reservation: E2 is copied from what the ledger already holds.
                self._append(
                    E_RESERVATION,
                    {"intent_id": intent_id, "reservation_created_at": reservation[1]},
                )
                kinds.add(E_RESERVATION)
            if E_VM_AMBIGUOUS in kinds:
                # Wins over any later vm_decision: the attempt was called ambiguous first and stays
                # INCONCLUSIVE, whatever a held handler wrote afterwards.
                outcomes[intent_id] = MINT_AMBIGUOUS
                continue
            decided = self._ledger_vm_decisions(intent_id)
            if E_VM_DECISION in kinds:
                record = next(r for r in records if r.get("kind") == E_VM_DECISION)
                seq = int(record["audit_seq"])
                if seq not in decided:
                    self._ledger.record_shadow_vm_decision(  # L2 written from E4
                        intent_id,
                        seq,
                        {"codes": list(record["codes"]), "entry_hash": record["entry_hash"],
                         "recovered_from_audit": bool(record["recovered_from_audit"])},
                    )
                outcomes[intent_id] = "vm_decided"
                continue
            if decided:
                raise ShadowAbort("ledger_decision_without_evidence")
            if E_MINT_STARTED not in kinds:
                if E_ATTEMPT_INCONCLUSIVE not in kinds:
                    self._mark_inconclusive(intent_id, session_id, PREP_INTERRUPTED_BEFORE_MINT)
                outcomes[intent_id] = PREP_INTERRUPTED_BEFORE_MINT
                continue
            started_at = datetime.fromisoformat(
                str(next(r for r in records if r.get("kind") == E_MINT_STARTED)["at"])
            )
            if self._clock() < started_at + self._mint_timeout:
                outcomes[intent_id] = "deferred"
                continue
            if audit is None:
                audit = self._audit_entries()
            entries = audit.get(intent_id, [])
            digest = str(first["intent_sha256"])
            if len(entries) > 1:
                raise ShadowAbort("audit_duplicate_entries")
            if len(entries) == 1:
                entry = entries[0]
                if entry.intent_sha256 != digest or tuple(entry.codes) != (VM_CODE_KILL_SWITCH,):
                    raise ShadowAbort("audit_entry_mismatch")
                self._record_decision(
                    session_id,
                    VmDecision(intent_id, digest, tuple(entry.codes), entry.seq, entry.entry_hash),
                    recovered=True,
                )
                outcomes[intent_id] = "vm_decided"
                continue
            self._append(E_VM_AMBIGUOUS, {"intent_id": intent_id})
            self._mark_inconclusive(intent_id, session_id, MINT_AMBIGUOUS)
            outcomes[intent_id] = MINT_AMBIGUOUS
        return outcomes

    def _ledger_vm_decisions(self, intent_id: str) -> set[int]:
        return {
            int(event.payload["audit_seq"])
            for event in self._ledger.list_events(intent_id)
            if event.event_type == SHADOW_VM_DECISION_EVENT
        }

    def scan_audit(self) -> dict[str, str]:
        """Page the VM audit and reconcile it to evidence. Records a late entry for an ambiguous mint.

        Every entry must map to a recorded intent and hash and carry exactly ``["kill_switch"]``.
        Run at every later recovery, at close and at ``orders-check``. Never POSTs.
        """

        grouped = self._by_intent()
        entries = self._audit_entries()
        outcomes: dict[str, str] = {}
        for intent_id, found in entries.items():
            records = grouped.get(intent_id, [])
            first = next((r for r in records if r.get("kind") == E_INTENT), None)
            if first is None:
                raise ShadowAbort("audit_entry_without_intent_record")
            kinds = {str(record.get("kind")) for record in records}
            for entry in found:
                if entry.intent_sha256 != str(first["intent_sha256"]):
                    raise ShadowAbort("audit_entry_mismatch")
                if tuple(entry.codes) != (VM_CODE_KILL_SWITCH,):
                    raise ShadowAbort("audit_entry_codes")
            if len(found) > 1:
                raise ShadowAbort("audit_duplicate_entries")
            if E_MINT_STARTED not in kinds:
                raise ShadowAbort("audit_entry_without_mint_started")
            entry = found[0]
            if E_VM_AMBIGUOUS in kinds:
                late = [r for r in records if r.get("kind") == E_VM_LATE_ENTRY]
                if late:
                    if int(late[0]["audit_seq"]) != entry.seq or late[0]["entry_hash"] != entry.entry_hash:
                        raise ShadowAbort("audit_duplicate_entries")
                else:
                    self._append(
                        E_VM_LATE_ENTRY,
                        {"intent_id": intent_id, "audit_seq": entry.seq, "entry_hash": entry.entry_hash},
                    )
                    outcomes[intent_id] = E_VM_LATE_ENTRY
            elif E_VM_DECISION in kinds:
                decision = next(r for r in records if r.get("kind") == E_VM_DECISION)
                if int(decision["audit_seq"]) != entry.seq:
                    raise ShadowAbort("audit_duplicate_entries")
        return outcomes

    def release_inconclusive(self) -> list[str]:
        """The close step's SL-7 expiry: release every INCONCLUSIVE attempt's reservation.

        Holdings, the exit obligation and ``stop_open`` are untouched. A VM refusal alone never
        calls this.
        """

        released: list[str] = []
        for intent_id, records in self._by_intent().items():
            if not any(r.get("kind") == E_ATTEMPT_INCONCLUSIVE for r in records):
                continue
            order = self._ledger.get_order(intent_id)
            if order is not None and order.state in ("PENDING", "PARTIALLY_FILLED"):
                self._ledger.expire_simulated_order(
                    intent_id, f"close:{intent_id}", INCONCLUSIVE_NO_FILL
                )
                released.append(intent_id)
        return released

    # -- booking across stores (SL-8) -------------------------------------------

    def book_fill(
        self,
        *,
        proposal_id: str,
        cumulative_quantity: Decimal,
        price: Decimal,
        charges: Decimal,
        schedule_sha256: str,
        scenario_set_sha256: str,
        attempt_id: str,
    ) -> SimulatedBooking:
        """Evidence ``booking_intent``, then the ledger commit, then evidence ``booking_done``."""

        booking_id = simulated_booking_id(attempt_id, scenario_set_sha256)
        self._append(
            E_BOOKING_INTENT,
            {
                "intent_id": proposal_id,
                "booking_id": booking_id,
                "cumulative_quantity": _text(cumulative_quantity),
                "price": _text(price),
                "charges": _text(charges),
                "schedule_sha256": schedule_sha256,
                "scenario_set_sha256": scenario_set_sha256,
                "attempt_id": attempt_id,
            },
        )
        try:
            booking = self._ledger.book_simulated_fill(
                booking_id,
                proposal_id,
                cumulative_quantity,
                price,
                charges,
                schedule_sha256,
                scenario_set_sha256,
                attempt_id,
            )
        except _BOOKING_REFUSALS as exc:
            # The ledger changed nothing. Close the intent so recovery never retries it.
            self._record_booking_refused(proposal_id, booking_id, exc)
            raise
        self._append(
            E_BOOKING_DONE,
            {
                "intent_id": proposal_id,
                "booking_id": booking_id,
                "ledger_event_id": self._booking_event_id(proposal_id, booking_id),
            },
        )
        return booking

    def _booking_event_id(self, proposal_id: str, booking_id: str) -> int:
        for event in self._ledger.list_events(proposal_id):
            if event.event_type == SIMULATED_BOOKING_EVENT and event.payload.get("booking_id") == booking_id:
                return event.event_id
        raise ShadowAbort("booking_event_missing")

    def _record_booking_refused(self, proposal_id: str, booking_id: str, exc: Exception) -> None:
        self._append(
            E_BOOKING_REFUSED,
            {
                "intent_id": proposal_id,
                "booking_id": booking_id,
                "code": str(getattr(exc, "code", type(exc).__name__)),
            },
        )

    def recover_bookings(self) -> dict[str, str]:
        """Close every booking that stopped between its evidence and its ledger commit.

        Pass one adopts each intent whose booking already exists in the ledger. Pass two re-books
        the rest. A ledger refusal closes its intent with a ``booking_refused`` row and recovery
        moves on, so one refused intent never blocks another attempt's booking. Anything that is
        not a refusal still raises.
        """

        live: dict[str, list[Mapping[str, Any]]] = {}
        seen: set[str] = set()
        refused: set[str] = set()
        done: set[str] = set()
        for record in self._evidence.records():
            kind = record.get("kind")
            if kind == E_BOOKING_INTENT:
                booking_id = str(record["booking_id"])
                live.setdefault(booking_id, []).append(record)
                seen.add(booking_id)
            elif kind == E_BOOKING_REFUSED:
                booking_id = str(record["booking_id"])
                if not live.get(booking_id):
                    raise ShadowAbort("booking_refusal_without_intent")
                live[booking_id].pop()  # the refusal closes the intent just before it
                refused.add(booking_id)
            elif kind == E_BOOKING_DONE:
                done.add(str(record["booking_id"]))
        for booking in self._ledger.simulated_bookings():
            if booking.booking_id not in seen:
                raise ShadowAbort("ledger_booking_without_intent")
        outcomes: dict[str, str] = {}
        pending: list[tuple[str, Mapping[str, Any], tuple[Any, ...]]] = []
        for booking_id, records in live.items():
            if booking_id in done:
                outcomes[booking_id] = "done"
                continue
            if not records:
                outcomes[booking_id] = "refused"
                continue
            record = records[-1]
            proposal_id = str(record["intent_id"])
            args = (
                Decimal(str(record["cumulative_quantity"])),
                Decimal(str(record["price"])),
                Decimal(str(record["charges"])),
                str(record["schedule_sha256"]),
                str(record["scenario_set_sha256"]),
                str(record["attempt_id"]),
            )
            stored = self._ledger.simulated_booking(booking_id)
            if stored is None:
                pending.append((booking_id, record, args))
                continue
            same = (
                stored.proposal_id == proposal_id
                and (stored.cumulative_quantity, stored.price, stored.charges) == args[:3]
                and (stored.schedule_sha256, stored.scenario_set_sha256, stored.attempt_id)
                == args[3:]
            )
            if not same:
                raise ShadowAbort("booking_payload_mismatch")
            outcomes[booking_id] = "adopted"
            self._append_booking_done(proposal_id, booking_id)
        for booking_id, record, args in pending:
            proposal_id = str(record["intent_id"])
            try:
                self._ledger.book_simulated_fill(booking_id, proposal_id, *args)
            except _BOOKING_REFUSALS as exc:
                self._record_booking_refused(proposal_id, booking_id, exc)
                outcomes[booking_id] = "refused"
                continue
            outcomes[booking_id] = "rebooked"
            self._append_booking_done(proposal_id, booking_id)
        return outcomes

    def _append_booking_done(self, proposal_id: str, booking_id: str) -> None:
        self._append(
            E_BOOKING_DONE,
            {
                "intent_id": proposal_id,
                "booking_id": booking_id,
                "ledger_event_id": self._booking_event_id(proposal_id, booking_id),
            },
        )


__all__ = [
    "AuditEntry",
    "AuditPage",
    "AuditPort",
    "EvidencePort",
    "INCONCLUSIVE_NO_FILL",
    "MINT_AMBIGUOUS",
    "MintPort",
    "MintRequest",
    "MintUnresolved",
    "PREP_INTERRUPTED_BEFORE_MINT",
    "PrepareResult",
    "QuoteSample",
    "REGIME_WINDOW_INSUFFICIENT",
    "ShadowAbort",
    "ShadowAdmissionAdapter",
    "ShadowPreparation",
    "VM_CODE_KILL_SWITCH",
    "VmDecision",
    "deterministic_proposal_id",
    "open_simulation_ledger",
    "simulation_binding",
    "simulation_ledger_path",
]
