"""The shadow portfolio with Option B (64-03 Task 2 part A, D-05, D-08, D-15, Q-6).

One class, ``ShadowPortfolio``, sits between the simulation account (P-11), the 63 latch machine and the
evidence store. It reads holdings and reservations from the account, marks them at raw closes with the
account's cash (which already carries every booked charge), and lets ``risk_india`` decide. Nothing here
re-implements a threshold:

- equity is cash plus quantity times the raw close (62 D-05), so it is cost inclusive and carries open exposure;
- the -8% halt, the floor(q / 2) halves, the -15% flatten and the -12% close-based stop are
  ``risk_india.drawdown.evaluate_session``, driven through the same ``LatchStore`` file the admission guard
  reads, so a buy is refused ``halt_latch`` / ``pilot_ended`` / ``stop_open`` by the real gate;
- the peak and the drawdown in every ``mark`` record are the latch's high-water mark. They are never taken from
  ``ShadowAdmissionAdapter.portfolio_state()``: that figure is seed-relative at cost, so when it is recorded it
  is a separate field stamped with ``SIMULATOR_DRAWDOWN_BASIS``;
- the peak never rebases. A halt release is ``release_halt``: the 63 reset first (refused while drawdown is at
  or below -8% unless the operator accepts the loss with ``rebase_halt_anchor``), then the ``resume`` record.
  Recovery of equity releases nothing. Phase 62's automatic halt release is expected divergence, pinned in the
  tests.

The evidence is a mirror of the latch, not a second source: ``_reconcile`` raises the ``drawdown_halt`` pause and
the terminal ``drawdown_flatten_15`` run end from the latch state, so a crash between the latch write and the
evidence write is repaired by the next call.

Exits are rebuilt from the latch (``pending_batches``), never from what one evaluation returned. An exit whose
attempt closed INCONCLUSIVE leaves no fill, so the latch keeps it open and the next decide proposes it again
(``reproposed``), with the stop latch (and so the buy block) intact.

What is still due is derived from durable state, not from a notification. ``apply_exit_fill`` subtracts what it is
told, so a retried or lost hook would over-sell or drop an exit. Instead every mark, every decide and every
``on_booked_sell`` first reconciles each open exit with the holdings the simulation ledger carries: stop and
flatten sell what is held; a halve sells ``held - ceil(q / 2)`` where ``q`` is the quantity the evaluation
recorded in its ``position_mark`` (written BEFORE the latch moves), capped at what the latch still owes. The same
reconcile run twice, or after a crash between the booking and the hook, gives the same answer, and
``on_booked_sell`` is a nudge to reconcile: its ``sold_quantity`` is validated and never trusted.

The terminal flatten obligation covers every holding, not only the ones held at the -15% close: once the latch or the
run says ``ended``, any name still held (a buy admitted before the end that books after it) is owed a flatten. The
latch cannot hold it (``evaluate_session`` builds the flatten batch only on the transition), so the portfolio adds
it to the view ``pending_batches`` reads and records a ``latch`` line ``flatten`` for the ISIN the first time it
sees it. The obligation is a function of the ended flag and the holdings, so a restart changes nothing.

Evaluation inputs are persisted first. The ``position_mark`` lines (quantity, cost, raw close or carried close) are
appended before ``evaluate_session`` moves the latch, so a crash after the latch write leaves the raw closes in the
evidence and a later carried close or halve target is read from them, never rebuilt from cost. A retry whose
inputs differ appends a superseding line (the latest line for the session and ISIN wins), so the evidence always
matches the evaluation that finally reached the latch.

Q-6 (provisional, fail closed): a recorded split or bonus ex-date on a held name raises the existing
``unresolved_corporate_action`` pause scoped to that ISIN. While it is open the name keeps carrying its last
pre-ex raw close (``carried_ex_date_pause``), no stop is evaluated on it (the latch position gets a cost equal to
the carried value, so the -12% or a tighter vol stop cannot fire), it moves nothing in the drawdown, and a buy of it
is blocked. Only an operator ``resume`` with a reason clears the pause; the next session then marks the quantity
the ledger holds at the raw close, which is the operator's reconciled position. No factor adjustment is made here.

Keys: this latch file is keyed by ISIN (``Position.isin`` carries the ISIN and ``RiskState.stops`` and
``open_exits`` use it), while ``risk_india/state.py`` documents the Mac latch as keyed by the ledger's execution
ticker. The shadow latch is its own file, so the two key spaces never meet; the 63 CLI ``status`` prints ISINs here.

Pure except for the files the latch store and the evidence store own. No network, no clock, no ``execution``
import: the account is a Protocol the ledger satisfies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from typing import Any, Mapping, NoReturn, Protocol, Sequence

from risk_india.drawdown import OpenExit, ResetRefused, RiskState, StateInconsistent, pending_batches
from risk_india.exits import FLATTEN, HALVE, ExitBatch, ExitError, Position, halve_quantity
from risk_india.state import LatchStore, StateUnreadable, StateUnwritable

from . import policy
from .admission import SIMULATOR_DRAWDOWN_BASIS, EvidencePort

PEAK_SOURCE = "risk_india_latch_high_water_mark"
MARK_RAW_CLOSE = "raw_close"
MARK_CARRIED_EX_DATE = "carried_ex_date_pause"
STRUCTURAL_ACTIONS = frozenset({"split", "bonus"})
CORPORATE_ACTION_PAUSE = "unresolved_corporate_action"
LATCH_FLATTEN = "flatten"
TICKER_PREFIX = "NSE:CASH:"

_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]\Z")
_SYMBOL = re.compile(r"^[A-Z0-9&-]{1,32}\Z")


class PortfolioError(Exception):
    """A portfolio refusal. ``code`` is stable and carries no value."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PortfolioAborted(PortfolioError):
    """The portfolio recorded an abort (``code`` is the policy event). Nothing later may run."""


