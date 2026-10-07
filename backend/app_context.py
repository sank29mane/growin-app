"""
Shared application state and models to avoid circular imports.
"""

import hashlib
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Callable, Dict, List, Mapping, Optional
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
    PRICE_SOURCE_OPERATOR_RECORDED,
    PracticeCaps,
    VenueBinding,
    VenueContext,
    VenueError,
    DispatcherFactoryMap,
    execution_mode_label,
    resolve_factory,
    spec_for,
)
from execution.india_guard import IndiaAdmissionGuard, IndiaQuoteEvidence
from private_config import PrivateConfigError, load_workspace_config
from risk_india.drawdown import SessionResult, pending_batches
from risk_india.exits import ExitBatch, Position
from workspace_credentials import process_workspace
from model_registry import (
    ModelRegistry,
    active_registry_error,
    active_registry_or_none,
    set_active_registry,
)
from simulation import PreFlightSimulator, RiskSwarmGate
from simulation.regime_severity import RegimeSeverityMap, build_scaling_policy_connection
from market_data import (
    IndiaInstrument,
    MarketDataEvent,
    MarketDataError,
    MarketDataSession,
    MarketDataSessionState,
    RegimeClassifier,
    ReplayMarketDataProvider,
    TopOfBook,
    UkInstrument,
    build_market_preflight_context,
)
from market_data.admission import (
    RecordedQuoteReading,
    parse_max_slippage_bps,
    practice_notional,
    slippage_denial,
)
import re
from typing import Sequence

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
        # Phase 66: the bound venue's dispatcher (the practice adapter), kept so
        # the practice routes can reach its transport, metadata cache and
        # reconciler. None for paper and whenever execution is disabled.
        self.venue_adapter: Any = None
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
        allow_test_price_sources: bool = False,
        india_clock: Optional[Callable[[], datetime]] = None,
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

        India (Phase 63-04, P-15) also needs a valid ``private/india/execution.json``
        (collar and slippage cap), and installs the Mac's India limits on admission.
        ``india_clock`` is a test seam for that guard's IST-aware clock.
        """
        self.close_execution()
        ledger = None
        try:
            # One fail-closed handler: an unsupported workspace (ValueError), bad
            # private config, an unpinned or foreign ledger, an unavailable venue,
            # or an I/O failure leaves execution disabled instead of aborting startup.
            ws = coerce_workspace(workspace)
            # India execution authority needs a valid private/india/execution.json (P-15).
            config = load_workspace_config(
                private_dir, ws.value, require_india_execution=ws is Workspace.INDIA
            )
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
            # The model's severity ordering feeds the size policy every admission uses. A
            # model that cannot be loaded or ordered leaves execution disabled.
            severity_map = self._regime_severity_map()
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
            # The Mac's own India limits, latch file and slippage cap (63-04). A config or
            # limits error here leaves execution disabled, like every other start failure.
            india_guard = None
            if ws is Workspace.INDIA:
                clock_args = {} if india_clock is None else {"clock": india_clock}
                india_guard = IndiaAdmissionGuard.from_config(config, ledger, **clock_args)
        except (
            LedgerError,
            OSError,
            sqlite3.Error,
            ValueError,
            PrivateConfigError,
            VenueError,
            MarketDataError,
        ) as exc:
            if ledger is not None:
                ledger.close()
            self._execution_service = ExecutionService()
            self.execution_authority = False
            self.execution_venue_binding = None
            self.venue_adapter = None
            self.workspace_config = None
            # Error text carries codes, field names and paths, never config values.
            if isinstance(exc, MarketDataError):
                # The regime model's stable refusal code (for example REGIME_SEVERITY_AMBIGUOUS).
                self.execution_startup_error = f"{type(exc).__name__}: {exc.code}"
            else:
                self.execution_startup_error = f"{type(exc).__name__}: {exc}"
            return False
        self._execution_ledger = ledger
        self.execution_venue_binding = binding
        self.venue_adapter = dispatcher if binding is not None else None
        attach = getattr(dispatcher, "attach", None)
        if binding is not None and callable(attach):
            attach(ledger)
        self._preflight_policy_connection = self._local_preflight_policy_connection(severity_map)
        self._execution_service = ExecutionService(
            dispatcher,
            ledger,
            require_approval=True,
            simulator=PreFlightSimulator(),
            risk_gate=RiskSwarmGate(),
            require_runtime_preflight=True,
            allow_test_price_sources=allow_test_price_sources,
            india_guard=india_guard,
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
        self.venue_adapter = None
        self.workspace_config = None

    async def verify_execution_ready(self) -> bool:
        """Prove the broker account behind a bound venue (D-13); disable execution if it fails.

        Paper and test doubles have nothing to prove and return True. A practice
        adapter must see ``account/summary`` name the bound account id and
        currency. On any failure the ledger is closed and execution stays
        disabled with the stable code in ``execution_startup_error``; approve then
        answers 503 because no dispatcher is installed.
        """

        adapter = self.venue_adapter
        verify = getattr(adapter, "verify_account", None)
        if adapter is None or not callable(verify):
            return True
        try:
            await verify()
        except Exception as exc:  # noqa: BLE001 - any failure leaves execution disabled
            code = getattr(exc, "code", "PIN_FAILED")
            self.close_execution()
            self._execution_service = ExecutionService()
            self.execution_startup_error = f"PracticePinError: {code}"
            return False
        return True

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
        quote: Optional[IndiaQuoteEvidence] = None,
    ):
        """Run canonical Phase 54 admission from the active Phase 55 snapshot.

        ``quote`` carries the India rule inputs (63-04): circuits, tick reference, bid and ask
        with the time they were read. Without a fresh one the Mac's India limits deny the
        order ``quote_unavailable``. A halve, flatten or stop SELL registered by
        ``register_india_exit_batches`` comes through here too.
        """

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
            return self.execution_service.admit(intent, currency="INR", india_quote=quote)
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
            return self.execution_service.admit(
                intent, currency="INR", deny_reason=exc.code, india_quote=quote
            )
        return self.execution_service.prepare(
            intent,
            currency="INR",
            portfolio_state=portfolio_state,
            risk_db_connection=self._preflight_policy_connection,
            india_quote=quote,
            **context.execution_kwargs(),
        )

    # --- UK practice admission (Phase 66-04, D-02, D-03, D-18, D-19, D-20, D-26) -----

    # The recorded-quote freshness window. It is the same 30 s the execution service
    # applies to any admission evidence: there is no wider window for operator quotes.
    UK_PRACTICE_MAX_AGE_SECONDS = 30
    UK_PRACTICE_MAX_READINGS = 20
    _UK_TICKER = re.compile(r"^[A-Za-z0-9._-]{1,32}$")

    async def admit_uk_practice_proposal(
        self,
        *,
        ticker: str,
        side: str,
        quantity: int,
        limit_price: Decimal,
        readings: Sequence[RecordedQuoteReading],
        price_source: str = PRICE_SOURCE_OPERATOR_RECORDED,
        now: Optional[datetime] = None,
    ):
        """Admit one UK practice order from a recorded-quote replay, or record a denial.

        Server-owned: workspace, account, broker and mode are stamped from the open
        ledger's binding and never come from the caller. The price evidence is a
        replay of the operator's recorded readings (D-02); the order passes the same
        PreFlightSimulator, RiskSwarmGate and RegimeClassifier India uses. Every
        refusal is recorded as a DENIED admission with a stable reason code and
        creates no reservation. Returns ``(proposal, admission)``.
        """

        ledger = self._execution_ledger
        binding = self.execution_venue_binding
        adapter = self.venue_adapter
        if (
            not self.execution_authority
            or ledger is None
            or binding is None
            or self._preflight_policy_connection is None
            or ledger.workspace != Workspace.UK
        ):
            raise LedgerError("practice execution authority is unavailable")
        if getattr(adapter, "execution_ready", True) is False:
            raise LedgerError("the practice account is not verified")
        side = str(side).upper()
        if side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        if not isinstance(quantity, int) or isinstance(quantity, bool) or quantity <= 0:
            raise ValueError("quantity must be a whole number of shares")
        if not isinstance(ticker, str) or self._UK_TICKER.fullmatch(ticker) is None:
            raise ValueError("ticker is not a Trading 212 ticker")
        limit_price = Decimal(limit_price)
        if not limit_price.is_finite() or limit_price <= 0:
            raise ValueError("limit price must be positive")
        checked_now = now or datetime.now(timezone.utc)

        proposal = {
            "proposal_id": str(uuid.uuid4()),
            "client_order_id": f"uk-practice-{uuid.uuid4()}",
            # Decision 2: stamped from the ledger and its venue binding, never the caller.
            "workspace": ledger.workspace.value,
            "account": binding.account_id,
            "broker": binding.venue,
            "mode": "PRACTICE",
            "ticker": ticker,
            "action": side,
            "quantity": str(quantity),
            "order_type": "LIMIT",
            "limit_price": format(limit_price, "f"),
            "reasoning": (
                "UK practice order from operator-recorded quotes. Approval with Touch ID "
                "sends one LIMIT DAY order to the Trading 212 demo account."
            ),
            "status": "PENDING",
        }
        intent = self.execution_service.register_proposal(proposal)
        self.trade_proposals[proposal["proposal_id"]] = proposal

        denial, kwargs = await self._uk_practice_preflight(
            intent, readings=readings, price_source=price_source, now=checked_now
        )
        if denial is not None:
            admission = self.execution_service.admit(
                proposal,
                currency="GBP",
                deny_reason=denial,
                price_source=price_source,
                max_age_seconds=self.UK_PRACTICE_MAX_AGE_SECONDS,
            )
            return proposal, admission
        admission = self.execution_service.prepare(proposal, currency="GBP", **kwargs)
        return proposal, admission

    async def _uk_practice_preflight(
        self,
        intent,
        *,
        readings: Sequence[RecordedQuoteReading],
        price_source: str,
        now: datetime,
    ):
        """Every UK practice check that precedes the shared simulator and risk gate.

        Returns ``(denial_code, None)`` or ``(None, admit_kwargs)``. Nothing here
        reserves anything; the reservation transaction re-checks the caps itself.
        """

        ledger = self._execution_ledger
        binding = self.execution_venue_binding
        adapter = self.venue_adapter
        side = intent.side.value
        if price_source not in self.execution_service.admissible_price_sources:
            return "PRICE_SOURCE_NOT_ADMISSIBLE", None

        metadata = getattr(adapter, "metadata", None)
        if metadata is None:
            return "METADATA_UNAVAILABLE", None
        try:
            await metadata.refresh_if_stale()
        except Exception:  # noqa: BLE001 - an unreadable cache is a denial, not a crash
            pass
        if not metadata.fresh():
            return "METADATA_UNAVAILABLE", None
        info = metadata.instrument(intent.ticker)
        if info is None:
            return "INSTRUMENT_UNKNOWN", None
        if info.currency_code not in ("GBP", "GBX"):
            return "CURRENCY_NOT_ADMISSIBLE", None
        if info.max_open_quantity is None or intent.quantity > info.max_open_quantity:
            return "QUANTITY_OVER_MAX_OPEN", None
        if not metadata.exchange_open(info.working_schedule_id):
            return "EXCHANGE_CLOSED", None
        instrument = UkInstrument(symbol=intent.ticker, currency=info.currency_code)

        if not readings or len(readings) > self.UK_PRACTICE_MAX_READINGS:
            return "REGIME_WINDOW_INSUFFICIENT", None
        if any(reading.bid is None or reading.ask is None for reading in readings):
            return "SLIPPAGE_QUOTE_UNAVAILABLE", None
        ordered = sorted(readings, key=lambda reading: reading.observed_at)
        stamps = [reading.observed_at for reading in ordered]
        if len(set(stamps)) != len(stamps):
            # Three copies of one reading are not three readings.
            return "QUOTE_READINGS_NOT_DISTINCT", None
        try:
            events = tuple(
                TopOfBook(
                    instrument=instrument,
                    source=price_source,
                    observed_at=reading.observed_at,
                    received_at=reading.observed_at,
                    bid=reading.bid,
                    ask=reading.ask,
                    sequence=index,
                )
                for index, reading in enumerate(ordered, start=1)
            )
        except ValueError:
            return "QUOTE_INVALID", None

        session = MarketDataSession(
            ReplayMarketDataProvider(events, name=price_source),
            max_age_seconds=self.UK_PRACTICE_MAX_AGE_SECONDS,
            clock=lambda: now,
        )
        try:
            await session.start((instrument,))
            while await session.poll_once() is not None:
                pass
            classifier = self._regime_classifier or RegimeClassifier()
            self._regime_classifier = classifier
            regime = classifier.evidence(session, instrument, now=now)
            context = build_market_preflight_context(
                session,
                intent=intent,
                instrument=instrument,
                regime=regime,
                now=now,
                max_regime_age_seconds=float(self.UK_PRACTICE_MAX_AGE_SECONDS),
            )
        except MarketDataError as exc:
            return exc.code, None
        finally:
            await session.stop()

        # D-26: side-adjusted slippage against the recorded quote. A missing cap or
        # quote denies. The cap is private config, parsed here at admission.
        execution_config = getattr(self.workspace_config, "execution", None)
        cap = parse_max_slippage_bps(getattr(execution_config, "max_slippage_bps", None))
        denial = slippage_denial(
            side, intent.limit_price, context.snapshot.bid, context.snapshot.ask, cap
        )
        if denial is not None:
            return denial, None

        limits = ledger.get_venue_limits()
        if limits is None:
            return "VENUE_LIMITS_UNAVAILABLE", None
        capital_cap, per_position_cap = limits
        divisor = instrument.price_divisor
        # The worst case an order can cost is its LIMIT, never the mid or the simulator
        # fill. One pinned price feeds both this pre-check and the admission, so the
        # ledger's own cap check reserves exactly what is checked here.
        pinned_price = intent.limit_price / divisor
        notional = practice_notional(intent.quantity, intent.limit_price, divisor)
        headroom = ledger.practice_headroom(binding.account_id, binding.currency, intent.ticker)
        broker_available = None
        if side == "BUY":
            held = headroom["held_notional"] + headroom["open_buy_notional"]
            if held + notional > per_position_cap:
                return "PER_POSITION_CAP_EXCEEDED", None
            if notional > headroom["budget_available"]:
                return "BUDGET_EXHAUSTED", None
        else:
            if headroom["held_quantity"] - headroom["open_sell_quantity"] < intent.quantity:
                return "POSITION_UNAVAILABLE", None
            reader = getattr(adapter, "broker_available_quantity", None)
            if not callable(reader):
                return "BROKER_POSITION_UNAVAILABLE", None
            try:
                broker_available = await reader(intent.ticker)
            except Exception:  # noqa: BLE001 - a failed read denies, it never guesses
                return "BROKER_POSITION_UNAVAILABLE", None
            if broker_available < intent.quantity:
                return "BROKER_QUANTITY_INSUFFICIENT", None

        scale = float(divisor)
        window = {
            name: [value / scale for value in values]
            for name, values in context.tick_window.items()
        }
        # The simulator and risk gate see pounds, matching the price and the notional.
        window["spread"] = list(context.tick_window["spread"])
        kwargs = {
            "price": pinned_price,
            "price_divisor": divisor,
            "price_source": price_source,
            "tick_window": window,
            "regime_id": context.regime.regime_id,
            "regime_policy_hash": context.regime.policy_hash,
            "regime_audit": context.regime.audit(),
            "current_spread_pct": context.snapshot.spread_pct,
            "evidence_at": context.evidence_at,
            "portfolio_state": {
                "equity": float(capital_cap),
                "peak_equity": float(capital_cap),
            },
            "risk_db_connection": self._preflight_policy_connection,
            "max_age_seconds": self.UK_PRACTICE_MAX_AGE_SECONDS,
        }
        if broker_available is not None:
            kwargs["broker_available_quantity"] = broker_available
        return None, kwargs

    def prepare_india_paper_local(
        self,
        *,
        symbol: str,
        quantity: str,
        limit_price: Optional[Decimal] = None,
        quote: Optional[IndiaQuoteEvidence] = None,
    ):
        """Create a server-owned PAPER intent; this stops before approval/dispatch.

        The Mac's India limits need a LIMIT price and a fresh quote (63-04). Without them the
        admission is recorded DENIED (``intent_invalid`` or ``quote_unavailable``).
        """
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
        if limit_price is not None:
            proposal["order_type"] = "LIMIT"
            proposal["limit_price"] = format(Decimal(limit_price), "f")
        admission = self.admit_india_paper_proposal(
            proposal, instrument=instrument,
            portfolio_state={"equity": 100000.0, "peak_equity": 100000.0},
            quote=quote,
        )
        return proposal, admission

    # --- India Option B latches and exit batches (Phase 63-04, D-05 to D-08) -------------

    def _india_authority(self):
        ledger = self._execution_ledger
        service = self._execution_service
        guard = service.india_guard if service is not None else None
        if (
            not self.execution_authority
            or ledger is None
            or guard is None
            or ledger.workspace != Workspace.INDIA
        ):
            raise LedgerError("India execution authority is unavailable")
        return ledger, guard

    @staticmethod
    def _india_positions(view) -> list[Position]:
        positions = []
        for ticker, quantity, cost in view.positions:
            if quantity != quantity.to_integral_value():
                raise LedgerError("India positions are whole shares")
            positions.append(
                Position(
                    isin=ticker,
                    stock_code=ticker.rsplit(":", 1)[-1],
                    quantity=int(quantity),
                    cost=cost,
                )
            )
        return positions

    def evaluate_india_session(
        self, marks: Mapping[str, Decimal], session_date: date, *, cash: Decimal
    ) -> SessionResult:
        """Apply one session close to the Mac's Option B latches and persist them.

        ``marks`` are the closes by execution ticker (``NSE:CASH:SYMBOL``); a held position
        with no mark raises and writes nothing. ``cash`` is the pilot's cash after charges.
        The Mac ledger cannot derive it (it does not book SELL fills or charges), so the
        caller supplies it: 63-05 takes it from the VM, tests pass it explicitly. The result
        carries the halve, flatten and stop batches newly raised, to be registered with
        ``register_india_exit_batches``.
        """

        ledger, guard = self._india_authority()
        positions = self._india_positions(ledger.india_account_view())
        return guard.store.evaluate_session(marks, session_date, cash=cash, positions=positions)

    def pending_india_exit_batches(self, decided_on: date) -> tuple[ExitBatch, ...]:
        """Every open exit rebuilt for the next session (a missed limit is re-issued, not dropped)."""

        ledger, guard = self._india_authority()
        state = guard.store.load()
        return pending_batches(state, self._india_positions(ledger.india_account_view()), decided_on)

    def register_india_exit_batches(
        self, batches, *, limit_prices: Mapping[str, Decimal]
    ) -> list[Dict[str, Any]]:
        """Register one SELL proposal per position of each batch, sharing the batch id.

        ``limit_prices`` maps an execution ticker to the limit price chosen now, inside the
        collar and the circuit band (the exit intents carry none, so it is never stale). A
        missing or non-positive price refuses the whole call before anything is registered.
        Admission re-checks the collar, band, tick and session when the SELL is admitted. The
        batch id and reason are stored beside each proposal as a ledger event, not in the
        intent, so intent hashes do not change. Registering the same batch again is a no-op.
        """

        ledger, _guard = self._india_authority()
        planned = []
        for batch in batches:
            for exit_intent in batch.intents:
                price = limit_prices.get(exit_intent.isin)
                if not isinstance(price, Decimal) or not price.is_finite() or price <= 0:
                    raise ValueError(f"a positive limit price is required for {exit_intent.isin}")
                planned.append((batch, exit_intent, price))
        proposals: list[Dict[str, Any]] = []
        for batch, exit_intent, price in planned:
            slug = hashlib.sha256(exit_intent.isin.encode("utf-8")).hexdigest()[:10]
            proposal_id = f"exit-{batch.batch_id}-{slug}"
            proposal = {
                "proposal_id": proposal_id,
                "client_order_id": f"india-{proposal_id}",
                "workspace": ledger.workspace.value,
                "account": "paper",
                "broker": "paper",
                "mode": "PAPER",
                "ticker": exit_intent.isin,
                "action": "SELL",
                "quantity": str(exit_intent.quantity),
                "order_type": "LIMIT",
                "limit_price": format(price, "f"),
                "reasoning": (
                    f"Mac Option B {batch.reason} exit: sell {exit_intent.quantity} "
                    f"{exit_intent.stock_code}. Each order needs its own Touch ID approval."
                ),
                "status": "PENDING",
            }
            self.execution_service.register_proposal(proposal)
            ledger.record_exit_batch(proposal_id, batch.batch_id, batch.reason)
            self.trade_proposals[proposal_id] = proposal
            proposals.append(self.get_trade_proposal(proposal_id) or proposal)
        return proposals

    @staticmethod
    def _local_preflight_policy_connection(severity_map: Optional[RegimeSeverityMap] = None):
        """The size-policy tables for one model's severity map (the shipped model by default)."""

        return build_scaling_policy_connection(severity_map or RegimeClassifier().severity_map)

    def _regime_severity_map(self) -> RegimeSeverityMap:
        """The loaded model's artifact-bound severity ordering (one classifier per process)."""

        classifier = self._regime_classifier or RegimeClassifier()
        self._regime_classifier = classifier
        return classifier.severity_map

    def _local_paper_preflight(self) -> Dict[str, Any]:
        if self._preflight_policy_connection is None:
            raise LedgerError("local preflight policy is unavailable")
        severity_map = self._regime_severity_map()
        return {
            "tick_window": {"bid": [0.99], "ask": [1.01], "spread": [0.02]},
            "portfolio_state": {"equity": 100.0, "peak_equity": 100.0},
            # A local fixture has no quotes to classify. It is sized as the model's calmest
            # component, found through the severity map, never as a literal raw id.
            "regime_id": severity_map.calm_id,
            "regime_policy_hash": severity_map.policy_hash,
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
                cost=Decimal("0"), tick_size=Decimal("0.01"),
                regime_id=self._regime_severity_map().calm_id,
                observed_at=now, source="local-requote-uat-fixture",
            ),
            policy=RequotePolicy.for_severity_map(
                self._regime_severity_map(), max_age_seconds=30
            ),
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
