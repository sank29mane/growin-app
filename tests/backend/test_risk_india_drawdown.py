"""Option B latch machine and exit batches on the Mac (Phase 63-02, Task 2).

Synthetic Decimal paths only. The VM's drawdown vectors are replayed through the Mac
module as well, so the two implementations are compared on the same rows (RISK-03).
"""

from __future__ import annotations

import dataclasses
import json
import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from risk_india import drawdown, exits, rules

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "backend" / "fixtures" / "relay_orders"
LV = json.loads((FIXTURES / "limits_vectors.json").read_text(encoding="utf-8"))
LIMITS = rules.Limits.from_fields(LV["limits"])
CAP = LIMITS.capital_cap
D1, D2, D3, D4 = date(2026, 10, 9), date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14)
A, B = "INE000A01012", "INE111B01023"
BATCH_ID = re.compile(r"[a-z0-9-]{8,64}")


def pos(isin: str, qty: int, cost: str, code: str | None = None) -> exits.Position:
    return exits.Position(isin, code or isin[-4:], qty, Decimal(cost))


def step(state, session, *, cash="0", positions=(), closes=None, vol_stops=None):
    return drawdown.evaluate_session(
        state,
        LIMITS,
        session,
        cash=Decimal(cash),
        positions=list(positions),
        closes={k: Decimal(v) for k, v in (closes or {}).items()},
        vol_stops=vol_stops,
    )


def one_position(close: str, qty: int = 100, cost: str = "50000"):
    """A 100-share position costing 500 each, no cash: equity = 100 x close."""
    return step(
        drawdown.initial_state(LIMITS), D1, positions=[pos(A, qty, cost)], closes={A: close}
    )


# ------------------------------------------------- the VM's drawdown vectors

DD = LV["drawdown_cases"]


def replay(case: dict) -> drawdown.RiskState:
    state = drawdown.initial_state(LIMITS)
    cash = CAP
    if "start" in case:
        state = dataclasses.replace(state, peak=Decimal(case["start"]["peak"]))
        cash = Decimal(case["start"]["cash"])
    held: dict[str, list] = {}  # isin -> [quantity, cost]
    for fill in case["fills"]:
        quantity, price, charges = fill["quantity"], Decimal(fill["price"]), Decimal(fill["charges"])
        entry = held.setdefault(fill["isin"], [0, Decimal(0)])
        if fill["side"] == "buy":
            entry[0] += quantity
            entry[1] += quantity * price
            cash -= quantity * price + charges
        else:
            entry[1] = entry[1] * (entry[0] - quantity) / entry[0]
            entry[0] -= quantity
            cash += quantity * price - charges
    positions = [pos(isin, q, str(c)) for isin, (q, c) in held.items() if q > 0]
    steps = case["sessions"] or [{"session": "2026-10-09", "closes": {}}]
    for item in steps:
        state = drawdown.evaluate_session(
            state,
            LIMITS,
            date.fromisoformat(item["session"]),
            cash=cash,
            positions=positions,
            closes={k: Decimal(v) for k, v in item["closes"].items()},
        ).state
    return state


@pytest.mark.parametrize("case", DD, ids=[c["name"] for c in DD])
def test_mac_drawdown_matches_the_vm_vector(case):
    state = replay(case)
    assert state.halt is case["expected"]["halt"]
    assert state.ended is case["expected"]["ended"]
    assert sorted(state.stops) == sorted(case["expected"]["stops"])


# --------------------------------------------------------- halt and halve


def test_minus_7_99_percent_does_not_latch():
    result = one_position("460.05")  # 46005 of a 50000 peak
    assert result.state.drawdown == Decimal("-0.079900")
    assert not result.state.halt and not result.state.ended
    assert result.batches == () and result.state.open_exits == {}


def test_exactly_minus_8_percent_halts_entries_and_prebuilds_the_halve_batch():
    result = one_position("460")  # 46000 = peak x 0.92, edge inclusive
    assert result.state.halt and not result.state.ended
    (batch,) = result.batches
    assert batch.reason == "halve"
    (intent,) = batch.intents
    assert (intent.isin, intent.quantity, intent.side, intent.decided_on) == (A, 50, "sell", D1)
    assert result.state.open_exits[A].reason == "halve"


