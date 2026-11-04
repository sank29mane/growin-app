"""The production shadow runtime (64-09 Task B): the edge that builds a ``ShadowRunner`` from files and a relay link.

Everything the runner and the CLI took as a seam is wired here and nowhere else. This module is an edge module: it may
import the execution spine, and it is the only place that opens the relay link, the session store and the ledger for a
real run. It sends no order. The one order call the runner makes is the mint ``POST /v1/orders/intents``, answered by
the VM's kill flag, and nothing here authorizes, halts, signs or loads a key.

Three phases, in this order, and no later phase starts before the earlier one passed:

1. **Local preflight, read-only.** Every file a run reads is read, bound to the others and checked: the forbidden
   literals, ``private/india/shadow_runtime.json``, the strategy and execution configs, the pinned calendar, the frozen
   dataset, the seeded session store (read-only) and its seed report, the instruments, the live table pins, the
   evidence of an existing run, and the simulation ledger on a read-only probe. No relay call, no writable open, no
   directory is made. A refusal here leaves every byte on disk as it was, a torn evidence tail included.
2. **Relay activation** (open-run, decide, close, hand-flatten only). ``link.client()``, then ``link.orders()``, then
   one ``orders.current_key()`` read compared byte for byte with the key id the runtime config names. It writes no Mac
   file. The VM's own mint check stays the second line.
3. **Writable opens.** Every ``EvidenceStore.open`` and ``create`` here passes ``forbidden=`` from the same helper the
   CLI uses.

A refusal is a ``RuntimeRefused`` with a closed code: never a path, a value, an account id or a key id.
"""

from __future__ import annotations

import argparse
import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from brokers.breeze_gateway.client import RelayClient
from brokers.breeze_gateway.config import ConfigError, load_gateway_config
from brokers.breeze_gateway.errors import OrdersKeyRouteUnavailable, TunnelError, UpstreamError
from brokers.breeze_gateway.india_quote import QuoteUnavailable, RelayQuoteSource, parse_quote
from brokers.breeze_gateway.orders import OrdersClient, OrdersProtocolError, RelayLink
from execution.india_guard import IndiaAdmissionGuard, IndiaQuoteEvidence
from execution.ledger import SimulationProbeRefused, probe_simulation_ledger
from execution.models import Workspace
from pilot_data.bhavcopy import NON_REGULAR_SERIES, primary_bars_on
from pilot_data.core import PilotDataError
from pilot_data.corporate_actions import events_for_symbol
from pilot_data.dataset import verify_dataset
from pilot_data.models import CrossCheckTolerances
from pilot_data.nse_calendar import PinnedCalendar, load_pinned_calendar
from pilot_data.session_store import SeedReport, load_seed_report
from pilot_data.store import PilotDataStore
from pilot_data.targets import TargetMember
from pilot_data.universe import UniversePolicy
from private_config.errors import PrivateConfigError
from private_config.loader import load_shadow_forbidden_literals, load_shadow_runtime_config, load_workspace_config
from private_config.schemas import ShadowRuntime
from risk_india import rules
from risk_india.state import StateUnreadable, read_state, state_path_for

from .admission import (
    MIN_QUOTE_SAMPLES,
    QuoteSample,
    open_simulation_ledger,
    simulation_init,
    simulation_ledger_path,
)
from .evidence import EvidenceStore, read_records
from .portfolio import Instrument, PortfolioError, ShadowPortfolio
from .runner import (
    LiveTablePins,
    NoPostSubmissionEvidence,
    RunnerPorts,
    ShadowRunner,
    VmIdentity,
    assemble,
    open_run,
    session_id_for,
)
from .signals import SignalInputs, StrategyConfig, SignalsError, load_strategy_config

NO_FORBIDDEN_SOURCE = "forbidden_literals_not_registered"
PREFLIGHT_ATTR = "shadow_preflight"
RELAY_COMMANDS = ("open-run", "decide", "close", "hand-flatten")
STRUCTURAL_KINDS = ("split", "bonus")


