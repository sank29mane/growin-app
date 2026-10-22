"""The shadow runner's decide, close, resume and hand-flatten steps (64-03 Task 3, D-05, D-07, D-08, P-05, P-06, SL-9).

One invocation is one pass over one session, over ports a test replaces: the relay health read, the quote source, the
reference prices, the fill evidence and the VM orders client. Nothing here talks to a broker, and nothing here
authorizes: the only order call this module makes is the mint ``POST /v1/orders/intents``, answered by the VM's
production kill flag (blocked), so every answer must be a typed ``423 kill_switch`` with one matching audit entry.

Order of a decide, with the reason for each step:

1. refusals that need no state read (abort, run class, session id, the 09:30-10:00 IST window, relay health);
2. recovery (``ShadowPreparation.recover``, booking recovery, the audit scan): it never POSTs;
3. expiry of earlier sessions' open day orders, before the mark, so no stale reservation reads as an open exit;
4. the signal step for S (it may pause the run), then the quarantine check on held names;
5. the mark of S-1 at raw closes through the portfolio (peak and drawdown come from the 63 latch);
6. exits, then strategy sells, then entries, one intent at a time. Each intent is admitted and reserved before the
   next is sized, so cash, slots and the cap are read fresh. The policy table is asked before every admission and mint,
   and an open pause with scope ``all`` ends the invocation.

The VM leg: the body is built and checked locally first (a body the VM would refuse never costs a round trip), the
VM-side identity is written to evidence (``vm_intent``) before the POST, and the answer is classified code first
(``vm_codes``). A 423 ``kill_switch`` must correlate with exactly one verified audit entry carrying exactly
``["kill_switch"]``; a pause or abort is written before anything else can run.

Exit sells are minted only under ``portfolio.exit_intent_id(obligation_id, attempt)``. A strategy sell on a name that
has an open exit obligation is suppressed.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence

from brokers.breeze_gateway.errors import RelayError
from brokers.breeze_gateway.orders import OrdersClient, VmAuditEntry, intent_body, intent_sha256
from costs.core import Side
from costs.fills import load_fill_scenarios
from costs.schedule import load_schedule_set
from costs.ticks import InstrumentClass, align_limit, committed_tick_table, resolve_nse_cash_tick
from execution.india_guard import IndiaAdmissionGuard
from execution.ledger import ExecutionLedger
from pilot_data.constituents import latest_index_list
from pilot_data.core import PilotDataError, canonical_sha256
from pilot_data.price_bands import band_for_upcoming_session
from risk_india.state import LatchStore
from strategy_india.portfolio import ENTRY_CASH_BUFFER

from . import policy, vm_codes
from .admission import (
    E_ATTEMPT_INCONCLUSIVE,
    E_INTENT,
    E_VM_DECISION,
    SIMULATOR_DRAWDOWN_BASIS,
    VM_CODE_KILL_SWITCH,
    AuditEntry,
    AuditPage,
    MintRequest,
    MintUnresolved,
    PrepareResult,
    QuoteSample,
    ShadowAbort,
    ShadowAdmissionAdapter,
    ShadowPreparation,
    VmDecision,
)
from .evidence import GENESIS, IST, EvidenceStore
from .portfolio import (
    CORPORATE_ACTION_PAUSE,
    ExitProposal,
    Instrument,
    PortfolioAborted,
    PortfolioError,
    ShadowPortfolio,
    exit_intent_id,
    exit_obligation_id,
)
from .signals import (
    STATUS_EVALUATED,
    STATUS_PAUSED,
    SUPPRESS_QUARANTINED,
    SignalInputs,
    SignalResult,
    SignalsError,
    evaluate_session,
    previous_session,
)

DECIDE_OPENS = time(9, 30)
DECIDE_CLOSES = time(10, 0)
RUN_CLASS_QUALIFYING = "qualifying"
LEG_ENTRY = "entry"
LEG_EXIT = "exit"  # an exit obligation's sell: its proposal id IS ``exit_intent_id(obligation_id, attempt)``
LEG_SWING = "swing"  # a strategy sell (a held name fell out of the hold cutoff)
EXPECTED_KILL_LABEL = "blocked"
NO_FILL_EVIDENCE = "no_post_submission_evidence"
OUTCOME_INCONCLUSIVE = "INCONCLUSIVE"
OUTCOME_FILLED = "FILLED"
DAY_CLOSE = "day_close"
HAND_FLATTEN_PAUSE_REASON = "hand_flatten_requested_by_operator"
_EXIT_ID = re.compile(r"^exit-(halve|stop|flatten)-\d{8}-[A-Z]{2}[A-Z0-9]{9}[0-9]-t[1-9][0-9]*\Z")
_BPS = Decimal(10000)
_ONE = Decimal(1)
_OPEN_STATES = ("PENDING", "PARTIALLY_FILLED")

# What a ``ShadowAbort`` from the admission module means for the policy table. Everything else is a disagreement
# between the ledger and the evidence.
_ABORT_EVENTS: Mapping[str, str] = {
    "audit_chain_unverified": "vm_audit_chain_break",
    "audit_duplicate_entries": "vm_audit_duplicate_entries",
    "audit_entry_codes": "vm_audit_codes_unexpected",
}


class DecideRefused(Exception):
    """A refusal before any step ran. ``code`` is stable and carries no value. Nothing was written."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RunnerAbort(ShadowAbort):
    """The runner already wrote this abort to the evidence. The caller must not write it again."""


def session_id_for(day: date) -> str:
    return f"s-{day:%Y%m%d}"


def session_date_of(session_id: str) -> date:
    return datetime.strptime(session_id, "s-%Y%m%d").date()


# --- ports --------------------------------------------------------------------------------------------------------


class HealthPort(Protocol):
    def health(self) -> Any: ...  # ``.state`` and ``.egress_ok``, as the relay client's ``RelayHealth``


class QuotePort(Protocol):
    def samples(self, instrument: Instrument) -> Sequence[QuoteSample]: ...