@pytest.mark.parametrize(
    "quantity, sell",
    [(7, 3), (1, 0), (2, 1), (3, 1), (5, 2), (100, 50), (101, 50), (0, 0)],
)
def test_halve_floors_odd_quantities(quantity, sell):
    assert exits.halve_quantity(quantity) == sell


def test_halve_batch_for_odd_and_single_share_positions():
    result = step(  # 70 + 10 + 45920 = 46000, exactly -8%
        drawdown.initial_state(LIMITS),
        D1,
        cash="0",
        positions=[pos(A, 7, "70"), pos(B, 1, "10"), pos("INE222C01034", 100, "49800")],
        closes={A: "10", B: "10", "INE222C01034": "459.20"},
    )
    assert result.state.halt
    (batch,) = result.batches
    quantities = {i.isin: i.quantity for i in batch.intents}
    assert quantities == {A: 3, "INE222C01034": 50}  # 7 -> 3; the 1-share position has no halve leg
    assert B not in result.state.open_exits


def test_halve_is_pro_rata_across_every_position():
    result = step(
        drawdown.initial_state(LIMITS),
        D1,
        cash="0",
        positions=[pos(A, 40, "20000"), pos(B, 59, "29500")],
        closes={A: "460", B: "460"},  # 99 x 460 = 45540, about -8.9%
    )
    (batch,) = result.batches
    assert {i.isin: i.quantity for i in batch.intents} == {A: 20, B: 29}


def test_halve_is_not_rebuilt_while_halted():
    first = one_position("460")
    again = step(first.state, D2, positions=[pos(A, 100, "50000")], closes={A: "450"})
    assert again.state.halt and again.batches == ()


# --------------------------------------------------------- ended and flatten


def test_exactly_minus_15_percent_ends_the_pilot_and_flattens_every_position():
    edge = step(
        drawdown.initial_state(LIMITS),
        D1,
        cash="0",
        positions=[pos(A, 60, "30000"), pos(B, 40, "20000")],
        closes={A: "425", B: "425"},  # 100 x 425 = 42500 = peak x 0.85
    )
    assert edge.state.ended and edge.state.halt
    (batch,) = edge.batches
    assert batch.reason == "flatten"
    assert {i.isin: i.quantity for i in batch.intents} == {A: 60, B: 40}
    assert all(i.reason == "flatten" and i.side == "sell" for i in batch.intents)


def test_one_paisa_above_minus_15_is_halt_not_ended():
    result = step(
        drawdown.initial_state(LIMITS),
        D1,
        cash="0",
        positions=[pos(A, 100, "50000")],
        closes={A: "425.01"},
    )
    assert result.state.halt and not result.state.ended


def test_gap_from_minus_5_to_minus_16_sets_both_latches_and_emits_flatten_only():
    state = one_position("475").state  # -5%: nothing yet
    assert not state.halt and state.last_session == D1
    gap = step(state, D2, positions=[pos(A, 100, "50000")], closes={A: "420"})
    assert gap.state.halt and gap.state.ended
    (batch,) = gap.batches  # flatten only: no halve and no stop batch beside it
    assert batch.reason == "flatten"
    assert A in gap.state.stops  # the stop latch is still set (buys stay blocked)
    assert gap.state.open_exits[A].reason == "flatten"


def test_flatten_is_not_rebuilt_once_ended():
    first = one_position("400")
    assert first.state.ended
    again = step(first.state, D2, positions=[pos(A, 100, "50000")], closes={A: "380"})
    assert again.batches == ()


# ------------------------------------------------------------------- peak


def test_peak_starts_at_capital_cap_and_rises_only_on_evaluated_closes():
    state = drawdown.initial_state(LIMITS)
    assert state.peak == CAP
    up = step(state, D1, cash="52000")
    assert up.state.peak == Decimal("52000") and up.state.drawdown == 0
    down = step(up.state, D2, cash="51000")
    assert down.state.peak == Decimal("52000")  # never lowered
    stale = step(down.state, D2, cash="90000")  # same session again: ignored, peak untouched
    older = step(down.state, D1, cash="90000")
    assert stale.state == down.state and older.state == down.state
    assert stale.state.peak == Decimal("52000")