class RuntimeRefused(Exception):
    """A preflight, activation or open refused. ``code`` is a closed word and never carries a value or a path."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ForbiddenLiteralsRefused(Exception):
    """No usable registered forbidden literals: the evidence store is not opened."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# --- the forbidden literals: one helper for the CLI and the runtime -------------------------------------------------


def _default_forbidden_literals(args: argparse.Namespace) -> Sequence[str]:
    """The registered literals from ``<private-dir>/india/shadow_forbidden.json`` through the fail-closed loader."""

    private_dir = getattr(args, "private_dir", None)
    if private_dir is None:
        raise ForbiddenLiteralsRefused(NO_FORBIDDEN_SOURCE)
    try:
        return load_shadow_forbidden_literals(private_dir)
    except PrivateConfigError as error:
        raise ForbiddenLiteralsRefused(f"forbidden_literals_refused:{error.code}") from None


def _forbidden_literals(
    args: argparse.Namespace, source: Optional[Callable[[argparse.Namespace], Sequence[str]]] = None
) -> tuple[str, ...]:
    """The literals every ``EvidenceStore.open`` and ``create`` passes as ``forbidden=``. Never empty.

    ``source`` is the explicit seam for a test or the operator's own wrapper. It does not bypass the empty check."""

    literals = tuple((source or _default_forbidden_literals)(args))
    if not literals or not all(isinstance(item, str) and item for item in literals):
        raise ForbiddenLiteralsRefused("forbidden_literals_empty")
    return literals


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# --- the preflight result -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Preflight:
    """What phase 1 learned. It holds values from private files, so its repr names none of them."""

    command: str
    root: Path
    run_id: str
    session: date
    literals: tuple[str, ...]
    runtime: ShadowRuntime
    strategy: StrategyConfig
    exec_config: Any
    calendar: Optional[PinnedCalendar]
    dataset_sha256: str
    report: SeedReport
    instruments: tuple[Instrument, ...]
    members: Mapping[str, TargetMember]
    window_start: date
    run_start_session: date
    sim_root: Path
    ledger_path: Path
    latch_path: Path
    seed: Decimal
    session_store_root: Path
    gateway_loader: Optional[Callable[[], Any]] = field(default=None, repr=False)

    def __repr__(self) -> str:
        return "Preflight(<private values omitted>)"


def _refuse(code: str) -> RuntimeRefused:
    return RuntimeRefused(code)


def _preflight_seed(store: PilotDataStore, root: Path, manifest: Any, window_start: date) -> SeedReport:
    """Bind the seed report to the dataset and the session store. Reads only."""

    if not store.table_exists("session_store_seeds"):
        raise _refuse("not_a_session_store")
    try:
        report = load_seed_report(root)
    except PilotDataError as error:
        raise _refuse(f"seed_refused:{error.code}") from None
    rows = store.query("SELECT seed_report_sha256, dataset_sha256, window_start FROM session_store_seeds")
    recorded = [row for row in rows if str(row[0]) == report.report_sha256]
    if not recorded:
        raise _refuse("seed_report_unrecorded")
    if report.dataset_sha256 != manifest.dataset_sha256 or any(
        str(row[1]) != manifest.dataset_sha256 for row in rows
    ):
        raise _refuse("seed_dataset_mismatch")
    if report.targets_sha256 != manifest.targets_sha256 or report.targets.target_sha256 != manifest.targets_sha256:
        raise _refuse("seed_targets_mismatch")
    # Equality, not a range: the seeded session store was cut at exactly this window start (the strategy's own window
    # and the as-of cutoff on the member lists), so a config one day inside the seeded window reads a different store.
    if report.window_start != window_start or any(row[2] != window_start for row in recorded):
        raise _refuse("window_start_not_seeded")
    return report


def _instruments(report: SeedReport) -> tuple[tuple[Instrument, ...], dict[str, TargetMember]]:
    built: list[Instrument] = []
    members: dict[str, TargetMember] = {}
    for member in report.targets.members:
        try:
            built.append(Instrument(member.anchor_isin, member.nse_symbol, member.stock_code))
        except PortfolioError:
            raise _refuse("instrument_invalid") from None
        members[member.anchor_isin] = member
    for index in ("isin", "symbol", "stock_code"):
        values = [getattr(item, index) for item in built]
        if len(set(values)) != len(values):
            raise _refuse("instrument_mapping_conflict")
    return tuple(built), members