class ReferencePort(Protocol):
    def closes(self, session: date, isins: Sequence[str]) -> Mapping[str, Decimal]:
        """Raw closes dated ``session`` for the names that have one."""

    def adv(self, isin: str, session: date) -> Optional[Decimal]:
        """Median traded value over the ADV window ending ``session``, or None."""

    def structural_actions(self, session: date, isins: Sequence[str]) -> Mapping[str, str]:
        """``isin -> split | bonus`` for held names with an ex-date on ``session``."""


class PinsPort(Protocol):
    def tick_sha256(self) -> str: ...

    def cost_sha256(self) -> str: ...

    def scenario_sha256(self) -> str: ...


@dataclass(frozen=True)
class FillQuery:
    """What a fill source is asked about. ``submitted_at`` is the ledger's reservation time (P-05), never a clock."""

    intent_id: str
    symbol: str
    side: str
    quantity: Decimal
    limit_price: Decimal
    submitted_at: str


@dataclass(frozen=True)
class FillOutcome:
    outcome: str
    reason: str
    quantity: Optional[Decimal] = None
    price: Optional[Decimal] = None
    charges: Optional[Decimal] = None
    schedule_sha256: Optional[str] = None
    scenario_set_sha256: Optional[str] = None


class FillPort(Protocol):
    def fill(self, query: FillQuery) -> FillOutcome: ...


class NoPostSubmissionEvidence:
    """The default fill source: nothing after the submission is known, so every attempt is INCONCLUSIVE.

    An order the VM refused has no fill. Assuming one would be an invented fill, so the default never books.
    """

    def fill(self, query: FillQuery) -> FillOutcome:
        return FillOutcome(OUTCOME_INCONCLUSIVE, NO_FILL_EVIDENCE)


@dataclass
class RunnerPorts:
    quotes: QuotePort
    reference: ReferencePort
    fills: FillPort = field(default_factory=NoPostSubmissionEvidence)


class LiveTablePins:
    """The tick, cost and scenario pins, read from the committed tables on every call (never cached here)."""

    def tick_sha256(self) -> str:
        return canonical_sha256(
            {
                kind.value: [v.version_hash for v in committed_tick_table(kind).versions]
                for kind in (InstrumentClass.EQUITY, InstrumentClass.NON_GOLD_ETF)
            }
        )

    def cost_sha256(self) -> str:
        return canonical_sha256({"schedules": [v.schedule_hash for v in load_schedule_set().versions]})

    def scenario_sha256(self) -> str:
        return load_fill_scenarios().scenarios_hash


@dataclass(frozen=True)
class VmIdentity:
    """The three values the VM checks every intent against. Hashes and a key id, no secret."""

    limits_sha256: str
    params_sha256: str
    key_id: str


# --- the VM leg ---------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunnerVmDecision(VmDecision):
    status: Optional[int] = None
    code: Optional[str] = None
    kill: Optional[str] = None


def vm_intent_id_for(proposal_id: str) -> str:
    return "i" + hashlib.sha256(proposal_id.encode("ascii")).hexdigest()[:40]


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def verify_vm_chain(entries: Sequence[VmAuditEntry], *, base_seq: int, base_sha256: str) -> bool:
    """The VM's recipe: every entry hashes to its own ``entry_sha256``, links to the one before and counts up by one.

    The first entry links to the entry the run recorded at ``base_seq`` (``GENESIS`` for an empty audit).
    """

    previous, seq = base_sha256, base_seq
    for entry in entries:
        raw = entry.raw
        if entry.seq != seq + 1 or raw.get("prev_sha256") != previous:
            return False
        digest = hashlib.sha256(_canonical({k: v for k, v in raw.items() if k != "entry_sha256"})).hexdigest()
        if digest != entry.entry_sha256 or raw.get("entry_sha256") != digest:
            return False
        previous, seq = entry.entry_sha256, entry.seq
    return True


class VmAuditReader:
    """Read-only paging of the VM order audit, translated into the ledger's terms (``AuditPort``).

    The VM derives its own intent id and hash from the O3 body, so an entry is matched through the ``vm_intent``
    record the mint wrote before its POST. Entries that carry no intent (a halt, an evaluation) are chain links only.
    """

    def __init__(self, orders: OrdersClient, evidence: EvidenceStore, *, base_seq: int, base_sha256: str) -> None:
        self._orders = orders
        self._evidence = evidence
        self._base_seq = base_seq
        self._base_sha256 = base_sha256
        self.labels: dict[int, str] = {}

    def _after(self, from_seq: int) -> list[VmAuditEntry]:
        out: list[VmAuditEntry] = []
        after = from_seq
        while True:
            page = self._orders.audit(after)
            out.extend(page)
            if len(page) < 200:
                return out
            after = page[-1].seq

    def _translation(self) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
        digests: dict[str, str] = {}
        vm_ids: dict[str, tuple[str, str]] = {}
        records = self._evidence.records()
        for record in records:
            if record.get("kind") == E_INTENT:
                digests[str(record["intent_id"])] = str(record["intent_sha256"])
        for record in records:
            if record.get("kind") == "vm_intent":
                proposal = str(record["intent_id"])
                vm_ids[str(record["vm_intent_id"])] = (proposal, str(record["vm_intent_sha256"]))
        return vm_ids, digests

    def page(self, from_seq: int) -> AuditPage:
        entries = self._after(from_seq)
        if from_seq == self._base_seq:
            verified = verify_vm_chain(entries, base_seq=from_seq, base_sha256=self._base_sha256)
        else:  # a later start has no recorded head to link to: verify the entries among themselves
            verified = not entries or verify_vm_chain(
                entries, base_seq=entries[0].seq - 1, base_sha256=str(entries[0].raw.get("prev_sha256"))
            )
        vm_ids, digests = self._translation()
        out: list[AuditEntry] = []
        for entry in entries:
            if entry.intent_id is None:
                continue
            kill = entry.raw.get("kill")
            if isinstance(kill, str):
                self.labels[entry.seq] = kill
            mapped = vm_ids.get(entry.intent_id)
            if mapped is not None and entry.intent_sha256 == mapped[1]:
                out.append(AuditEntry(entry.seq, mapped[0], digests[mapped[0]], entry.codes, entry.entry_sha256))
            else:  # unknown to this run, or a hash the run never minted: admission aborts on it
                out.append(
                    AuditEntry(entry.seq, mapped[0] if mapped else entry.intent_id, str(entry.intent_sha256), entry.codes, entry.entry_sha256)
                )
        return AuditPage(tuple(out), verified)


