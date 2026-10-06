"""Simulation loop, purged walk-forward and the holdout run (D-04, D-08, D-09, D-13, D-14, D-20).

``simulate_segment`` runs one fill scenario over a list of sessions on a fresh
``Book``. Decisions at the close of session ``d`` become orders for the next
session with ``information_as_of = d``, so 60 raises ``LookaheadError`` for any
order that is not strictly earlier than its fill session. ``LookaheadError`` and
every other input error propagate; only ``TickSizeUnavailable`` is handled, and
it becomes a recorded attempt (missing evidence), never a default tick.

``run_walk_forward`` fits the regime filter (and, in ``fit`` mode, the edge slope)
inside each fold on purged training data only, then runs the test window under
the base, adverse and pessimistic fill scenarios. ``run_holdout_segment`` does
the same for the opened holdout. Neither function reads the holdout unless the
view it is given was opened with a ``HoldoutGrant``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

from costs.core import Side, TickSizeUnavailable, sha256_hex
from costs.fills import FillScenario, FillScenarioSet
from costs.schedule import PricingBasis, ScheduleSet
from private_config.schemas import IndiaLimits
from pilot_data.universe import check_smallcap_exposure

from .data import (
    BandSource,
    Bar,
    DatasetView,
    DecisionView,
    DividendEvents,
    EligibilitySource,
    session_bar_for,
)
from .errors import StrategyIndiaError
from .folds import Fold, FoldRules, Observation, make_folds, purge_at_cutoff, purge_at_holdout
from .holdout import HoldoutRange
from .hurdle import evaluate_candidate, fit_slope
from .params import StrategyParams
from .portfolio import ENTRY_CASH_BUFFER, TICK_UNAVAILABLE, Book, FillRecord, PendingOrder, TradeRecord
from .ticks import EQUITY, TickTables, align_limit, resolve_tick
from .regime import RegimeModel, fit_regime, regime_flags
from .signals import (
    MODE_BASE,
    MODE_SENSITIVITY,
    DecisionContext,
    FeatureRow,
    MomentumProvider,
    SignalProvider,
    SignalTable,
    zscores,
)

ZERO = Decimal(0)
ONE = Decimal(1)
BPS = Decimal(10000)
SCENARIO_ORDER = ("base", "adverse", "pessimistic")


def order_information_date(decision_date: date, session_date: date) -> date:
    """The sizing information date stamped on an order: the decision date, always before the fill session."""
    return decision_date


@dataclass
class RunContext:
    view: DatasetView
    params: StrategyParams
    limits: IndiaLimits
    eligibility: EligibilitySource
    universe_policy: Any
    bands: BandSource
    unavailable: Mapping[tuple[str, date], str]
    events: DividendEvents
    scenarios: FillScenarioSet
    schedules: ScheduleSet
    pricing_basis: PricingBasis
    ticks: TickTables
    provider: SignalProvider = field(default_factory=MomentumProvider)
    _tables: dict[str, SignalTable] = field(default_factory=dict)

    def table(self, mode: str) -> SignalTable:
        if mode not in self._tables:
            self._tables[mode] = SignalTable(self.view, self.params, self.events, mode=mode)
        return self._tables[mode]


@dataclass(frozen=True)
class OpenPosition:
    anchor_isin: str
    stock_code: str
    quantity: int
    value: Decimal
    entry_date: date
    entry_score: Decimal
    exit_reason: str | None


@dataclass(frozen=True)
class SegmentResult:
    fold: str
    scenario_id: str
    mode: str
    sessions: tuple[date, ...]
    start_equity: Decimal
    end_equity: Decimal
    curve: tuple[tuple[date, Decimal], ...]
    exposure: tuple[tuple[date, Decimal], ...]
    closed: tuple[TradeRecord, ...]
    open_positions: tuple[OpenPosition, ...]
    attempts: tuple
    fills: tuple[FillRecord, ...]
    charges_total: Decimal
    traded_notional: Decimal
    swaps: int
    entries: int
    halt_events: int
    flatten_events: int
    hurdle_rejections: int
    smallcap_rejections: int
    scenario_refs: tuple[tuple[str, str, str], ...]
    contract_notes: tuple
    schedule_refs: tuple[tuple[str, str], ...]
    tick_refs: tuple[tuple[str, str], ...]
    run_chain_sha256: str

    @property
    def net_return(self) -> Decimal:
        return self.end_equity / self.start_equity - ONE

    @property
    def affected_attempts(self) -> int:
        return sum(1 for a in self.attempts if a.affected)


def _limit_price(ref: Decimal, offset_bps: Decimal, side: Side, tick) -> Decimal:
    sign = ONE if side is Side.BUY else -ONE
    return align_limit(ref * (ONE + sign * offset_bps / BPS), tick, side)


def simulate_segment(
    ctx: RunContext,
    *,
    sessions: Sequence[date],
    scenario: FillScenario,
    slope: Decimal,
    regime_cash: Mapping[date, bool] | None,
    mode: str,
    fold: str,
) -> SegmentResult:
    """One fill scenario over ``sessions`` on a fresh book."""
    if not sessions:
        raise StrategyIndiaError("a segment needs at least one session")
    params, limits, view = ctx.params, ctx.limits, ctx.view
    table = ctx.table(mode)
    book = Book(capital=limits.capital_cap, limits=limits, params=params, fold=fold)
    offset = Decimal(params.limit_offset_bps)
    n = len(sessions)
    for i, session in enumerate(sessions):
        today = view.bars_on(session)
        _execute(ctx, book, session, i, today, scenario)
        closes = {a: b.raw_close for a, b in today.items() if a in book.positions}
        ex_open = {
            a: b.raw_open for a, b in today.items() if a in book.positions and session in ctx.events.ex_dates(a)
        }
        book.mark(session, closes, ex_date_open=ex_open)
        if i == n - 1:
            break
        nxt = sessions[i + 1]
        cash_flag = bool(regime_cash.get(session, False)) if regime_cash else False
        book.update_risk(session, i, regime_cash=cash_flag)
        rebalance = i % params.rebalance_every == 0
        snapshot = None
        z: dict[str, Decimal] = {}
        if rebalance:
            snapshot = ctx.eligibility.snapshot(session)
            if snapshot.as_of != session:
                raise StrategyIndiaError("eligibility snapshot is not for the decision date")
            raw = ctx.provider.scores(DecisionContext(session, snapshot.eligible, DecisionView(view, session), table))
            if len(raw) >= params.min_universe_for_entry:
                z = zscores(raw)
            ranked = sorted(z.items(), key=lambda kv: (-kv[1], kv[0]))
            ranks = {anchor: r + 1 for r, (anchor, _) in enumerate(ranked)}
            book.request_swing_exits(session, i, ranks=ranks, eligible=snapshot.eligible)
        _place_exits(ctx, book, session, nxt, today, scenario, offset)
        if rebalance and snapshot is not None and z and not book.entries_blocked and not cash_flag:
            _place_entries(ctx, book, session, nxt, today, scenario, offset, z, slope, snapshot)
    last = sessions[-1]
    open_positions = tuple(
        OpenPosition(p.anchor_isin, p.stock_code, p.quantity, p.quantity * p.last_price, p.entry_date, p.entry_score,
                     p.exit.reason if p.exit else None)
        for p in sorted(book.positions.values(), key=lambda p: p.anchor_isin)
    )
    return SegmentResult(
        fold=fold, scenario_id=scenario.scenario_id, mode=mode, sessions=tuple(sessions),
        start_equity=book.capital, end_equity=book.equity(), curve=tuple(book.curve), exposure=tuple(book.exposure),
        closed=tuple(book.closed), open_positions=open_positions, attempts=tuple(book.attempts),
        fills=tuple(book.fills), charges_total=book.charges_total, traded_notional=book.traded_notional,
        swaps=book.swaps, entries=book.entries, halt_events=book.halt_events, flatten_events=book.flatten_events,
        hurdle_rejections=book.hurdle_rejections, smallcap_rejections=book.smallcap_rejections,
        scenario_refs=tuple(sorted(book.scenario_refs)), contract_notes=tuple(book.contract_notes),
        schedule_refs=tuple(sorted(book.schedule_refs)), tick_refs=tuple(sorted(book.tick_refs)),
        run_chain_sha256=sha256_hex("|".join(book.run_hashes)),
    )


def _execute(ctx: RunContext, book: Book, session: date, index: int, today: Mapping[str, Bar], scenario: FillScenario) -> None:
    due = [p for p in book.pending if p.session_date == session]
    if not due:
        return
    bars = {}
    for p in due:
        bar = today.get(p.anchor_isin)
        if bar is None:
            bars[p.anchor_isin] = None
            continue
        prev = ctx.view.previous_bar(p.anchor_isin, session)
        bars[p.anchor_isin] = session_bar_for(
            bar,
            previous_raw_close=prev.raw_close if prev is not None else None,
            observation=ctx.bands.observe(bar.isin, session),
            unavailable_reason=ctx.unavailable.get((bar.isin, session)),
            tick=p.tick,
        )
    book.execute_session(session, index, bars, scenario=scenario, schedules=ctx.schedules, pricing_basis=ctx.pricing_basis)


def _order_id(book: Book, scenario: FillScenario, decision: date, anchor: str, side: Side) -> str:
    return f"{book.fold}|{scenario.scenario_id}|{decision.isoformat()}|{anchor}|{side.value}"


def _place_exits(ctx: RunContext, book: Book, session: date, nxt: date, today: Mapping[str, Bar],
                 scenario: FillScenario, offset: Decimal) -> None:
    for anchor, pos in sorted(book.positions.items()):
        if pos.exit is None or any(o.anchor_isin == anchor for o in book.pending):
            continue
        bar = today.get(anchor)
        ref = bar.raw_close if bar is not None else pos.last_price
        series = ctx.view.series(anchor, end=session)[-1].series
        quantity = min(pos.exit.quantity if pos.exit.quantity is not None else pos.quantity, pos.quantity)
        try:
            tick = resolve_tick(ctx.ticks, session_date=nxt, band_reference_price=ref, instrument_class=EQUITY, series=series)
        except TickSizeUnavailable:
            book.record_attempt(outcome=TICK_UNAVAILABLE, reason_code=TICK_UNAVAILABLE, affected=True, session=nxt,
                                decision_date=session, anchor=anchor, stock_code=pos.stock_code, kind="exit",
                                reason=pos.exit.reason)
            pos.blocked_exit_sessions += 1
            continue
        book.queue(
            PendingOrder(
                order_id=_order_id(book, scenario, session, anchor, Side.SELL), anchor_isin=anchor,
                stock_code=pos.stock_code, side=Side.SELL, quantity=quantity,
                limit_price=_limit_price(ref, offset, Side.SELL, tick), reference_price=ref, decision_date=session,
                session_date=nxt, information_as_of=order_information_date(session, nxt), tick=tick, kind="exit",
                reason=pos.exit.reason, score=pos.entry_score,
            )
        )


def _place_entries(ctx: RunContext, book: Book, session: date, nxt: date, today: Mapping[str, Bar],
                   scenario: FillScenario, offset: Decimal, z: Mapping[str, Decimal], slope: Decimal, snapshot) -> None:
    params, limits = ctx.params, ctx.limits
    ranked = [a for a, _ in sorted(z.items(), key=lambda kv: (-kv[1], kv[0]))][: params.max_positions]
    pending = {o.anchor_isin for o in book.pending}
    for anchor in ranked:
        if book.free_slots() <= 0:
            break
        if anchor in book.positions or anchor in pending:
            continue
        bar = today.get(anchor)
        if bar is None or bar.quarantined:
            continue
        ref = bar.raw_close
        adv = ctx.view.median_traded_value(anchor, session, params.adv_window)
        if adv is None:
            continue
        budget = min(Decimal(limits.per_position_cap), book.deployable(),
                     Decimal(params.liquidity_adv_fraction) * adv)
        quantity = int(budget // ref)
        if quantity < 1:
            continue
        verdict = evaluate_candidate(
            z=z[anchor], slope=slope, isin=bar.isin, quantity=quantity, reference_price=ref, session_date=nxt,
            params=params, schedules=ctx.schedules, pricing_basis=ctx.pricing_basis,
        )
        if not verdict.clears:
            book.hurdle_rejections += 1
            continue
        values = {a: p.quantity * p.last_price for a, p in book.positions.items()}
        for o in book.pending:
            if o.side is Side.BUY:
                values[o.anchor_isin] = o.quantity * o.limit_price
        values[anchor] = quantity * ref
        exposure = check_smallcap_exposure(
            values, capital=Decimal(limits.capital_cap), classification=snapshot.smallcap,
            policy=ctx.universe_policy, workspace="india",
        )
        if not exposure.passed:
            book.smallcap_rejections += 1
            continue
        try:
            tick = resolve_tick(ctx.ticks, session_date=nxt, band_reference_price=ref, instrument_class=EQUITY, series=bar.series)
        except TickSizeUnavailable:
            book.record_attempt(outcome=TICK_UNAVAILABLE, reason_code=TICK_UNAVAILABLE, affected=True, session=nxt,
                                decision_date=session, anchor=anchor, stock_code=bar.stock_code, kind="entry",
                                reason="momentum_entry")
            pending.add(anchor)  # one attempt per name per decision
            continue
        limit = _limit_price(ref, offset, Side.BUY, tick)
        quantity = min(quantity, int(book.deployable() / (limit * (ONE + ENTRY_CASH_BUFFER))))
        if quantity < 1:
            continue
        book.queue(
            PendingOrder(
                order_id=_order_id(book, scenario, session, anchor, Side.BUY), anchor_isin=anchor,
                stock_code=bar.stock_code, side=Side.BUY, quantity=quantity, limit_price=limit, reference_price=ref,
                decision_date=session, session_date=nxt, information_as_of=order_information_date(session, nxt),
                tick=tick, kind="entry", reason="momentum_entry", score=z[anchor],
            )
        )
        pending.add(anchor)


# ---- walk-forward --------------------------------------------------------------------
@dataclass(frozen=True)
class FoldResult:
    fold: Fold
    regime: dict[str, Any]
    slope: Decimal
    slope_fitted: bool
    training_trades_kept: int
    training_trades_purged: int
    scenarios: dict[str, SegmentResult]
    sensitivity: SegmentResult | None

    @property
    def gate(self) -> SegmentResult:
        return self.scenarios["pessimistic"]


@dataclass(frozen=True)
class WalkForwardResult:
    rules: FoldRules
    folds: tuple[FoldResult, ...]
    dev_sessions: int


def _label_observations(ctx: RunContext, dev_sessions: Sequence[date]) -> list[Observation]:
    """Label ledger for edge-slope fitting: the registered prior strategy, no regime filter, under the gate scenario."""
    gate = ctx.scenarios.gate()
    ledger = simulate_segment(
        ctx, sessions=dev_sessions, scenario=gate, slope=Decimal(ctx.params.edge_map.slope), regime_cash=None,
        mode=MODE_BASE, fold="label",
    )
    out = [Observation(t.entry_date, t.exit_date, t.entry_score, t.net_return) for t in ledger.closed]
    out += [Observation(p.entry_date, None, p.entry_score, ZERO) for p in ledger.open_positions]
    return out


def _flags_for(model: RegimeModel, features: Sequence[FeatureRow], spec, start: date, end: date) -> dict[date, bool]:
    rows = [f for f in features if start <= f.session <= end]
    return regime_flags(model, rows, spec)


def fit_fold_components(
    ctx: RunContext,
    features: Sequence[FeatureRow],
    *,
    cutoff: date,
    observations: Sequence[Observation] | None,
    holdout_start: date | None = None,
) -> tuple[RegimeModel, Decimal, bool, int, int]:
    """Fit the regime model and the edge slope on training data only (purged)."""
    train = [f for f in features if f.session <= cutoff]
    if not train:
        raise StrategyIndiaError("no regime features exist up to the fitting cutoff")
    model = fit_regime(train, spec=ctx.params.regime, seed=ctx.params.seed)
    kept: list[Observation] = []
    purged: list[Observation] = []
    if observations is not None:
        if holdout_start is None:
            kept, purged = purge_at_cutoff(observations, cutoff)
        else:
            kept, purged = purge_at_holdout(observations, holdout_start)
    slope, _, fitted = fit_slope(kept, ctx.params.edge_map)
    return model, slope, fitted, len(kept), len(purged)


def run_walk_forward(ctx: RunContext, rules: FoldRules) -> WalkForwardResult:
    """Purged, forward-only folds over the development window."""
    dev_sessions = ctx.view.sessions()
    folds = make_folds(dev_sessions, rules)
    features = ctx.table(MODE_BASE).market_features()
    observations = _label_observations(ctx, dev_sessions) if ctx.params.edge_map.mode == "fit" else None
    results: list[FoldResult] = []
    for fold in folds:
        model, slope, fitted, kept, purged = fit_fold_components(
            ctx, features, cutoff=fold.cutoff, observations=observations
        )
        test_sessions = [s for s in dev_sessions if fold.in_test(s)]
        flags = _flags_for(model, features, ctx.params.regime, fold.test_start, fold.test_end)
        scen = {
            s.scenario_id: simulate_segment(ctx, sessions=test_sessions, scenario=s, slope=slope, regime_cash=flags,
                                            mode=MODE_BASE, fold=str(fold.index))
            for s in ctx.scenarios.all()
        }
        sens = None
        if ctx.events:
            sens = simulate_segment(ctx, sessions=test_sessions, scenario=ctx.scenarios.gate(), slope=slope,
                                    regime_cash=flags, mode=MODE_SENSITIVITY, fold=str(fold.index))
        results.append(FoldResult(fold, model.record(), slope, fitted, kept, purged, scen, sens))
    return WalkForwardResult(rules, tuple(results), len(dev_sessions))


@dataclass(frozen=True)
class HoldoutRun:
    holdout: HoldoutRange
    regime: dict[str, Any]
    slope: Decimal
    scenarios: dict[str, SegmentResult]
    sensitivity: SegmentResult | None
    sessions: tuple[date, ...]


def run_holdout_segment(ctx: RunContext, holdout: HoldoutRange) -> HoldoutRun:
    """Evaluate the opened holdout. ``ctx.view`` must have been opened with a ``HoldoutGrant``."""
    if not ctx.view.opened:
        raise StrategyIndiaError("the holdout view is not opened")
    all_sessions = ctx.view.sessions()
    dev_sessions = [s for s in all_sessions if s < holdout.start]
    sessions = [s for s in all_sessions if holdout.contains(s)]
    if not dev_sessions or not sessions:
        raise StrategyIndiaError("the holdout evaluation needs development sessions and holdout sessions")
    features = ctx.table(MODE_BASE).market_features()
    observations = _label_observations(ctx, dev_sessions) if ctx.params.edge_map.mode == "fit" else None
    model, slope, _, _, _ = fit_fold_components(
        ctx, features, cutoff=dev_sessions[-1], observations=observations, holdout_start=holdout.start
    )
    flags = _flags_for(model, features, ctx.params.regime, holdout.start, holdout.end)
    scen = {
        s.scenario_id: simulate_segment(ctx, sessions=sessions, scenario=s, slope=slope, regime_cash=flags,
                                        mode=MODE_BASE, fold="holdout")
        for s in ctx.scenarios.all()
    }
    sens = None
    if ctx.events:
        sens = simulate_segment(ctx, sessions=sessions, scenario=ctx.scenarios.gate(), slope=slope, regime_cash=flags,
                                mode=MODE_SENSITIVITY, fold="holdout")
    return HoldoutRun(holdout, model.record(), slope, scen, sens, tuple(sessions))