def _run_start(root: Path, run_id: str, window_start: date) -> tuple[date, dict[str, Any]]:
    """The recorded ``run_start`` of an existing run, bound to the config. A restart cannot move it."""

    run_dir = Path(root) / run_id
    if not run_dir.is_dir():
        raise _refuse("run_not_opened")
    start = next((r for r in read_records(root, run_id) if r.get("kind") == "run_start"), None)
    if start is None:
        raise _refuse("run_not_opened")
    try:
        started = date.fromisoformat(str(start["run_start_session"]))
        recorded_window = date.fromisoformat(str(start["window_start"]))
    except (KeyError, ValueError):
        raise _refuse("run_start_mismatch") from None
    if recorded_window != window_start or start.get("session_id") != session_id_for(started):
        raise _refuse("run_start_mismatch")
    return started, start


def preflight(
    args: argparse.Namespace,
    *,
    relay: bool,
    calendar: bool,
    gateway_loader: Optional[Callable[[], Any]] = None,
) -> Preflight:
    """Phase 1: read-only. Nothing is written, made or opened for writing, and no relay call is made."""

    literals = _forbidden_literals(args, getattr(args, "forbidden_source", None))  # 1
    private_dir = getattr(args, "private_dir", None)
    try:
        runtime = load_shadow_runtime_config(private_dir)  # 2
    except PrivateConfigError as error:
        raise _refuse(f"runtime_config_refused:{error.code}") from None
    try:
        strategy = load_strategy_config(private_dir)  # 3
    except SignalsError as error:
        raise _refuse(str(error)) from None
    try:
        exec_config = load_workspace_config(private_dir, "india", require_india_execution=True)  # 4
    except PrivateConfigError as error:
        raise _refuse(f"execution_config_refused:{error.code}") from None
    pinned: Optional[PinnedCalendar] = None
    if calendar:  # 5
        try:
            pinned = load_pinned_calendar(Path(runtime.calendar_path), expected_sha256=runtime.calendar_sha256)
        except PilotDataError as error:
            raise _refuse(error.code) from None
        if pinned is None:
            raise _refuse("calendar_unpinned")
    try:
        manifest = verify_dataset(Path(runtime.dataset_dir), workspace="india")  # 6
    except PilotDataError as error:
        raise _refuse(f"dataset_refused:{error.code}") from None
    session_root = Path(runtime.session_store_root)  # 7, 8
    try:
        store = PilotDataStore(session_root, workspace="india", read_only=True)
    except PilotDataError as error:
        raise _refuse("session_store_missing" if error.code == "store_missing" else "session_store_refused") from None
    except Exception:  # a file that is not a database: the engine's own error, never a path
        raise _refuse("session_store_refused") from None
    try:
        report = _preflight_seed(store, session_root, manifest, runtime.window_start)
    except (RuntimeRefused, PilotDataError):
        raise
    except Exception:
        raise _refuse("session_store_refused") from None
    finally:
        store.close()
    instruments, members = _instruments(report)  # 9
    try:
        pins = LiveTablePins()
        pins.tick_sha256(), pins.cost_sha256(), pins.scenario_sha256()
    except Exception:
        raise _refuse("pins_unavailable") from None
    if relay and gateway_loader is not None:  # 10
        try:
            gateway_loader()
        except ConfigError as error:
            raise _refuse(f"relay_config_refused:{error.code}") from None
    root = Path(args.root)
    sim_root = Path(runtime.simulation_root)
    ledger_path = simulation_ledger_path(sim_root, args.run_id)
    seed = Decimal(strategy.limits.capital_cap)
    if args.command == "open-run":  # 11
        if (root / args.run_id).exists():
            raise _refuse("run_exists")
        run_start_session = args.session
    else:
        run_start_session, _ = _run_start(root, args.run_id, runtime.window_start)
    if args.command == "open-run":  # 12
        if ledger_path.exists():
            raise _refuse("simulation_ledger_exists")
    else:
        if not ledger_path.exists():
            raise _refuse("simulation_ledger_missing")
        try:
            probe_simulation_ledger(
                ledger_path, workspace=Workspace.INDIA, simulation=simulation_init(args.run_id, seed)
            )
        except SimulationProbeRefused as error:
            raise _refuse(f"simulation_ledger_refused:{error.reason}") from None
    return Preflight(
        command=args.command, root=root, run_id=args.run_id, session=args.session, literals=literals,
        runtime=runtime, strategy=strategy, exec_config=exec_config, calendar=pinned,
        dataset_sha256=manifest.dataset_sha256, report=report, instruments=instruments, members=members,
        window_start=runtime.window_start, run_start_session=run_start_session, sim_root=sim_root,
        ledger_path=ledger_path, latch_path=state_path_for(ledger_path), seed=seed,
        session_store_root=session_root, gateway_loader=gateway_loader,
    )