@dataclass(frozen=True)
class MintMeta:
    body: Mapping[str, Any]
    vm_intent_sha256: str


class VmMint:
    """The one POST per intent (``MintPort``): classify the typed answer, correlate the audit, never authorize."""

    def __init__(self, orders: OrdersClient, evidence: EvidenceStore, reader: VmAuditReader, *, base_seq: int) -> None:
        self._orders = orders
        self._evidence = evidence
        self._reader = reader
        self._base_seq = base_seq
        self._meta: dict[str, MintMeta] = {}

    def register(self, proposal_id: str, body: Mapping[str, Any]) -> None:
        self._meta[proposal_id] = MintMeta(body, intent_sha256(body))

    def _raise(self, verdict: vm_codes.Classification) -> None:
        reason = f"{verdict.detail} code={verdict.code} status={verdict.status}"
        assert verdict.event is not None
        policy.raise_event(self._evidence, verdict.event, reason)
        if verdict.kind == vm_codes.ABORT:
            raise RunnerAbort(verdict.event)
        raise MintUnresolved(verdict.event)

    async def mint(self, request: MintRequest) -> VmDecision:
        meta = self._meta.pop(request.intent_id, None)
        if meta is None:
            raise ShadowAbort("mint_without_registered_body")
        self._evidence.append(
            "vm_intent",
            {"intent_id": request.intent_id, "vm_intent_id": str(meta.body["intent_id"]),
             "vm_intent_sha256": meta.vm_intent_sha256},
        )
        try:
            self._orders.mint(meta.body)
        except RelayError as error:
            verdict = vm_codes.classify_error(error)
        else:
            # A challenge came back: the VM did not refuse. It is never authorized and never signed.
            verdict = vm_codes.classify(vm_codes.VmAnswer(None, None, None, 200))
        if verdict.kind != vm_codes.EXPECTED:
            self._raise(verdict)
        return self._correlate(request, verdict)

    def _correlate(self, request: MintRequest, verdict: vm_codes.Classification) -> RunnerVmDecision:
        def refuse(event: str, detail: str) -> RunnerVmDecision:
            policy.raise_event(self._evidence, event, f"{detail} status={verdict.status}")
            raise RunnerAbort(event)

        page = self._reader.page(self._base_seq)
        if not page.chain_verified:
            return refuse("vm_audit_chain_break", "audit_chain_not_verified")
        mine = [entry for entry in page.entries if entry.intent_id == request.intent_id]
        if not mine:
            return refuse("vm_423_no_audit_entry", "kill_switch_without_audit_entry")
        if len(mine) > 1:
            return refuse("vm_audit_duplicate_entries", "more_than_one_audit_entry")
        entry = mine[0]
        if entry.intent_sha256 != request.intent_sha256 or tuple(entry.codes) != (VM_CODE_KILL_SWITCH,):
            return refuse("vm_audit_codes_unexpected", "audit_entry_is_not_the_expected_refusal")
        return RunnerVmDecision(
            request.intent_id, request.intent_sha256, tuple(entry.codes), entry.seq, entry.entry_hash,
            status=verdict.status, code=verdict.code, kill=self._reader.labels.get(entry.seq),
        )


