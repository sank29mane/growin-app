"""AC-8, AC-9 (blocked entry and exit, D-01a) and AC-13 (portfolio and risk, D-08)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from costs.core import Side
from costs.fills import BandUnavailable, FillOutcome, PriceBand, SessionBar, load_fill_scenarios
from costs.schedule import PricingBasis, load_schedule_set

from strategy_india.engine import simulate_segment
from strategy_india.errors import ParamsError
from strategy_india.holdout import HoldoutRange
from strategy_india.params import parse_params, placeholder_params
from strategy_india.portfolio import Book, PendingOrder
from strategy_india.ticks import EQUITY, resolve_tick

from test_strategy_india_support import (
    SCHEDULE_VERSION,
    SESSION_START,
    default_names,
    etf_names,
    limits,
    make_context,
    make_rows,
    params,
    sha,
    weekday_sessions,
)

D = weekday_sessions(SESSION_START, 12)
GATE = load_fill_scenarios().gate()
SCHEDULES = load_schedule_set()
BASIS = PricingBasis.pinned(SCHEDULE_VERSION)
CAPITAL = Decimal("50000")
FIXED = lambda day: PriceBand("fixed", Decimal("60"), Decimal("140"), day, "synthetic", sha("band"))  # noqa: E731


def tick(day):
    return resolve_tick(_tables(), session_date=day, band_reference_price=Decimal("100"), instrument_class=EQUITY, series="EQ")


def _tables():
    from test_strategy_india_support import tick_tables

    return tick_tables()


def sbar(day, band="fixed", *, low="98", high="104", open_="100", close="101", anchor="A") -> SessionBar:
    return SessionBar(isin=anchor, exchange="NSE", session_date=day, open=Decimal(open_), high=Decimal(high),
                      low=Decimal(low), close=Decimal(close), volume=2_000_000, price_basis="raw",
                      price_band=FIXED(day) if band == "fixed" else BandUnavailable("band_crosscheck_row_conflict"),
                      source="synthetic")


def order(book: Book, side: Side, qty: int, limit: str, decision: date, session: date, *, kind=None, reason="r", anchor="A") -> PendingOrder:
    p = PendingOrder(
        order_id=f"{book.fold}|{decision}|{anchor}|{side.value}", anchor_isin=anchor, stock_code=anchor, side=side,
        quantity=qty, limit_price=Decimal(limit), reference_price=Decimal("100"), decision_date=decision,
        session_date=session, information_as_of=decision, tick=tick(session),
        kind=kind or ("entry" if side is Side.BUY else "exit"), reason=reason, score=Decimal(1),
    )
    book.queue(p)
    return p


def run(book: Book, day: date, index: int, bar: SessionBar | None) -> None:
    book.execute_session(day, index, {"A": bar}, scenario=GATE, schedules=SCHEDULES, pricing_basis=BASIS)


def new_book(**kw) -> Book:
    return Book(capital=CAPITAL, limits=kw.pop("limits_obj", limits()), params=kw.pop("params_obj", params()), fold="1")


def with_position(qty=50, **kw) -> Book:
    book = new_book(**kw)
    order(book, Side.BUY, qty, "100.00", D[0], D[1])
    run(book, D[1], 1, sbar(D[1]))
    assert book.positions["A"].quantity == qty
    book.mark(D[1], {"A": Decimal("100")})
    return book


# ---- AC-8 -----------------------------------------------------------------------------------------
def test_entry_on_an_unavailable_band_is_not_filled_and_is_counted_by_target_and_fold():
    book = new_book()
    order(book, Side.BUY, 50, "100.00", D[0], D[1])
    run(book, D[1], 1, sbar(D[1], band="unavailable"))
    assert book.positions == {} and book.cash == CAPITAL and book.fills == [] and book.closed == []
    (attempt,) = book.attempts
    assert (attempt.fold, attempt.stock_code, attempt.kind) == ("1", "A", "entry")
    assert attempt.outcome == FillOutcome.NO_ASSUMED_FILL.value and attempt.affected
    assert attempt.reason_code == "NO_FILL_BAND_UNAVAILABLE"
    assert book.pending == []  # the order expired at the close; nothing is carried silently


def test_the_same_entry_fills_on_a_known_band():
    book = new_book()
    order(book, Side.BUY, 50, "100.00", D[0], D[1])
    run(book, D[1], 1, sbar(D[1]))
    assert book.positions["A"].quantity == 50 and book.cash < CAPITAL
    assert not book.attempts[0].affected


# ---- AC-9 -----------------------------------------------------------------------------------------
def _intent(book: Book, reason: str) -> None:
    """Put the position into the exit state for ``reason`` through the real rules."""
    pos = book.positions["A"]
    if reason == "swing_max_hold":
        book.request_swing_exits(D[3], 3, ranks={"A": 99}, eligible=None)
    elif reason == "stop":
        book.mark(D[3], {"A": Decimal("85")})
        book.update_risk(D[3], 3, regime_cash=False)
    elif reason == "flatten":
        book.mark(D[3], {"A": Decimal("70")})
        book.update_risk(D[3], 3, regime_cash=False)
    assert pos.exit is not None and pos.exit.reason in {"swing_max_hold", "rank_rotation", "stop", "flatten"}


@pytest.mark.parametrize("reason, qty", [("swing_max_hold", 50), ("stop", 50), ("flatten", 300)])
def test_blocked_exit_keeps_the_position_open_and_retries_next_session(reason, qty):
    book = with_position(qty)
    cash_before, cost_before = book.cash, book.positions["A"].cost_total
    _intent(book, reason)
    mark_close = book.positions["A"].last_price
    # session D[4]: the exit order meets an unavailable band
    first = order(book, Side.SELL, qty, "90.00", D[3], D[4])
    invested_before = book.invested()
    run(book, D[4], 4, sbar(D[4], band="unavailable", low="60", high="140", open_="100", close=str(mark_close)))
    pos = book.positions["A"]
    assert pos.quantity == qty, "the position stays open at full quantity"
    assert book.cash == cash_before, "no cash is released by an unfilled exit"
    assert pos.cost_total == cost_before and book.closed == [] and book.swaps == 0
    assert pos.exit is not None and pos.blocked_exit_sessions == 1, "the exit intent survives"
    assert book.attempts[-1].affected and book.attempts[-1].kind == "exit"
    assert book.pending == []
    # capital stays reserved: nothing deployable beyond what the open exposure leaves
    assert book.deployable() <= CAPITAL - book.invested() and book.invested() == invested_before
    # exposure is marked to raw closes each session
    equity = book.mark(D[4], {"A": Decimal("95")})
    assert book.exposure[-1] == (D[4], book.invested() / equity) and book.exposure[-1][1] > 0
    assert book.curve[-1] == (D[4], equity)
    # session D[5]: a fresh order, a known band, and the exit fills there
    later = order(book, Side.SELL, qty, "95.00", D[4], D[5])
    run(book, D[5], 5, sbar(D[5], low="94", high="99", open_="96", close="96"))
    assert book.positions == {}
    (trade,) = book.closed
    assert trade.exit_date == D[5] and trade.blocked_exit_sessions == 1 and trade.exit_reason == pos.exit.reason
    assert book.fills[-1].result.fill_price == later.limit_price != first.limit_price
    assert book.cash == CAPITAL + trade.net_pnl, "realised P&L uses the later fill"
    assert book.swaps == 1


def test_a_blocked_exit_does_not_free_capital_for_a_new_entry():
    book = with_position(300)  # 30,000 of a 50,000 book
    book.request_swing_exits(D[3], 3, ranks={"A": 99}, eligible=None)
    order(book, Side.SELL, 300, "90.00", D[3], D[4])
    run(book, D[4], 4, sbar(D[4], band="unavailable"))
    assert book.invested() == Decimal(30000)
    assert book.deployable() <= CAPITAL - book.invested(), "the unreleased 30,000 cannot be reused"


def test_engine_retries_a_band_blocked_exit_on_the_next_session():
    sessions = weekday_sessions(SESSION_START, 150)
    rows = make_rows(sessions, default_names(8) + etf_names())
    holdout = HoldoutRange(sessions[-20], sessions[-1])
    base_ctx = make_context(rows, holdout)
    dev = base_ctx.view.sessions()
    control = simulate_segment(base_ctx, sessions=dev, scenario=GATE, slope=Decimal("0.01"), regime_cash=None, mode="base", fold="c")
    first = control.closed[0]
    exit_session = first.exit_date
    # every name's band is unavailable on the intended exit session
    blocked = {(n, exit_session): "band_crosscheck_row_conflict" for n in {r.isin for r in rows}}
    ctx = make_context(rows, holdout, unavailable=blocked)
    res = simulate_segment(ctx, sessions=dev, scenario=GATE, slope=Decimal("0.01"), regime_cash=None, mode="base", fold="b")
    same = next(t for t in res.closed if t.anchor_isin == first.anchor_isin and t.entry_date == first.entry_date)
    nxt = dev[dev.index(exit_session) + 1]
    assert same.exit_date == nxt and same.blocked_exit_sessions >= 1
    assert any(a.affected and a.kind == "exit" and a.session == exit_session for a in res.attempts)


# ---- AC-13 ----------------------------------------------------------------------------------------
@pytest.mark.parametrize("positions", [4, 11])
def test_position_count_is_five_to_ten(positions):
    raw = placeholder_params()
    raw["max_positions"] = positions
    raw["min_positions"] = 5
    with pytest.raises(ParamsError):
        parse_params(raw)


def test_engine_respects_the_position_count_and_both_caps():
    sessions = weekday_sessions(SESSION_START, 150)
    rows = make_rows(sessions, default_names(10) + etf_names())
    holdout = HoldoutRange(sessions[-20], sessions[-1])
    ctx = make_context(rows, holdout, limits_obj=limits(capital_cap="30000", per_position_cap="9000"),
                       params_obj=params(max_positions=6, min_positions=5, hold_rank_cutoff=8))
    dev = ctx.view.sessions()
    res = simulate_segment(ctx, sessions=dev, scenario=GATE, slope=Decimal("0.01"), regime_cash=None, mode="base", fold="x")
    held: dict[str, int] = {}
    cost: dict[str, Decimal] = {}
    peak_names, peak_cost = 0, Decimal(0)
    for f in res.fills:  # replay at cost: buys add, a sell removes pro rata
        r = f.result
        if r.side is Side.BUY:
            assert r.notional <= Decimal("9000"), "per-position cap"
            held[f.anchor_isin] = held.get(f.anchor_isin, 0) + r.filled_quantity
            cost[f.anchor_isin] = cost.get(f.anchor_isin, Decimal(0)) + r.notional
        else:
            share = r.filled_quantity / held[f.anchor_isin]
            cost[f.anchor_isin] -= cost[f.anchor_isin] * Decimal(share)
            held[f.anchor_isin] -= r.filled_quantity
            if held[f.anchor_isin] == 0:
                del held[f.anchor_isin], cost[f.anchor_isin]
        peak_names = max(peak_names, len(held))
        peak_cost = max(peak_cost, sum(cost.values(), Decimal(0)))
    assert 1 <= peak_names <= 6
    assert peak_cost <= Decimal("30000"), "capital cap"
    assert res.entries >= 5


def test_minus_8_halts_new_entries_and_halves_positions_then_releases():
    book = with_position(300, limits_obj=limits(position_stop="-0.5"))
    book.mark(D[2], {"A": Decimal("85")})  # about -9% on the book
    book.update_risk(D[2], 2, regime_cash=False)
    assert book.halted and book.halt_events == 1 and book.entries_blocked and not book.flattened
    assert book.positions["A"].exit.reason == "halve" and book.positions["A"].exit.quantity == 150
    book.mark(D[3], {"A": Decimal("98")})
    book.update_risk(D[3], 3, regime_cash=False)
    assert not book.halted  # recovered above halt_release


def test_minus_15_flattens_everything():
    book = with_position(300, limits_obj=limits(position_stop="-0.9"))
    book.mark(D[2], {"A": Decimal("70")})  # -18% on the book
    book.update_risk(D[2], 2, regime_cash=False)
    assert book.flattened and book.flatten_events == 1
    assert book.positions["A"].exit.reason == "flatten" and book.positions["A"].exit.quantity is None
    book.update_risk(D[3], 3, regime_cash=False)
    assert book.flatten_events == 1  # one event per flatten, and entries stay blocked
    assert book.entries_blocked


def test_position_stop_fires():
    book = with_position(100)
    book.mark(D[2], {"A": Decimal("87.5")})
    book.update_risk(D[2], 2, regime_cash=False)
    assert book.positions["A"].exit.reason == "stop" and not book.halted
    book2 = with_position(100)
    book2.mark(D[2], {"A": Decimal("89")})
    book2.update_risk(D[2], 2, regime_cash=False)
    assert book2.positions["A"].exit is None


@pytest.mark.parametrize("reason", ["stop", "flatten", "regime_cash"])
def test_minimum_hold_never_delays_a_forced_exit(reason):
    p = params(min_hold_sessions=5, max_hold_sessions=20)
    book = with_position(300 if reason == "flatten" else 100, params_obj=p)
    assert D[2] and book.positions["A"].entry_index == 1  # held for one session, minimum hold is five
    if reason == "stop":
        book.mark(D[2], {"A": Decimal("85")})
    elif reason == "flatten":
        book.mark(D[2], {"A": Decimal("70")})
    book.update_risk(D[2], 2, regime_cash=reason == "regime_cash")
    assert book.positions["A"].exit.reason == reason


def test_minimum_hold_does_hold_back_a_swing_exit():
    book = with_position(100, params_obj=params(min_hold_sessions=5, max_hold_sessions=20))
    book.request_swing_exits(D[2], 2, ranks={"A": 99}, eligible=None)
    assert book.positions["A"].exit is None  # inside the minimum hold
    book.request_swing_exits(D[6], 6, ranks={"A": 99}, eligible=None)
    assert book.positions["A"].exit.reason == "rank_rotation"
    book2 = with_position(100, params_obj=params(min_hold_sessions=2, max_hold_sessions=4))
    book2.request_swing_exits(D[5], 5, ranks={}, eligible=None)
    assert book2.positions["A"].exit.reason == "swing_max_hold"
