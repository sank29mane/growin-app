"""Option B in simulation (Phase 63-02, Task 2; criterion 3, RISK-01, RISK-02).

Each test drives Phase 62's ``Book`` through its public API (it queues limit orders
and runs them through Phase 60's ``simulate_and_price`` under a committed fill
scenario) and feeds the resulting cash and positions to the Mac risk module. Exit
batches the module builds are queued back into the same Book, so a miss here is a
real Phase 60 ``MISSED`` outcome, not an assumption.

No ledger, broker or HTTP is involved; ``backend/strategy_india`` is not edited.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from costs.core import Side
from costs.fills import FillOutcome, PriceBand, SessionBar, load_fill_scenarios
from costs.schedule import PricingBasis, load_schedule_set
from strategy_india.portfolio import Book, PendingOrder
from strategy_india.ticks import EQUITY, resolve_tick

from risk_india import drawdown, exits, rules
from test_strategy_india_support import (
    SCHEDULE_VERSION,
    SESSION_START,
    limits,
    params,
    sha,
    tick_tables,
    weekday_sessions,
)

ROOT = Path(__file__).resolve().parents[2]
LV = json.loads(
    (ROOT / "tests/backend/fixtures/relay_orders/limits_vectors.json").read_text(encoding="utf-8")
)
LIMITS = rules.Limits.from_fields(LV["limits"])
CAPITAL = LIMITS.capital_cap
D = weekday_sessions(SESSION_START, 14)
SCENARIOS = load_fill_scenarios()
SCHEDULES = load_schedule_set()
BASIS = PricingBasis.pinned(SCHEDULE_VERSION)
TICKS = tick_tables()
A, B = "INE000A01012", "INE111B01023"
SCENARIO_IDS = [s.scenario_id for s in SCENARIOS.all()]  # base k=1, adverse k=2, pessimistic k=3


def test_the_committed_scenarios_are_k_1_2_3():
    assert [(s.scenario_id, s.k_ticks) for s in SCENARIOS.all()] == [
        ("base", 1), ("adverse", 2), ("pessimistic", 3),
    ]


def _band(day: date) -> PriceBand:
    return PriceBand("fixed", Decimal("40"), Decimal("160"), day, "synthetic", sha("band"))


def make_bar(anchor: str, day: date, *, low: str, high: str, open_: str, close: str) -> SessionBar:
    return SessionBar(
        isin=anchor, exchange="NSE", session_date=day, open=Decimal(open_), high=Decimal(high),
        low=Decimal(low), close=Decimal(close), volume=2_000_000, price_basis="raw",
        price_band=_band(day), source="synthetic",
    )


class Sim:
    """A Phase 62 Book plus the Mac risk state, one scenario."""

    def __init__(self, scenario_id: str) -> None:
        self.scenario = SCENARIOS.get(scenario_id)
        self.book = Book(capital=CAPITAL, limits=limits(), params=params(), fold="sim")
        self.state = drawdown.initial_state(LIMITS)
        self.cost: dict[str, Decimal] = {}
        self.codes: dict[str, str] = {}
        self.last_close: dict[str, Decimal] = {}

    # -- Phase 62 plumbing --------------------------------------------------
    def _tick(self, day: date):
        return resolve_tick(
            TICKS, session_date=day, band_reference_price=Decimal("100"),
            instrument_class=EQUITY, series="EQ",
        )

    def _run(self, orders: list[PendingOrder], day: date, index: int, bars: dict[str, SessionBar]):
        for order in orders:
            self.book.queue(order)
        self.book.execute_session(
            day, index, bars, scenario=self.scenario, schedules=SCHEDULES, pricing_basis=BASIS
        )

    def _order(self, side: Side, anchor: str, qty: int, limit: str, decided: date, day: date, tag: str):
        return PendingOrder(
            order_id=f"sim|{tag}|{anchor}|{side.value}", anchor_isin=anchor, stock_code=anchor,
            side=side, quantity=qty, limit_price=Decimal(limit), reference_price=Decimal("100"),
            decision_date=decided, session_date=day, information_as_of=decided, tick=self._tick(day),
            kind="entry" if side is Side.BUY else "exit", reason="sim", score=Decimal(1),
        )

    def buy(self, anchor: str, qty: int, day_index: int = 1) -> None:
        day = D[day_index]
        bar = make_bar(anchor, day, low="96", high="104", open_="100", close="100")
        self._run([self._order(Side.BUY, anchor, qty, "100.00", D[day_index - 1], day, "buy")],
                  day, day_index, {anchor: bar})
        assert self.book.positions[anchor].quantity == qty
        self.cost[anchor] = Decimal(qty) * Decimal("100.00")  # notional at the limit, charges excluded

    # -- Mac risk plumbing ----------------------------------------------------
    def positions(self) -> list[exits.Position]:
        return [
            exits.Position(a, a, p.quantity, self.cost[a])
            for a, p in sorted(self.book.positions.items())
        ]

    def close_session(self, day: date, closes: dict[str, str], *, vol_stops=None) -> drawdown.SessionResult:
        marks = {k: Decimal(v) for k, v in closes.items()}
        self.book.mark(day, marks)
        self.last_close.update(marks)
        result = drawdown.evaluate_session(
            self.state, LIMITS, day, cash=self.book.cash, positions=self.positions(),
            closes=marks, vol_stops=vol_stops,
        )
        self.state = result.state
        return result

    def sell_batches(self, batches, day: date, index: int, limit: str, *, fills: bool, tag: str):
        """Queue every intent of the batches next session and run them through Phase 60.

        ``fills`` False gives a bar whose high is exactly the limit, so a sell at
        limit + k ticks cannot trade under any committed scenario (k = 1, 2, 3).
        """
        orders, bars = [], {}
        for batch in batches:
            for intent in batch.intents:
                orders.append(
                    self._order(Side.SELL, intent.isin, intent.quantity, limit, intent.decided_on, day,
                                f"{tag}-{intent.batch_id}")
                )
                lim = Decimal(limit)
                high = lim + Decimal("5") if fills else lim
                bars[intent.isin] = make_bar(
                    intent.isin, day, low=str(lim - 3), high=str(high), open_=str(lim - 2), close=str(lim - 1)
                )
        before = {a: p.quantity for a, p in self.book.positions.items()}
        self._run(orders, day, index, bars)
        for order in orders:
            held = self.book.positions.get(order.anchor_isin)
            remaining = held.quantity if held else 0
            sold = before[order.anchor_isin] - remaining
            if sold:
                held_cost = self.cost[order.anchor_isin]
                self.cost[order.anchor_isin] = (
                    held_cost * remaining / before[order.anchor_isin] if remaining else Decimal(0)
                )
            # Report the outcome for every order, a miss (sold = 0) included: a miss must
            # change nothing, and reporting it is how a bug that treats it as a fill shows.
            self.state = drawdown.apply_exit_fill(
                self.state, order.anchor_isin, sold_quantity=sold, remaining_quantity=remaining
            )
        return before


def buy_codes(state: drawdown.RiskState) -> tuple[str, ...]:
    """Run an otherwise valid TESTCO buy through the Mac rules with this state's flags."""
    case = next(c for c in LV["evaluator_cases"] if c["name"] == "buy_ok")
    q = case["quote"]
    return rules.evaluate(
        LIMITS,
        state.flags(),
        rules.Account(),
        rules.Quote(q["stock_code"], q["isin"], q["series"], Decimal(q["ltp"]),
                    Decimal(q["lower_circuit"]), Decimal(q["upper_circuit"]),
                    Decimal(q["previous_close"]), date.fromisoformat(q["session_date"]),
                    tick_reference=Decimal(case["tick_reference"]),
                    tick_reference_month=date.fromisoformat(case["tick_reference_month"])),
        datetime.fromisoformat(case["now_ist"]),
        rules.OrderRequest("buy", "TESTCO", "INE000A01012", 10, Decimal("100.00")),
        kill_enabled=True,
    ).codes