class RunnerPreparation(ShadowPreparation):
    """``ShadowPreparation`` with exit ids from the portfolio and the P-06 fields on ``vm_decision``."""

    def __init__(self, *args: Any, labels: Optional[dict[int, str]] = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._labels = labels if labels is not None else {}

    def proposal_id(self, session_id: str, signal_id: str, leg: str) -> str:
        if leg != LEG_EXIT:
            return super().proposal_id(session_id, signal_id, leg)
        if _EXIT_ID.fullmatch(signal_id) is None:  # an exit sell is only ever minted under exit_intent_id
            raise ShadowAbort("exit_intent_id_malformed")
        return signal_id

    def _record_decision(self, session_id: str, decision: VmDecision, *, recovered: bool) -> None:
        if tuple(decision.codes) != (VM_CODE_KILL_SWITCH,):
            raise ShadowAbort("vm_codes_unexpected")
        status = getattr(decision, "status", None)
        kill = getattr(decision, "kill", None) or self._labels.get(decision.audit_seq)
        payload: dict[str, Any] = {
            "intent_id": decision.intent_id,
            "audit_seq": decision.audit_seq,
            "entry_hash": decision.entry_hash,
            "intent_sha256": decision.intent_sha256,
            "codes": list(decision.codes),
            "recovered_from_audit": recovered,
            "code": VM_CODE_KILL_SWITCH,
            "vm_portfolio_checks": vm_codes.PORTFOLIO_CHECKS_NOT_RUN,
        }
        if isinstance(status, int):
            payload["status"] = status
        if isinstance(kill, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", kill):
            payload["kill"] = kill
        self._append("vm_decision", payload)  # E4
        self._ledger.record_shadow_vm_decision(  # L2
            decision.intent_id,
            decision.audit_seq,
            {"codes": list(decision.codes), "entry_hash": decision.entry_hash, "recovered_from_audit": recovered},
        )
        if kill != EXPECTED_KILL_LABEL:
            # An unreadable (or any other) label is not the blocked flag the run expects. Fail closed: stop everything.
            policy.raise_event(self._evidence, "kill_label_unreadable", f"kill label is not {EXPECTED_KILL_LABEL}")


# --- reports ------------------------------------------------------------------------------------------------------


@dataclass
class DecideReport:
    session: date
    status: str = "done"  # done | paused | aborted
    stopped: Optional[str] = None
    marked: bool = False
    signals: Optional[str] = None
    intents: list[tuple[str, str]] = field(default_factory=list)  # (proposal id, stage)
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (what, why)
    expired: list[str] = field(default_factory=list)
    recovered: Mapping[str, str] = field(default_factory=dict)


@dataclass
class CloseReport:
    session: date
    outcome: str = "ok"
    attempts: list[tuple[str, str]] = field(default_factory=list)
    released: list[str] = field(default_factory=list)
    witness: Optional[str] = None


# --- the runner ---------------------------------------------------------------------------------------------------


class ShadowRunner:
    def __init__(
        self,
        *,
        evidence: EvidenceStore,
        adapter: ShadowAdmissionAdapter,
        portfolio: ShadowPortfolio,
        preparation: RunnerPreparation,
        mint: VmMint,
        reader: VmAuditReader,
        identity: VmIdentity,
        base_inputs: SignalInputs,
        pins: PinsPort,
        ports: RunnerPorts,
        health: HealthPort,
        instruments: Sequence[Instrument],
        clock: Callable[[], datetime],
    ) -> None:
        self._evidence = evidence
        self._adapter = adapter
        self._ledger: ExecutionLedger = adapter.ledger
        self._portfolio = portfolio
        self._prep = preparation
        self._mint = mint
        self._reader = reader
        self._identity = identity
        self._base_inputs = base_inputs
        self._pins = pins
        self._ports = ports
        self._health = health
        self._by_isin = {i.isin: i for i in instruments}
        self._clock = clock

    # -- refusals ---------------------------------------------------------------------------------------------

    def _state(self) -> policy.RunState:
        return policy.run_state(self._evidence)

    def _stopped_all(self) -> bool:
        state = self._state()
        return state.aborted or any(p.scope == policy.SCOPE_ALL for p in state.open_pauses)

    def _refuse_unless_decidable(self, session: date) -> None:
        self._state().require(policy.STEP_READ_ONLY)  # an abort refuses everything, with RunAborted
        if self._evidence.manifest.run_class == RUN_CLASS_QUALIFYING:
            raise DecideRefused("run_class_qualifying")
        if self._evidence.session_id != session_id_for(session):
            raise DecideRefused("session_id_mismatch")
        start = self._run_start()
        if session < start:
            raise DecideRefused("session_before_run_start")
        now = self._clock()
        local = now.astimezone(IST)
        if local.date() != session or not DECIDE_OPENS <= local.time() < DECIDE_CLOSES:
            raise DecideRefused("outside_decide_window")
        health = self._health.health()
        if getattr(health, "state", None) != "active" or getattr(health, "egress_ok", None) is not True:
            raise DecideRefused("relay_not_active")

    def _run_start(self) -> date:
        for record in self._evidence.records():
            if record.get("kind") == "run_start":
                return datetime.strptime(str(record["run_start_session"]), "%Y-%m-%d").date()
        raise DecideRefused("run_not_opened")

    # -- decide -----------------------------------------------------------------------------------------------

    def decide(self, session: date) -> DecideReport:
        return asyncio.run(self.decide_async(session))

    async def decide_async(self, session: date) -> DecideReport:
        self._refuse_unless_decidable(session)
        report = DecideReport(session)
        try:
            await self._decide(session, report)
        except RunnerAbort:
            report.status, report.stopped = "aborted", "abort"
        except (ShadowAbort, PortfolioAborted) as exc:
            self._record_abort(exc)
            report.status, report.stopped = "aborted", "abort"
        if report.status == "done" and self._stopped_all():
            report.status, report.stopped = ("aborted", "abort") if self._state().aborted else ("paused", "pause")
        return report

    def _record_abort(self, exc: Exception) -> None:
        if isinstance(exc, PortfolioAborted) or self._state().aborted:
            return  # the portfolio wrote its own abort
        code = str(getattr(exc, "code", "abort"))
        policy.raise_event(self._evidence, _ABORT_EVENTS.get(code, "ledger_evidence_disagree"), f"shadow_abort {code}")

    async def _decide(self, session: date, report: DecideReport) -> None:
        if self._stopped_all():
            report.status, report.stopped = "paused", "open_pause"
            return
        report.recovered = self._recover()
        if self._stopped_all():
            report.status, report.stopped = "paused", "open_pause"
            return
        report.expired = self._expire_day_orders(session, report.recovered)
        inputs = replace(
            self._base_inputs,
            tick_sha256=self._pins.tick_sha256(),
            cost_sha256=self._pins.cost_sha256(),
            scenario_sha256=self._pins.scenario_sha256(),
        )
        try:
            result = evaluate_session(self._evidence, inputs, decision_session=session)
        except SignalsError as exc:
            raise DecideRefused(f"signals:{exc.code}") from None
        report.signals = result.status
        if result.status == STATUS_PAUSED or self._stopped_all():
            report.status, report.stopped = "paused", "signals_paused"
            return
        as_of = result.as_of
        assert as_of is not None
        self._pause_quarantined_held_names(result)
        if not self._mark(as_of, report):
            return
        plan = self._portfolio.exit_proposals(session)
        report.skipped.extend((isin, "exit_held_out") for isin in plan.held_out)
        report.skipped.extend((isin, "exit_ambiguous") for isin in plan.ambiguous)
        report.skipped.extend((isin, "exit_waiting") for isin in plan.waiting)
        obligated = set(plan.held_out) | set(plan.ambiguous) | set(plan.waiting) | {p.isin for p in plan.proposals}
        self._resolve_exit_bands(result, plan.proposals, as_of, session)
        for proposal in plan.proposals:
            if self._stopped_all():
                return
            await self._place_exit(session, as_of, result, proposal, report)
        if result.status == STATUS_EVALUATED:
            await self._strategy_sells(session, as_of, result, obligated, report)
            await self._entries(session, as_of, result, inputs, obligated, report)

    # -- recovery and expiry -----------------------------------------------------------------------------------

    def _recover(self) -> Mapping[str, str]:
        outcomes = self._prep.recover()  # never POSTs
        if not self._state().blocks(policy.STEP_BOOK):
            self._prep.recover_bookings()
        self._prep.scan_audit()
        return outcomes

    def _intent_records(self) -> dict[str, Mapping[str, Any]]:
        return {str(r["intent_id"]): r for r in self._evidence.records() if r.get("kind") == E_INTENT}

    def _expire_day_orders(self, session: date, recovered: Mapping[str, str]) -> list[str]:
        """Close every open order of an earlier session before the mark: a day order does not live past its day."""

        if self._state().blocks(policy.STEP_EXPIRE):
            return []
        today = session_id_for(session)
        intents = self._intent_records()
        done = {str(r["intent_id"]) for r in self._evidence.records() if r.get("kind") == "expiry"}
        expired: list[str] = []
        for order in self._ledger.list_orders(_OPEN_STATES):
            pid = order.proposal_id
            record = intents.get(pid)
            if record is None or record.get("session_id") == today or recovered.get(pid) == "deferred":
                continue
            if pid not in done:
                self._evidence.append(
                    "expiry", {"intent_id": pid, "reason": DAY_CLOSE, "released_quantity": str(record["quantity"])}
                )
            self._ledger.expire_simulated_order(pid, f"day-expiry:{pid}", DAY_CLOSE)
            expired.append(pid)
        return expired

    # -- signals, quarantine and the mark ----------------------------------------------------------------------

    def _open_pause_isins(self, event: str) -> set[str]:
        prefix = f"{policy.SCOPE_ISIN}:"
        return {p.scope[len(prefix):] for p in self._state().open_pauses if p.event == event and p.scope.startswith(prefix)}

    def _pause_quarantined_held_names(self, result: SignalResult) -> None:
        """A held name whose adjusted bar is quarantined may carry an unrecorded split or bonus: pause that ISIN."""

        if result.status != STATUS_EVALUATED:
            return
        held = {h.instrument.isin for h in self._portfolio.snapshot().holdings}
        already = self._open_pause_isins(CORPORATE_ACTION_PAUSE)
        for signal in result.signals:
            isin = signal.isin if signal.isin in held else signal.anchor_isin
            if signal.suppressed == SUPPRESS_QUARANTINED and isin in held and isin not in already:
                policy.raise_event(self._evidence, CORPORATE_ACTION_PAUSE, SUPPRESS_QUARANTINED, isin=isin)
                already.add(isin)

    def _simulator_drawdown(self) -> Decimal:
        state = self._adapter.portfolio_state()
        equity, peak = Decimal(str(state["equity"])), Decimal(str(state["peak_equity"]))
        return ((equity - peak) / peak).quantize(Decimal("0.000001"))

    def _mark(self, as_of: date, report: DecideReport) -> bool:
        isins = sorted(h.instrument.isin for h in self._portfolio.snapshot().holdings)
        closes = self._ports.reference.closes(as_of, isins) if isins else {}
        actions = self._ports.reference.structural_actions(as_of, isins) if isins else {}
        try:
            self._portfolio.mark_session(
                as_of, closes, structural_actions=actions or None, simulator_drawdown_pct=self._simulator_drawdown()
            )
        except PortfolioError as exc:
            if exc.code != "mark_missing":
                raise
            report.status, report.stopped = "paused", "mark_missing"
            return False
        report.marked = True
        return True

    # -- bands, sizing and one intent --------------------------------------------------------------------------

    def _resolve_exit_bands(
        self, result: SignalResult, proposals: Sequence[ExitProposal], as_of: date, session: date
    ) -> None:
        """The signal step bands only names ranked within the hold cutoff, so a held name being exited has none.

        Resolve it here. A band that cannot be resolved pauses that ISIN's entries (``band_ambiguity``); the sell goes on.
        """

        already = self._open_pause_isins("band_ambiguity")
        for proposal in proposals:
            if proposal.isin in result.bands or proposal.isin in already:
                continue
            try:
                observed = band_for_upcoming_session(
                    self._base_inputs.store, isin=proposal.isin, prior_session=as_of, session=session,
                    calendar=self._base_inputs.calendar, workspace="india",
                )
            except PilotDataError:
                observed = None
            if observed is None or observed.status == "unknown":
                policy.raise_event(self._evidence, "band_ambiguity", "exit_name_band_unresolved", isin=proposal.isin)
                already.add(proposal.isin)

    def _limit(self, ref: Decimal, side: Side, session: date, series: str) -> Decimal:
        tick = resolve_nse_cash_tick(
            session_date=session, band_reference_price=ref, instrument_class=InstrumentClass.EQUITY, series=series
        ).tick
        offset = Decimal(self._base_inputs.strategy.params.limit_offset_bps)
        sign = _ONE if side is Side.BUY else -_ONE
        return align_limit(ref * (_ONE + sign * offset / _BPS), tick, side)

    def _vm_body(
        self, proposal_id: str, instrument: Instrument, side: str, quantity: int, limit: Decimal, reason: str,
        batch_id: Optional[str],
    ) -> Mapping[str, Any]:
        return intent_body(
            intent_id=vm_intent_id_for(proposal_id), proposal_id=proposal_id, side=side.lower(),
            stock_code=instrument.stock_code, isin=instrument.isin, quantity=quantity,
            limit_price=format(limit.quantize(Decimal("0.01")), "f"), reason=reason, batch_id=batch_id,
            limits_sha256=self._identity.limits_sha256, params_sha256=self._identity.params_sha256,
            key_id=self._identity.key_id,
        )

    async def _submit(
        self, *, session: date, instrument: Instrument, side: str, quantity: int, limit: Decimal, reason: str,
        batch_id: Optional[str], signal_id: str, leg: str, report: DecideReport,
    ) -> Optional[PrepareResult]:
        """One intent through every gate. The policy table is asked first, then the body is checked, then SL-9 runs."""

        sell = side == "SELL"
        policy_side = policy.SIDE_SELL if sell else policy.SIDE_BUY
        state = self._state()
        if state.blocks(policy.STEP_ADMIT, isin=instrument.isin, side=policy_side) or state.blocks(
            policy.STEP_MINT, isin=instrument.isin, side=policy_side
        ):
            report.skipped.append((instrument.isin, "policy_blocked"))
            return None
        session_id = session_id_for(session)
        proposal_id = self._prep.proposal_id(session_id, signal_id, leg)
        if proposal_id in self._intent_records():
            report.skipped.append((instrument.isin, "already_prepared"))
            return None
        try:
            body = self._vm_body(proposal_id, instrument, side, quantity, limit, reason, batch_id)
        except Exception as exc:  # MintMismatch: a body the VM would refuse is refused here, before anything is written
            report.skipped.append((instrument.isin, f"body_invalid:{getattr(exc, 'reason', 'unknown')}"))
            return None
        try:
            samples = self._ports.quotes.samples(instrument)
        except RelayError as error:
            self._stop_for_relay_error(error)
            report.skipped.append((instrument.isin, "quote_unavailable"))
            return None
        self._mint.register(proposal_id, body)
        outcome = await self._prep.prepare(
            session_id=session_id, signal_id=signal_id, leg=leg, symbol=instrument.symbol, side=side,
            quantity=quantity, limit_price=limit, samples=samples,
        )
        report.intents.append((proposal_id, outcome.status))
        return outcome

    def _stop_for_relay_error(self, error: RelayError) -> None:
        """A relay failure outside the mint (the quote read): the table says pause or abort, never carry on."""

        verdict = vm_codes.classify_error(error)
        assert verdict.event is not None
        policy.raise_event(self._evidence, verdict.event, f"{verdict.detail} code={verdict.code} status={verdict.status}")
        if verdict.kind == vm_codes.ABORT:
            raise RunnerAbort(verdict.event)

    def _series_of(self, result: SignalResult, isin: str) -> str:
        for signal in result.signals:
            if isin in (signal.isin, signal.anchor_isin):
                return signal.series
        return "EQ"

    def _attempt_number(self, obligation_id: str) -> int:
        prefix = f"{obligation_id}-t"
        return 1 + sum(1 for pid in self._intent_records() if pid.startswith(prefix))

    async def _place_exit(
        self, session: date, as_of: date, result: SignalResult, proposal: ExitProposal, report: DecideReport
    ) -> None:
        instrument = self._by_isin[proposal.isin]
        today = session_id_for(session)
        prefix = f"{proposal.obligation_id}-t"
        if any(pid.startswith(prefix) and rec.get("session_id") == today for pid, rec in self._intent_records().items()):
            report.skipped.append((proposal.isin, "exit_already_prepared"))
            return
        ref = self._ports.reference.closes(as_of, [proposal.isin]).get(proposal.isin)
        if ref is None:
            report.skipped.append((proposal.isin, "exit_no_reference_price"))
            return
        iid = exit_intent_id(proposal.obligation_id, self._attempt_number(proposal.obligation_id))
        limit = self._limit(ref, Side.SELL, session, self._series_of(result, proposal.isin))
        await self._submit(
            session=session, instrument=instrument, side="SELL", quantity=proposal.quantity, limit=limit,
            reason=proposal.reason, batch_id=proposal.batch_id, signal_id=iid, leg=LEG_EXIT, report=report,
        )

    async def _strategy_sells(
        self, session: date, as_of: date, result: SignalResult, obligated: set[str], report: DecideReport
    ) -> None:
        """Sell a held name that fell out of the hold cutoff, unless that name already owes an exit.

        A second sell on the same shares would race the obligation (and an unrelated sell on a name with an open halve is
        ``ambiguous_exit_fill``), so it is suppressed, not queued. A name under a corporate-action or ambiguity pause is
        not sold at a quote that may be adjusted.
        """

        cutoff = self._base_inputs.strategy.params.hold_rank_cutoff
        by_isin = {s.isin: s for s in result.signals}
        frozen = self._open_pause_isins(CORPORATE_ACTION_PAUSE) | self._open_pause_isins(policy.AMBIGUOUS_EXIT_FILL)
        for holding in self._portfolio.snapshot().holdings:
            isin = holding.instrument.isin
            signal = by_isin.get(isin)
            if signal is not None and signal.rank is not None and signal.rank <= cutoff:
                continue
            if isin in obligated:
                report.skipped.append((isin, "strategy_sell_suppressed_open_exit"))
                continue
            if isin in frozen:
                report.skipped.append((isin, "strategy_sell_suppressed_pause"))
                continue
            if self._stopped_all():
                return
            ref = self._ports.reference.closes(as_of, [isin]).get(isin)
            if ref is None:
                report.skipped.append((isin, "strategy_sell_no_reference_price"))
                continue
            limit = self._limit(ref, Side.SELL, session, self._series_of(result, isin))
            await self._submit(
                session=session, instrument=holding.instrument, side="SELL", quantity=holding.quantity, limit=limit,
                reason="exit", batch_id=None, signal_id=f"swing-{as_of:%Y%m%d}-{isin}", leg=LEG_SWING, report=report,
            )

    async def _entries(
        self, session: date, as_of: date, result: SignalResult, inputs: SignalInputs, obligated: set[str],
        report: DecideReport,
    ) -> None:
        if not result.entries_possible:
            return
        params, limits = inputs.strategy.params, inputs.strategy.limits
        ranked = sorted((s for s in result.signals if s.rank is not None), key=lambda s: s.rank or 0)
        for signal in ranked[: params.max_positions]:
            if self._stopped_all():
                return
            isin = signal.isin
            instrument = self._by_isin.get(isin)
            if instrument is None or signal.suppressed is not None or signal.buy_blocked or isin in obligated:
                report.skipped.append((isin, "entry_not_eligible"))
                continue
            reasons = self._portfolio.entry_block_reasons(isin)
            if reasons:
                report.skipped.append((isin, "entry_blocked:" + ",".join(reasons)))
                if any(r in reasons for r in ("halt_latch", "pilot_ended", "stop_open", "run_ended", "run_paused", "run_aborted")):
                    return
                continue
            snap = self._portfolio.snapshot()  # reservations of the intents before this one are already in it
            held = {h.instrument.isin for h in snap.holdings} | {b.instrument.isin for b in snap.open_buys}
            if isin in held:
                continue
            if len(held) >= params.max_positions:
                return
            ref = self._ports.reference.closes(as_of, [isin]).get(isin)
            adv = self._ports.reference.adv(isin, as_of)
            if ref is None or adv is None:
                report.skipped.append((isin, "entry_no_reference_or_adv"))
                continue
            deployable = snap.cash - snap.reserved
            budget = min(Decimal(limits.per_position_cap), deployable, Decimal(params.liquidity_adv_fraction) * adv)
            quantity = int(budget // ref)
            if quantity < 1:
                report.skipped.append((isin, "entry_budget_below_one_share"))
                continue
            limit = self._limit(ref, Side.BUY, session, signal.series)
            # 62 sizes from the reference close. The guard prices the cap at the limit (a little above the close), so a
            # name sized at exactly the cap would be denied: also bound the notional at the limit by the cap.
            quantity = min(
                quantity, int(deployable / (limit * (_ONE + ENTRY_CASH_BUFFER))), int(Decimal(limits.per_position_cap) // limit)
            )
            if quantity < 1:
                report.skipped.append((isin, "entry_budget_below_one_share"))
                continue
            await self._submit(
                session=session, instrument=instrument, side="BUY", quantity=quantity, limit=limit, reason="entry",
                batch_id=None, signal_id=f"sig-{as_of:%Y%m%d}-{isin}", leg=LEG_ENTRY, report=report,
            )

    # -- hand flatten -------------------------------------------------------------------------------------------

    def hand_flatten(self, session: date, reason: str) -> DecideReport:
        """The operator flattens every holding by hand: pause entries, then sell under flatten obligation ids."""

        if not isinstance(reason, str) or not reason.strip():
            raise DecideRefused("reason_required")
        self._refuse_unless_decidable(session)
        report = DecideReport(session)
        try:
            asyncio.run(self._hand_flatten(session, reason.strip(), report))
        except RunnerAbort:
            report.status, report.stopped = "aborted", "abort"
        except (ShadowAbort, PortfolioAborted) as exc:
            self._record_abort(exc)
            report.status, report.stopped = "aborted", "abort"
        return report

    async def _hand_flatten(self, session: date, reason: str, report: DecideReport) -> None:
        if self._stopped_all():
            report.status, report.stopped = "paused", "open_pause"
            return
        as_of = previous_session(self._base_inputs.calendar, session)
        self._evidence.append("latch", {"latch": "hand_flatten", "scope": "all", "reason": "operator_requested"})
        if not any(p.event == "divergence_breach" and p.scope == policy.SCOPE_ENTRIES for p in self._state().open_pauses):
            policy.raise_event(self._evidence, "divergence_breach", HAND_FLATTEN_PAUSE_REASON)
        report.recovered = self._recover()
        report.expired = self._expire_day_orders(session, report.recovered)
        for holding in self._portfolio.snapshot().holdings:
            if self._stopped_all():
                return
            isin = holding.instrument.isin
            ref = self._ports.reference.closes(as_of, [isin]).get(isin)
            if ref is None:
                report.skipped.append((isin, "flatten_no_reference_price"))
                continue
            obligation = exit_obligation_id("flatten", isin, session)
            if any(p.startswith(f"{obligation}-t") for p in self._intent_records()):
                report.skipped.append((isin, "flatten_already_prepared"))
                continue
            iid = exit_intent_id(obligation, 1)
            limit = self._limit(ref, Side.SELL, session, "EQ")
            await self._submit(
                session=session, instrument=holding.instrument, side="SELL", quantity=holding.quantity, limit=limit,
                reason="flatten", batch_id=_hand_batch_id(session, isin, holding.quantity), signal_id=iid,
                leg=LEG_EXIT, report=report,
            )

    # -- close --------------------------------------------------------------------------------------------------

    def close(self, session: date) -> CloseReport:
        report = CloseReport(session)
        state = self._state()
        if state.aborted:
            report.outcome = "aborted"
        elif any(p.scope == policy.SCOPE_ALL for p in state.open_pauses):
            report.outcome = "paused"  # nothing is booked or expired under an open pause: resume first
        else:
            try:
                self._close_steps(session, report)
            except RunnerAbort:
                report.outcome = "aborted"
            except (ShadowAbort, PortfolioAborted) as exc:
                self._record_abort(exc)
                report.outcome = "aborted"
        self._evidence.append(
            "session_close", {"outcome": report.outcome, "record_count": len(self._evidence.records())}
        )
        report.witness = self._evidence.witness_line()
        return report

    def _close_steps(self, session: date, report: CloseReport) -> None:
        self._recover()
        records = self._evidence.records()
        decided = [str(r["intent_id"]) for r in records if r.get("kind") == E_VM_DECISION]
        attempted = {str(r["intent_id"]) for r in records if r.get("kind") in ("attempt", E_ATTEMPT_INCONCLUSIVE)}
        intents = self._intent_records()
        reservations = {str(r["intent_id"]): str(r["reservation_created_at"]) for r in records if r.get("kind") == "reservation"}
        for pid in decided:
            if pid in attempted or pid not in intents or self._stopped_all():
                continue
            record = intents[pid]
            query = FillQuery(
                pid, str(record["symbol"]), str(record["side"]), Decimal(str(record["quantity"])),
                Decimal(str(record["limit_price"])), reservations.get(pid, ""),
            )
            fill = self._ports.fills.fill(query)
            attempt_id = f"att-{pid}"[:128]
            self._evidence.append("attempt", {"intent_id": pid, "attempt_id": attempt_id, "outcome": fill.outcome, "reason": fill.reason})
            self._evidence.append(
                "fill_result", {"intent_id": pid, "attempt_id": attempt_id, "outcome": fill.outcome, "reason": fill.reason}
            )
            if fill.outcome == OUTCOME_FILLED and None not in (
                fill.quantity, fill.price, fill.charges, fill.schedule_sha256, fill.scenario_set_sha256
            ):
                self._prep.book_fill(
                    proposal_id=pid, cumulative_quantity=fill.quantity, price=fill.price, charges=fill.charges,  # type: ignore[arg-type]
                    schedule_sha256=fill.schedule_sha256, scenario_set_sha256=fill.scenario_set_sha256,  # type: ignore[arg-type]
                    attempt_id=attempt_id,
                )
                if str(record["side"]).upper() == "SELL":
                    isin = next((i.isin for i in self._by_isin.values() if i.symbol == record["symbol"]), None)
                    if isin is not None:
                        self._portfolio.on_booked_sell(isin, int(fill.quantity))  # type: ignore[arg-type]
                outcome = "booked"
            else:
                self._evidence.append("attempt_inconclusive", {"intent_id": pid, "code": fill.reason})
                outcome = OUTCOME_INCONCLUSIVE
            report.attempts.append((pid, outcome))
        report.released = self._prep.release_inconclusive()
        self._portfolio.session_end_report(session)

    # -- resume and status ---------------------------------------------------------------------------------------

    def resume(self, reason: str) -> Mapping[str, Any]:
        """The only way to clear a pause. An abort, a run end, a halt and a corporate-action pause are refused."""

        return policy.resume(self._evidence, reason)

    def status(self) -> dict[str, Any]:
        state = self._state()
        seq, head = self._evidence.head()
        return {
            "run_id": self._evidence.manifest.run_id,
            "run_class": self._evidence.manifest.run_class,
            "aborted": list(state.abort_events),
            "ended": list(state.ended_events),
            "open_pauses": [{"seq": p.seq, "event": p.event, "scope": p.scope} for p in state.open_pauses],
            "records": seq,
            "head": head,
        }


def _hand_batch_id(session: date, isin: str, quantity: int) -> str:
    digest = hashlib.sha256(f"hand|{session.isoformat()}|{isin}:{quantity}".encode("ascii")).hexdigest()[:12]
    return f"flatten-{session:%Y%m%d}-{digest}"


# --- opening a run and assembling the runner -------------------------------------------------------------------------


class OpenRunRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def open_run(
    root: Any,
    *,
    run_id: str,
    inputs: SignalInputs,
    orders: OrdersClient,
    run_class: str = "plumbing",
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    forbidden: Sequence[str] = (),
) -> EvidenceStore:
    """Create the run: the ten pins in the manifest, ``holdout_dated_inputs=True`` and the cadence and audit anchors.

    Fails closed unless both index lists were fetched on or before the window start (the S-1 cutoff is the IST fetch
    date), because the universe at S-1 is built from them.
    """

    for name in ("nifty500", "smallcap250"):
        if latest_index_list(inputs.store, name, fetched_by=inputs.window_start) is None:  # type: ignore[arg-type]
            raise OpenRunRefused("index_list_snapshot_not_seeded")
    hashes = inputs.current_hashes()
    audit = orders.audit_all()
    base_seq, base_sha = (audit[-1].seq, audit[-1].entry_sha256) if audit else (0, GENESIS)
    store = EvidenceStore.create(
        root, run_id=run_id, run_class=run_class, session_id=session_id_for(inputs.run_start_session), hashes=hashes,
        holdout_dated_inputs=True, clock=clock, forbidden=forbidden,
    )
    store.append(
        "run_start",
        {
            "run_start_session": inputs.run_start_session.isoformat(), "audit_base_seq": base_seq,
            "audit_base_sha256": base_sha, "window_start": inputs.window_start.isoformat(),
        },
    )
    return store


def assemble(
    *,
    evidence: EvidenceStore,
    ledger: ExecutionLedger,
    india_guard: IndiaAdmissionGuard,
    latch: LatchStore,
    instruments: Sequence[Instrument],
    orders: OrdersClient,
    identity: VmIdentity,
    base_inputs: SignalInputs,
    pins: PinsPort,
    ports: RunnerPorts,
    health: HealthPort,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    mint_timeout: timedelta = timedelta(seconds=10),
    adapter: Optional[ShadowAdmissionAdapter] = None,
) -> ShadowRunner:
    """Wire the runner over an opened run. The audit base and the cadence anchor come from the evidence, not the caller."""

    start = next((r for r in evidence.records() if r.get("kind") == "run_start"), None)
    if start is None:
        raise DecideRefused("run_not_opened")
    base_seq, base_sha = int(start["audit_base_seq"]), str(start["audit_base_sha256"])
    if base_inputs.run_start_session.isoformat() != start["run_start_session"]:
        raise DecideRefused("run_start_session_moved")  # a restart cannot shift the cadence
    reader = VmAuditReader(orders, evidence, base_seq=base_seq, base_sha256=base_sha)
    mint = VmMint(orders, evidence, reader, base_seq=base_seq)
    adapter = adapter or ShadowAdmissionAdapter(ledger, india_guard, clock=clock)
    preparation = RunnerPreparation(
        run_id=evidence.manifest.run_id, adapter=adapter, evidence=evidence, mint=mint, audit=reader,
        audit_base_seq=base_seq, mint_timeout=mint_timeout, clock=clock, labels=reader.labels,
    )
    portfolio = ShadowPortfolio(ledger, latch, evidence, instruments)
    return ShadowRunner(
        evidence=evidence, adapter=adapter, portfolio=portfolio, preparation=preparation, mint=mint, reader=reader,
        identity=identity, base_inputs=base_inputs, pins=pins, ports=ports, health=health, instruments=instruments,
        clock=clock,
    )


__all__ = [
    "CloseReport",
    "DecideRefused",
    "DecideReport",
    "FillOutcome",
    "FillPort",
    "FillQuery",
    "LiveTablePins",
    "NoPostSubmissionEvidence",
    "OpenRunRefused",
    "RunnerAbort",
    "RunnerPorts",
    "RunnerPreparation",
    "ShadowRunner",
    "SIMULATOR_DRAWDOWN_BASIS",
    "VmAuditReader",
    "VmIdentity",
    "VmMint",
    "assemble",
    "exit_obligation_id",
    "open_run",
    "session_date_of",
    "session_id_for",
    "verify_vm_chain",
]