def cached_preflight(
    args: argparse.Namespace,
    *,
    relay: bool,
    calendar: bool,
    gateway_loader: Optional[Callable[[], Any]] = None,
) -> Preflight:
    """Phase 1 once per invocation: the latch path source and the release runtime share one read of the files."""

    held = getattr(args, PREFLIGHT_ATTR, None)
    if held is not None and held[0] == (relay, calendar):
        return held[1]
    result = preflight(args, relay=relay, calendar=calendar, gateway_loader=gateway_loader)
    setattr(args, PREFLIGHT_ATTR, ((relay, calendar), result))
    return result


def release_latch_path(args: argparse.Namespace) -> Path:
    """The latch file of the run, from the read-only preflight (no relay, no calendar). Task A's step 4 reads it."""

    return cached_preflight(args, relay=False, calendar=False).latch_path


# --- phase 2: relay activation --------------------------------------------------------------------------------------


def activate_relay(link: RelayLink, expected_key_id: str) -> OrdersClient:
    """``link.client()``, ``link.orders()``, one ``current_key()``: the pinned key or a refusal. Writes no Mac file."""

    try:
        link.client()
    except TunnelError:
        raise _refuse("relay_unavailable") from None
    orders = link.orders()
    try:
        key_id = orders.current_key()
    except OrdersKeyRouteUnavailable:
        raise _refuse("vm_key_route_unavailable") from None
    except OrdersProtocolError:
        raise _refuse("vm_key_unreadable") from None
    if key_id != expected_key_id:
        raise _refuse("vm_key_mismatch")
    return orders


# --- ports ----------------------------------------------------------------------------------------------------------


class _LinkHealth:
    """The relay health read, through the link's client on every call."""

    def __init__(self, link: RelayLink) -> None:
        self._link = link

    def health(self) -> Any:
        return self._link.client().health()


class RelayQuoteSamples:
    """Three relay readings of one name, each pinned to the exact bytes it parsed.

    One ``quote`` GET per sample; ``sha256(payload.body)`` must equal ``payload.sha256`` or the sample is refused
    (``UpstreamError``). ``fetched_at`` is taken once, on return, and is the evidence's ``observed_at``. The evidence is
    built from that same read as ``RelayQuoteSource.evidence`` builds it, but this class never calls ``.quote()`` or
    ``.evidence()`` (each would fetch a second body the sample's hash does not cover).
    """

    def __init__(self, link: RelayLink, *, clock: Callable[[], datetime] = _utc_now) -> None:
        self._link = link
        self._clock = clock
        self._source = RelayQuoteSource(link, clock=clock)

    def samples(self, instrument: Instrument) -> list[QuoteSample]:
        code = instrument.stock_code
        out: list[QuoteSample] = []
        references: dict[date, tuple[Decimal, date]] = {}
        for _ in range(MIN_QUOTE_SAMPLES):
            payload = self._link.client().quote(code)
            if hashlib.sha256(payload.body).hexdigest() != payload.sha256:
                raise UpstreamError(reason="quote_hash_mismatch")
            try:
                read = parse_quote(payload.body, code)
            except QuoteUnavailable as error:
                raise UpstreamError(reason=error.reason) from None
            fetched_at = self._clock()
            if read.bid is None or read.ask is None:
                raise UpstreamError(reason="quote_no_bid_ask")
            entry = self._source.master_entry(code)
            if entry is None:
                raise UpstreamError(reason="isin_unbound")
            if entry.isin != instrument.isin:
                raise UpstreamError(reason="isin_mismatch")
            if read.session_date not in references:
                try:
                    references[read.session_date] = self._source.tick_reference(code, read.session_date)
                except QuoteUnavailable as error:
                    raise UpstreamError(reason=error.reason) from None
            reference, reference_day = references[read.session_date]
            quote = rules.Quote(
                stock_code=code, isin=entry.isin, series=entry.series, ltp=read.ltp,
                lower_circuit=read.lower_circuit, upper_circuit=read.upper_circuit,
                previous_close=read.previous_close, session_date=read.session_date, tick_reference=reference,
                tick_reference_month=reference_day, bid=read.bid, ask=read.ask,
            )
            out.append(
                QuoteSample(
                    payload.sha256, fetched_at, read.bid, read.ask,
                    IndiaQuoteEvidence(quote=quote, observed_at=fetched_at),
                )
            )
        return out


