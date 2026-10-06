"""
Shared application state and models to avoid circular imports.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, List, Optional
import sqlite3
import uuid
from pydantic import BaseModel, ConfigDict
from chat_manager import ChatManager
from rag_manager import RAGManager
from mcp_client import Trading212MCPClient
from execution import (
    ExecutionDisabledError,
    ExecutionLedger,
    ExecutionService,
    LedgerError,
    LocalPaperVenue,
    OrderAck,
    OrderSide,
    QuoteEvidence,
    ReconciliationSnapshot,
    ReconciliationStatus,
    RequoteCoordinator,
    RequotePolicy,
    Workspace,
    WorkspaceMismatch,
    coerce_workspace,
)
from execution.venue import (
    PracticeCaps,
    VenueBinding,
    VenueContext,
    VenueError,
    DispatcherFactoryMap,
    execution_mode_label,
    resolve_factory,
    spec_for,
)
from private_config import PrivateConfigError, load_workspace_config
from workspace_credentials import process_workspace
from model_registry import (
    ModelRegistry,
    active_registry_error,
    active_registry_or_none,
    set_active_registry,
)
from simulation import PreFlightSimulator, RiskSwarmGate
from market_data import (
    IndiaInstrument,
    MarketDataEvent,
    MarketDataError,
    MarketDataSession,
    MarketDataSessionState,
    RegimeClassifier,
    ReplayMarketDataProvider,
    build_market_preflight_context,
)

import time

# Phase 67 (P8): no OPENAI_* environment mutation here. Every LLM call takes its
# endpoint, key and model from the model role registry, so ambient OPENAI_* and
# MAGENTIC_* variables cannot redirect a role.


class ANEConfig(BaseModel):
    enabled: bool = False
    compute_units: str = "ALL"  # CPU_ONLY | CPU_GPU | ALL

class AppState:
    """Manages global application state with lazy initialization for heavy components"""
    def __init__(self):
        self._chat_manager = None
        self._rag_manager = None
        self._mcp_client = None
        self._execution_service = None
        self._execution_ledger = None
        self._preflight_policy_connection = None
        self._market_data_session = None
        self._regime_classifier = None
        self.execution_authority = False
        self.execution_startup_error = None
        # Phase 66: the ledger's venue binding while authority is held; None for a
        # paper ledger and whenever execution is disabled.
        self.execution_venue_binding: Optional[VenueBinding] = None
        # Loaded private WorkspaceConfig for Phases 62 and 63; None until a successful start.
        self.workspace_config = None
        self.lm_studio_client = None  # Lazy init to avoid startup blocking
        self.start_time = time.time()
        # On-device ANE configuration (default off; auto-detect on startup)
        self.ane_config = ANEConfig()
        # Phase 30: High-Velocity Trade Proposals (HITL)
        self.trade_proposals: Dict[str, Any] = {}

    @property
    def model_registry(self) -> Optional[ModelRegistry]:
        """The loaded model role registry, or None when none is valid.

        Held by ``model_registry`` so call sites that must not import this
        module can read it. Independent of execution authority (P6): nothing in
        ``start_execution`` reads or waits on it.
        """
        return active_registry_or_none()

    @model_registry.setter
    def model_registry(self, value: Optional[ModelRegistry]) -> None:
        set_active_registry(value)

    @property
    def model_registry_error(self) -> Optional[str]:
        """Stable code of the last registry load failure, or None."""
        return active_registry_error()

    def load_model_registry(self, private_dir) -> bool:
        """Load ``private/models.json`` once. A failure leaves AI routes at 503."""
        from model_registry import ModelRegistryError, load_registry

        try:
            registry = load_registry(private_dir)
        except ModelRegistryError as exc:
            set_active_registry(None, exc.code)
            return False
        set_active_registry(registry)
        return True

    @property
    def chat_manager(self) -> ChatManager:
        if self._chat_manager is None:
            self._chat_manager = ChatManager()
        return self._chat_manager

    @chat_manager.setter
    def chat_manager(self, value):
        self._chat_manager = value

    @property
    def rag_manager(self) -> RAGManager:
        if self._rag_manager is None:
            self._rag_manager = RAGManager()
        return self._rag_manager

    @rag_manager.setter
    def rag_manager(self, value):
        self._rag_manager = value

    @property
    def mcp_client(self) -> Trading212MCPClient:
        if self._mcp_client is None:
            self._mcp_client = Trading212MCPClient()
        return self._mcp_client

    @mcp_client.setter
    def mcp_client(self, value):
        self._mcp_client = value

    @property
    def execution_service(self) -> ExecutionService:
        if self._execution_service is None:
            # Phase 53 containment: no production broker dispatcher is installed.
            self._execution_service = ExecutionService()
        return self._execution_service

    @execution_service.setter
    def execution_service(self, value: ExecutionService):
        self._execution_service = value

    @property
    def execution_mode(self) -> str:
        """``practice``, ``paper`` or ``disabled``: the one source for every status."""

        return execution_mode_label(self.execution_authority, self.execution_venue_binding)

    def start_execution(
        self,
        db_path,
        *,
        workspace,
        private_dir,
        dispatcher_factories: Optional[DispatcherFactoryMap] = None,
    ) -> bool:
        """Acquire local execution authority and install the venue's dispatcher.

        Every execution, paper included, needs valid private configuration for
        its workspace (decision 1). The config loads before the ledger path is
        resolved or opened, so a config failure never touches a ledger file.
        ``db_path`` may be None, meaning the venue's default ledger path.

        The venue comes from ``private/<workspace>/execution.json`` (absent
        means ``paper``). The dispatcher comes from ``dispatcher_factories``,
        default the production map, which holds ``paper`` only. An unknown or
        unregistered venue, or a practice venue outside a UK process, leaves
        execution disabled: it is never replaced by paper.
        """
        self.close_execution()
        ledger = None
        try:
            # One fail-closed handler: an unsupported workspace (ValueError), bad
            # private config, an unpinned or foreign ledger, an unavailable venue,
            # or an I/O failure leaves execution disabled instead of aborting startup.
            ws = coerce_workspace(workspace)
            config = load_workspace_config(private_dir, ws.value)
            venue = config.venue
            binding = None
            caps = None
            spec = spec_for(venue)
            if spec is not None:
                # A bound venue runs only in its spec's workspace, in a process
                # of that workspace.
                if ws.value != spec.workspace or process_workspace() != spec.workspace:
                    raise VenueError("VENUE_WORKSPACE_MISMATCH", venue)
                binding = VenueBinding(
                    venue=venue,
                    account_id=config.execution.account_id,
                    currency=config.execution.currency,
                )
                if config.uk_limits is not None:
                    caps = PracticeCaps(
                        capital_cap=config.uk_limits.capital_cap,
                        per_position_cap=config.uk_limits.per_position_cap,
                    )
            # Resolve the factory before any ledger is opened: an unregistered
            # venue must not create or touch a ledger file.
            factory = resolve_factory(venue, dispatcher_factories)
            # db_path None means the ledger's own default: the venue spec's path
            # for a bound venue, the workspace's real ledger for paper.
            ledger = ExecutionLedger(
                db_path, workspace=ws, require_approval=True, venue=binding
            )
            if binding is not None and caps is not None:
                # Both caps are stored once, and the budget equals capital_cap. A
                # change to either cap later is refused (a new ledger is needed).
                ledger.configure_venue_limits(
                    caps.capital_cap, caps.per_position_cap, workspace=ws
                )
            dispatcher = factory(
                VenueContext(workspace=ws, venue=venue, binding=binding, caps=caps)
            )
        except (
            LedgerError,
            OSError,
            sqlite3.Error,
            ValueError,
            PrivateConfigError,
            VenueError,
        ) as exc:
            if ledger is not None:
                ledger.close()
            self._execution_service = ExecutionService()
            self.execution_authority = False
            self.execution_venue_binding = None
            self.workspace_config = None
            # Error text carries codes, field names and paths, never config values.
            self.execution_startup_error = f"{type(exc).__name__}: {exc}"
            return False
        self._execution_ledger = ledger
        self.execution_venue_binding = binding
        self._preflight_policy_connection = self._local_preflight_policy_connection()
        self._execution_service = ExecutionService(
            dispatcher,
            ledger,
            require_approval=True,
            simulator=PreFlightSimulator(),
            risk_gate=RiskSwarmGate(),
            require_runtime_preflight=True,
        )
        self.execution_authority = True
        self.workspace_config = config
        self.execution_startup_error = None
        return True

    def close_execution(self) -> None:
        if self._preflight_policy_connection is not None:
            self._preflight_policy_connection.close()
        self._preflight_policy_connection = None
        if self._execution_ledger is not None:
            self._execution_ledger.close()
        self._execution_ledger = None
        self._execution_service = None
        self.execution_authority = False
        self.execution_venue_binding = None
        self.workspace_config = None

    def market_data_status(self) -> Dict[str, Any]:
        session = self._market_data_session
        return {
            "state": (
                MarketDataSessionState.STOPPED.value
                if session is None
                else session.state.value
            ),
            "provider": None if session is None else session.provider_name,
            "instruments": (
                []
                if session is None
                else [instrument.model_dump(mode="json") for instrument in session.instruments]
            ),
            "read_only": True,
        }

    async def start_market_data_replay(
        self,
        instruments: tuple[IndiaInstrument, ...],
        events: tuple[MarketDataEvent, ...],
    ) -> Dict[str, Any]:
        """Explicitly load a finite normalized replay; never contact a provider."""

        if (
            self._market_data_session is not None
            and self._market_data_session.state is not MarketDataSessionState.STOPPED
        ):
            raise MarketDataError(
                "SESSION_STATE_CONFLICT",
                "market-data session is already active",
            )
        session = MarketDataSession(ReplayMarketDataProvider(events))
        self._market_data_session = session
        await session.start(instruments)
        while await session.poll_once() is not None:
            pass
        return self.market_data_status()

    async def close_market_data(self) -> None:
        session = self._market_data_session
        if session is not None:
            await session.stop()
        self._market_data_session = None

    def market_data_snapshot(self, instrument: IndiaInstrument):
        session = self._market_data_session
        if session is None:
            raise MarketDataError(
                "SESSION_NOT_RUNNING",
                "market-data session is not running",
            )
        return session.snapshot(instrument)

    def admit_india_paper_proposal(
        self,
        proposal: Dict[str, Any],
        *,
        instrument: IndiaInstrument,
        portfolio_state: Dict[str, Any],
    ):
        """Run canonical Phase 54 admission from the active Phase 55 snapshot."""

        if (
            not self.execution_authority
            or self._execution_ledger is None
            or self._preflight_policy_connection is None
        ):
            raise LedgerError("local paper execution authority is unavailable")
        if self._execution_ledger.workspace != Workspace.INDIA:
            raise LedgerError("India market admission requires the India execution workspace")
        intent = self.execution_service.register_proposal(proposal)
        session = self._market_data_session
        if session is None:
            return self.execution_service.admit(intent, currency="INR")
        try:
            classifier = self._regime_classifier or RegimeClassifier()
            self._regime_classifier = classifier
            regime = classifier.evidence(session, instrument)
            context = build_market_preflight_context(
                session,
                intent=intent,
                instrument=instrument,
                regime=regime,
            )
        except MarketDataError as exc:
            return self.execution_service.admit(intent, currency="INR", deny_reason=exc.code)
        return self.execution_service.prepare(
            intent,
            currency="INR",
            portfolio_state=portfolio_state,
            risk_db_connection=self._preflight_policy_connection,
            **context.execution_kwargs(),
        )

    def prepare_india_paper_local(self, *, symbol: str, quantity: str):
        """Create a server-owned PAPER intent; this stops before approval/dispatch."""
        if self._execution_ledger is None:
            raise LedgerError("local paper execution authority is unavailable")
        instrument = IndiaInstrument(symbol=symbol)
        proposal = {
            "proposal_id": str(uuid.uuid4()),
            "client_order_id": f"india-paper-local-{uuid.uuid4()}",
            # Decision 2: the server stamps the workspace from the open ledger.
            # A non-India ledger is then refused by admit_india_paper_proposal.
            "workspace": self._execution_ledger.workspace.value,
            "account": "paper", "broker": "paper", "mode": "PAPER",
            "ticker": instrument.execution_ticker, "action": "BUY", "quantity": quantity,
            "reasoning": "Explicit local India paper preparation. No broker is contacted.",
            "status": "PENDING",
        }
        admission = self.admit_india_paper_proposal(
            proposal, instrument=instrument,
            portfolio_state={"equity": 100000.0, "peak_equity": 100000.0},
        )
        return proposal, admission

    @staticmethod
    def _local_preflight_policy_connection():
        connection = sqlite3.connect(":memory:")
        connection.execute(
            "CREATE TABLE scaling_policies (regime_id INTEGER PRIMARY KEY, scale_multiplier REAL NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO scaling_policies (regime_id, scale_multiplier) VALUES (?, ?)",
            ((0, 1.0), (1, 0.5), (2, 0.1), (3, 0.05)),
        )
        return connection

    def _local_paper_preflight(self) -> Dict[str, Any]:
        if self._preflight_policy_connection is None:
            raise LedgerError("local preflight policy is unavailable")
        return {
            "tick_window": {"bid": [0.99], "ask": [1.01], "spread": [0.02]},
            "portfolio_state": {"equity": 100.0, "peak_equity": 100.0},
            "regime_id": 0,
            "current_spread_pct": 0.02,
            "risk_db_connection": self._preflight_policy_connection,
        }

    def register_trade_proposal(self, proposal: Dict[str, Any]) -> None:
        """Persist executable fields before exposing a proposal to the UI.

        Decision 2: the server stamps the workspace from the open ledger and
        rejects any mismatch. With no open ledger the proposal is rejected.
        """
        ledger = self._execution_ledger
        if not self.execution_authority or ledger is None:
            raise ExecutionDisabledError("no open execution ledger; proposal rejected")
        if "workspace" not in proposal:
            proposal["workspace"] = ledger.workspace.value
        elif proposal["workspace"] != ledger.workspace:
            # Includes the audit-only marker "unscoped" (D3): it is never a workspace.
            raise WorkspaceMismatch(
                f"proposal names a workspace other than the open ledger's ({ledger.workspace.value})"
            )
        self.execution_service.register_proposal(proposal)
        self.trade_proposals[str(proposal["proposal_id"])] = proposal

    def get_trade_proposal(self, proposal_id: str) -> Optional[Dict[str, Any]]:
        durable = self.execution_service.get_proposal(proposal_id)
        if durable is not None:
            existing = self.trade_proposals.get(proposal_id, {})
            existing.update(durable)
            self.trade_proposals[proposal_id] = existing
            return existing
        return self.trade_proposals.get(proposal_id)

    def create_paper_approval_check(self) -> Dict[str, Any]:
        """Create one explicitly bounded local-only proposal for approval UAT."""

        if not self.execution_authority or self._execution_ledger is None:
            raise LedgerError("local paper execution authority is unavailable")
        # The UAT builder is UK-only (GBP budget): it must never write into an
        # India ledger, and never into a practice ledger.
        self._execution_ledger.require_workspace(Workspace.UK)
        if self._execution_ledger.venue_binding is not None:
            raise LedgerError("paper approval check is unavailable in a practice ledger")
        # Re-open the frozen pending review rather than allocating another UAT
        # proposal. Older `paper-uat` entries are included for recovery from
        # the first implementation; neither path can reach a real broker.
        for account in ("paper-uat-v2", "paper-uat"):
            pending_id = self._execution_ledger.find_active_pending_reservation(
                account, workspace=Workspace.UK
            )
            if pending_id is not None:
                existing = self.get_trade_proposal(pending_id)
                if existing is not None:
                    return existing
        proposal = {
            "proposal_id": str(uuid.uuid4()),
            "client_order_id": f"paper-approval-uat-{uuid.uuid4()}",
            "workspace": Workspace.UK.value,
            "account": "paper-uat-v2",
            "broker": "paper",
            "mode": "PAPER",
            "ticker": "PAPER-UAT",
            "action": "BUY",
            "quantity": "1",
            "reasoning": "Local-only paper approval integrity check. No broker is contacted.",
            "status": "PENDING",
        }
        # This is an intentionally tiny, immutable UAT budget. It is distinct
        # from every user account and cannot authorize a real broker order.
        self._execution_ledger.configure_paper_budget(
            "paper-uat-v2", "GBP", "1", workspace=Workspace.UK
        )
        admission = self.execution_service.prepare(
            proposal,
            currency="GBP",
            price="1",
            **self._local_paper_preflight(),
        )
        if admission.decision.value != "ADMITTED":
            raise LedgerError("paper approval check was not admitted")
        self.trade_proposals[proposal["proposal_id"]] = proposal
        return proposal

    def create_paper_requote_check(self) -> Dict[str, Any]:
        """Build one fixture replacement and stop before any dispatch boundary.

        This is deliberately a local-only manual-UAT adapter.  It creates a
        synthetic acknowledged parent, reconciles a zero-fill cancellation,
        creates the fresh LIMIT replacement, then runs fresh admission and
        reservation.  It never calls a dispatcher or creates broker traffic.
        """

        if not self.execution_authority or self._execution_ledger is None:
            raise LedgerError("local paper execution authority is unavailable")
        self._execution_ledger.require_workspace(Workspace.UK)
        if self._execution_ledger.venue_binding is not None:
            raise LedgerError("re-quote check is unavailable in a practice ledger")
        account = "paper-requote-uat-v1"
        pending_id = self._execution_ledger.find_active_pending_reservation(
            account, workspace=Workspace.UK
        )
        if pending_id is not None:
            existing = self.get_trade_proposal(pending_id)
            if existing is not None:
                return existing

        now = datetime.now(timezone.utc)
        parent_id = str(uuid.uuid4())
        parent = {
            "proposal_id": parent_id,
            "client_order_id": f"paper-requote-parent-{uuid.uuid4()}",
            "workspace": Workspace.UK.value,
            "account": account,
            "broker": "paper",
            "mode": "PAPER",
            "ticker": "PAPER-REQUOTE-UAT",
            "action": "BUY",
            "quantity": "2",
            "reasoning": "Local fixture parent for re-quote signing UAT. No broker is contacted.",
            "status": "PENDING",
        }
        # The cancelled parent releases its reservation before the replacement
        # takes one. This isolated budget cannot be used by a real account.
        self._execution_ledger.configure_paper_budget(
            account, "GBP", "2", workspace=Workspace.UK
        )
        admission = self.execution_service.prepare(
            parent,
            currency="GBP",
            price="1",
            evidence_at=now,
            **self._local_paper_preflight(),
        )
        if admission.decision.value != "ADMITTED":
            raise LedgerError("local re-quote parent was not admitted")
        parent_ack = OrderAck(
            proposal_id=parent_id,
            broker="paper",
            broker_order_id=f"local-requote-parent-{parent_id}",
        )
        self._execution_ledger.acknowledge_local_requote_fixture(parent_id, parent_ack)
        evaluated = RequoteCoordinator(self._execution_ledger).evaluate_and_record(
            proposal_id=parent_id,
            side=OrderSide.BUY,
            evidence=QuoteEvidence(
                bid=Decimal("1"), ask=Decimal("1"), volatility=Decimal("0"),
                cost=Decimal("0"), tick_size=Decimal("0.01"), regime_id=0,
                observed_at=now, source="local-requote-uat-fixture",
            ),
            policy=RequotePolicy(max_age_seconds=30),
            venue=LocalPaperVenue(),
            now=now,
        )
        self.execution_service.reconcile(
            ReconciliationSnapshot(
                proposal_id=parent_id,
                broker_order_id=parent_ack.broker_order_id,
                source="local-requote-uat-fixture",
                cumulative_quantity="0",
                cumulative_notional="0",
                status=ReconciliationStatus.CANCELLED,
                evidence_fingerprint=f"local-requote-cancel:{parent_id}",
                observed_at=now,
            )
        )
        replacement_id = str(uuid.uuid4())
        prepared = RequoteCoordinator(self._execution_ledger).prepare_replacement(
            requote_id=evaluated.record.requote_id,
            replacement_proposal_id=replacement_id,
            now=now,
        )
        replacement_admission = self.execution_service.prepare(
            prepared.intent,
            currency="GBP",
            price=prepared.intent.limit_price,
            evidence_at=now,
            **self._local_paper_preflight(),
        )
        if replacement_admission.decision.value != "ADMITTED":
            raise LedgerError("local re-quote replacement was not admitted")
        proposal = self.get_trade_proposal(replacement_id)
        if proposal is None:
            raise LedgerError("local re-quote replacement was not persisted")
        proposal["reasoning"] = (
            "Local-only replacement after reconciled cancellation. "
            "Signing verifies the frozen LIMIT fields; it cannot dispatch."
        )
        self.trade_proposals[replacement_id] = proposal
        return proposal

    def is_paper_requote_check(self, proposal_id: str) -> bool:
        proposal = self.get_trade_proposal(proposal_id)
        return bool(proposal and proposal.get("account") == "paper-requote-uat-v1")

class AccountContext:
    """
    Manages the active Trading212 account context (Invest vs ISA).
    This allows the backend to be stateful regarding which account is being viewed/acted upon.
    """
    def __init__(self):
        self._active_account: str = "invest" # Default to invest
        
    def get_active_account(self) -> str:
        return self._active_account

    def set_active_account(self, account_type: str):
        acc_type = account_type.lower() if account_type else "invest"
        if acc_type not in ["invest", "isa"]:
            # 'all' is valid for querying but not for setting active viewing state in some contexts,
            # but usually we want to switch between specific accounts. 
            # If the UI allows "All", we should permit it, but typically T212 is one or the other.
            # Allowing "all" for flexibility if needed, but primary use is Invest/ISA.
            if acc_type != "all":
                raise ValueError(f"Invalid account type: {account_type}. Must be 'invest' or 'isa'.")
        self._active_account = acc_type
        
    def get_account_or_default(self, requested_account: Optional[str]) -> str:
        """
        Returns the requested account if provided, otherwise returns the active account.
        Used by API endpoints to determine which account to target.
        """
        if requested_account:
            return requested_account.lower()
        return self._active_account

# Global state instances
state = AppState()
account_context = AccountContext()

# Request Models
class ChatMessage(BaseModel):
    """Chat request. No model, provider or key fields: the registry decides (P9)."""

    model_config = ConfigDict(extra="forbid")

    message: str
    conversation_id: Optional[str] = None
    account_type: Optional[str] = None  # None = ask user interactively
    images: Optional[List[str]] = None

class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    account_type: Optional[str] = None  # None = ask user interactively

class AgentResponse(BaseModel):
    messages: List[Dict[str, Any]]
    final_answer: str

class T212ConfigRequest(BaseModel):
    account_type: str
    invest_key: Optional[str] = None
    invest_secret: Optional[str] = None
    isa_key: Optional[str] = None
    isa_secret: Optional[str] = None