def two_positions(sim: Sim) -> None:
    sim.buy(A, 300)  # 30,000
    sim.buy(B, 100, day_index=2)  # 10,000, leaves about 10,000 cash
    assert sim.book.cash > 0


# ------------------------------------------------ -8%: halt and halve (D-07)


@pytest.mark.parametrize("scenario", SCENARIO_IDS)
def test_minus_8_halts_entries_and_prebuilds_a_pro_rata_halve_batch(scenario):
    sim = Sim(scenario)
    two_positions(sim)
    result = sim.close_session(D[3], {A: "90", B: "90"})
    drawdown_now = sim.book.drawdown().quantize(Decimal("0.000001"))
    assert sim.state.drawdown == drawdown_now
    assert Decimal("-0.15") < sim.state.drawdown <= Decimal("-0.08")
    assert sim.state.halt and not sim.state.ended and sim.state.stops == {}
    (batch,) = result.batches
    assert batch.reason == "halve"
    assert {i.isin: i.quantity for i in batch.intents} == {A: 150, B: 50}
    assert "halt_latch" in buy_codes(sim.state)

    sim.sell_batches([batch], D[4], 4, "90.00", fills=True, tag="halve")
    assert {a: p.quantity for a, p in sim.book.positions.items()} == {A: 150, B: 50}
    assert sim.state.open_exits == {}
    assert sim.state.halt, "halving does not lift the halt"
    assert "halt_latch" in buy_codes(sim.state)

    # A full recovery above the peak does not release it either; only a named reset does.
    sim.close_session(D[5], {A: "130", B: "130"})
    assert sim.book.drawdown() == 0 and sim.state.drawdown == 0
    assert sim.state.halt and "halt_latch" in buy_codes(sim.state)
    released = drawdown.reset(sim.state, "halt", "operator", limits=LIMITS)
    assert "halt_latch" not in buy_codes(released)


