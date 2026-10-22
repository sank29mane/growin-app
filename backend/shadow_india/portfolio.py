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

What is still due is derived from durable state, not from a notification. Every mark, every decide and every
``on_booked_sell`` first reconciles each open exit with the ledger. Stop and flatten sell what is held. A halve owes
``floor(q / 2)`` (``q`` is the quantity the raising evaluation marked) less the sell fills the simulation ledger holds
under that obligation's own intent ids (``exit_obligation_id`` and ``exit_intent_id``, counted once per booking id),
capped at what is held and at what the latch still owes. It is never read from the aggregate holding: a buy that
books later, or a sell that is not the exit's, cannot change it. The same reconcile run twice, or after a crash
between the booking and the hook, gives the same answer, and ``on_booked_sell`` is a nudge to reconcile: its
``sold_quantity`` is validated and never trusted. A runner must mint every exit sell under ``exit_intent_id``. A sell of
a name with an open halve that is booked under any other id, after the halve was first seen, makes what the halve still
owes unknowable (it may or may not have been meant as the halve): the name gets an ``ambiguous_exit_fill`` pause, the
halve is listed in ``ExitPlan.ambiguous`` and never proposed again, and the exit stays in the latch until the name is
flat. Stop and flatten sell what is held, so a foreign sell cannot make them over-sell.

The terminal flatten obligation covers every holding, not only the ones held at the -15% close: once the latch or the
run says ``ended``, any name still held (a buy admitted before the end that books after it) is owed a flatten. The
latch cannot hold it (``evaluate_session`` builds the flatten batch only on the transition), so the portfolio adds
it to the view ``pending_batches`` reads and records a ``latch`` line ``flatten`` for the ISIN the first time it
sees it. The obligation is a function of the ended flag and the holdings, so a restart changes nothing.

Evaluation inputs are persisted first, and only a committed evaluation is read back. The ``position_mark`` lines
(quantity, cost, raw close or carried value) are appended before ``evaluate_session`` moves the latch. After the latch
moves, an ``evaluation_commit`` line names the lines that evaluation read. A carried close, a carried value and a halve
target are read from committed lines only, so an attempt that crashed before the latch (or a line a retry superseded)
never becomes a valuation. A crash between the latch write and the commit line is repaired by the next call.

Q-6 (provisional, fail closed): a recorded split or bonus ex-date on a held name raises the existing
``unresolved_corporate_action`` pause scoped to that ISIN. While it is open the name is valued at its last committed
value (``carried_ex_date_pause``), moved only by what the ledger booked (a buy's cost, a sell's shares), never by a
quantity the ledger reconciled at the same cost, so it cannot lift the peak. No stop is evaluated on it (the latch
position gets a cost equal to the carried value), it moves nothing in the drawdown, a buy of it is blocked, and any
exit owed on it (a halve or the terminal flatten) stays in the latch but is held out of ``exit_proposals`` (``held_out``)
so it is not sold at the post-adjustment quote. Only ``release_corporate_action`` clears the pause (``policy.resume``
refuses it). The bookings since the last committed valuation (a ``booking_baseline`` latch line holds the count, written before
the latch moves) are replayed in ledger order: a sell takes only the shares held at that moment, the carried lot first
and then the bought lots in booking order (oldest first), and a buy opens a
lot at its booked cost. Where the count is missing or the replay does not land on the held quantity, the value is the
lowest plausible reading and the ISIN gets a ``carry_unreconciled`` pause that only an operator ``resume`` clears: no
fallback can lift the peak. That search is enumerative and bounded (``CARRY_SEARCH_BUDGET`` replay steps); past the
bound the value is a floor that no plausible reading can fall below (``_carry_floor``), found in one pass: per boundary,
the cheapest ``held`` shares that opened when no gain is needed, the dearest-unit-value bound when one is. The release refuses ``corporate_action_not_booked`` for a name that is still
held unless the account exposes an explicit adjustment record (``corporate_action_adjustments``: ticker, kind, ex_date)
that matches a committed ex-date mark of the name. Nothing is inferred from quantity or cost: equal cost with another
quantity is also what a buy and a sell give. It also refuses ``exit_owed_in_pre_adjustment_units`` while a halve is open
for the name, because a halve is a share count fixed before the adjustment. The real ``ExecutionLedger`` has no split or
bonus booking path and exposes no such record, so on it the release is refused while the name is held: the operator
flattens the name (which closes the exit and the pause releases) or leaves it paused. No factor adjustment is made here.

The sell booking boundary of a halve (how many sells the ledger held when the raising evaluation ran) is persisted
before that evaluation moves the latch, never rebuilt from later bookings. A halve with no recorded boundary (a latch
written outside the portfolio, or a line lost) cannot be sized and is treated as ``ambiguous_exit_fill``.