@dataclass(frozen=True)
class Instrument:
    """One tradable name the run knows: the ISIN the latch is keyed by and the ledger's execution ticker."""

    isin: str
    symbol: str
    stock_code: str

    def __post_init__(self) -> None:
        if not isinstance(self.isin, str) or not _ISIN.match(self.isin):
            raise PortfolioError("bad_instrument")
        if not isinstance(self.symbol, str) or not _SYMBOL.match(self.symbol):
            raise PortfolioError("bad_instrument")
        if not isinstance(self.stock_code, str) or not self.stock_code:
            raise PortfolioError("bad_instrument")

    @property
    def ticker(self) -> str:
        return f"{TICKER_PREFIX}{self.symbol}"


class SimulationAccount(Protocol):
    """The two reads ``ExecutionLedger`` offers on a simulation ledger. Nothing else is touched."""

    def simulated_cash_view(self) -> Any: ...  # seed, reserved, net_cash_out

    def india_account_view(self, *, exclude_proposal_id: str = "") -> Any: ...  # positions, open_buys, open_sells


@dataclass(frozen=True)
class Holding:
    instrument: Instrument
    quantity: int
    cost: Decimal  # total cost basis, charges excluded (the latch's measure)


@dataclass(frozen=True)
class OpenBuy:
    instrument: Instrument
    quantity: int
    limit_price: Decimal


@dataclass(frozen=True)
class AccountSnapshot:
    """Holdings and reservations read from the simulation account in one pass."""

    cash: Decimal  # seed less net cash out: realised, charges included, reservations still inside it
    reserved: Decimal
    holdings: tuple[Holding, ...]
    open_buys: tuple[OpenBuy, ...]
    open_sells: Mapping[str, int]  # ISIN -> admitted sell quantity not yet terminal


@dataclass(frozen=True)
class MarkResult:
    session: date
    evaluated: bool  # False when the latch had already evaluated this session
    equity: Decimal
    peak: Decimal
    drawdown: Decimal
    state: RiskState
    batches: tuple[ExitBatch, ...]  # raised by THIS evaluation only; exit_proposals is the durable view
    config_errors: tuple[str, ...]
    paused_isins: tuple[str, ...]
    stop_open: tuple[str, ...]


@dataclass(frozen=True)
class ExitProposal:
    batch_id: str
    reason: str  # halve | stop | flatten
    isin: str
    symbol: str
    stock_code: str
    quantity: int
    decided_on: date  # the last evaluated close
    since: date  # the close that first raised the exit
    reproposed: bool  # True when an earlier decide already proposed it and it is still unfilled