# ------------------------------------------------ -15%: flatten, ended (D-06)


@pytest.mark.parametrize("scenario", SCENARIO_IDS)
def test_minus_15_builds_a_flatten_batch_and_ends_the_pilot(scenario):
    sim = Sim(scenario)
    two_positions(sim)
    result = sim.close_session(D[3], {A: "70", B: "70"})
    assert sim.state.drawdown <= Decimal("-0.15")
    assert sim.state.ended and sim.state.halt
    (batch,) = result.batches  # flatten only, although both stop latches are set too
    assert batch.reason == "flatten"
    assert {i.isin: i.quantity for i in batch.intents} == {A: 300, B: 100}
    assert {i.batch_id for i in batch.intents} == {batch.batch_id}

    sim.sell_batches([batch], D[4], 4, "70.00", fills=True, tag="flatten")
    assert sim.book.positions == {}
    assert sim.state.ended, "the pilot stays ended after the book is flat"
    assert sim.state.stops == {}, "the exit fills are the evidence that clears the stop latches"
    assert buy_codes(sim.state)[:1] == ("pilot_ended",)
    with pytest.raises(drawdown.ResetRefused):
        drawdown.reset(sim.state, "ended", "operator")


# ----------------------------------------- -12%: the stop exits next session


@pytest.mark.parametrize("scenario", SCENARIO_IDS)
def test_a_close_at_minus_12_exits_the_next_session(scenario):
    sim = Sim(scenario)
    sim.buy(A, 100)
    result = sim.close_session(D[2], {A: "88"})  # exactly cost x 0.88
    assert not sim.state.halt and A in sim.state.stops
    (batch,) = result.batches
    assert batch.reason == "stop"
    (intent,) = batch.intents
    assert (intent.isin, intent.quantity, intent.decided_on) == (A, 100, D[2])
    assert sim.book.positions[A].quantity == 100, "nothing sold on the day the close raised it"
    assert buy_codes(sim.state) == ("stop_open",)  # every ISIN is blocked while the exit is open

    sim.sell_batches([batch], D[3], 3, "88.00", fills=True, tag="stop")
    assert sim.book.positions == {} and A not in sim.state.stops
    assert buy_codes(sim.state) == ()


def test_a_tighter_vol_scaled_stop_exits_where_the_fixed_stop_would_not():
    sim = Sim("pessimistic")
    sim.buy(A, 100)
    result = sim.close_session(D[2], {A: "91"}, vol_stops={A: Decimal("-0.09")})
    assert A in sim.state.stops and result.config_errors == ()
    control = Sim("pessimistic")
    control.buy(A, 100)
    assert control.close_session(D[2], {A: "91"}).batches == ()


def test_a_looser_vol_scaled_stop_is_refused_and_the_fixed_stop_still_exits():
    sim = Sim("pessimistic")
    sim.buy(A, 100)
    result = sim.close_session(D[2], {A: "87.5"}, vol_stops={A: Decimal("-0.20")})
    assert result.config_errors == (f"vol_stop_looser_than_position_stop:{A}",)
    assert A in sim.state.stops  # -12.5% is past the fixed stop


# ---------------------------- missed exits keep their latch and re-issue (k=1..3)


EXIT_SETUPS = {
    # reason: (closes, limit, positions after one successful pass)
    "halve": ({A: "90", B: "90"}, "90.00"),
    "flatten": ({A: "70", B: "70"}, "70.00"),
    "stop": ({A: "88"}, "88.00"),
}