def test_halt_is_measured_from_the_risen_peak_not_the_capital_cap():
    up = step(drawdown.initial_state(LIMITS), D1, cash="60000")
    just_above = step(up.state, D2, cash="55200.01")
    assert not just_above.state.halt
    at_edge = step(up.state, D2, cash="55200")  # 60000 x 0.92
    assert at_edge.state.halt


def test_missing_close_for_a_held_isin_fails_closed():
    with pytest.raises(drawdown.MarkMissing):
        step(drawdown.initial_state(LIMITS), D1, cash="10", positions=[pos(A, 1, "5")], closes={})


# -------------------------------------------------------------------- stops


def test_close_at_exactly_cost_times_0_88_queues_a_stop_exit_for_the_next_session():
    result = step(
        drawdown.initial_state(LIMITS),
        D1,
        cash="40000",
        positions=[pos(A, 20, "10000")],
        closes={A: "440"},  # 20 x 440 = 8800 = 10000 x 0.88; equity 48800, only -2.4%
    )
    assert not result.state.halt
    (batch,) = result.batches
    assert batch.reason == "stop"
    (intent,) = batch.intents
    assert (intent.isin, intent.quantity, intent.decided_on) == (A, 20, D1)
    assert result.state.stops[A].session == D1
    # one cent above the edge: no stop
    above = step(
        drawdown.initial_state(LIMITS),
        D1,
        cash="40000",
        positions=[pos(A, 20, "10000")],
        closes={A: "440.01"},
    )
    assert above.batches == () and A not in above.state.stops


def test_a_tighter_vol_scaled_stop_of_minus_9_percent_is_honoured():
    args = dict(cash="40000", positions=[pos(A, 20, "10000")])
    hit = step(drawdown.initial_state(LIMITS), D1, closes={A: "455"}, vol_stops={A: Decimal("-0.09")}, **args)
    assert A in hit.state.stops and hit.config_errors == ()  # 20 x 455 = 9100 = 10000 x 0.91
    (batch,) = hit.batches
    assert batch.reason == "stop"
    miss = step(drawdown.initial_state(LIMITS), D1, closes={A: "455.05"}, vol_stops={A: Decimal("-0.09")}, **args)
    assert miss.batches == ()
    fixed = step(drawdown.initial_state(LIMITS), D1, closes={A: "455"}, **args)
    assert fixed.batches == ()  # the fixed -12% stop alone would not have fired


def test_a_vol_scaled_stop_looser_than_position_stop_is_refused_and_the_fixed_stop_applies():
    args = dict(cash="40000", positions=[pos(A, 20, "10000")], vol_stops={A: Decimal("-0.15")})
    close_at_fixed = step(drawdown.initial_state(LIMITS), D1, closes={A: "437.5"}, **args)
    assert A in close_at_fixed.state.stops  # -12.5%: stopped by the fixed stop
    assert close_at_fixed.config_errors == (f"vol_stop_looser_than_position_stop:{A}",)
    between = step(drawdown.initial_state(LIMITS), D1, closes={A: "445"}, **args)
    assert A not in between.state.stops  # -11%: neither stop fires
    assert between.config_errors == (f"vol_stop_looser_than_position_stop:{A}",)


def test_effective_stop_rules():
    fixed = LIMITS.position_stop
    assert drawdown.effective_stop(LIMITS, None) == (fixed, None)
    assert drawdown.effective_stop(LIMITS, Decimal("-0.12")) == (fixed, None)  # equal is not looser
    assert drawdown.effective_stop(LIMITS, Decimal("-0.05")) == (Decimal("-0.05"), None)
    assert drawdown.effective_stop(LIMITS, Decimal("-0.1201")) == (fixed, "vol_stop_looser_than_position_stop")
    for bad in (Decimal("0"), Decimal("0.05"), Decimal("-1"), Decimal("NaN"), Decimal("-Infinity"), -0.05, "-0.05"):
        assert drawdown.effective_stop(LIMITS, bad) == (fixed, "vol_stop_invalid")  # type: ignore[arg-type]