@dataclass(frozen=True)
class ExitPlan:
    proposals: tuple[ExitProposal, ...]
    waiting: tuple[str, ...]  # ISINs whose earlier exit order is still open in the ledger


def _dec(value: Decimal) -> str:
    return format(value, "f")


def _whole(value: Decimal) -> int:
    if not isinstance(value, Decimal) or not value.is_finite() or value != value.to_integral_value() or value < 0:
        raise PortfolioError("quantity_not_whole")
    return int(value)


class ShadowPortfolio:
    """Option B on the simulation account. One instance per run; the latch file and evidence outlive it."""

    def __init__(
        self,
        account: SimulationAccount,
        latch: LatchStore,
        evidence: EvidencePort,
        instruments: Sequence[Instrument],
    ) -> None:
        by_isin: dict[str, Instrument] = {}
        for instrument in instruments:
            if instrument.isin in by_isin:
                raise PortfolioError("duplicate_instrument")
            by_isin[instrument.isin] = instrument
        if len({i.ticker for i in by_isin.values()}) != len(by_isin):
            raise PortfolioError("duplicate_instrument")
        if account.simulated_cash_view().seed != latch.limits.capital_cap:
            # The latch peak starts at the capital cap (P-07); a different seed would measure drawdown from nothing real.
            raise PortfolioError("seed_capital_cap_mismatch")
        self._account = account
        self._latch = latch
        self._store = evidence
        self._by_isin = by_isin
        self._by_ticker = {i.ticker: i for i in by_isin.values()}

    # -- plumbing ------------------------------------------------------------------------

    @property
    def limits(self) -> Any:
        return self._latch.limits

    def _append(self, kind: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        return self._store.append(kind, payload)

    def _abort(self, event: str, reason: str) -> NoReturn:
        policy.raise_event(self._store, event, reason)
        raise PortfolioAborted(event)

    def _state(self) -> RiskState:
        try:
            return self._latch.load()
        except (StateUnreadable, StateUnwritable) as exc:
            self._abort("state_corrupt", exc.code)

    def _instrument(self, ticker: str) -> Instrument:
        instrument = self._by_ticker.get(ticker)
        if instrument is None:
            # An account position or order for a name the run never admitted: D-07 abort.
            self._abort("unexplained_account_position", "account_ticker_not_in_run_universe")
        return instrument

    def _records(self) -> list[Mapping[str, Any]]:
        return list(self._store.records())

    # -- the simulation account ------------------------------------------------------------

    def snapshot(self) -> AccountSnapshot:
        """Holdings and reservations from the simulation account. An unmapped ticker aborts the run."""

        cash = self._account.simulated_cash_view()
        view = self._account.india_account_view()
        holdings: list[Holding] = []
        for ticker, quantity, cost in view.positions:
            try:
                whole = _whole(quantity)
            except PortfolioError:
                self._abort("state_corrupt", "position_quantity_not_whole")
            if whole == 0:
                continue
            holdings.append(Holding(self._instrument(ticker), whole, cost))
        open_buys = []
        for ticker, quantity, limit in view.open_buys:
            try:
                whole = _whole(quantity)
            except PortfolioError:
                self._abort("state_corrupt", "open_buy_quantity_not_whole")
            open_buys.append(OpenBuy(self._instrument(ticker), whole, limit))
        open_sells: dict[str, int] = {}
        for ticker, quantity in view.open_sells.items():
            try:
                whole = _whole(quantity)
            except PortfolioError:
                self._abort("state_corrupt", "open_sell_quantity_not_whole")
            if whole:
                open_sells[self._instrument(ticker).isin] = whole
        holdings.sort(key=lambda h: h.instrument.isin)
        open_buys.sort(key=lambda b: (b.instrument.isin, b.limit_price))
        return AccountSnapshot(
            cash.seed - cash.net_cash_out, cash.reserved, tuple(holdings), tuple(open_buys), open_sells
        )

    @staticmethod
    def _positions(holdings: Sequence[Holding], carried: Mapping[str, Decimal] | None = None) -> list[Position]:
        """The latch's positions. A name in ``carried`` (Q-6) gets a cost equal to its carried value, so the
        stop test ``value <= cost x (1 + stop)`` cannot fire for any stop in (-1, 0). Only the stop test reads it."""

        carried = carried or {}
        return [
            Position(
                h.instrument.isin,
                h.instrument.stock_code,
                h.quantity,
                h.quantity * carried[h.instrument.isin] if h.instrument.isin in carried else h.cost,
            )
            for h in holdings
        ]

    # -- marks -------------------------------------------------------------------------------

    def _last_raw_close(self, isin: str) -> Decimal | None:
        for record in reversed(self._records()):
            if (
                record.get("kind") == "position_mark"
                and record.get("isin") == isin
                and record.get("mark_basis") == MARK_RAW_CLOSE
            ):
                return Decimal(str(record["close"]))
        return None

    def _has_mark(self, session: date) -> bool:
        return any(r.get("kind") == "mark" and r.get("as_of_date") == session.isoformat() for r in self._records())

    def _has_position_mark(self, session: date, isin: str) -> bool:
        return any(
            r.get("kind") == "position_mark" and r.get("as_of_date") == session.isoformat() and r.get("isin") == isin
            for r in self._records()
        )

    def _pause_isin(self, event: str, isin: str, reason: str) -> None:
        scope = f"{policy.SCOPE_ISIN}:{isin}"
        already = any(p.event == event and p.scope == scope for p in policy.run_state(self._store).open_pauses)
        if not already:
            policy.raise_event(self._store, event, reason, isin=isin)

    def _pause_missing_bar(self, isin: str) -> None:
        self._pause_isin("missing_bar", isin, "no raw close for a held name at the mark")

    def _frozen_isins(self, held: set[str]) -> set[str]:
        """Held names with an open ``unresolved_corporate_action`` pause: carried, never stopped, never moving."""

        scopes = {
            p.scope for p in policy.run_state(self._store).open_pauses if p.event == CORPORATE_ACTION_PAUSE
        }
        return {isin for isin in held if f"{policy.SCOPE_ISIN}:{isin}" in scopes}

    def _session_position_marks(self, session: date) -> dict[str, Mapping[str, Any]]:
        """The latest ``position_mark`` line per ISIN for ``session`` (a superseding line wins)."""

        latest: dict[str, Mapping[str, Any]] = {}
        for record in self._records():
            if record.get("kind") == "position_mark" and record.get("as_of_date") == session.isoformat():
                latest[str(record["isin"])] = record
        return latest

    def _write_position_marks(
        self,
        session: date,
        snap: AccountSnapshot,
        used: Mapping[str, Decimal],
        basis: Mapping[str, str],
        structural: Mapping[str, str],
        *,
        supersede: bool,
    ) -> None:
        """Persist the evaluation inputs. Appends a line when none exists for (session, ISIN), and, with
        ``supersede``, when the existing line differs (a retry after a crash before the latch moved)."""

        latest = self._session_position_marks(session)
        for holding in snap.holdings:
            isin = holding.instrument.isin
            payload: dict[str, Any] = {
                "as_of_date": session.isoformat(),
                "isin": isin,
                "symbol": holding.instrument.symbol,
                "quantity": str(holding.quantity),
                "close": _dec(used[isin]),
                "cost": _dec(holding.cost),
                "mark_basis": basis[isin],
            }
            if isin in structural:
                payload["corporate_action"] = structural[isin]
            prior = latest.get(isin)
            if prior is not None and (not supersede or all(prior.get(k) == v for k, v in payload.items())):
                continue
            self._append("position_mark", payload)

    def mark_session(
        self,
        session: date,
        closes: Mapping[str, Decimal],
        *,
        structural_actions: Mapping[str, str] | None = None,
        vol_stops: Mapping[str, Decimal] | None = None,
        simulator_drawdown_pct: Decimal | None = None,
    ) -> MarkResult:
        """Mark every holding at the raw close of ``session`` and apply Option B.

        ``closes`` is keyed by ISIN and holds raw (unadjusted) closes. ``structural_actions`` maps the ISIN of a
        held name with a recorded split or bonus ex-date on ``session`` to its kind (Q-6): it raises the
        ``unresolved_corporate_action`` pause for that ISIN, and every name under that pause is carried (see the
        module note). A held name with no close that is not carried records a ``missing_bar`` pause for that ISIN and
        raises ``PortfolioError("mark_missing")`` before anything reaches the latch. ``simulator_drawdown_pct`` is the
        adapter's seed-relative figure, recorded only with its basis stamp.
        """

        run = policy.run_state(self._store)
        if run.aborted:
            raise policy.RunAborted("run_aborted")
        if not isinstance(session, date):
            raise PortfolioError("bad_session")
        structural = dict(structural_actions or {})
        for isin, action in structural.items():
            if action not in STRUCTURAL_ACTIONS or not _ISIN.match(isin):
                raise PortfolioError("structural_action_unknown")
        stamp = None
        if simulator_drawdown_pct is not None:
            if not isinstance(simulator_drawdown_pct, Decimal) or not simulator_drawdown_pct.is_finite():
                raise PortfolioError("bad_simulator_figure")
            stamp = _dec(simulator_drawdown_pct)

        before = self._state()
        if before.last_session is not None and session < before.last_session:
            raise PortfolioError("session_already_evaluated")
        self._reconcile(before)

        snap = self.snapshot()
        held = {h.instrument.isin for h in snap.holdings}
        for isin in sorted(structural.keys() & held):
            self._pause_isin(
                CORPORATE_ACTION_PAUSE, isin, "split or bonus ex-date on a held name: marks carried until operator review"
            )
        frozen = self._frozen_isins(held)
        before = self._sync_exits(before, snap)
        used: dict[str, Decimal] = {}
        basis: dict[str, str] = {}
        missing: list[str] = []
        for holding in snap.holdings:
            isin = holding.instrument.isin
            if isin in frozen:
                carried = self._last_raw_close(isin)
                used[isin] = carried if carried is not None else holding.cost / holding.quantity
                basis[isin] = MARK_CARRIED_EX_DATE
                continue
            close = closes.get(isin)
            if close is None:
                missing.append(isin)
                continue
            if not isinstance(close, Decimal) or not close.is_finite() or not close > 0:
                raise PortfolioError("bad_close")
            used[isin] = close
            basis[isin] = MARK_RAW_CLOSE
        if missing:
            for isin in missing:
                self._pause_missing_bar(isin)
            raise PortfolioError("mark_missing")

        evaluated = before.last_session is None or session > before.last_session
        # The inputs reach the evidence BEFORE the latch moves: a crash after the latch write must not lose the raw
        # close a later carried mark or halve target reads.
        self._write_position_marks(session, snap, used, basis, structural, supersede=evaluated)
        batches: tuple[ExitBatch, ...] = ()
        config_errors: tuple[str, ...] = ()
        if evaluated:
            try:
                result = self._latch.evaluate_session(
                    used,
                    session,
                    cash=snap.cash,
                    positions=self._positions(snap.holdings, {i: used[i] for i in frozen}),
                    vol_stops=vol_stops,
                )
            except (StateUnreadable, StateUnwritable) as exc:
                self._abort("state_corrupt", exc.code)
            after, batches, config_errors = result.state, result.batches, result.config_errors
        else:
            after = before
        self._reconcile(after)
        self._flatten_obligations(after, snap)
        for error in config_errors:
            self._append("latch", {"latch": "stop_config", "scope": "session", "reason": error})

        marked_value = sum((h.quantity * used[h.instrument.isin] for h in snap.holdings), Decimal(0))
        if not self._has_mark(session):
            mark: dict[str, Any] = {
                "as_of_date": session.isoformat(),
                "equity": _dec(after.last_equity if after.last_equity is not None else snap.cash + marked_value),
                "peak": _dec(after.peak),
                "drawdown": _dec(after.drawdown),
                "cash": _dec(snap.cash),
                "held_capital": _dec(sum((h.cost for h in snap.holdings), Decimal(0))),
                "exposure": _dec(marked_value + snap.reserved),
                "peak_source": PEAK_SOURCE,
            }
            if stamp is not None:
                mark["simulator_drawdown_pct"] = stamp
                mark["simulator_drawdown_basis"] = SIMULATOR_DRAWDOWN_BASIS
            self._append("mark", mark)
        self.session_end_report(session)
        return MarkResult(
            session=session,
            evaluated=evaluated,
            equity=after.last_equity if after.last_equity is not None else snap.cash + marked_value,
            peak=after.peak,
            drawdown=after.drawdown,
            state=after,
            batches=batches,
            config_errors=config_errors,
            paused_isins=tuple(sorted(frozen)),
            stop_open=tuple(sorted(after.stops)),
        )

    # -- latch to evidence ----------------------------------------------------------------------

    def _has_latch_record(self, latch: str, scope: str, reason: str) -> bool:
        return any(
            r.get("kind") == "latch" and r.get("latch") == latch and r.get("scope") == scope and r.get("reason") == reason
            for r in self._records()
        )

    def _reconcile(self, state: RiskState) -> None:
        """Make the evidence say what the latch says. Idempotent: it writes only what is missing."""

        run = policy.run_state(self._store)
        if state.ended and not run.ended:
            policy.raise_event(self._store, policy.DRAWDOWN_FLATTEN_15, "equity_at_or_below_minus_15_percent_of_peak")
            self._append(
                "latch", {"latch": "ended", "scope": "run", "reason": f"raised_{self._session_tag(state)}"}
            )
            run = policy.run_state(self._store)
        if state.halt and not any(p.event == policy.DRAWDOWN_HALT for p in run.open_pauses):
            policy.raise_event(self._store, policy.DRAWDOWN_HALT, "equity_at_or_below_minus_8_percent_of_peak")
            self._append(
                "latch", {"latch": "halt", "scope": "entries", "reason": f"raised_{self._session_tag(state)}"}
            )
        for isin in sorted(state.stops):
            latched = state.stops[isin]
            scope = f"{policy.SCOPE_ISIN}:{isin}"
            reason = f"raised_{latched.session.isoformat()}"
            if not self._has_latch_record("stop", scope, reason):
                self._append("latch", {"latch": "stop", "scope": scope, "reason": reason})

    @staticmethod
    def _session_tag(state: RiskState) -> str:
        return state.last_session.isoformat() if state.last_session is not None else "unknown"

    def session_end_report(self, session: date) -> Mapping[str, Any] | None:
        """D-08: while any stop exit is open, buys are blocked and the session end says so. Once per session."""

        state = self._state()
        if not state.stops:
            return None
        for record in self._records():
            if record.get("kind") == "stop_open_report" and record.get("as_of_date") == session.isoformat():
                return record
        return self._append(
            "stop_open_report",
            {"as_of_date": session.isoformat(), "isins": sorted(state.stops), "buys_blocked": True},
        )

    # -- entry blocks ----------------------------------------------------------------------------

    def entry_block_reasons(self, isin: str | None = None) -> tuple[str, ...]:
        """Why a buy is blocked right now. Empty means nothing here blocks it. Fails closed on a bad latch."""

        reasons: set[str] = set()
        try:
            state = self._latch.load()
        except (StateUnreadable, StateUnwritable):
            reasons.add("latch_unreadable")
        else:
            if state.halt:
                reasons.add("halt_latch")
            if state.ended:
                reasons.add("pilot_ended")
            if state.stops:
                reasons.add("stop_open")
        run = policy.run_state(self._store)
        if run.aborted:
            reasons.add("run_aborted")
        if run.ended:
            reasons.add("run_ended")
        if any(p.scope in (policy.SCOPE_ALL, policy.SCOPE_ENTRIES) for p in run.open_pauses):
            reasons.add("run_paused")
        if isin is not None and any(p.scope == f"{policy.SCOPE_ISIN}:{isin}" for p in run.open_pauses):
            reasons.add("instrument_paused")
        return tuple(sorted(reasons))

    def buys_blocked(self, isin: str | None = None) -> bool:
        return bool(self.entry_block_reasons(isin))

    # -- exits -------------------------------------------------------------------------------------

    def _halve_target(self, isin: str, since: date) -> int | None:
        """Shares a halve leaves held: ``q - floor(q / 2)`` for the ``q`` the raising evaluation recorded."""

        line = self._session_position_marks(since).get(isin)
        if line is None:
            return None
        quantity = int(Decimal(str(line["quantity"])))
        return quantity - halve_quantity(quantity)

    def _due(self, isin: str, current: OpenExit, held_quantity: int) -> int:
        """What the open exit still owes, from the holdings the ledger carries. Pure in (exit, holdings, evidence)."""

        if current.reason != HALVE:
            return held_quantity  # stop and flatten sell everything still held
        target = self._halve_target(isin, current.since)
        owed = held_quantity if target is None else max(0, held_quantity - target)
        # Never more than the latch still owes: a buy that books later does not grow an old halve.
        return min(current.quantity, owed, held_quantity)

    def _sync_exits(self, state: RiskState, snap: AccountSnapshot) -> RiskState:
        """Reconcile every open exit and stop with the ledger holdings (idempotent, crash safe).

        A name no longer held closes its exit and stop (the ledger holds the fill). A halve is reduced by what the
        holdings show was sold, whether or not any hook was told. The latch is written only when it is stale.
        """

        held = {h.instrument.isin: h.quantity for h in snap.holdings}
        for isin in sorted(set(state.open_exits) | set(state.stops)):
            quantity = held.get(isin, 0)
            current = state.open_exits.get(isin)
            if quantity == 0:
                sold, remaining = 0, 0
            elif current is None:
                continue
            else:
                due = self._due(isin, current, quantity)
                if due == current.quantity or (current.reason != HALVE and quantity == current.quantity):
                    continue
                sold = current.quantity - due if current.reason == HALVE else 0
                remaining = quantity
            try:
                after = self._latch.apply_exit_fill(isin, sold_quantity=sold, remaining_quantity=remaining)
            except (StateUnreadable, StateUnwritable) as exc:
                self._abort("state_corrupt", exc.code)
            except ExitError:
                self._abort("state_corrupt", "exit_reconcile_invalid")
            self._record_stop_cleared(state, after, isin)
            state = after
        return state

    def _record_stop_cleared(self, before: RiskState, after: RiskState, isin: str) -> None:
        if isin in before.stops and isin not in after.stops:
            self._append("latch", {"latch": "stop", "scope": f"{policy.SCOPE_ISIN}:{isin}", "reason": "cleared_by_fill"})

    def _flatten_since(self, isin: str) -> date | None:
        scope = f"{policy.SCOPE_ISIN}:{isin}"
        for record in self._records():
            if record.get("kind") == "latch" and record.get("latch") == LATCH_FLATTEN and record.get("scope") == scope:
                return date.fromisoformat(str(record["reason"]).removeprefix("due_"))
        return None

    def _flatten_obligations(self, state: RiskState, snap: AccountSnapshot) -> dict[str, OpenExit]:
        """The terminal flatten owed by every holding once the latch or the run has ended (D-08).

        ``evaluate_session`` builds the flatten batch only on the -15% transition, so a name that books afterwards
        (a buy admitted before the end) has no open exit in the latch. Each such holding, and each one whose open
        exit is weaker than a flatten, is returned here and its first sighting is recorded as a ``flatten`` latch
        line. Derived from the ended flag and the holdings alone, so it is safe to call on every mark and decide.
        """

        if not (state.ended or policy.run_state(self._store).ended) or state.last_session is None:
            return {}
        owed: dict[str, OpenExit] = {}
        for holding in snap.holdings:
            isin = holding.instrument.isin
            current = state.open_exits.get(isin)
            if current is not None and current.reason == FLATTEN:
                continue
            since = self._flatten_since(isin)
            if since is None:
                since = state.last_session
                self._append(
                    "latch",
                    {"latch": LATCH_FLATTEN, "scope": f"{policy.SCOPE_ISIN}:{isin}", "reason": f"due_{since.isoformat()}"},
                )
            owed[isin] = OpenExit(FLATTEN, holding.quantity, holding.instrument.stock_code, since)
        return owed

    def exit_proposals(self, decide_session: date) -> ExitPlan:
        """Every open exit, rebuilt from the latch and the holdings for the decide on ``decide_session``.

        An exit stays open until a fill reduces it, so one whose attempt closed INCONCLUSIVE is proposed
        again here (``reproposed``). A name whose earlier exit order is still open in the ledger is listed
        as ``waiting`` instead, so the same shares are never offered twice. Quantities come from the holdings the
        ledger carries (see the module note), so a booking whose hook never ran is already netted off. After the
        run ended, every holding is owed a flatten, including one that booked after the -15% close.
        """

        state = self._state()
        snap = self.snapshot()
        state = self._sync_exits(state, snap)
        owed = self._flatten_obligations(state, snap)
        if not state.open_exits and not owed:
            return ExitPlan((), ())
        if state.last_session is None:
            raise PortfolioError("marks_missing")
        if state.last_session >= decide_session:
            raise PortfolioError("marks_not_before_decide")
        view = replace(state, open_exits={**state.open_exits, **owed})
        try:
            batches = pending_batches(view, self._positions(snap.holdings), state.last_session)
        except (ExitError, StateInconsistent):
            self._abort("state_corrupt", "exit_batch_invalid")
        proposals: list[ExitProposal] = []
        waiting: list[str] = []
        for batch in batches:
            for intent in batch.intents:
                if snap.open_sells.get(intent.isin):
                    waiting.append(intent.isin)
                    continue
                since = view.open_exits[intent.isin].since
                instrument = self._by_isin[intent.isin]
                proposals.append(
                    ExitProposal(
                        batch_id=intent.batch_id,
                        reason=intent.reason,
                        isin=intent.isin,
                        symbol=instrument.symbol,
                        stock_code=intent.stock_code,
                        quantity=intent.quantity,
                        decided_on=intent.decided_on,
                        since=since,
                        reproposed=since < intent.decided_on,
                    )
                )
        return ExitPlan(tuple(proposals), tuple(sorted(set(waiting))))

    def on_booked_sell(self, isin: str, sold_quantity: int) -> RiskState:
        """Nudge the latch after a simulated sell booked. Idempotent: it reconciles with the holdings.

        ``sold_quantity`` is validated and not trusted: what is still due is derived from the ledger holdings, so
        a retry, a repeat or a call that never happens (a crash after the booking) cannot over-sell or drop an
        exit. Only a fill, seen as a holding that shrank, reduces an exit or clears a stop.
        """

        if isin not in self._by_isin:
            raise PortfolioError("unknown_instrument")
        if isinstance(sold_quantity, bool) or not isinstance(sold_quantity, int) or sold_quantity < 0:
            raise PortfolioError("bad_fill_quantity")
        return self._sync_exits(self._state(), self.snapshot())

    def is_flat(self) -> bool:
        snap = self.snapshot()
        return not snap.holdings and not snap.open_buys and not snap.open_sells

    # -- release -----------------------------------------------------------------------------------

    def release_halt(self, actor: str, reason: str, *, rebase_halt_anchor: bool = False) -> Mapping[str, Any]:
        """The only release of the -8% halt: the 63 reset, then the ``resume`` record for its pause.

        Refused after an abort and after the run ended. The 63 reset refuses while drawdown is at or below
        the halt line; ``rebase_halt_anchor`` accepts the loss and restarts only the -8% test (the peak never
        rebases). The latch is reset before the resume record is written, and a retry after a crash between
        the two writes only the missing record.
        """

        run = policy.run_state(self._store)
        if run.aborted:
            raise policy.PolicyError("resume_refused_abort")
        if run.ended:
            raise policy.PolicyError("resume_refused_run_ended")
        if not isinstance(reason, str) or not reason.strip():
            raise policy.PolicyError("reason_required")
        state = self._state()
        if state.ended:
            raise policy.PolicyError("resume_refused_run_ended")
        open_halt = [p.seq for p in run.open_pauses if p.event == policy.DRAWDOWN_HALT]
        if state.halt:
            def audit(before: RiskState, after: RiskState) -> None:
                self._append("latch", {"latch": "halt", "scope": "entries", "reason": "released_by_operator"})

            try:
                self._latch.reset("halt", actor, rebase_halt_anchor=rebase_halt_anchor, audit=audit)
            except ResetRefused:
                raise PortfolioError("halt_release_refused") from None
            except (StateUnreadable, StateUnwritable) as exc:
                self._abort("state_corrupt", exc.code)
        elif not open_halt:
            raise policy.PolicyError("nothing_to_resume")
        if not open_halt:
            return {}
        return self._append("resume", {"reason": reason.strip(), "cleared_pause_seqs": sorted(open_halt)})


__all__ = [
    "AccountSnapshot",
    "ExitPlan",
    "ExitProposal",
    "Holding",
    "Instrument",
    "MARK_CARRIED_EX_DATE",
    "MARK_RAW_CLOSE",
    "MarkResult",
    "OpenBuy",
    "PEAK_SOURCE",
    "PortfolioAborted",
    "PortfolioError",
    "STRUCTURAL_ACTIONS",
    "ShadowPortfolio",
    "SimulationAccount",
]
