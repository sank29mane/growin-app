"""Parity of the Mac risk module with Phase 62's backtest rules (Phase 63-02, Task 2; P-14).

Both are driven over the drawdown paths in the shared limits vectors. They must first
raise halt, flatten and stop on the same session, and agree on the exit reason and
the halve quantity. Release is not compared: Phase 62's ``halt_release`` frees entries
automatically on recovery, while D-05 makes the live release admin-only. That divergence
is deliberate and is pinned below so it cannot drift in either direction unnoticed.

Phase 62 is driven through ``Book`` (``mark`` and ``update_risk``). ``backend/strategy_india``
is not edited.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from strategy_india.portfolio import Book
from strategy_india.portfolio import Position as BookPosition

from risk_india import drawdown, exits, rules
from test_strategy_india_support import limits, params, weekday_sessions

ROOT = Path(__file__).resolve().parents[2]
LV = json.loads(
    (ROOT / "tests/backend/fixtures/relay_orders/limits_vectors.json").read_text(encoding="utf-8")
)
LIMITS = rules.Limits.from_fields(LV["limits"])
CAP = LIMITS.capital_cap
DD = LV["drawdown_cases"]
DAYS = weekday_sessions(date(2026, 10, 9), 6)
QUANT = Decimal("0.000001")


def test_phase_62_and_the_vectors_use_the_same_three_thresholds():
    ours = limits()
    assert (Decimal(ours.drawdown_halt), Decimal(ours.drawdown_flatten), Decimal(ours.position_stop)) == (
        LIMITS.drawdown_halt, LIMITS.drawdown_flatten, LIMITS.position_stop,
    )
    assert Decimal(ours.capital_cap) == CAP


def _book_with(positions: dict[str, tuple[int, Decimal]], cash: Decimal, peak: Decimal) -> Book:
    book = Book(capital=CAP, limits=limits(), params=params(), fold="parity")
    book.cash = cash
    book.peak = peak
    for isin, (quantity, price) in positions.items():
        book.positions[isin] = BookPosition(
            anchor_isin=isin, isin=isin, stock_code=isin, quantity=quantity, bought_qty=quantity,
            cost_total=quantity * price, buy_cost_total=quantity * price, entry_date=DAYS[0],
            entry_index=0, entry_score=Decimal(1), last_price=price,
        )
    return book


def _setup(case: dict):
    cash = Decimal(case["start"]["cash"]) if "start" in case else CAP
    peak = Decimal(case["start"]["peak"]) if "start" in case else CAP
    held: dict[str, tuple[int, Decimal]] = {}
    costs: dict[str, Decimal] = {}
    for fill in case["fills"]:
        assert fill["side"] == "buy" and Decimal(fill["charges"]) == 0  # the paths are buy-and-hold
        price = Decimal(fill["price"])
        quantity = fill["quantity"]
        held[fill["isin"]] = (quantity, price)
        costs[fill["isin"]] = quantity * price
        cash -= quantity * price
    return cash, peak, held, costs


# A repeated session date is ignored by the live rule (VM and Mac). Phase 62 has no such
# rule because its engine never repeats a date, so only the first row of a date is fed to it.
def _distinct_sessions(case: dict) -> list[dict]:
    seen: set[str] = set()
    out = []
    for item in case["sessions"]:
        if item["session"] not in seen:
            seen.add(item["session"])
            out.append(item)
    return out or [{"session": DAYS[0].isoformat(), "closes": {}}]


def _first(flags: list[bool]) -> int | None:
    return flags.index(True) if True in flags else None


@pytest.mark.parametrize("case", DD, ids=[c["name"] for c in DD])
def test_62_and_the_mac_module_first_raise_halt_flatten_and_stop_on_the_same_session(case):
    cash, peak, held, costs = _setup(case)
    sessions = _distinct_sessions(case)
    book = _book_with(held, cash, peak)
    state = drawdown.initial_state(LIMITS)
    state = drawdown.RiskState(peak=peak) if "start" in case else state
    stop_ratio = LIMITS.position_stop

    mine = {"halt": [], "flatten": [], "stop": []}
    theirs = {"halt": [], "flatten": [], "stop": []}
    for index, item in enumerate(sessions, start=1):
        day = date.fromisoformat(item["session"])
        closes = {k: Decimal(v) for k, v in item["closes"].items()}
        book.mark(day, closes)
        book.update_risk(day, index, regime_cash=False)
        theirs["halt"].append(book.halted or book.flattened)
        theirs["flatten"].append(book.flattened)
        theirs["stop"].append(any(p.stop_ratio - 1 <= stop_ratio for p in book.positions.values()))

        positions = [exits.Position(i, i, q, costs[i]) for i, (q, _) in held.items()]
        result = drawdown.evaluate_session(
            state, LIMITS, day, cash=cash, positions=positions, closes=closes
        )
        state = result.state
        mine["halt"].append(state.halt)
        mine["flatten"].append(state.ended)
        mine["stop"].append(bool(state.stops))

        assert state.drawdown == book.drawdown().quantize(QUANT), (case["name"], index)
        # Same exit reason per position, and the same halve quantity, where the Mac emits one.
        for batch in result.batches:
            for intent in batch.intents:
                intent62 = book.positions[intent.isin].exit
                assert intent62 is not None and intent62.reason == intent.reason, (case["name"], intent)
                if intent.reason == "halve":
                    assert intent62.quantity == intent.quantity
                else:
                    assert intent62.quantity is None

    for kind in ("halt", "flatten", "stop"):
        assert _first(mine[kind]) == _first(theirs[kind]), (case["name"], kind, mine, theirs)


def test_the_parity_run_reaches_every_trigger_so_it_is_not_vacuous():
    """Guard against a vacuous parity: paths must reach each of the three triggers."""
    raised = {"halt": 0, "flatten": 0, "stop": 0}
    for case in DD:
        cash, peak, held, costs = _setup(case)
        book = _book_with(held, cash, peak)
        for index, item in enumerate(_distinct_sessions(case), start=1):
            book.mark(date.fromisoformat(item["session"]), {k: Decimal(v) for k, v in item["closes"].items()})
            book.update_risk(date.fromisoformat(item["session"]), index, regime_cash=False)
        raised["halt"] += book.halted or book.flattened
        raised["flatten"] += book.flattened
        raised["stop"] += any(p.exit is not None and p.exit.reason in {"stop", "flatten"} for p in book.positions.values())
    assert raised["halt"] >= 3 and raised["stop"] >= 3 and raised["flatten"] >= 2, raised


@pytest.mark.parametrize("quantity", [1, 2, 3, 7, 8, 99, 100, 101])
def test_halve_quantity_matches_62_for_every_parity_size(quantity):
    close = Decimal("100")
    cash = Decimal("46000") - quantity * close  # equity lands exactly on -8% of a 50,000 peak
    book = _book_with({A_ISIN: (quantity, close)}, cash, CAP)
    book.mark(DAYS[0], {A_ISIN: close})
    book.update_risk(DAYS[0], 1, regime_cash=False)
    assert book.halted
    result = drawdown.evaluate_session(
        drawdown.initial_state(LIMITS), LIMITS, DAYS[0], cash=cash,
        positions=[exits.Position(A_ISIN, A_ISIN, quantity, quantity * close)], closes={A_ISIN: close},
    )
    intent62 = book.positions[A_ISIN].exit
    if quantity // 2 >= 1:
        (batch,) = result.batches
        assert intent62 is not None and intent62.reason == "halve"
        assert intent62.quantity == batch.intents[0].quantity == quantity // 2
    else:
        assert result.batches == () and intent62 is None


A_ISIN = "INE000A01012"


def test_halt_release_differs_on_purpose_62_releases_on_recovery_the_mac_does_not():
    cost = Decimal("500")
    held = {A_ISIN: (100, cost)}
    book = _book_with(held, Decimal(0), CAP)
    book.mark(DAYS[0], {A_ISIN: Decimal("460")})
    book.update_risk(DAYS[0], 1, regime_cash=False)
    book.mark(DAYS[1], {A_ISIN: Decimal("700")})  # far above the release level
    book.update_risk(DAYS[1], 2, regime_cash=False)
    assert book.halted is False, "62 (backtest) releases the halt automatically"

    position = exits.Position(A_ISIN, A_ISIN, 100, Decimal("50000"))
    state = drawdown.evaluate_session(
        drawdown.initial_state(LIMITS), LIMITS, DAYS[0], cash=Decimal(0), positions=[position],
        closes={A_ISIN: Decimal("460")},
    ).state
    state = drawdown.evaluate_session(
        state, LIMITS, DAYS[1], cash=Decimal(0), positions=[position], closes={A_ISIN: Decimal("700")}
    ).state
    assert state.halt is True, "live: admin-only release (D-05)"
    assert drawdown.reset(state, "halt", "operator").halt is False