def test_halt_and_stop_on_one_position_the_stop_supersedes_the_halve():
    result = step(
        drawdown.initial_state(LIMITS),
        D1,
        cash="0",
        positions=[pos(A, 100, "50000"), pos(B, 100, "46000")],
        closes={A: "425.01", B: "460"},
    )
    # A closed at 425.01 (below its stop), B at 460 (inside it); the book is at about -15%
    reasons = {b.reason: {i.isin: i.quantity for i in b.intents} for b in result.batches}
    assert reasons["stop"] == {A: 100}
    assert A not in reasons.get("halve", {})
    assert result.state.open_exits[A].reason == "stop"


# ------------------------------------------------------- latches and reset


def test_latches_never_clear_on_recovery():
    halted = one_position("460").state
    back = step(halted, D2, positions=[pos(A, 100, "50000")], closes={A: "700"})
    assert back.state.halt and back.state.peak == Decimal("70000")
    ended = one_position("400").state
    recovered = step(ended, D2, positions=[pos(A, 100, "50000")], closes={A: "700"})
    assert recovered.state.halt and recovered.state.ended
    stopped = step(
        drawdown.initial_state(LIMITS), D1, cash="40000", positions=[pos(A, 20, "10000")], closes={A: "440"}
    ).state
    healed = step(stopped, D2, cash="40000", positions=[pos(A, 20, "10000")], closes={A: "900"})
    assert A in healed.state.stops


def test_only_an_explicit_reset_with_an_actor_clears_halt():
    halted = one_position("460").state
    released = drawdown.reset(halted, "halt", "operator@admin-window")
    assert released.halt is False and released.ended is False
    assert released.resets == (drawdown.ResetRecord("halt", "operator@admin-window", None),)
    assert released.peak == halted.peak  # no rebase
    for actor in ("", "   ", None, 7):
        with pytest.raises(drawdown.ResetRefused):
            drawdown.reset(halted, "halt", actor)  # type: ignore[arg-type]


def test_reset_without_a_rebased_peak_latches_again_while_still_below_minus_8():
    halted = one_position("460").state
    released = drawdown.reset(halted, "halt", "op")
    again = step(released, D2, positions=[pos(A, 100, "50000")], closes={A: "460"})
    assert again.state.halt is True


def test_ended_cannot_be_reset_and_halt_cannot_be_released_while_ended():
    ended = one_position("400").state
    with pytest.raises(drawdown.ResetRefused):
        drawdown.reset(ended, "ended", "op")
    with pytest.raises(drawdown.ResetRefused):
        drawdown.reset(ended, "halt", "op")
    assert ended.ended and ended.halt


def test_reset_refuses_unknown_and_unset_latches():
    clean = drawdown.initial_state(LIMITS)
    for latch in ("halt", "stop"):
        with pytest.raises(drawdown.ResetRefused):
            drawdown.reset(clean, latch, "op")
    for latch in ("mac_halt", "account_mismatch", "kill", ""):
        with pytest.raises(drawdown.ResetRefused):
            drawdown.reset(clean, latch, "op")


def test_stop_reset_by_isin_or_all_and_open_exits_are_left_alone():
    state = step(
        drawdown.initial_state(LIMITS),
        D1,
        cash="40000",
        positions=[pos(A, 20, "10000"), pos(B, 20, "10000")],
        closes={A: "440", B: "430"},
    ).state
    assert set(state.stops) == {A, B}
    one = drawdown.reset(state, "stop", "op", isin=A)
    assert set(one.stops) == {B} and set(one.open_exits) == {A, B}
    with pytest.raises(drawdown.ResetRefused):
        drawdown.reset(one, "stop", "op", isin=A)
    assert drawdown.reset(state, "stop", "op").stops == {}


def test_state_is_frozen():
    state = one_position("460").state
    with pytest.raises(dataclasses.FrozenInstanceError):
        state.halt = False  # type: ignore[misc]
    with pytest.raises(TypeError):
        state.open_exits[B] = state.open_exits[A]  # type: ignore[index]