class SessionStoreReference:
    """Raw closes, ADV and structural actions from the seeded session store, one identity per name.

    The identity of a name is its anchor ISIN and its anchor series (``EQ``). A bar of the same ISIN in a block,
    buy-back or T+0 series is never a close. No row gives ``close_missing`` and two or more give ``close_conflict``;
    in both the name is left out of ``closes`` and recorded in ``gaps``, never guessed. What a gap means for a mark is
    the portfolio's decision (a held name that is not frozen pauses ``mark_missing``; a frozen one keeps its carried
    mark). Nothing here reads a row dated after the session it is asked about.
    """

    def __init__(self, store: PilotDataStore, members: Mapping[str, TargetMember], adv_window: int) -> None:
        self._store = store
        self._members = dict(members)
        self._adv_window = adv_window
        self.gaps: dict[str, str] = {}

    def _rows(self, session: date, isin: str) -> Optional[list[Any]]:
        member = self._members.get(isin)
        if member is None:
            return None
        return [
            bar for bar in primary_bars_on(self._store, session)
            if bar.isin == isin and bar.series == member.anchor_series and bar.series not in NON_REGULAR_SERIES
        ]

    def closes(self, session: date, isins: Sequence[str]) -> dict[str, Decimal]:
        found: dict[str, Decimal] = {}
        for isin in isins:
            rows = self._rows(session, isin)
            if not rows:
                self.gaps[isin] = "close_missing"
            elif len(rows) > 1:
                self.gaps[isin] = "close_conflict"
            else:
                found[isin] = rows[0].close
        return found

    def adv(self, isin: str, session: date) -> Optional[Decimal]:
        if self._members.get(isin) is None or not self._store.table_exists("bhavcopy_files"):
            return None
        days = self._store.query(
            "SELECT DISTINCT trade_date FROM bhavcopy_files WHERE file_kind IN ('udiff', 'cm_legacy') "
            "AND trade_date <= ? ORDER BY trade_date DESC LIMIT ?",
            [session, self._adv_window * 3 + 10],
        )
        values: list[Decimal] = []
        for (day,) in days:
            rows = self._rows(day, isin)
            if rows is None or len(rows) > 1:
                return None
            if rows:
                values.append(rows[0].traded_value)
            if len(values) == self._adv_window:
                break
        if len(values) < self._adv_window:
            return None
        ordered = sorted(values)
        mid = len(ordered) // 2
        return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2

    def structural_actions(self, session: date, isins: Sequence[str]) -> dict[str, str]:
        found: dict[str, str] = {}
        for isin in isins:
            member = self._members.get(isin)
            if member is None:
                continue
            kinds = {
                part.kind
                for event in events_for_symbol(self._store, member.nse_symbol, ex_from=session, ex_to=session)
                if event.ex_date == session
                for part in event.parts
                if part.kind in STRUCTURAL_KINDS
            }
            if kinds:
                found[isin] = "split" if "split" in kinds else "bonus"
        return found