Keys: this latch file is keyed by ISIN (``Position.isin`` carries the ISIN and ``RiskState.stops`` and
``open_exits`` use it), while ``risk_india/state.py`` documents the Mac latch as keyed by the ledger's execution
ticker. The shadow latch is its own file, so the two key spaces never meet; the 63 CLI ``status`` prints ISINs here.

Pure except for the files the latch store and the evidence store own. No network, no clock, no ``execution``
import: the account is a Protocol the ledger satisfies.
"""

from __future__ import annotations

import heapq
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
CORPORATE_ACTION_PAUSE = policy.UNRESOLVED_CORPORATE_ACTION
LATCH_FLATTEN = "flatten"
LATCH_HALVE_BASELINE = "halve_baseline"
EVALUATION_COMMIT = "evaluation_commit"
EVALUATION_INPUTS = "evaluation_inputs"
LATCH_BOOKING_BASELINE = "booking_baseline"  # how many bookings of a name the ledger held at an evaluation
LATCH_SELL_BASELINE = "sell_baseline"  # the sell booking boundary, written before the evaluation
AMBIGUOUS_EXIT_FILL = policy.AMBIGUOUS_EXIT_FILL
CARRY_UNRECONCILED = policy.CARRY_UNRECONCILED
# Work budget of the unreconciled-carry search, in replay steps (one booking applied to the lots = one step). The
# search tries every boundary x both sale orders x every adjustment position, which grows with the cube of the booking
# count; past the budget the search is skipped and ``_carry_floor`` answers instead, in a single pass.
CARRY_SEARCH_BUDGET = 20_000
TICKER_PREFIX = "NSE:CASH:"

_ISIN = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]\Z")
_SYMBOL = re.compile(r"^[A-Z0-9&-]{1,32}\Z")


def exit_obligation_id(reason: str, isin: str, since: date) -> str:
    """The stable id of one exit obligation: it does not change when a decide proposes the exit again."""

    return f"exit-{reason}-{since.strftime('%Y%m%d')}-{isin}"


def exit_intent_id(obligation_id: str, attempt: int) -> str:
    """The order intent id for the ``attempt``-th try at an obligation. Sell fills booked under an id of this
    shape are what the portfolio counts against the obligation; a sell under any other id is not."""

    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise PortfolioError("bad_attempt")
    return f"{obligation_id}-t{attempt}"


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

    def simulated_bookings(self, proposal_id: str | None = None) -> Sequence[Any]: ...  # booking_id, proposal_id, side, ticker, delta_quantity, price, charges


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
    obligation_id: str  # stable across re-proposals; build the order intent id with ``exit_intent_id``
    reason: str  # halve | stop | flatten
    isin: str
    symbol: str
    stock_code: str
    quantity: int
    decided_on: date  # the last evaluated close
    since: date  # the close that first raised the exit
    reproposed: bool  # True when an earlier decide already proposed it and it is still unfilled


@dataclass(frozen=True)
class _Carry:
    """What a name was worth when its valuation was last committed: shares, value and ledger cost then, and how many
    bookings of the name the ledger held (``None`` when no ``booking_baseline`` line was written for that session)."""

    quantity: Decimal
    value: Decimal
    cost: Decimal
    bookings: int | None = None


@dataclass(frozen=True)
class ExitPlan:
    proposals: tuple[ExitProposal, ...]
    waiting: tuple[str, ...]  # ISINs whose earlier exit order is still open in the ledger
    held_out: tuple[str, ...] = ()  # ISINs with an exit owed but an open corporate-action pause: not proposed yet
    ambiguous: tuple[str, ...] = ()  # ISINs whose halve cannot be sized: a sell booked under an id that is not its own


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

    def _commit_session(self, state: RiskState) -> None:
        """Bind the latch's latest evaluation to the ``position_mark`` lines it used (idempotent, crash safe).

        A ``position_mark`` is evaluation input until the latch shows its session. ``mark_session`` writes an
        ``evaluation_inputs`` line naming the exact lines an attempt will hand the latch, before the latch moves; this
        line copies the latest of those for the session. It is never "the latest line per ISIN": a line an earlier
        attempt wrote for a name the retry no longer held is not an input. A crash between the latch write and this
        line is repaired by the next call, before any read of the marks, from the same ``evaluation_inputs`` line.
        """

        if state.last_session is None:
            return
        tag = state.last_session.isoformat()
        records = self._records()
        if any(r.get("kind") == EVALUATION_COMMIT and r.get("as_of_date") == tag for r in records):
            return
        seqs: list[int] = []
        for record in records:
            if record.get("kind") == EVALUATION_INPUTS and record.get("as_of_date") == tag:
                seqs = sorted(int(s) for s in record["position_mark_seqs"])
        payload: dict[str, Any] = {"as_of_date": tag, "position_mark_seqs": seqs}
        if state.last_equity is not None:
            payload["equity"] = _dec(state.last_equity)
        self._append(EVALUATION_COMMIT, payload)

    def _record_evaluation_inputs(self, session: date, seqs: Sequence[int]) -> None:
        """Write which ``position_mark`` lines this attempt hands the latch, unless the latest attempt wrote the same."""

        tag = session.isoformat()
        latest = None
        for record in self._records():
            if record.get("kind") == EVALUATION_INPUTS and record.get("as_of_date") == tag:
                latest = sorted(int(s) for s in record["position_mark_seqs"])
        if latest != sorted(seqs):
            self._append(EVALUATION_INPUTS, {"as_of_date": tag, "position_mark_seqs": sorted(seqs)})

    def _committed_marks(self) -> list[Mapping[str, Any]]:
        """The ``position_mark`` lines a committed evaluation read, in evidence order. An unfinished attempt's
        lines (a crash before the latch moved, or a line a retry superseded) are never in this list."""

        records = self._records()
        committed = {
            int(seq) for r in records if r.get("kind") == EVALUATION_COMMIT for seq in r["position_mark_seqs"]
        }
        return [r for r in records if r.get("kind") == "position_mark" and int(r["seq"]) in committed]

    def _carry_base(self, isin: str) -> _Carry | None:
        """The last committed valuation of ``isin``: a raw mark is shares x close, a carried mark is the value it
        carried. ``None`` when no committed evaluation ever marked the name."""

        for record in reversed(self._committed_marks()):
            if record.get("isin") != isin:
                continue
            cost = Decimal(str(record["cost"]))
            bookings = self._booking_boundary(isin, date.fromisoformat(str(record["as_of_date"])))
            if record.get("mark_basis") == MARK_RAW_CLOSE:
                quantity = Decimal(str(record["quantity"]))
                return _Carry(quantity, quantity * Decimal(str(record["close"])), cost, bookings)
            return _Carry(
                Decimal(str(record["carried_quantity"])), Decimal(str(record["carried_value"])), cost, bookings
            )
        return None

    @staticmethod
    def _take(lots: list[list[Decimal]], quantity: Decimal, *, dearest_first: bool) -> None:
        """Remove up to ``quantity`` shares from ``lots`` ([shares, value] pairs), each lot losing value pro rata. The
        oldest lot goes first (the carried lot, then the bought lots in booking order), or with ``dearest_first`` the
        lot with the highest value per share, which leaves the lowest value behind. A quantity past every lot
        drains them and is dropped; the caller sees it as a share count that no longer matches."""

        left = quantity
        while left > 0:
            live = [lot for lot in lots if lot[0] > 0]
            if not live:
                return
            lot = max(live, key=lambda x: x[1] / x[0]) if dearest_first else live[0]
            take = min(left, lot[0])
            lot[1] = lot[1] * (lot[0] - take) / lot[0]
            lot[0] -= take
            left -= take

    @classmethod
    def _replay(
        cls,
        base: _Carry,
        since: Sequence[Any],
        *,
        dearest_first: bool = False,
        adjust: tuple[int, Decimal] | None = None,
    ) -> tuple[Decimal, Decimal]:
        """(shares, value) after the bookings in ``since``, replayed in ledger order from the committed ``base``: a
        buy opens a lot at its booked cost (price x quantity plus charges), a sell takes only the shares held at that
        moment. ``adjust`` is an unbooked share change (``u`` at position ``i``, before booking ``i``): a gain scales
        every lot's shares at the same value (a bonus or split), a loss removes shares."""

        lots: list[list[Decimal]] = [[base.quantity, base.value]]
        for index in range(len(since) + 1):
            if adjust is not None and adjust[0] == index:
                change = adjust[1]
                total = sum((lot[0] for lot in lots), Decimal(0))
                if change > 0 and total > 0:
                    for lot in lots:
                        lot[0] = lot[0] * (total + change) / total
                elif change < 0:
                    cls._take(lots, -change, dearest_first=True)
            if index == len(since):
                break
            booking = since[index]
            if str(booking.side).upper() == "BUY":
                lots.append([booking.delta_quantity, booking.delta_quantity * booking.price + booking.charges])
            elif str(booking.side).upper() == "SELL":
                cls._take(lots, booking.delta_quantity, dearest_first=dearest_first)
        return sum((lot[0] for lot in lots), Decimal(0)), sum((lot[1] for lot in lots), Decimal(0))

    def _adjustment_record(self, isin: str) -> bool:
        """True when the account exposes an explicit adjustment record for the open corporate-action pause of ``isin``."""

        seqs = [
            p.seq
            for p in policy.run_state(self._store).open_pauses
            if p.event == CORPORATE_ACTION_PAUSE and p.scope == f"{policy.SCOPE_ISIN}:{isin}"
        ]
        return bool(seqs) and self._adjustment_booked(isin, min(seqs))

    @staticmethod
    def _search_cost(bookings: int) -> int:
        """Replay steps the exhaustive search spends on a window of ``bookings`` bookings: two sale orders, each one plain
        replay plus one per adjustment position, every replay applying at most ``bookings`` bookings."""

        return 2 * (bookings + 2) * (bookings + 1)

    @staticmethod
    def _carry_floor(base: _Carry, window: Sequence[Any], held: Decimal, *, every_boundary: bool) -> Decimal:
        """A value at or below every reading the exhaustive search could return, in one backward pass (O(n log n)).

        Every lot's value per share is fixed when it opens (a sale or a loss takes value pro rata) or only goes down (a
        gain adjustment spreads the same value over more shares). A reading from boundary ``k`` opens the base lot and
        every buy of ``window[k:]``, then only loses shares. Each boundary gets its own bound, and the minimum is
        returned:

        * No gain needed (the plain replay's final stock ``F`` is at least ``held``; sales drop what the stock cannot
          cover, so ``F`` is the stock walk, not buys less sells). The ``held`` shares that survive are a subset of the
          shares that opened, each at its lot's unit value, so the reading is at least the sum of the ``held`` cheapest
          opened shares. A bounded heap of the cheapest ``held`` shares answers that as the boundary moves back and
          opens one more lot at a time. It is a floor, not the reading: a sale that can only take a cheap lot (the dear
          one has not opened yet) leaves a dearer reading than the cheapest shares.
        * A gain needed (``F < held``): the opened value less ``u_max`` (the dearest opening unit value) for every share
          that left. Shares in are ``q0`` plus the buys plus the gain ``held - F``; all but ``held`` leave.

        Both are never below ``u_min * held / (1 + held)``: shares are whole, a gain of ``g <= held`` shares scales the
        unit value of the lots it touches by at least ``1 / (1 + held)``, and no unit value is below ``u_min``. That
        keeps the floor above zero, which ``mark_session`` would read as "no value" and replace by the ledger cost."""

        zero = Decimal(0)
        tolerance = Decimal("1e-9")  # the share tolerance the search accepts a reading within
        buys = [b for b in window if str(b.side).upper() == "BUY" and b.delta_quantity > 0]
        base_unit = base.value / base.quantity
        units = [base_unit] + [(b.delta_quantity * b.price + b.charges) / b.delta_quantity for b in buys]
        positive = min(units) * held / (1 + held) if min(units) > 0 else zero

        cheapest: list[tuple[Decimal, int, Decimal, Decimal]] = []  # max-heap by unit: (-unit, tie, shares, value)
        held_shares, held_value, tie = zero, zero, 0

        def open_lot(shares: Decimal, value: Decimal) -> None:
            """Open a lot and keep only the ``held`` cheapest shares of everything opened so far."""
            nonlocal held_shares, held_value, tie
            tie += 1
            heapq.heappush(cheapest, (-(value / shares), tie, shares, value))
            held_shares += shares
            held_value += value
            while held_shares > held:
                neg_unit, order, shares, value = heapq.heappop(cheapest)
                excess = held_shares - held
                if shares <= excess:
                    held_shares -= shares
                    held_value -= value
                    continue
                kept = value * (shares - excess) / shares
                held_value -= value - kept
                held_shares = held
                heapq.heappush(cheapest, (neg_unit, order, shares - excess, kept))

        open_lot(base.quantity, base.value)
        u_max = base_unit
        # the stock walk as a map x -> max(c, x + d), built from the back so every boundary reads its final stock in O(1)
        floor_stock, shift = zero, zero
        value_in, bought = base.value, zero
        best: Decimal | None = None
        for index in range(len(window), -1, -1):
            if index < len(window):
                booking = window[index]
                side = str(booking.side).upper()
                if side == "BUY" and booking.delta_quantity > 0:
                    unit = (booking.delta_quantity * booking.price + booking.charges) / booking.delta_quantity
                    value_in += booking.delta_quantity * booking.price + booking.charges
                    bought += booking.delta_quantity
                    shift += booking.delta_quantity
                    u_max = max(u_max, unit)
                    open_lot(booking.delta_quantity, booking.delta_quantity * booking.price + booking.charges)
                elif side == "SELL":
                    floor_stock = max(floor_stock, shift)
                    shift -= booking.delta_quantity
            if not every_boundary and index != 0:
                continue
            final = max(floor_stock, base.quantity + shift)
            if final >= held - tolerance:
                reading = held_value - u_max * tolerance
            else:
                reading = value_in - u_max * (base.quantity + bought - final + tolerance)
            best = reading if best is None else min(best, reading)
        return max(best if best is not None else zero, positive)

    def _carried_value(self, holding: Holding, base: _Carry, bookings: Sequence[Any]) -> tuple[Decimal, bool]:
        """(carried value, reconciled) of ``holding`` given the last committed valuation ``base``.

        The bookings since ``base`` are replayed in ledger order (``_replay``): a sell takes only the shares held at
        that moment, the carried lot first and then the bought lots in booking order, and a buy opens a lot at the
        cost the ledger booked. That is the reconciled value, and it is used when the booking count is known and the
        replay lands on the held quantity (or the shortfall is a gain the account's own adjustment record explains,
        a bonus or split, valued as the lowest placement of it).

        Anything else is unreconciled and fails closed: a mark with no booking count, or bookings that do not add up
        to the held quantity. The value is then the LOWEST of every plausible reading (each boundary the count could
        have been, the oldest-first and the dearest-first sale order, the unexplained quantity as an adjustment at
        every position among the bookings, and the net-quantity rule), so the fallback cannot lift the peak, and the
        caller raises the ISIN-scoped ``carry_unreconciled`` pause for the operator.

        The search is bounded: when its replay steps would pass ``CARRY_SEARCH_BUDGET`` it is not run, and the value is
        the lower of the net-quantity rule and ``_carry_floor``, which no reading can fall below. A name carried
        through a bonus with a booked adjustment record is also left unreconciled in that case (the pause stands until
        the operator reviews it), since its exact lowest placement was not searched."""

        held = Decimal(holding.quantity)
        if base.quantity <= 0:
            return holding.cost, False
        tolerance = Decimal("1e-9")

        def readings(since: Sequence[Any]) -> list[Decimal]:
            values: list[Decimal] = []
            for dearest in (False, True):
                shares, value = self._replay(base, since, dearest_first=dearest)
                if abs(shares - held) <= tolerance:
                    values.append(value)
                    continue
                gap = held - shares
                for position in range(len(since) + 1):
                    shares, value = self._replay(base, since, dearest_first=dearest, adjust=(position, gap))
                    if abs(shares - held) <= tolerance:
                        values.append(value)
            return values

        net = base.value * held / base.quantity if held < base.quantity else base.value + (holding.cost - base.cost)
        if base.bookings is not None:
            since = bookings[base.bookings :]
            shares, value = self._replay(base, since)
            if abs(shares - held) <= tolerance:
                return value, True
            if self._search_cost(len(since)) > CARRY_SEARCH_BUDGET:
                return min(net, self._carry_floor(base, since, held, every_boundary=False)), False
            adjusted = readings(since)
            if adjusted and shares < held and self._adjustment_record(holding.instrument.isin):
                return min(adjusted), True
            candidates = adjusted
        else:
            if sum(self._search_cost(len(bookings) - k) for k in range(len(bookings) + 1)) > CARRY_SEARCH_BUDGET:
                return min(net, self._carry_floor(base, bookings, held, every_boundary=True)), False
            candidates = [v for k in range(len(bookings) + 1) for v in readings(bookings[k:])]
        return min([net, *candidates]), False

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
        carry: Mapping[str, _Carry],
        *,
        supersede: bool,
    ) -> list[int]:
        """Persist the evaluation inputs. Appends a line when none exists for (session, ISIN), and, with
        ``supersede``, when the existing line differs (a retry after a crash before the latch moved). Returns the
        seqs of the lines that stand for the holdings of ``snap``: the lines this evaluation reads."""

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
            if isin in carry:
                payload["carried_quantity"] = _dec(carry[isin].quantity)
                payload["carried_value"] = _dec(carry[isin].value)
            prior = latest.get(isin)
            if prior is not None and (not supersede or all(prior.get(k) == v for k, v in payload.items())):
                continue
            self._append("position_mark", payload)
        standing = self._session_position_marks(session)
        return sorted(int(standing[h.instrument.isin]["seq"]) for h in snap.holdings if h.instrument.isin in standing)

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
        carry: dict[str, _Carry] = {}
        missing: list[str] = []
        ledger = {h.instrument.isin: self._bookings(h.instrument) for h in snap.holdings}
        for holding in snap.holdings:
            isin = holding.instrument.isin
            if isin in frozen:
                # Valued at the last COMMITTED value, moved only by what the ledger booked since: a sell takes its
                # shares' carried value out, a buy adds its cost, and a quantity the ledger reconciled at the same
                # cost (a bonus or split booked) moves nothing, so a paused name can neither fire a stop nor lift the
                # peak.
                base = self._carry_base(isin) or _Carry(
                    Decimal(holding.quantity), holding.cost, holding.cost, len(ledger[isin])
                )
                held_quantity = Decimal(holding.quantity)
                value, reconciled = self._carried_value(holding, base, ledger[isin])
                if not reconciled:
                    self._pause_isin(
                        CARRY_UNRECONCILED,
                        isin,
                        "carried value does not reconcile to the ledger bookings: valued at its lowest reading until operator review",
                    )
                if not value > 0:
                    value = holding.cost
                carry[isin] = _Carry(held_quantity, value, holding.cost)
                used[isin] = value / holding.quantity
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
        input_seqs = self._write_position_marks(session, snap, used, basis, structural, carry, supersede=evaluated)
        if evaluated:
            self._record_evaluation_inputs(session, input_seqs)
            # The sell boundary is also persisted before the latch can open an exit, never inferred afterwards.
            self._record_sell_boundaries(session, snap)
            self._record_booking_boundaries(session, snap)
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
        self._commit_session(after)
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

    def _halve_original(self, isin: str, current: OpenExit) -> int:
        """Shares the halve owed when it was raised: ``floor(q / 2)`` for the ``q`` the raising evaluation marked.

        Read from the committed ``position_mark`` of the raising session. A latch written outside the portfolio has
        none, so the latch's quantity at first sight is recorded once and read back from then on.
        """

        line = next(
            (r for r in reversed(self._committed_marks()) if r.get("isin") == isin and r.get("as_of_date") == current.since.isoformat()),
            None,
        )
        if line is not None:
            return halve_quantity(int(Decimal(str(line["quantity"]))))
        scope = f"{policy.SCOPE_ISIN}:{isin}"
        tag = f"since_{current.since.isoformat()}_quantity_"
        for record in self._records():
            if record.get("kind") == "latch" and record.get("latch") == LATCH_HALVE_BASELINE and record.get("scope") == scope:
                reason = str(record["reason"])
                if reason.startswith(tag):
                    return int(reason.removeprefix(tag))
        self._append("latch", {"latch": LATCH_HALVE_BASELINE, "scope": scope, "reason": f"{tag}{current.quantity}"})
        return current.quantity

    def _bookings(self, instrument: Instrument) -> list[Any]:
        """Every booking of ``instrument`` in ledger order, each booking id once."""

        seen: dict[str, Any] = {}
        for booking in self._account.simulated_bookings():
            if booking.ticker == instrument.ticker:
                seen.setdefault(str(booking.booking_id), booking)
        return list(seen.values())

    def _sell_bookings(self, instrument: Instrument) -> list[Any]:
        """The SELL bookings of ``instrument`` in ledger order, each booking id once."""

        return [b for b in self._bookings(instrument) if str(b.side).upper() == "SELL"]

    @staticmethod
    def _own_id(obligation_id: str) -> re.Pattern[str]:
        return re.compile(re.escape(obligation_id) + r"(-t[0-9]+)?\Z")

    def _attributed_sold(self, obligation_id: str, instrument: Instrument) -> int:
        """Shares sold by the sell fills booked under this obligation's intent ids (``exit_intent_id``).

        Each booking counts once by its booking id, so a repeated read or a repeated hook adds nothing. A sell under
        another id, and every buy, is not this obligation's.
        """

        own = self._own_id(obligation_id)
        try:
            return sum(
                (_whole(b.delta_quantity) for b in self._sell_bookings(instrument) if own.match(str(b.proposal_id))), 0
            )
        except PortfolioError:
            self._abort("state_corrupt", "booked_sell_quantity_not_whole")

    def _sell_boundary(self, isin: str, since: date) -> int | None:
        """How many sells of ``isin`` the ledger held at the evaluation of ``since``, read from the line
        ``_record_sell_boundaries`` wrote BEFORE that evaluation moved the latch. ``None`` when there is none.

        Sells before the boundary are history, not part of a halve raised at ``since``. The ledger is append-only,
        so a sell past this count was booked after the halve may have been raised. The boundary is never rebuilt
        from the bookings of a later moment: a latch written outside the portfolio, or a line lost to a crash,
        leaves no boundary, and the halve is then unsizable (``ambiguous_exit_fill``)."""

        scope = f"{policy.SCOPE_ISIN}:{isin}"
        tag = f"since_{since.isoformat()}_sells_"
        found: int | None = None
        for record in self._records():
            if record.get("kind") == "latch" and record.get("latch") == LATCH_SELL_BASELINE and record.get("scope") == scope:
                reason = str(record["reason"])
                if reason.startswith(tag):
                    found = int(reason.removeprefix(tag))
        return found

    def _record_sell_boundaries(self, session: date, snap: AccountSnapshot) -> None:
        """Persist the sell booking count of every held name before the latch can open an exit for ``session``.

        The latest line for (name, session) stands, so a retry after a crash that never reached the latch supersedes
        the earlier attempt. Once the latch has evaluated the session this is not called again."""

        for holding in snap.holdings:
            isin = holding.instrument.isin
            count = len(self._sell_bookings(holding.instrument))
            if self._sell_boundary(isin, session) != count:
                self._append(
                    "latch",
                    {
                        "latch": LATCH_SELL_BASELINE,
                        "scope": f"{policy.SCOPE_ISIN}:{isin}",
                        "reason": f"since_{session.isoformat()}_sells_{count}",
                    },
                )

    def _booking_boundary(self, isin: str, session: date) -> int | None:
        """How many bookings of ``isin`` the ledger held at the evaluation of ``session``, from the line
        ``_record_booking_boundaries`` wrote before that evaluation moved the latch. The latest line for the session
        stands (a retry that never reached the latch supersedes it). ``None`` when there is none."""

        scope = f"{policy.SCOPE_ISIN}:{isin}"
        tag = f"since_{session.isoformat()}_bookings_"
        found: int | None = None
        for record in self._records():
            if record.get("kind") == "latch" and record.get("latch") == LATCH_BOOKING_BASELINE and record.get("scope") == scope:
                reason = str(record["reason"])
                if reason.startswith(tag):
                    found = int(reason.removeprefix(tag))
        return found

    def _record_booking_boundaries(self, session: date, snap: AccountSnapshot) -> None:
        """Persist the booking count of every held name before the latch moves for ``session``: the point a later
        carried valuation reads the sells and buys booked since from (``_carried_value``)."""

        for holding in snap.holdings:
            isin = holding.instrument.isin
            count = len(self._bookings(holding.instrument))
            if self._booking_boundary(isin, session) != count:
                self._append(
                    "latch",
                    {
                        "latch": LATCH_BOOKING_BASELINE,
                        "scope": f"{policy.SCOPE_ISIN}:{isin}",
                        "reason": f"since_{session.isoformat()}_bookings_{count}",
                    },
                )

    def _ambiguous_exit_fill(self, isin: str, current: OpenExit) -> bool:
        """True when a sell booked while this halve was open is not under its own intent ids (and pauses the name).

        The halve owes ``floor(q / 2)`` less its own fills. A sell under any other id (the runner minted an ordinary
        order id, a hand sale) may or may not have been meant as the halve, so what is still owed cannot be known.
        Proposing it again could sell the same shares twice, so the exit is held until an operator flattens the name.
        """

        if current.reason != HALVE:
            return False
        own = self._own_id(exit_obligation_id(HALVE, isin, current.since))
        boundary = self._sell_boundary(isin, current.since)
        if boundary is None:
            self._pause_isin(AMBIGUOUS_EXIT_FILL, isin, "no sell booking boundary was recorded before this halve opened")
            return True
        sells = self._sell_bookings(self._by_isin[isin])
        if not any(not own.match(str(b.proposal_id)) for b in sells[boundary:]):
            return False
        self._pause_isin(AMBIGUOUS_EXIT_FILL, isin, "sell booked on a name with an open halve under an id that is not the halve's")
        return True

    def _due(self, isin: str, current: OpenExit, held_quantity: int) -> int:
        """What the open exit still owes. Stop and flatten sell what is held. A halve owes what it was raised for less
        the sell fills booked under its own obligation id, never an amount derived from the aggregate holding (a buy
        that books later, or a sell that is not the exit's, cannot change it)."""

        if current.reason != HALVE:
            return held_quantity
        obligation = exit_obligation_id(HALVE, isin, current.since)
        owed = max(0, self._halve_original(isin, current) - self._attributed_sold(obligation, self._by_isin[isin]))
        return min(current.quantity, owed, held_quantity)

    def _sync_exits(self, state: RiskState, snap: AccountSnapshot) -> RiskState:
        """Reconcile every open exit and stop with the ledger (idempotent, crash safe).

        A name no longer held closes its exit and stop (the ledger holds the fill). A halve is reduced by the sell
        fills booked under its obligation id, whether or not any hook was told. The latch is written only when stale.
        """

        self._commit_session(state)
        held = {h.instrument.isin: h.quantity for h in snap.holdings}
        for isin in sorted(set(state.open_exits) | set(state.stops)):
            quantity = held.get(isin, 0)
            current = state.open_exits.get(isin)
            if quantity == 0:
                sold, remaining = 0, 0
            elif current is None:
                continue
            else:
                if self._ambiguous_exit_fill(isin, current):
                    continue  # fail closed: leave the exit as the latch has it, and propose nothing from it
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
        every = {**state.open_exits, **owed}
        # Q-6: an exit on a name with an open corporate-action pause stays in the latch (and the flatten line) but is
        # not proposed. It would sell the unadjusted quantity at the adjusted quote and book a loss that is not real.
        frozen = self._frozen_isins({h.instrument.isin for h in snap.holdings})
        held_out = tuple(sorted(isin for isin in every if isin in frozen))
        held = {h.instrument.isin for h in snap.holdings}
        ambiguous = tuple(
            sorted(i for i, e in every.items() if i in held and i not in frozen and self._ambiguous_exit_fill(i, e))
        )
        view = replace(state, open_exits={i: e for i, e in every.items() if i not in frozen and i not in ambiguous})
        if not view.open_exits:
            return ExitPlan((), (), held_out, ambiguous)
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
                        obligation_id=exit_obligation_id(intent.reason, intent.isin, since),
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
        return ExitPlan(tuple(proposals), tuple(sorted(set(waiting))), held_out, ambiguous)

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

    def _adjustment_booked(self, isin: str, pause_seq: int) -> bool:
        """True when the account exposes an explicit adjustment record for this name and the ex-date it was paused on.

        The evidence is a record the account books (``corporate_action_adjustments``: ticker, kind, ex_date), never an
        inference from quantities or cost: equal cost with a different quantity can be a buy and a sell as well as a
        bonus. A committed mark since the pause names the kind and the ex-date session; the record must match both.
        The real ``ExecutionLedger`` exposes no such record, so on it nothing is ever booked.
        """

        records = getattr(self._account, "corporate_action_adjustments", None)
        if records is None:
            return False
        ticker = self._by_isin[isin].ticker
        wanted = {
            (str(r["corporate_action"]), str(r["as_of_date"]))
            for r in self._committed_marks()
            if r.get("isin") == isin and int(r["seq"]) > pause_seq and r.get("corporate_action")
        }
        return any(
            a.ticker == ticker and (str(a.kind), a.ex_date.isoformat()) in wanted for a in records()
        )

    def release_corporate_action(self, isin: str, reason: str) -> Mapping[str, Any]:
        """The release of an ``unresolved_corporate_action`` pause (Q-6). ``policy.resume`` refuses it.

        Refused ``corporate_action_not_booked`` for a name that is still held unless the account exposes an explicit
        adjustment record for the ex-date (see ``_adjustment_booked``); without one the next mark would value the
        unadjusted quantity at the adjusted close (the false -50%). Refused ``exit_owed_in_pre_adjustment_units`` while
        a halve is open for it. The real ``ExecutionLedger`` has no adjustment record, so on it the release is refused
        while the name is held; the operator flattens the name or the pause stays open.
        A name that is no longer held is released freely. A stop or flatten held out for the name is proposed again
        from the next decide (both sell what is held).
        """

        run = policy.run_state(self._store)
        if run.aborted:
            raise policy.PolicyError("resume_refused_abort")
        if not isinstance(reason, str) or not reason.strip():
            raise policy.PolicyError("reason_required")
        if isin not in self._by_isin:
            raise PortfolioError("unknown_instrument")
        seqs = sorted(
            p.seq for p in run.open_pauses if p.event == CORPORATE_ACTION_PAUSE and p.scope == f"{policy.SCOPE_ISIN}:{isin}"
        )
        if not seqs:
            raise policy.PolicyError("nothing_to_resume")
        state = self._state()
        self._commit_session(state)
        holding = next((h for h in self.snapshot().holdings if h.instrument.isin == isin), None)
        if holding is not None:
            if not self._adjustment_booked(isin, min(seqs)):
                raise PortfolioError("corporate_action_not_booked")
            current = state.open_exits.get(isin)
            if current is not None and current.reason == HALVE:
                # A halve is a number of shares fixed before the adjustment. Re-proposed after it, it would sell the
                # wrong fraction. Fail closed: the operator flattens the name, which closes the exit.
                raise PortfolioError("exit_owed_in_pre_adjustment_units")
        return self._append("resume", {"reason": reason.strip(), "cleared_pause_seqs": seqs})

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