# ----------------------------------------------------- fills and re-issue


def test_a_partial_fill_does_not_clear_the_stop_and_the_full_fill_does():
    state = step(
        drawdown.initial_state(LIMITS), D1, cash="40000", positions=[pos(A, 20, "10000")], closes={A: "440"}
    ).state
    partial = drawdown.apply_exit_fill(state, A, sold_quantity=8, remaining_quantity=12)
    assert A in partial.stops and partial.open_exits[A].quantity == 12
    done = drawdown.apply_exit_fill(partial, A, sold_quantity=12, remaining_quantity=0)
    assert A not in done.stops and A not in done.open_exits


def test_reporting_a_missed_exit_changes_nothing():
    stopped = step(
        drawdown.initial_state(LIMITS), D1, cash="40000", positions=[pos(A, 20, "10000")], closes={A: "440"}
    ).state
    assert drawdown.apply_exit_fill(stopped, A, sold_quantity=0, remaining_quantity=20) == stopped
    halted = one_position("460").state
    assert drawdown.apply_exit_fill(halted, A, sold_quantity=0, remaining_quantity=100) == halted
    ended = one_position("400").state
    assert drawdown.apply_exit_fill(ended, A, sold_quantity=0, remaining_quantity=100) == ended


def test_fills_never_touch_halt_or_ended():
    halted = one_position("460").state
    sold = drawdown.apply_exit_fill(halted, A, sold_quantity=50, remaining_quantity=50)
    assert sold.halt and sold.open_exits == {}  # the halve target is met, halt stays
    ended = one_position("400").state
    flat = drawdown.apply_exit_fill(ended, A, sold_quantity=100, remaining_quantity=0)
    assert flat.halt and flat.ended and flat.open_exits == {}


def test_halve_partial_fill_keeps_the_remaining_target():
    halted = one_position("460").state
    part = drawdown.apply_exit_fill(halted, A, sold_quantity=20, remaining_quantity=80)
    assert part.open_exits[A].quantity == 30
    (batch,) = drawdown.pending_batches(part, [pos(A, 80, "40000")], D2)
    assert [i.quantity for i in batch.intents] == [30]


def test_pending_batches_reissue_every_open_exit_on_a_later_session():
    state = step(
        drawdown.initial_state(LIMITS),
        D1,
        cash="0",
        positions=[pos(A, 101, "50500"), pos(B, 7, "700")],
        closes={A: "400", B: "50"},
    ).state
    held = [pos(A, 101, "50500"), pos(B, 7, "700")]
    batches = drawdown.pending_batches(state, held, D2)
    assert [b.reason for b in batches] == ["flatten"]
    assert {i.isin: i.quantity for i in batches[0].intents} == {A: 101, B: 7}
    assert all(i.decided_on == D2 for i in batches[0].intents)
    first = state.open_exits
    again = drawdown.pending_batches(state, held, D2)
    assert again == batches and state.open_exits == first  # re-issue is idempotent


def test_an_open_exit_with_no_held_position_raises_instead_of_vanishing():
    state = one_position("460").state
    with pytest.raises(drawdown.StateInconsistent):
        drawdown.pending_batches(state, [], D2)


# ----------------------------------------------------------- batch shape


def test_a_batch_is_one_sell_intent_per_position_with_a_shared_pattern_valid_id():
    batch = exits.flatten_batch([pos(A, 5, "1"), pos(B, 9, "1")], D1)
    assert batch is not None
    assert len({i.isin for i in batch.intents}) == len(batch.intents) == 2
    assert {i.batch_id for i in batch.intents} == {batch.batch_id}
    assert BATCH_ID.fullmatch(batch.batch_id)
    assert {i.side for i in batch.intents} == {"sell"} and {i.reason for i in batch.intents} == {"flatten"}
    assert batch.batch_id == exits.flatten_batch([pos(B, 9, "1"), pos(A, 5, "1")], D1).batch_id
    assert batch.batch_id != exits.flatten_batch([pos(A, 5, "1"), pos(B, 9, "1")], D2).batch_id
    assert exits.flatten_batch([], D1) is None and exits.halve_batch([pos(A, 1, "1")], D1) is None


