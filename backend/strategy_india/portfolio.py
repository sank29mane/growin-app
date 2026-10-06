"""Portfolio, risk rules and the blocked-trade behaviour of D-01a (D-08, D-14).

``Book`` holds cash, positions and the order queue for one run segment. It
drives 60's ``simulate_and_price`` for each session's orders and applies the
fills it gets back. What it guarantees:

* An entry on a ``BandUnavailable`` bar (``NO_ASSUMED_FILL``) is not filled, cash
  does not move, and the attempt is recorded by target and fold.
* An exit that does not fill leaves the position open at full quantity. Its
  capital stays reserved (cash is only released by a fill), it is marked to raw
  closes every session, the exit intent survives, a fresh order is placed for
  the next session and realised P&L uses the later fill. A stop and a -15%
  flatten are delayed the same way, never dropped.
* -8% drawdown halts new entries and halves positions, -15% flattens, the
  position stop fires, and the minimum hold never delays a stop, a flatten or a
  regime cash exit.

Money is Decimal. Charges for a day are split across that day's fills pro rata
by notional (the cash effect is exact; the per-trade attribution of
buy-only and sell-only lines is approximate and stated in the report).
"""

from __future__ import annotations

import decimal
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal

from costs.charges import price_trade_day
from costs.core import COST_CONTEXT, IST, Side, TradeFill, round_money
from costs.fills import FillOutcome, FillResult, FillScenario, LimitOrder, SessionBar, TickSize
from costs.run import simulate_and_price
from costs.schedule import PricingBasis, ScheduleSet
from private_config.schemas import IndiaLimits

from .errors import StrategyIndiaError
from .params import StrategyParams

ZERO = Decimal(0)
ONE = Decimal(1)
SUBMIT_TIME = time(8, 30)
ENTRY_CASH_BUFFER = Decimal("0.005")  # reserve this much above notional for buy-day charges

PRIORITY = {
    "flatten": 5,
    "stop": 4,
    "regime_cash": 3,
    "swing_max_hold": 2,
    "rank_rotation": 2,
    "ineligible": 2,
    "halve": 1,
}
FORCED_REASONS = frozenset({"flatten", "stop", "regime_cash"})  # never delayed by the minimum hold

TICK_UNAVAILABLE = "TICK_UNAVAILABLE"
NO_BAR = "NO_BAR"


@dataclass
class ExitIntent:
    reason: str
    quantity: int | None  # None: the whole position
    since: date
    since_index: int


@dataclass
class Position:
    anchor_isin: str
    isin: str
    stock_code: str
    quantity: int
    bought_qty: int
    cost_total: Decimal
    buy_cost_total: Decimal
    entry_date: date
    entry_index: int
    entry_score: Decimal
    last_price: Decimal
    stop_ratio: Decimal = ONE
    pnl: Decimal = ZERO
    charges: Decimal = ZERO
    exit: ExitIntent | None = None
    blocked_exit_sessions: int = 0
    first_exit_index: int | None = None


@dataclass(frozen=True)
class PendingOrder:
    order_id: str
    anchor_isin: str
    stock_code: str
    side: Side
    quantity: int
    limit_price: Decimal
    reference_price: Decimal
    decision_date: date
    session_date: date
    information_as_of: date
    tick: TickSize
    kind: str  # entry | exit
    reason: str
    score: Decimal


@dataclass(frozen=True)
class TradeRecord:
    anchor_isin: str
    stock_code: str
    entry_date: date
    exit_date: date  # when the last share was actually sold: the outcome-availability date
    quantity: int
    net_pnl: Decimal
    net_return: Decimal
    exit_reason: str
    entry_score: Decimal
    charges: Decimal
    blocked_exit_sessions: int


@dataclass(frozen=True)
class Attempt:
    """One entry or exit attempt. ``affected`` marks unsupported-data outcomes: missing evidence, not a market miss."""

    fold: str
    session: date
    decision_date: date
    anchor_isin: str
    stock_code: str
    kind: str
    reason: str
    outcome: str
    reason_code: str
    affected: bool


@dataclass(frozen=True)
class FillRecord:
    result: FillResult
    kind: str
    stock_code: str
    anchor_isin: str