# --- the runtimes ---------------------------------------------------------------------------------------------------


class OpenRunRuntime:
    """What ``open-run`` needs: the run is created on ``open_run()``, then the simulation ledger."""

    def __init__(self, pre: Preflight, orders: OrdersClient, clock: Callable[[], datetime], link: Optional[RelayLink]) -> None:
        self._pre = pre
        self._orders = orders
        self._clock = clock
        self._link = link

    def open_run(self) -> dict[str, Any]:
        pre = self._pre
        store = PilotDataStore(pre.session_store_root, workspace="india")
        try:
            pins = LiveTablePins()
            inputs = _inputs(pre, store, pins)
            evidence = open_run(
                pre.root, run_id=pre.run_id, inputs=inputs, orders=self._orders, clock=self._clock,
                forbidden=pre.literals,
            )
            ledger = open_simulation_ledger(pre.sim_root, pre.run_id, pre.seed)
            ledger.close()
            return {"run_id": pre.run_id, "run_start_session": pre.run_start_session.isoformat(), "records": evidence.head()[0]}
        finally:
            store.close()

    def shutdown(self) -> None:
        if self._link is not None:
            self._link.close()


@dataclass
class ProductionRuntime:
    """A built runner and what it must close. ``.runner`` is what the CLI drives."""

    runner: ShadowRunner
    reference: SessionStoreReference
    _closers: list[Callable[[], None]] = field(default_factory=list, repr=False)

    @property
    def portfolio(self) -> ShadowPortfolio:
        return self.runner.portfolio

    @property
    def reference_gaps(self) -> dict[str, str]:
        return dict(self.reference.gaps)

    def ambiguous_exit_isins(self) -> frozenset[str]:
        return self.runner.ambiguous_exit_isins()

    def shutdown(self) -> None:
        while self._closers:
            self._closers.pop()()


@dataclass
class ReleaseRuntime:
    """The portfolio alone, for ``resume``, ``release-halt`` and ``release-corporate-action``. No relay, no store writes."""

    portfolio: ShadowPortfolio
    evidence: EvidenceStore
    _closers: list[Callable[[], None]] = field(default_factory=list, repr=False)

    def ambiguous_exit_isins(self) -> frozenset[str]:
        return self.portfolio.ambiguous_exit_isins()

    def shutdown(self) -> None:
        while self._closers:
            self._closers.pop()()


def _inputs(pre: Preflight, store: PilotDataStore, pins: LiveTablePins) -> SignalInputs:
    return SignalInputs(
        store=store, strategy=pre.strategy, targets=pre.report.targets, universe_policy=UniversePolicy(),
        calendar=pre.calendar, tolerances=CrossCheckTolerances(), window_start=pre.window_start,
        run_start_session=pre.run_start_session, dataset_sha256=pre.dataset_sha256,
        tick_sha256=pins.tick_sha256(), cost_sha256=pins.cost_sha256(), scenario_sha256=pins.scenario_sha256(),
    )


def _link_and_loader(
    link_factory: Optional[Callable[[], RelayLink]],
    gateway_config_loader: Optional[Callable[[], Any]],
) -> tuple[RelayLink, Optional[Callable[[], Any]], bool]:
    """The link, the config check and whether this module owns the link (production) and must close it."""

    if link_factory is not None:
        return link_factory(), gateway_config_loader, False
    loader = gateway_config_loader or load_gateway_config

    def opener() -> RelayClient:
        return RelayClient.open(loader())

    return RelayLink(opener), loader, True