def test_duplicate_legs_unknown_reasons_and_bad_inputs_are_refused():
    with pytest.raises(exits.ExitError):
        exits.build_batch("flatten", D1, [(A, "X", 1), (A, "X", 2)])
    with pytest.raises(exits.ExitError):
        exits.build_batch("liquidate", D1, [(A, "X", 1)])
    with pytest.raises(exits.ExitError):
        exits.build_batch("stop", D1, [(A, "X", True)])  # type: ignore[arg-type]
    with pytest.raises(exits.ExitError):
        exits.Position(A, "X", 0, Decimal("1"))
    with pytest.raises(exits.ExitError):
        exits.Position(A, "X", 1, 1.5)  # type: ignore[arg-type]
    with pytest.raises(exits.ExitError):
        drawdown.evaluate_session(
            drawdown.initial_state(LIMITS), LIMITS, D1,
            cash=Decimal(0), positions=[pos(A, 1, "1"), pos(A, 2, "1")], closes={A: Decimal(1)},
        )


# ------------------------------------------------- the buy block, end to end

CASES = {c["name"]: c for c in LV["evaluator_cases"]}


def _buy_codes(flags: rules.RiskFlags, case_name: str = "buy_ok", **overrides) -> tuple[str, ...]:
    case = CASES[case_name]
    intent = {**case["intent"], **overrides}
    return rules.evaluate(
        LIMITS,
        flags,
        rules.Account(),
        rules.Quote(
            stock_code=case["quote"]["stock_code"],
            isin=case["quote"]["isin"],
            series=case["quote"]["series"],
            ltp=Decimal(case["quote"]["ltp"]),
            lower_circuit=Decimal(case["quote"]["lower_circuit"]),
            upper_circuit=Decimal(case["quote"]["upper_circuit"]),
            previous_close=Decimal(case["quote"]["previous_close"]),
            session_date=date.fromisoformat(case["quote"]["session_date"]),
            tick_reference=Decimal(case["tick_reference"]),
        ),
        datetime.fromisoformat(case["now_ist"]),
        rules.OrderRequest(
            side=intent["side"],
            stock_code=intent["stock_code"],
            isin=intent["isin"],
            quantity=intent["quantity"],
            limit_price=Decimal(intent["limit_price"]),
        ),
        kill_enabled=True,
    ).codes


def test_open_stop_exit_blocks_buys_on_any_isin_until_the_exit_fill_arrives():
    state = step(
        drawdown.initial_state(LIMITS), D1, cash="40000", positions=[pos(B, 20, "10000")], closes={B: "440"}
    ).state
    assert B in state.stops
    assert _buy_codes(state.flags()) == ("stop_open",)  # TESTCO is not the stopped ISIN
    sell_codes = rules.evaluate(  # selling the stopped ISIN still passes the stop rule
        LIMITS,
        state.flags(),
        rules.Account(holdings=(rules.Holding(B, 20, Decimal("10000")),)),
        rules.Quote(
            "STOPCO", B, "EQ", Decimal("100"), Decimal("90"), Decimal("110"),
            Decimal("99.80"), date(2026, 10, 8), tick_reference=Decimal("99.80"),
        ),
        datetime.fromisoformat("2026-10-08T10:00:00+05:30"),
        rules.OrderRequest("sell", "STOPCO", B, 20, Decimal("100.00")),
        kill_enabled=True,
    ).codes
    assert "stop_open" not in sell_codes and sell_codes == ()
    filled = drawdown.apply_exit_fill(state, B, sold_quantity=20, remaining_quantity=0)
    assert _buy_codes(filled.flags()) == ()


def test_halt_and_ended_flags_refuse_buys_but_not_sells_in_the_rules():
    halted = one_position("460").state
    assert _buy_codes(halted.flags()) == ("halt_latch",)
    ended = one_position("400").state
    assert _buy_codes(ended.flags()) == ("pilot_ended", "stop_open", "halt_latch")
    sell = _buy_codes(halted.flags(), side="sell")
    assert "halt_latch" not in sell