@dataclass
class Book:
    capital: Decimal
    limits: IndiaLimits
    params: StrategyParams
    fold: str
    cash: Decimal = ZERO
    positions: dict[str, Position] = field(default_factory=dict)
    pending: list[PendingOrder] = field(default_factory=list)
    closed: list[TradeRecord] = field(default_factory=list)
    attempts: list[Attempt] = field(default_factory=list)
    fills: list[FillRecord] = field(default_factory=list)
    charges_total: Decimal = ZERO
    traded_notional: Decimal = ZERO
    swaps: int = 0
    entries: int = 0
    halted: bool = False
    flattened: bool = False
    halt_events: int = 0
    flatten_events: int = 0
    peak: Decimal = ZERO
    curve: list[tuple[date, Decimal]] = field(default_factory=list)
    exposure: list[tuple[date, Decimal]] = field(default_factory=list)
    run_hashes: list[str] = field(default_factory=list)
    contract_notes: list = field(default_factory=list)
    scenario_refs: set[tuple[str, str, str]] = field(default_factory=set)
    schedule_refs: set[tuple[str, str]] = field(default_factory=set)
    tick_refs: set[tuple[str, str]] = field(default_factory=set)
    hurdle_rejections: int = 0
    smallcap_rejections: int = 0

    def __post_init__(self) -> None:
        if not self.cash:
            self.cash = self.capital
        self.peak = self.capital

    # ---- valuation ------------------------------------------------------------
    def invested(self) -> Decimal:
        return sum((p.quantity * p.last_price for p in self.positions.values()), ZERO)

    def equity(self) -> Decimal:
        return self.cash + self.invested()

    def reserved_cash(self) -> Decimal:
        return sum(
            (o.quantity * o.limit_price * (ONE + ENTRY_CASH_BUFFER) for o in self.pending if o.side is Side.BUY), ZERO
        )

    def deployable(self) -> Decimal:
        """Capital an entry may use. A position whose exit is blocked still counts as invested, so its capital stays reserved."""
        by_cash = self.cash - self.reserved_cash()
        by_cap = self.capital - self.invested() - self.reserved_cash()
        return max(ZERO, min(by_cash, by_cap))

    def drawdown(self) -> Decimal:
        return self.equity() / self.peak - ONE if self.peak > 0 else ZERO

    def pending_entries(self) -> int:
        return sum(1 for o in self.pending if o.kind == "entry")

    def free_slots(self) -> int:
        return max(0, self.params.max_positions - len(self.positions) - self.pending_entries())

    # ---- execution ------------------------------------------------------------
    def queue(self, order: PendingOrder) -> None:
        if any(o.anchor_isin == order.anchor_isin for o in self.pending):
            raise StrategyIndiaError("one pending order per name per session")
        self.pending.append(order)

    def record_attempt(self, *, outcome: str, reason_code: str, affected: bool,
                       session: date, decision_date: date, anchor: str, stock_code: str, kind: str, reason: str) -> None:
        self.attempts.append(
            Attempt(self.fold, session, decision_date, anchor, stock_code, kind, reason, outcome, reason_code, affected)
        )

    def execute_session(
        self,
        session: date,
        session_index: int,
        bars: Mapping[str, SessionBar | None],
        *,
        scenario: FillScenario,
        schedules: ScheduleSet,
        pricing_basis: PricingBasis,
    ) -> None:
        """Run this session's queued orders through 60 and apply the fills. Unfilled orders expire at the close."""
        due = [o for o in self.pending if o.session_date == session]
        self.pending = [o for o in self.pending if o.session_date != session]
        if not due:
            return
        orders: list[LimitOrder] = []
        used: dict[str, SessionBar] = {}
        by_id: dict[str, PendingOrder] = {}
        for p in due:
            bar = bars.get(p.anchor_isin)
            if bar is None:
                self.record_attempt(outcome=NO_BAR, reason_code=NO_BAR, affected=False, session=session,
                                    decision_date=p.decision_date, anchor=p.anchor_isin, stock_code=p.stock_code,
                                    kind=p.kind, reason=p.reason)
                self._note_blocked(p, session_index)
                continue
            by_id[p.order_id] = p
            used[bar.isin] = bar
            orders.append(
                LimitOrder(
                    order_id=p.order_id, isin=bar.isin, exchange="NSE", side=p.side, quantity=p.quantity,
                    limit_price=p.limit_price, reference_price=p.reference_price, session_date=session,
                    submitted_at=datetime.combine(session, SUBMIT_TIME, tzinfo=IST),
                    information_as_of=p.information_as_of, tick=p.tick,
                )
            )
        if not orders:
            return
        run = simulate_and_price(
            workspace="india", currency="INR", orders=orders, bars=list(used.values()), scenario=scenario,
            schedules=schedules, pricing_basis=pricing_basis,
        )
        self.run_hashes.append(run.run_hash)
        self.scenario_refs.add((run.scenario_id, run.scenarios_version, run.scenarios_hash))
        for day in run.days:
            self.contract_notes.append(day)
            self.schedule_refs.add((day.schedule_version, day.schedule_hash))
        filled = [r for r in run.fills if r.filled_quantity > 0]
        total_notional = sum((r.notional for r in filled), ZERO)
        allocated = self._allocate(run.total_charges, filled, total_notional)
        for result in run.fills:
            p = by_id[result.order_id]
            self.tick_refs.add((result.tick_source, result.tick_source_hash))
            affected = result.outcome is FillOutcome.NO_ASSUMED_FILL
            self.record_attempt(outcome=result.outcome.value, reason_code=result.reason_code, affected=affected,
                                session=session, decision_date=p.decision_date, anchor=p.anchor_isin,
                                stock_code=p.stock_code, kind=p.kind, reason=p.reason)
            if result.filled_quantity <= 0:
                self._note_blocked(p, session_index)
                continue
            self.fills.append(FillRecord(result, p.kind, p.stock_code, p.anchor_isin))
            charge = allocated[result.order_id]
            if p.side is Side.BUY:
                self._apply_buy(p, result, charge, session, session_index)
            else:
                self._apply_sell(p, result, charge, session, session_index)
        if self.cash < Decimal("-0.01"):
            raise StrategyIndiaError("cash would be overdrawn; the entry cash reserve was too small")

    def _note_blocked(self, order: PendingOrder, session_index: int) -> None:
        if order.kind != "exit":
            return
        pos = self.positions.get(order.anchor_isin)
        if pos is not None:
            pos.blocked_exit_sessions += 1

    @staticmethod
    def _allocate(total: Decimal, filled: Sequence[FillResult], notional: Decimal) -> dict[str, Decimal]:
        out: dict[str, Decimal] = {}
        if not filled:
            return out
        with decimal.localcontext(COST_CONTEXT):
            running = ZERO
            for i, r in enumerate(filled):
                if i == len(filled) - 1:
                    share = total - running
                else:
                    share = round_money(total * r.notional / notional)
                    running += share
                out[r.order_id] = share
        return out

    def _apply_buy(self, p: PendingOrder, r: FillResult, charge: Decimal, session: date, index: int) -> None:
        assert r.fill_price is not None
        cost = r.notional + charge
        self.cash -= cost
        self.charges_total += charge
        self.traded_notional += r.notional
        pos = self.positions.get(p.anchor_isin)
        if pos is None:
            self.positions[p.anchor_isin] = Position(
                anchor_isin=p.anchor_isin, isin=r.isin, stock_code=p.stock_code, quantity=r.filled_quantity,
                bought_qty=r.filled_quantity, cost_total=cost, buy_cost_total=cost, entry_date=session, entry_index=index, entry_score=p.score,
                last_price=r.fill_price, charges=charge,
            )
            self.entries += 1
        else:
            pos.quantity += r.filled_quantity
            pos.bought_qty += r.filled_quantity
            pos.cost_total += cost
            pos.buy_cost_total += cost
            pos.charges += charge

    def _apply_sell(self, p: PendingOrder, r: FillResult, charge: Decimal, session: date, index: int) -> None:
        pos = self.positions[p.anchor_isin]
        net = r.notional - charge
        self.cash += net
        self.charges_total += charge
        self.traded_notional += r.notional
        with decimal.localcontext(COST_CONTEXT):
            removed = pos.cost_total * r.filled_quantity / pos.quantity
        pos.cost_total -= removed
        pos.quantity -= r.filled_quantity
        pos.pnl += net - removed
        pos.charges += charge
        intent = pos.exit
        if intent is not None and intent.quantity is not None:
            remaining = intent.quantity - r.filled_quantity
            pos.exit = None if remaining <= 0 else ExitIntent(intent.reason, remaining, intent.since, intent.since_index)
        if pos.quantity == 0:
            reason = intent.reason if intent is not None else p.reason
            self.closed.append(
                TradeRecord(
                    anchor_isin=pos.anchor_isin, stock_code=pos.stock_code, entry_date=pos.entry_date, exit_date=session,
                    quantity=pos.bought_qty,
                    net_pnl=pos.pnl, net_return=pos.pnl / pos.buy_cost_total, exit_reason=reason,
                    entry_score=pos.entry_score, charges=pos.charges, blocked_exit_sessions=pos.blocked_exit_sessions,
                )
            )
            self.swaps += 1
            del self.positions[p.anchor_isin]

    def exit_costs_at_mark(self, session: date, *, schedules: ScheduleSet, pricing_basis: PricingBasis) -> Decimal:
        """Sell-side charges to close every open position at its last mark on ``session`` (read-only).

        Same cost model as a real exit: ``price_trade_day`` over SELL fills on one NSE contract note, the schedule
        resolved for ``session`` and the run's pricing basis, so brokerage, STT, DP and GST are the ones a filled
        exit pays. The sale price is the last mark, the same raw close the ETF benchmark sells at. The book is
        not changed, so ordinary mark-to-market and every fold figure stay exactly as they were.
        """
        fills = [
            TradeFill(f"liquidate|{pos.anchor_isin}", pos.isin, "NSE", Side.SELL, pos.quantity, pos.last_price, session)
            for _, pos in sorted(self.positions.items()) if pos.quantity > 0
        ]
        if not fills:
            return ZERO
        day = price_trade_day(fills, schedules.resolve(session, pricing_basis), workspace="india", currency="INR",
                              pricing_basis=pricing_basis)
        return day.total

    # ---- marking ---------------------------------------------------------------
    def mark(self, session: date, closes: Mapping[str, Decimal], *, ex_date_open: Mapping[str, Decimal] | None = None) -> Decimal:
        """Mark every position to its raw close, update stop ratios and record equity and exposure.

        ``ex_date_open`` (D-20) names positions whose session is a ``dividend_amount_unknown`` ex-date: their stop
        return uses close over open, so the ex-date gap cannot trip the position stop. Equity always uses raw closes.
        """
        ex_open = ex_date_open or {}
        for anchor, pos in self.positions.items():
            close = closes.get(anchor)
            if close is None:
                continue
            if anchor in ex_open and pos.entry_date < session:
                step = close / ex_open[anchor]
            else:
                step = close / pos.last_price
            pos.stop_ratio *= step
            pos.last_price = close
        equity = self.equity()
        if equity > self.peak:
            self.peak = equity
        self.curve.append((session, equity))
        self.exposure.append((session, self.invested() / equity if equity > 0 else ZERO))
        return equity

    # ---- risk rules (D-08) -----------------------------------------------------
    def _set_intent(self, pos: Position, reason: str, quantity: int | None, session: date, index: int) -> None:
        current = pos.exit
        if current is not None and PRIORITY[current.reason] >= PRIORITY[reason]:
            return
        pos.exit = ExitIntent(reason, quantity, current.since if current else session,
                              current.since_index if current else index)
        if pos.first_exit_index is None:
            pos.first_exit_index = index

    def update_risk(self, session: date, index: int, *, regime_cash: bool) -> None:
        """Apply drawdown, stop and regime rules at the close of ``session``. None of them waits for the minimum hold."""
        dd = self.drawdown()
        flatten = Decimal(self.limits.drawdown_flatten)
        halt = Decimal(self.limits.drawdown_halt)
        if dd <= flatten:
            if not self.flattened:
                self.flattened = True
                self.flatten_events += 1
            for pos in self.positions.values():
                self._set_intent(pos, "flatten", None, session, index)
        elif dd <= halt and not self.halted:
            self.halted = True
            self.halt_events += 1
            for pos in self.positions.values():
                half = pos.quantity // 2
                if half >= 1:
                    self._set_intent(pos, "halve", half, session, index)
        if self.halted and dd > Decimal(self.params.halt_release):
            self.halted = False
        stop = Decimal(self.limits.position_stop)
        for pos in self.positions.values():
            if pos.stop_ratio - ONE <= stop:
                self._set_intent(pos, "stop", None, session, index)
            if regime_cash:
                self._set_intent(pos, "regime_cash", None, session, index)

    def request_swing_exits(self, session: date, index: int, *, ranks: Mapping[str, int], eligible: frozenset[str] | None) -> None:
        """Swing, rank and eligibility exits. These respect the minimum hold; forced exits do not."""
        for anchor, pos in self.positions.items():
            held = index - pos.entry_index
            if held < self.params.min_hold_sessions:
                continue
            if held >= self.params.max_hold_sessions:
                self._set_intent(pos, "swing_max_hold", None, session, index)
            elif eligible is not None and anchor not in eligible:
                self._set_intent(pos, "ineligible", None, session, index)
            elif anchor in ranks and ranks[anchor] > self.params.hold_rank_cutoff:
                self._set_intent(pos, "rank_rotation", None, session, index)

    @property
    def entries_blocked(self) -> bool:
        return self.halted or self.flattened