@pytest.mark.parametrize("scenario", SCENARIO_IDS)
@pytest.mark.parametrize("reason", sorted(EXIT_SETUPS))
def test_a_missed_exit_keeps_its_latch_and_is_reissued_never_dropped(scenario, reason):
    closes, limit = EXIT_SETUPS[reason]
    sim = Sim(scenario)
    if reason == "stop":
        sim.buy(A, 100)
    else:
        two_positions(sim)
    result = sim.close_session(D[3], closes)
    (first,) = result.batches
    assert first.reason == reason
    held_before = {a: p.quantity for a, p in sim.book.positions.items()}
    latches_before = sim.state.latch_names()
    exits_before = dict(sim.state.open_exits)
    assert exits_before

    issued = [first]
    session_index = 4
    for miss in range(2):  # it misses on two successive sessions, and nothing is lost
        day = D[session_index]
        sim.sell_batches(issued, day, session_index, limit, fills=False, tag=f"miss{miss}")
        outcome = sim.book.attempts[-1]
        assert outcome.outcome == FillOutcome.MISSED.value and outcome.reason_code == "MISSED_THRESHOLD"
        assert {a: p.quantity for a, p in sim.book.positions.items()} == held_before
        assert sim.state.latch_names() == latches_before
        assert dict(sim.state.open_exits) == exits_before
        assert sim.state.stops == result.state.stops
        # the close of the missed session re-issues the exit for the next one
        sim.close_session(D[session_index], {a: closes[a] for a in closes})
        reissued = drawdown.pending_batches(sim.state, sim.positions(), D[session_index])
        assert [b.reason for b in reissued] == [reason]
        assert reissued[0].batch_id != issued[0].batch_id
        assert {i.isin: i.quantity for i in reissued[0].intents} == {
            i.isin: i.quantity for i in first.intents
        }
        issued = list(reissued)
        session_index += 1

    sim.sell_batches(issued, D[session_index], session_index, limit, fills=True, tag="fill")
    if reason == "halve":
        assert {a: p.quantity for a, p in sim.book.positions.items()} == {A: 150, B: 50}
        assert sim.state.open_exits == {} and sim.state.halt
    elif reason == "flatten":
        assert sim.book.positions == {} and sim.state.open_exits == {} and sim.state.ended
        assert sim.state.stops == {}
    else:
        assert sim.book.positions == {} and sim.state.open_exits == {} and sim.state.stops == {}


@pytest.mark.parametrize("scenario", SCENARIO_IDS)
def test_a_partial_exit_fill_keeps_the_stop_latch_and_the_rest_is_reissued(scenario):
    sim = Sim(scenario)
    sim.buy(A, 100)
    result = sim.close_session(D[2], {A: "88"})
    assert [b.reason for b in result.batches] == ["stop"]
    # Sell only 40 of the 100 (a partial), then check the evidence rules by hand.
    sim.sell_batches(
        [exits.stop_batch([exits.Position(A, A, 40, Decimal("4000"))], D[2])],
        D[3], 3, "88.00", fills=True, tag="part",
    )
    assert sim.book.positions[A].quantity == 60
    assert A in sim.state.stops, "a partial fill is not the exit fill"
    (rest,) = drawdown.pending_batches(sim.state, sim.positions(), D[3])
    assert rest.reason == "stop" and [i.quantity for i in rest.intents] == [60]


# --------------------------------------------------- Phase 60 charges in equity


def test_equity_includes_phase_60_charges_and_the_halt_trips_on_the_net_figure():
    sim = Sim("pessimistic")
    sim.buy(A, 300)
    charges = sim.book.charges_total
    assert charges > 0, "the buy carried Phase 60 charges"
    gross_cash = CAPITAL - sim.cost[A]
    assert sim.book.cash == gross_cash - charges
    # A close that puts net equity at or just under -8% and gross equity just over it.
    target = Decimal("46000")
    close = ((target - sim.book.cash) / 300).quantize(Decimal("0.0001"), rounding="ROUND_DOWN")
    assert Decimal(300) * close + sim.book.cash <= target
    assert Decimal(300) * close + gross_cash > target, "ignoring charges would have missed the halt"
    result = sim.close_session(D[2], {A: str(close)})
    assert sim.state.halt, "net equity is at or below -8%"
    assert sim.book.equity() == sim.book.cash + 300 * close
    assert result.state.drawdown == sim.book.drawdown().quantize(Decimal("0.000001"))


def test_a_sell_leg_charges_also_reduce_cash_before_the_next_close():
    sim = Sim("base")
    sim.buy(A, 100)
    result = sim.close_session(D[2], {A: "88"})
    cash_before, charges_before = sim.book.cash, sim.book.charges_total
    sim.sell_batches(result.batches, D[3], 3, "88.00", fills=True, tag="stop")
    sell_charges = sim.book.charges_total - charges_before
    assert sell_charges > 0
    assert sim.book.cash == cash_before + Decimal(100) * Decimal("88.00") - sell_charges