def production_runtime(
    args: argparse.Namespace,
    *,
    link_factory: Optional[Callable[[], RelayLink]] = None,
    clock: Optional[Callable[[], datetime]] = None,
    gateway_config_loader: Optional[Callable[[], Any]] = None,
) -> Any:
    """Phases 1 to 3 for open-run, decide, close and hand-flatten.

    ``link_factory`` and ``clock`` are the test seams (an in-process link, a fixed clock). With a ``link_factory`` and no
    ``gateway_config_loader`` the gateway config file is not read at all."""

    clock = clock or _utc_now
    link, loader, owned = _link_and_loader(link_factory, gateway_config_loader)
    try:
        pre = cached_preflight(args, relay=True, calendar=True, gateway_loader=loader)  # phase 1
        orders = activate_relay(link, pre.runtime.vm_key_id)  # phase 2
        owner = link if owned else None
        if args.command == "open-run":
            return OpenRunRuntime(pre, orders, clock, owner)
        return _build_runner(pre, orders, link, clock, owner)  # phase 3
    except BaseException:
        if owned:
            link.close()
        raise


def _build_runner(
    pre: Preflight, orders: OrdersClient, link: RelayLink, clock: Callable[[], datetime], owner: Optional[RelayLink]
) -> ProductionRuntime:
    closers: list[Callable[[], None]] = []
    try:
        ledger = open_simulation_ledger(pre.sim_root, pre.run_id, pre.seed)
        closers.append(ledger.close)
        evidence = EvidenceStore.open(
            pre.root, run_id=pre.run_id, session_id=session_id_for(pre.session), clock=clock, forbidden=pre.literals
        )
        store = PilotDataStore(pre.session_store_root, workspace="india")
        closers.append(store.close)
        pins = LiveTablePins()
        inputs = _inputs(pre, store, pins)
        guard = IndiaAdmissionGuard.from_config(pre.exec_config, ledger, clock=clock)
        identity = VmIdentity(guard.limits_sha256, pre.strategy.params_sha256, pre.runtime.vm_key_id)
        reference = SessionStoreReference(store, pre.members, pre.strategy.params.adv_window)
        ports = RunnerPorts(
            quotes=RelayQuoteSamples(link, clock=clock), reference=reference, fills=NoPostSubmissionEvidence()
        )
        runner = assemble(
            evidence=evidence, ledger=ledger, india_guard=guard, latch=guard.store, instruments=pre.instruments,
            orders=orders, identity=identity, base_inputs=inputs, pins=pins, ports=ports,
            health=_LinkHealth(link), clock=clock,
        )
    except BaseException:
        for closer in reversed(closers):
            closer()
        raise
    if owner is not None:
        closers.append(owner.close)
    return ProductionRuntime(runner=runner, reference=reference, _closers=closers)


def release_runtime(args: argparse.Namespace, *, clock: Optional[Callable[[], datetime]] = None) -> ReleaseRuntime:
    """Phases 1 and 3 for resume and the release commands: no relay, no calendar, no session-store write."""

    clock = clock or _utc_now
    pre = cached_preflight(args, relay=False, calendar=False)
    try:
        latch = read_state(pre.latch_path)
    except StateUnreadable:
        latch = True  # an unreadable latch is the portfolio's to record (``state_corrupt``), not a missing one
    if latch is None and args.command != "resume":  # a resume of an ordinary pause needs no latch; the policy asks for one when it must
        raise _refuse("latch_missing")
    closers: list[Callable[[], None]] = []
    try:
        ledger = open_simulation_ledger(pre.sim_root, pre.run_id, pre.seed)
        closers.append(ledger.close)
        evidence = EvidenceStore.open(
            pre.root, run_id=pre.run_id, session_id=session_id_for(pre.session), clock=clock, forbidden=pre.literals
        )
        guard = IndiaAdmissionGuard.from_config(pre.exec_config, ledger, clock=clock)
        portfolio = ShadowPortfolio(ledger, guard.store, evidence, pre.instruments)
    except BaseException:
        for closer in reversed(closers):
            closer()
        raise
    return ReleaseRuntime(portfolio=portfolio, evidence=evidence, _closers=closers)


__all__ = [
    "ForbiddenLiteralsRefused",
    "NO_FORBIDDEN_SOURCE",
    "OpenRunRuntime",
    "Preflight",
    "ProductionRuntime",
    "RelayQuoteSamples",
    "ReleaseRuntime",
    "RuntimeRefused",
    "SessionStoreReference",
    "activate_relay",
    "cached_preflight",
    "preflight",
    "production_runtime",
    "release_latch_path",
    "release_runtime",
]
