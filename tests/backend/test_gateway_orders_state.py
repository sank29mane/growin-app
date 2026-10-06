"""VM risk state, latches, charge bound, durable store, session-end alert and
admin CLI (Phase 63-01, Task 2). Fakes and tmp dirs only.
"""

from __future__ import annotations

import hashlib
import io
import itertools
import json
import os
import stat
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "gateway" / "vm") not in sys.path:
    sys.path.insert(0, str(ROOT / "gateway" / "vm"))

from backend.costs.charges import price_trade_day  # noqa: E402
from backend.costs.core import Side, TradeFill  # noqa: E402
from backend.costs.schedule import PricingBasis, load_schedule_set  # noqa: E402
from gateway_vm.orders import OrderRefusal  # noqa: E402
from gateway_vm.orders import admin, risk  # noqa: E402
from gateway_vm.orders import limits as vm_limits  # noqa: E402
from gateway_vm.orders import store as store_mod  # noqa: E402
from gateway_vm.orders.audit import AuditBroken, AuditLog  # noqa: E402
from gateway_vm.orders.limits import (  # noqa: E402
    AccountSnapshot,
    Holding,
    Limits,
    Quote,
    Trade,
)
from gateway_vm.orders.intent import parse_intent  # noqa: E402
from gateway_vm.orders.store import StateStore  # noqa: E402

FIXTURES = ROOT / "tests" / "backend" / "fixtures" / "relay_orders"
LV = json.loads((FIXTURES / "limits_vectors.json").read_text(encoding="utf-8"))
LIMITS = Limits.from_fields(LV["limits"])
ISIN = "INE000A01012"
OTHER = "INE111B01023"

# Pinned sha256 of backend/costs/schedules/icici_nse_cash_charges.json at the time
# CHARGE_RATE_BOUND, CHARGE_ROUNDING_BOUND and CHARGE_SELL_FIXED_BOUND were derived.
# If the schedule changes, this fails until the bound is re-derived and this pin moves.
CHARGE_SCHEDULE_SHA256 = "92b988208488fb0a60eb14e1ce1760af634bc29c3c482bb740aa4b059797792d"


def _trade(spec: dict) -> Trade:
    return Trade(
        trade_id=spec["trade_id"],
        isin=spec["isin"],
        side=spec["side"],
        quantity=spec["quantity"],
        price=Decimal(spec["price"]),
        charges=Decimal(spec["charges"]),
    )


# ------------------------------------------------------------- Option B paths

DD = LV["drawdown_cases"]


@pytest.mark.parametrize("case", DD, ids=[c["name"] for c in DD])
def test_drawdown_vector(case):
    state = risk.initial_state(LIMITS)
    assert state.peak == LIMITS.capital_cap  # peak starts at capital_cap
    if "start" in case:
        state.cash = Decimal(case["start"]["cash"])
        state.peak = Decimal(case["start"]["peak"])
    risk.apply_trades(state, [_trade(t) for t in case["fills"]])
    for step in case["sessions"]:
        risk.evaluate_session(
            state,
            LIMITS,
            date.fromisoformat(step["session"]),
            {k: Decimal(v) for k, v in step["closes"].items()},
        )
    if not case["sessions"]:
        # The "start" cases model the state after a close was evaluated at that cash.
        risk.evaluate_session(state, LIMITS, date(2026, 10, 9), {})
    assert state.halt is case["expected"]["halt"]
    assert state.ended is case["expected"]["ended"]
    assert sorted(state.stops) == sorted(case["expected"]["stops"])


def test_equity_at_exactly_minus_8_pct_latches_but_one_paisa_above_does_not():
    for equity, halted in (("46000", True), ("46000.01", False)):
        state = risk.initial_state(LIMITS)
        state.cash = Decimal(equity)
        risk.evaluate_session(state, LIMITS, date(2026, 10, 9), {})
        assert state.halt is halted, equity


def test_peak_only_rises_and_records_its_date():
    state = risk.initial_state(LIMITS)
    state.cash = Decimal("52000")
    risk.evaluate_session(state, LIMITS, date(2026, 10, 9), {})
    assert (state.peak, state.peak_date) == (Decimal("52000"), "2026-10-09")
    state.cash = Decimal("51000")
    risk.evaluate_session(state, LIMITS, date(2026, 10, 12), {})
    assert state.peak == Decimal("52000") and state.last_evaluated_session == "2026-10-12"
    assert state.drawdown < 0


def test_missing_mark_for_a_held_isin_fails_closed():
    state = risk.initial_state(LIMITS)
    risk.apply_trades(state, [Trade("t1", ISIN, "buy", 10, Decimal("100"), Decimal("0"))])
    with pytest.raises(risk.MarkMissing):
        risk.evaluate_session(state, LIMITS, date(2026, 10, 9), {})
    assert state.last_evaluated_session is None  # nothing was half-applied


def test_marks_must_agree_with_quote_previous_close_within_one_tick():
    risk.assert_marks_agree({ISIN: Decimal("100.05")}, {ISIN: Decimal("100.00")}, {ISIN: Decimal("0.05")})
    with pytest.raises(risk.MarkMismatch):
        risk.assert_marks_agree({ISIN: Decimal("100.06")}, {ISIN: Decimal("100.00")}, {ISIN: Decimal("0.05")})
    with pytest.raises(risk.MarkMismatch):
        risk.assert_marks_agree({ISIN: Decimal("100")}, {}, {ISIN: Decimal("0.05")})


# ----------------------------------------------------------- latch lifecycle


def _stopped_state() -> risk.OrderState:
    state = risk.initial_state(LIMITS)
    risk.apply_trades(state, [Trade("b1", ISIN, "buy", 20, Decimal("500"), Decimal("0"))])
    risk.evaluate_session(state, LIMITS, date(2026, 10, 9), {ISIN: Decimal("440")})
    assert list(state.stops) == [ISIN]
    return state


def test_halt_latch_survives_a_verified_sell_and_a_sell_fill():
    state = risk.initial_state(LIMITS)
    risk.apply_trades(state, [Trade("b1", ISIN, "buy", 100, Decimal("500"), Decimal("0"))])
    risk.evaluate_session(state, LIMITS, date(2026, 10, 9), {ISIN: Decimal("460")})
    assert state.halt
    # Halving sell fills on the trade list: the -8% latch does not clear.
    risk.apply_trades(state, [Trade("s1", ISIN, "sell", 50, Decimal("460"), Decimal("0"))])
    assert state.halt and not state.ended
    # Even a full exit does not clear halt; only the admin reset does.
    risk.apply_trades(state, [Trade("s2", ISIN, "sell", 50, Decimal("460"), Decimal("0"))])
    assert state.halt
    with pytest.raises(risk.ResetRefused):  # still at -8%: it would only latch again
        risk.reset_latch(state, "halt", limits=LIMITS)
    assert state.halt
    risk.reset_latch(state, "halt", limits=LIMITS, rebase_halt_anchor=True)
    assert not state.halt


def test_stop_latch_clears_only_when_the_trade_list_shows_the_exit_fill():
    state = _stopped_state()
    # Nothing on the trade list yet: a verified sell changes nothing here.
    assert list(state.stops) == [ISIN]
    # A partial exit fill is not the exit.
    risk.apply_trades(state, [Trade("s1", ISIN, "sell", 10, Decimal("440"), Decimal("0"))])
    assert list(state.stops) == [ISIN]
    # The sell on another ISIN does not clear it either.
    risk.apply_trades(state, [Trade("b2", OTHER, "buy", 1, Decimal("100"), Decimal("0"))])
    risk.apply_trades(state, [Trade("s9", OTHER, "sell", 1, Decimal("100"), Decimal("0"))])
    assert list(state.stops) == [ISIN]
    # The fill that completes the exit does.
    risk.apply_trades(state, [Trade("s2", ISIN, "sell", 10, Decimal("440"), Decimal("0"))])
    assert state.stops == {}


def test_stop_and_halt_survive_a_restart_through_the_store(tmp_path):
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700)
    state = _stopped_state()
    state.halt = True
    StateStore(directory).save(state)
    reloaded = StateStore(directory).load()  # a restart: new store object, same files
    assert reloaded.halt and list(reloaded.stops) == [ISIN]
    assert reloaded.to_json() == state.to_json()


def test_apply_trades_is_idempotent_by_trade_id():
    state = risk.initial_state(LIMITS)
    trade = Trade("t1", ISIN, "buy", 10, Decimal("100"), Decimal("1"))
    assert risk.apply_trades(state, [trade]) is True
    cash = state.cash
    assert risk.apply_trades(state, [trade, trade]) is False
    assert state.cash == cash == Decimal("50000") - Decimal("1000") - Decimal("1")


def test_missing_charges_use_the_conservative_bound_and_given_charges_win():
    state = risk.initial_state(LIMITS)
    risk.apply_trades(state, [Trade("t1", ISIN, "buy", 10, Decimal("100"), None)])
    assert state.cash == Decimal("50000") - Decimal("1000") - risk.charge_bound("buy", Decimal("1000"))
    risk.apply_trades(state, [Trade("t2", ISIN, "sell", 10, Decimal("100"), None)])
    assert state.fills["t2"].charges == risk.charge_bound("sell", Decimal("1000"))
    risk.apply_trades(state, [Trade("t3", OTHER, "buy", 1, Decimal("10"), Decimal("0.25"))])
    assert state.fills["t3"].charges == Decimal("0.25")


def test_sell_below_zero_net_latches_account_mismatch():
    state = risk.initial_state(LIMITS)
    risk.apply_trades(state, [Trade("s1", ISIN, "sell", 5, Decimal("100"), Decimal("0"))])
    assert state.account_mismatch


def test_mismatch_detection_unexplained_isin_and_excess_quantity():
    state = risk.initial_state(LIMITS)
    risk.apply_trades(state, [Trade("b1", ISIN, "buy", 10, Decimal("100"), Decimal("0"))])
    assert not risk.detect_mismatch(state, [Holding(ISIN, 10, Decimal("1000"))])
    assert not risk.detect_mismatch(state, [Holding(ISIN, 4, Decimal("400"))])  # fewer is fine
    assert risk.detect_mismatch(state, [Holding(ISIN, 11, Decimal("1100"))])  # excess quantity
    assert risk.detect_mismatch(state, [Holding(OTHER, 1, Decimal("100"))])  # never bought here
    assert not risk.detect_mismatch(state, [Holding(OTHER, 0, Decimal("0"))])


def test_reset_semantics():
    state = risk.initial_state(LIMITS)
    state.halt = state.mac_halt = state.account_mismatch = True
    state.stops = {ISIN: {"session": "2026-10-09", "quantity": 5}, OTHER: {"session": "2026-10-09", "quantity": 5}}
    risk.reset_latch(state, "mac_halt")
    risk.reset_latch(state, "account_mismatch")
    risk.reset_latch(state, "stop", ISIN)
    assert (state.mac_halt, state.account_mismatch, list(state.stops)) == (False, False, [OTHER])
    with pytest.raises(risk.ResetRefused):
        risk.reset_latch(state, "stop", ISIN)  # already cleared
    risk.reset_latch(state, "stop")
    assert state.stops == {}
    state.ended = True
    with pytest.raises(risk.ResetRefused):
        risk.reset_latch(state, "ended")
    assert state.ended


# ------------------------------------------------ halt reset and the halt anchor


def _halted_state() -> risk.OrderState:
    """Peak 50000, 100 shares, close 460: equity 46000, exactly -8%, halt latched."""
    state = risk.initial_state(LIMITS)
    risk.apply_trades(state, [Trade("b1", ISIN, "buy", 100, Decimal("500"), Decimal("0"))])
    risk.evaluate_session(state, LIMITS, date(2026, 10, 9), {ISIN: Decimal("460")})
    assert state.halt and state.drawdown == Decimal("-0.08") and state.last_equity == Decimal("46000")
    return state


def _close(state: risk.OrderState, day: int, price: str) -> None:
    risk.evaluate_session(state, LIMITS, date(2026, 10, day), {ISIN: Decimal(price)})


def test_halt_reset_refuses_at_or_below_the_threshold_and_needs_limits():
    state = _halted_state()
    for kwargs in ({"limits": LIMITS}, {}):
        with pytest.raises(risk.ResetRefused):
            risk.reset_latch(state, "halt", **kwargs)
    assert state.halt and state.halt_anchor is None
    state.drawdown = Decimal("-0.080001")  # below the threshold too
    with pytest.raises(risk.ResetRefused):
        risk.reset_latch(state, "halt", limits=LIMITS)
    state.drawdown = Decimal("-0.079999")  # just above: an ordinary reset
    assert risk.reset_latch(state, "halt", limits=LIMITS) is False
    assert not state.halt and state.halt_anchor is None


def test_rebase_flag_is_for_the_halt_latch_only():
    state = _halted_state()
    state.mac_halt = True
    with pytest.raises(risk.ResetRefused):
        risk.reset_latch(state, "mac_halt", limits=LIMITS, rebase_halt_anchor=True)
    assert state.mac_halt


def test_rebase_sets_a_separate_anchor_and_leaves_the_true_peak_alone():
    state = _halted_state()
    assert risk.reset_latch(state, "halt", limits=LIMITS, rebase_halt_anchor=True) is True
    assert (state.halt, state.halt_anchor) == (False, Decimal("46000"))
    assert state.peak == Decimal("50000") and state.drawdown == Decimal("-0.08")


def test_after_a_rebase_the_next_close_does_not_latch_halt_again():
    state = _halted_state()
    risk.reset_latch(state, "halt", limits=LIMITS, rebase_halt_anchor=True)
    _close(state, 12, "460")  # same equity: still -8% from the true peak
    assert not state.halt and not state.ended
    _close(state, 13, "450")  # -10% from the peak, -2.2% from the anchor
    assert not state.halt and not state.ended and state.halt_anchor == Decimal("46000")
    # Contrast: the same unlatched state with no anchor re-latches at once.
    plain = _halted_state()
    plain.halt = False
    _close(plain, 12, "460")
    assert plain.halt


def test_the_minus_15_end_is_still_measured_from_the_real_peak():
    state = _halted_state()
    risk.reset_latch(state, "halt", limits=LIMITS, rebase_halt_anchor=True)
    _close(state, 12, "425.01")  # 42501: above 50000 x 0.85
    assert not state.ended and not state.halt
    _close(state, 13, "425")  # 42500 = exactly -15% from the peak, only -7.6% from the anchor
    assert state.ended and state.halt  # a rebase did not rebase the end
    assert state.peak == Decimal("50000")


def test_the_halt_anchor_follows_equity_up_and_halts_8_pct_below_that_high():
    state = _halted_state()
    risk.reset_latch(state, "halt", limits=LIMITS, rebase_halt_anchor=True)
    _close(state, 12, "480")  # 48000: anchor ratchets, still under the 50000 peak
    assert state.halt_anchor == Decimal("48000") and not state.halt
    _close(state, 13, "441.61")  # 44161: above 48000 x 0.92 = 44160
    assert not state.halt
    _close(state, 14, "441.60")  # 44160: exactly -8% from the anchor's high
    assert state.halt and not state.ended


def test_the_halt_anchor_is_dropped_once_equity_regains_the_true_peak():
    state = _halted_state()
    risk.reset_latch(state, "halt", limits=LIMITS, rebase_halt_anchor=True)
    _close(state, 12, "505")  # 50500: new peak
    assert state.halt_anchor is None and state.peak == Decimal("50500")
    _close(state, 13, "464.6")  # 46460 = -8.0% from the real new peak
    assert state.halt


def test_halt_anchor_and_last_equity_survive_a_restart(tmp_path):
    state = _halted_state()
    risk.reset_latch(state, "halt", limits=LIMITS, rebase_halt_anchor=True)
    store = _store(tmp_path)
    store.save(state)
    reloaded = StateStore(store.directory).load()
    assert (reloaded.halt_anchor, reloaded.last_equity) == (Decimal("46000"), Decimal("46000"))
    assert reloaded.to_json() == state.to_json()


def test_flags_view_carries_latches_and_ledger_cost():
    state = _stopped_state()
    flags = state.flags()
    assert flags.stops == frozenset({ISIN})
    assert flags.ledger_cost[ISIN] == Decimal("10000")
    assert state.latch_names() == ("stop",)


def test_ledger_cost_is_average_cost_and_shrinks_on_sells():
    state = risk.initial_state(LIMITS)
    risk.apply_trades(state, [
        Trade("b1", ISIN, "buy", 10, Decimal("100"), Decimal("0")),
        Trade("b2", ISIN, "buy", 10, Decimal("120"), Decimal("0")),
        Trade("s1", ISIN, "sell", 10, Decimal("130"), Decimal("0")),
    ])
    assert risk.ledger_cost(state)[ISIN] == Decimal("1100")  # 20 sh at 110 avg, 10 sold
    risk.apply_trades(state, [Trade("s2", ISIN, "sell", 10, Decimal("130"), Decimal("0"))])
    assert ISIN not in risk.ledger_cost(state)


# ------------------------------------- fill chronology survives a reload (#552 P1)

IST = timezone(timedelta(hours=5, minutes=30))


def _at(minute: int) -> datetime:
    return datetime(2026, 10, 8, 10, minute, 0, tzinfo=IST)


def _round_trip(z_id="z", y_id="y", a_id="a", *, timed=True):
    """buy 10 x 100, sell all 10, buy 10 x 200. Trade ids sort the opposite way to time."""
    def mk(tid, side, price, minute):
        return Trade(tid, ISIN, side, 10, Decimal(price), Decimal("0"), _at(minute) if timed else None)

    return [mk(z_id, "buy", "100", 0), mk(y_id, "sell", "100", 1), mk(a_id, "buy", "200", 2)]


def _evaluated(state: risk.OrderState) -> risk.OrderState:
    risk.evaluate_session(state, LIMITS, date(2026, 10, 9), {ISIN: Decimal("170")})
    return state


def _summary(state: risk.OrderState) -> dict:
    return {
        "cost": risk.ledger_cost(state),
        "net": risk.net_quantities(state),
        "stops": sorted(state.stops),
        "flags": state.flags(),
        "latches": state.latch_names(),
    }


def test_persisted_fills_replay_in_execution_order_not_trade_id_order(tmp_path):
    state = risk.initial_state(LIMITS)
    risk.apply_trades(state, _round_trip())
    assert risk.ledger_cost(state) == {ISIN: Decimal("2000")}
    _evaluated(state)
    assert list(state.stops) == [ISIN]  # 10 x 170 = 1700 <= 2000 x 0.88 = 1760
    store = _store(tmp_path)
    store.save(state)
    reloaded = StateStore(store.directory).load()
    assert risk.ledger_cost(reloaded) == {ISIN: Decimal("2000")}  # was 1000 when ids were sorted
    assert _summary(reloaded) == _summary(state)
    assert [tid for tid, _ in risk.chronological_fills(reloaded)] == ["z", "y", "a"]
    assert list(reloaded.fills) == ["z", "y", "a"]


@pytest.mark.parametrize("order", list(itertools.permutations(range(3))))
def test_shuffled_snapshot_order_and_trade_ids_give_the_same_ledger(tmp_path, order):
    reference = risk.initial_state(LIMITS)
    risk.apply_trades(reference, _round_trip())
    _evaluated(reference)
    for ids in (("z", "y", "a"), ("a", "y", "z"), ("m1", "k9", "b2")):
        trades = _round_trip(*ids)
        shuffled = [trades[i] for i in order]
        state = risk.initial_state(LIMITS)
        risk.apply_trades(state, shuffled)
        _evaluated(state)
        store = _store(tmp_path)
        store.save(state)
        reloaded = StateStore(store.directory).load()
        for candidate in (state, reloaded):
            assert risk.ledger_cost(candidate) == {ISIN: Decimal("2000")}
            assert sorted(candidate.stops) == [ISIN]
            assert candidate.account_mismatch is False  # a sell listed first is not a short
            assert candidate.cash == reference.cash


def test_json_key_order_is_irrelevant_to_replay(tmp_path):
    state = risk.initial_state(LIMITS)
    risk.apply_trades(state, _round_trip())
    body = state.to_json()
    body["fills"] = dict(sorted(body["fills"].items()))  # a, y, z: what sorted ids would give
    assert list(body["fills"]) == ["a", "y", "z"]
    assert risk.ledger_cost(risk.OrderState.from_json(body)) == {ISIN: Decimal("2000")}
    body["fills"] = dict(reversed(list(body["fills"].items())))
    assert risk.ledger_cost(risk.OrderState.from_json(body)) == {ISIN: Decimal("2000")}


def test_equal_exchange_times_fall_back_to_arrival_order_and_survive_reload(tmp_path):
    state = risk.initial_state(LIMITS)
    same = datetime(2026, 10, 8, 10, 0, 0, tzinfo=IST)
    for tid, side, price in (("z", "buy", "100"), ("y", "sell", "100"), ("a", "buy", "200")):
        risk.apply_trades(state, [Trade(tid, ISIN, side, 10, Decimal(price), Decimal("0"), same)])
    store = _store(tmp_path)
    store.save(state)
    reloaded = StateStore(store.directory).load()
    assert risk.ledger_cost(reloaded) == risk.ledger_cost(state) == {ISIN: Decimal("2000")}


def test_untimed_trades_keep_arrival_order_across_a_reload(tmp_path):
    state = risk.initial_state(LIMITS)
    for trade in _round_trip(timed=False):
        risk.apply_trades(state, [trade])
    store = _store(tmp_path)
    store.save(state)
    reloaded = StateStore(store.directory).load()
    assert risk.ledger_cost(reloaded) == {ISIN: Decimal("2000")}
    assert [tid for tid, _ in risk.chronological_fills(reloaded)] == ["z", "y", "a"]


def test_an_untimed_fill_after_timed_ones_takes_the_latest_ledger_time(tmp_path):
    """F-1: mixed timed and untimed fills. An untimed fill is stamped with the latest time seen.

    z (buy 10 x 100, 10:00) and y (sell 10, 10:01) are timed; a (buy 10 x 200) has no time and
    arrives on a later poll. It must replay last: cost 2000. Stamped "" instead it would sort
    first and the sell would shrink it: (2000 + 1000) x 10 / 20 = 1500, under-counting capital.
    """
    z, y, a = _round_trip()
    untimed_a = Trade(a.trade_id, ISIN, "buy", 10, Decimal("200"), Decimal("0"), None)
    state = risk.initial_state(LIMITS)
    risk.apply_trades(state, [z, y])
    risk.apply_trades(state, [untimed_a])
    assert risk.ledger_cost(state) == {ISIN: Decimal("2000")}
    assert [tid for tid, _ in risk.chronological_fills(state)] == ["z", "y", "a"]
    assert state.fills["a"].executed_at == state.fills["y"].executed_at != ""
    store = _store(tmp_path)
    store.save(state)
    assert risk.ledger_cost(StateStore(store.directory).load()) == {ISIN: Decimal("2000")}


def test_untimed_fills_go_last_within_a_batch_in_arrival_order():
    """F-2: a snapshot that lists untimed trades first still applies them after the timed ones.

    u1 (sell 10) and u2 (buy 10) have no time; t (buy 10, 10:00) does. Applied first, u1 would
    be a sell of nothing held and latch account_mismatch (a fail-closed refusal of every order).
    """
    t = Trade("t", ISIN, "buy", 10, Decimal("100"), Decimal("0"), _at(0))
    u1 = Trade("u1", ISIN, "sell", 10, Decimal("100"), Decimal("0"), None)
    u2 = Trade("u2", ISIN, "buy", 10, Decimal("100"), Decimal("0"), None)
    state = risk.initial_state(LIMITS)
    assert risk.apply_trades(state, [u1, u2, t])
    assert [tid for tid, _ in risk.chronological_fills(state)] == ["t", "u1", "u2"]
    assert [state.fills[tid].seq for tid in ("t", "u1", "u2")] == [0, 1, 2]
    assert state.account_mismatch is False
    assert risk.net_quantities(state) == {ISIN: 10}


def test_a_late_arriving_earlier_fill_is_replayed_at_its_exchange_time(tmp_path):
    z, y, a = _round_trip()
    state = risk.initial_state(LIMITS)
    risk.apply_trades(state, [y, a])  # the buy z shows up on a later poll
    risk.apply_trades(state, [z])
    assert [tid for tid, _ in risk.chronological_fills(state)] == ["z", "y", "a"]
    assert risk.ledger_cost(state) == {ISIN: Decimal("2000")}
    store = _store(tmp_path)
    store.save(state)
    assert risk.ledger_cost(StateStore(store.directory).load()) == {ISIN: Decimal("2000")}


def test_exchange_time_is_normalised_so_zone_offsets_do_not_reorder(tmp_path):
    utc = timezone.utc
    state = risk.initial_state(LIMITS)
    # 04:31 UTC is 10:01 IST: the sell. Written with a different offset than the buys.
    risk.apply_trades(state, [
        Trade("a", ISIN, "buy", 10, Decimal("200"), Decimal("0"), datetime(2026, 10, 8, 4, 32, tzinfo=utc)),
        Trade("y", ISIN, "sell", 10, Decimal("100"), Decimal("0"), datetime(2026, 10, 8, 10, 1, tzinfo=IST)),
        Trade("z", ISIN, "buy", 10, Decimal("100"), Decimal("0"), datetime(2026, 10, 8, 4, 30, tzinfo=utc)),
    ])
    assert [tid for tid, _ in risk.chronological_fills(state)] == ["z", "y", "a"]
    assert risk.ledger_cost(state) == {ISIN: Decimal("2000")}


def test_naive_exchange_time_is_refused():
    with pytest.raises(ValueError):
        Trade("t", ISIN, "buy", 1, Decimal("1"), Decimal("0"), datetime(2026, 10, 8, 10, 0))


def test_capital_cap_and_stop_results_are_identical_before_and_after_a_reload(tmp_path):
    sv = json.loads((FIXTURES / "signing_vectors.json").read_text(encoding="utf-8"))
    body = dict(sv["rows"][0]["payload"]["intent"], quantity=485, limit_price="100.05")
    intent = parse_intent(json.dumps(body))
    quote = Quote("TESTCO", ISIN, "EQ", Decimal("100.00"), Decimal("90.00"), Decimal("110.00"),
                  Decimal("99.80"), date(2026, 10, 8))
    now = vm_limits.to_ist(datetime(2026, 10, 8, 10, 0, tzinfo=IST))

    def decide(state):
        return vm_limits.evaluate(
            LIMITS, state.flags(), AccountSnapshot(), quote, now, intent,
            kill_enabled=True, tick_reference=Decimal("99.80"),
        )

    state = risk.initial_state(LIMITS)
    risk.apply_trades(state, _round_trip())
    _evaluated(state)
    before = decide(state)
    # 485 x 100.05 on top of a 2000 position is over 50000; on 1000 it would pass.
    assert "capital_cap" in before and "stop_open" in before
    store = _store(tmp_path)
    store.save(state)
    after = decide(StateStore(store.directory).load())
    assert after == before


def test_fill_records_are_validated_on_load():
    state = risk.initial_state(LIMITS)
    risk.apply_trades(state, _round_trip())
    good = state.to_json()

    def broken(mutate):
        body = json.loads(json.dumps(good))
        mutate(body)
        with pytest.raises(risk.StateInvalid):
            risk.OrderState.from_json(body)

    broken(lambda b: b["fills"]["z"].pop("seq"))
    broken(lambda b: b["fills"]["z"].update(seq=True))
    broken(lambda b: b["fills"]["z"].update(seq=-1))
    broken(lambda b: b["fills"]["y"].update(seq=b["fills"]["z"]["seq"]))  # duplicate sequence
    broken(lambda b: b["fills"]["z"].update(executed_at="2026-10-08 10:00:00"))
    broken(lambda b: b["fills"]["z"].update(executed_at=5))


# --------------------------------------------------------------- charge bound


def _schedule():
    schedules = load_schedule_set()
    return schedules.for_date(date(2026, 10, 8))


def _charges(side: Side, qty: int, price: str) -> Decimal:
    fill = TradeFill("f1", ISIN, "NSE", side, qty, Decimal(price), date(2026, 10, 8))
    return price_trade_day(
        [fill], _schedule(), workspace="india", currency="INR", pricing_basis=PricingBasis.trade_date()
    ).total


GRID = [
    (1, "1"), (1, "10"), (3, "33.33"), (7, "100.05"), (10, "100"), (10, "499.95"), (100, "500"),
    (25, "1999.95"), (10, "5000"), (4, "12345.65"), (1, "17500"), (100, "500.05"), (1000, "50"),
    (2000, "25"), (100000, "0.5"),
]


def test_charge_schedule_file_is_the_one_the_bound_was_derived_from():
    data = (ROOT / "backend/costs/schedules/icici_nse_cash_charges.json").read_bytes()
    assert hashlib.sha256(data).hexdigest() == CHARGE_SCHEDULE_SHA256, (
        "the Phase 60 charge schedule changed: re-derive risk.CHARGE_* and move this pin"
    )
    assert _schedule().version == risk.CHARGE_BOUND_SCHEDULE_VERSION


def test_charge_bound_is_positive_and_covers_every_schedule_row():
    assert risk.CHARGE_RATE_BOUND > 0 and risk.CHARGE_ROUNDING_BOUND > 0 and risk.CHARGE_SELL_FIXED_BOUND > 0
    smallest = risk.charge_bound("buy", Decimal("1"))
    assert smallest > 0
    for side in (Side.BUY, Side.SELL):
        for qty, price in GRID:
            value = qty * Decimal(price)
            bound = risk.charge_bound(side.value.lower(), value)
            actual = _charges(side, qty, price)
            assert bound > 0
            assert bound >= actual, (side, qty, price, bound, actual)


def test_charge_bound_covers_a_same_day_round_trip():
    schedule = _schedule()
    for qty, price in GRID:
        fills = [
            TradeFill("b", ISIN, "NSE", Side.BUY, qty, Decimal(price), date(2026, 10, 8)),
            TradeFill("s", ISIN, "NSE", Side.SELL, qty, Decimal(price), date(2026, 10, 8)),
        ]
        total = price_trade_day(
            fills, schedule, workspace="india", currency="INR", pricing_basis=PricingBasis.trade_date()
        ).total
        value = qty * Decimal(price)
        assert risk.charge_bound("buy", value) + risk.charge_bound("sell", value) >= total


def test_charge_bound_is_not_loose_by_orders_of_magnitude():
    # Sanity: within 2x of the real delivery buy cost at the cap, so it stays a bound and not a guess.
    actual = _charges(Side.BUY, 100, "500")
    assert risk.charge_bound("buy", Decimal("50000")) < actual * 2


# --------------------------------------------------------------- state store


def _store(tmp_path: Path) -> StateStore:
    directory = tmp_path / "state"
    directory.mkdir(mode=0o700, exist_ok=True)
    return StateStore(directory)


def test_store_initialises_on_first_start_with_peak_at_capital_cap(tmp_path):
    store = _store(tmp_path)
    state = store.load_or_init(LIMITS, audit_has_entries=False)
    assert state.peak == LIMITS.capital_cap and state.cash == LIMITS.capital_cap
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert store.load().to_json() == state.to_json()


def test_absent_state_beside_an_existing_audit_is_refused(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(OrderRefusal) as err:
        store.load_or_init(LIMITS, audit_has_entries=True)
    assert err.value.code == "state_unreadable" and err.value.status == 503


def test_consumed_intent_ids_survive_a_restart(tmp_path):
    store = _store(tmp_path)
    store.load_or_init(LIMITS, audit_has_entries=False)
    assert not store.is_consumed("intent-0001")
    store.consume("intent-0001")
    store.consume("intent-0001")  # idempotent
    reloaded = StateStore(store.directory)
    assert reloaded.is_consumed("intent-0001")
    assert reloaded.load().consumed_intents == ["intent-0001"]


def test_save_orders_fsync_rename_directory_fsync(tmp_path, monkeypatch):
    store = _store(tmp_path)
    calls: list[str] = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(store_mod.os, "fsync", lambda fd: (calls.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(store_mod.os, "replace", lambda a, b: (calls.append("replace"), real_replace(a, b))[1])
    store.save(risk.initial_state(LIMITS))
    assert calls == ["fsync", "replace", "fsync"]
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert [p.name for p in store.directory.iterdir() if "tmp" in p.name] == []


@pytest.mark.parametrize("breakage", ["corrupt_json", "hash_mismatch", "loose_mode", "symlink", "extra_key", "empty", "duplicate_key", "float"])
def test_unreadable_or_corrupt_state_raises_503_state_unreadable(tmp_path, breakage):
    store = _store(tmp_path)
    store.save(risk.initial_state(LIMITS))
    raw = store.path.read_text()
    if breakage == "corrupt_json":
        store.path.write_text(raw[:-20])
    elif breakage == "hash_mismatch":
        wrapper = json.loads(raw)
        wrapper["state"]["halt"] = True  # an edit that skips the hash
        store.path.write_text(json.dumps(wrapper))
    elif breakage == "loose_mode":
        store.path.chmod(0o644)
    elif breakage == "symlink":
        real = tmp_path / "real.json"
        real.write_text(raw)
        real.chmod(0o600)
        store.path.unlink()
        store.path.symlink_to(real)
    elif breakage == "extra_key":
        wrapper = json.loads(raw)
        wrapper["extra"] = 1
        store.path.write_text(json.dumps(wrapper))
    elif breakage == "empty":
        store.path.write_text("")
    elif breakage == "duplicate_key":
        store.path.write_text(raw.replace('"state_sha256"', '"state_sha256":"x","state_sha256"', 1))
    elif breakage == "float":
        store.path.write_text(raw.replace('"schema_version":1', '"schema_version":1.0', 1))
    with pytest.raises(OrderRefusal) as err:
        store.load()
    assert (err.value.status, err.value.code) == (503, "state_unreadable")
    with pytest.raises(OrderRefusal):
        store.is_consumed("x")


def test_unwritable_state_directory_raises_503_state_unwritable(tmp_path):
    store = _store(tmp_path)
    store.save(risk.initial_state(LIMITS))
    store.directory.chmod(0o500)
    try:
        if os.access(store.directory, os.W_OK):  # running as root: cannot simulate
            pytest.skip("directory stays writable for this user")
        with pytest.raises(OrderRefusal) as err:
            store.save(risk.initial_state(LIMITS))
        assert err.value.code == "state_unwritable"
    finally:
        store.directory.chmod(0o700)


def test_state_json_shape_has_numbers_and_ids_only(tmp_path):
    store = _store(tmp_path)
    state = _stopped_state()
    store.save(state)
    body = json.loads(store.path.read_text())["state"]
    assert set(body) == {
        "schema_version", "workspace", "start_equity", "cash", "peak", "peak_date",
        "last_evaluated_session", "drawdown", "last_equity", "halt_anchor", "halt", "ended",
        "mac_halt", "account_mismatch", "stops", "fills", "consumed_intents", "alerts_sent",
    }


def test_audit_chain_break_and_truncation_are_detected(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    for n in range(3):
        audit.append({"decision": "EVALUATED", "route": "test", "codes": [f"c{n}"]})
    assert audit.verify()[0] == 3
    raw = audit.path.read_bytes()
    audit.path.write_bytes(raw[:-10])  # truncated final line
    with pytest.raises(AuditBroken):
        audit.verify()
    audit.path.write_bytes(raw)
    lines = raw.splitlines(keepends=True)
    audit.path.write_bytes(lines[1] + lines[0] + lines[2])  # reordered
    with pytest.raises(AuditBroken):
        audit.verify()


# ------------------------------------- audit anchor: deletion and tail truncation


def _anchored(tmp_path: Path, entries: int = 0):
    """A state store plus an audit log anchored in it, with `entries` entries written."""
    store = _store(tmp_path)
    store.load_or_init(LIMITS, audit_has_entries=False)
    audit = AuditLog(store.directory / "audit.jsonl", anchor=store)
    for n in range(entries):
        audit.append({"decision": "EVALUATED", "route": "test", "codes": [f"c{n}"]})
    return store, audit


def test_anchor_follows_every_append_inside_the_state_file(tmp_path):
    store, audit = _anchored(tmp_path)
    assert store.audit_anchor() == (0, "0" * 64)
    for n in range(1, 4):
        entry = audit.append({"decision": "EVALUATED", "route": "test"})
        assert store.audit_anchor() == (n, entry["entry_sha256"])
        assert StateStore(store.directory).audit_anchor() == (n, entry["entry_sha256"])
    assert audit.verify() == (3, entry["entry_sha256"])


def test_a_deleted_log_is_refused_not_read_as_an_empty_chain(tmp_path):
    store, audit = _anchored(tmp_path, 3)
    plain = AuditLog(audit.path)  # no anchor: this is the weakness being closed
    audit.path.unlink()
    assert plain.verify() == (0, "0" * 64)
    with pytest.raises(AuditBroken, match="shorter than its anchor"):
        audit.verify()
    with pytest.raises(AuditBroken):
        audit.append({"decision": "EVALUATED", "route": "test"})
    assert not audit.path.exists()  # a refused append does not quietly start a new chain


@pytest.mark.parametrize("keep", [0, 1, 2])
def test_dropping_whole_tail_lines_is_refused(tmp_path, keep):
    store, audit = _anchored(tmp_path, 3)
    lines = audit.path.read_bytes().splitlines(keepends=True)
    audit.path.write_bytes(b"".join(lines[:keep]))  # a valid, shorter chain
    assert AuditLog(audit.path).verify()[0] == keep  # the chain alone still verifies
    with pytest.raises(AuditBroken, match="shorter than its anchor"):
        audit.verify()
    with pytest.raises(AuditBroken):
        audit.append({"decision": "EVALUATED", "route": "test"})
    assert audit.path.read_bytes() == b"".join(lines[:keep])


def test_a_rewritten_chain_of_the_same_length_is_refused(tmp_path):
    store, audit = _anchored(tmp_path, 3)
    other = AuditLog(tmp_path / "other.jsonl")
    for n in range(3):
        other.append({"decision": "REFUSED", "route": "forged", "codes": [f"x{n}"]})
    audit.path.write_bytes(other.path.read_bytes())
    assert AuditLog(audit.path).verify()[0] == 3
    with pytest.raises(AuditBroken, match="does not match its anchor"):
        audit.verify()


def test_a_log_one_entry_ahead_of_its_anchor_is_the_crash_window_and_heals(tmp_path, monkeypatch):
    store, audit = _anchored(tmp_path, 2)
    real_set = store.set_audit_anchor
    monkeypatch.setattr(store, "set_audit_anchor", lambda *a: (_ for _ in ()).throw(OSError("crash")))
    with pytest.raises(AuditBroken, match="anchor could not be written"):
        audit.append({"decision": "EVALUATED", "route": "test"})  # entry 3 on disk, never acknowledged
    monkeypatch.setattr(store, "set_audit_anchor", real_set)
    assert store.audit_anchor()[0] == 2
    assert audit.verify()[0] == 3  # tolerated: exactly one beyond the anchor
    entry = audit.append({"decision": "EVALUATED", "route": "test"})
    assert entry["seq"] == 4 and store.audit_anchor() == (4, entry["entry_sha256"])
    assert audit.verify()[0] == 4


def test_a_log_two_entries_ahead_of_its_anchor_is_refused(tmp_path):
    store, audit = _anchored(tmp_path, 1)
    old_state = store.path.read_bytes()  # anchor at 1
    audit.append({"decision": "EVALUATED", "route": "test"})
    audit.append({"decision": "EVALUATED", "route": "test"})
    store.path.write_bytes(old_state)  # state rolled back behind the log
    with pytest.raises(AuditBroken, match="ahead of its anchor"):
        audit.verify()


def test_torn_tail_from_a_crash_is_tolerated_once_and_removed_by_the_next_append(tmp_path):
    store, audit = _anchored(tmp_path, 2)
    good = audit.path.read_bytes()
    audit.path.write_bytes(good + b'{"seq":3,"prev_sha256":"ab')  # crash mid-write, no newline
    assert audit.verify()[0] == 2 and audit.has_torn_tail()
    with pytest.raises(AuditBroken):  # without an anchor nothing proves it is a crash artifact
        AuditLog(audit.path).verify()
    entry = audit.append({"decision": "EVALUATED", "route": "test"})
    assert entry["seq"] == 3
    raw = audit.path.read_bytes()
    assert raw.startswith(good) and raw.endswith(b"\n") and b'"seq":3,"prev_sha256":"ab' not in raw
    assert audit.verify() == (3, entry["entry_sha256"]) and not audit.has_torn_tail()


def test_torn_tail_cannot_hide_a_dropped_entry(tmp_path):
    store, audit = _anchored(tmp_path, 3)
    lines = audit.path.read_bytes().splitlines(keepends=True)
    # Entry 3 cut in half: the prefix holds 2 entries, the anchor says 3.
    audit.path.write_bytes(lines[0] + lines[1] + lines[2][:40])
    with pytest.raises(AuditBroken, match="shorter than its anchor"):
        audit.verify()
    with pytest.raises(AuditBroken):
        audit.append({"decision": "EVALUATED", "route": "test"})


def test_only_one_torn_tail_line_is_tolerated(tmp_path):
    store, audit = _anchored(tmp_path, 2)
    good = audit.path.read_bytes()
    audit.path.write_bytes(good + b"{garbage\n" + b'{"seq":4')  # a terminated bad line, then a torn one
    with pytest.raises(AuditBroken):
        audit.verify()
    with pytest.raises(AuditBroken):
        audit.append({"decision": "EVALUATED", "route": "test"})


def test_an_anchored_audit_needs_the_order_state_to_exist(tmp_path):
    store = _store(tmp_path)  # no state.json yet
    audit = AuditLog(store.directory / "audit.jsonl", anchor=store)
    assert audit.verify() == (0, "0" * 64)
    with pytest.raises(AuditBroken, match="state must exist"):
        audit.append({"decision": "EVALUATED", "route": "test"})
    assert not audit.path.exists()
    AuditLog(audit.path).append({"decision": "EVALUATED", "route": "test"})  # entries without any state
    with pytest.raises(AuditBroken, match="no anchor"):
        audit.verify()


def test_unreadable_state_keeps_its_own_503_when_the_audit_is_checked(tmp_path):
    store, audit = _anchored(tmp_path, 1)
    store.path.write_text("{corrupt")
    with pytest.raises(OrderRefusal) as err:
        audit.verify()
    assert (err.value.status, err.value.code) == (503, "state_unreadable")


def test_saving_stale_state_never_moves_the_anchor_back(tmp_path):
    store, audit = _anchored(tmp_path)
    stale = store.load()  # what the admin CLI holds while it audits
    entry = audit.append({"decision": "RESET", "route": "admin"})
    store.save(stale)
    assert store.audit_anchor() == (1, entry["entry_sha256"])
    assert audit.verify()[0] == 1


def test_set_audit_anchor_is_checked_and_monotonic(tmp_path):
    store, audit = _anchored(tmp_path, 2)
    seq, head = store.audit_anchor()
    for args in ((1, head), (seq, "zz"), (-1, head), (True, head), (0, head), (3, "0" * 64)):
        with pytest.raises((ValueError, TypeError)):
            store.set_audit_anchor(*args)
    assert store.audit_anchor() == (seq, head)


def test_the_anchor_is_covered_by_the_state_hash(tmp_path):
    store, _ = _anchored(tmp_path, 2)
    wrapper = json.loads(store.path.read_text())
    wrapper["audit_anchor"]["seq"] = 0
    wrapper["audit_anchor"]["head_sha256"] = "0" * 64  # try to make the log look unanchored
    store.path.write_text(json.dumps(wrapper))
    with pytest.raises(OrderRefusal) as err:
        store.load()
    assert err.value.code == "state_unreadable"


def test_state_wrapper_without_an_anchor_is_unreadable(tmp_path):
    store, _ = _anchored(tmp_path, 1)
    wrapper = json.loads(store.path.read_text())
    del wrapper["audit_anchor"]
    store.path.write_text(json.dumps(wrapper))
    with pytest.raises(OrderRefusal):
        store.load()


# ------------------------------------------------------------ session-end alert


class Clock:
    def __init__(self, text: str) -> None:
        self.now = datetime.fromisoformat(text)

    def __call__(self) -> datetime:
        return self.now


class Port:
    def __init__(self, fail: bool = False) -> None:
        self.alerts: list[risk.StopExitAlert] = []
        self.fail = fail

    def send(self, alert):
        if self.fail:
            raise RuntimeError("channel down")
        self.alerts.append(alert)


def test_session_end_alerts_once_at_1530_with_isin_date_quantity(tmp_path):
    state = _stopped_state()
    audit = AuditLog(tmp_path / "audit.jsonl")
    port = Port()
    clock = Clock("2026-10-12T15:30:00+05:30")  # Monday
    result = risk.session_end_check(state, clock, port, audit=audit)
    assert result.ok and len(port.alerts) == 1
    alert = port.alerts[0]
    assert (alert.isin, alert.session_date, alert.quantity) == (ISIN, date(2026, 10, 12), 20)
    assert set(vars(alert)) == {"isin", "session_date", "quantity"}  # no account values
    entries = audit.entries_after(0)
    assert [(e["decision"], e["codes"], e["isin"]) for e in entries] == [("EVALUATED", ["stop_exit_open"], ISIN)]
    # Same session date and ISIN: nothing more.
    again = risk.session_end_check(state, clock, port, audit=audit)
    assert again.sent == () and len(port.alerts) == 1 and audit.verify()[0] == 1
    # Latches are unchanged by the check.
    assert list(state.stops) == [ISIN] and not state.halt
    # A later session still open alerts again.
    clock.now = datetime.fromisoformat("2026-10-13T16:00:00+05:30")
    assert len(risk.session_end_check(state, clock, port, audit=audit).sent) == 1


def test_session_end_sends_nothing_before_1530_or_without_an_open_exit_or_on_weekends(tmp_path):
    state = _stopped_state()
    port = Port()
    for text in ("2026-10-12T15:29:59+05:30", "2026-10-12T09:00:00+05:30", "2026-10-10T16:00:00+05:30", "2026-10-11T16:00:00+05:30"):
        assert risk.session_end_check(state, Clock(text), port).sent == ()
    assert port.alerts == []
    state.stops.clear()  # exit filled
    assert risk.session_end_check(state, Clock("2026-10-12T15:30:00+05:30"), port).sent == ()
    assert port.alerts == []


def test_session_end_handles_each_open_exit_separately(tmp_path):
    state = _stopped_state()
    state.stops[OTHER] = {"session": "2026-10-09", "quantity": 7}
    port = Port()
    risk.session_end_check(state, Clock("2026-10-12T15:31:00+05:30"), port)
    assert sorted(a.isin for a in port.alerts) == sorted([ISIN, OTHER])
    assert {a.quantity for a in port.alerts if a.isin == OTHER} == {7}


def test_failing_alert_port_audits_alert_failed_and_is_retried(tmp_path):
    state = _stopped_state()
    audit = AuditLog(tmp_path / "audit.jsonl")
    clock = Clock("2026-10-12T15:30:00+05:30")
    result = risk.session_end_check(state, clock, Port(fail=True), audit=audit)
    assert not result.ok and len(result.failed) == 1 and result.sent == ()
    assert audit.entries_after(0)[0]["codes"] == ["alert_failed"]
    assert state.alerts_sent == []  # not recorded as sent
    assert list(state.stops) == [ISIN]  # never unblocks or blocks anything else
    retry = risk.session_end_check(state, clock, Port(), audit=audit)
    assert retry.ok and len(retry.sent) == 1


def test_unbound_alert_port_fails_loudly(tmp_path):
    state = _stopped_state()
    result = risk.session_end_check(
        state, Clock("2026-10-12T15:30:00+05:30"), risk.UnboundAlertPort(), audit=AuditLog(tmp_path / "a.jsonl")
    )
    assert not result.ok


def test_session_end_alert_goes_out_even_if_the_audit_is_broken(tmp_path):
    state = _stopped_state()
    audit = AuditLog(tmp_path / "audit.jsonl")
    audit.append({"decision": "EVALUATED", "route": "x"})
    audit.path.write_bytes(audit.path.read_bytes() + b"{trunc")
    port = Port()
    result = risk.session_end_check(state, Clock("2026-10-12T15:30:00+05:30"), port, audit=audit)
    assert len(port.alerts) == 1 and result.audit_failed and not result.ok


# --------------------------------------------------------------------- admin


def _run(directory: Path, *argv: str, clock=None, port=None, limits=LIMITS):
    out, err = io.StringIO(), io.StringIO()
    kwargs = {"limits": limits}
    if clock is not None:
        kwargs["clock"] = clock
    if port is not None:
        kwargs["alert_port"] = port
    code = admin.main([str(directory), *argv], out=out, err=err, **kwargs)
    return code, out.getvalue(), err.getvalue()


def _seeded(tmp_path: Path) -> StateStore:
    store = _store(tmp_path)
    state = _stopped_state()
    state.halt = state.mac_halt = state.account_mismatch = True
    store.save(state)
    AuditLog(store.directory / "audit.jsonl", anchor=store).append(
        {"decision": "EVALUATED", "route": "seed"}
    )
    return store


def test_admin_status_prints_numbers_and_names_only(tmp_path):
    store = _seeded(tmp_path)
    code, out, _ = _run(store.directory, "status")
    assert code == 0
    body = json.loads(out)
    assert body["latches"] == ["halt", "mac_halt", "account_mismatch", "stop"]
    assert body["audit_ok"] is True and body["audit_entries"] == 1


@pytest.mark.parametrize("latch", ["halt", "mac_halt", "account_mismatch"])
def test_admin_reset_clears_one_latch_with_a_reset_audit_entry(tmp_path, latch):
    store = _seeded(tmp_path)
    code, _, _ = _run(store.directory, "reset", "--latch", latch)
    assert code == 0
    assert getattr(store.load(), latch) is False
    last = AuditLog(store.directory / "audit.jsonl").entries_after(0)[-1]
    assert (last["decision"], last["codes"], last["route"]) == ("RESET", [latch], "admin")
    assert AuditLog(store.directory / "audit.jsonl").verify()[0] == 2


def test_admin_reset_stop_by_isin_and_all(tmp_path):
    store = _seeded(tmp_path)
    assert _run(store.directory, "reset", "--latch", "stop", "--isin", OTHER)[0] == 1  # no such stop
    assert _run(store.directory, "reset", "--latch", "stop", "--isin", ISIN)[0] == 0
    assert store.load().stops == {}
    assert _run(store.directory, "reset", "--latch", "halt", "--isin", ISIN)[0] == 1  # isin only with stop


def test_admin_refuses_to_reset_ended(tmp_path):
    store = _seeded(tmp_path)
    state = store.load()
    state.ended = True
    store.save(state)
    before = AuditLog(store.directory / "audit.jsonl").verify()
    code, _, err = _run(store.directory, "reset", "--latch", "ended")
    assert code == 1 and "terminal" in err
    assert store.load().ended is True
    assert AuditLog(store.directory / "audit.jsonl").verify() == before


def test_admin_refuses_reset_when_the_audit_chain_is_broken(tmp_path):
    store = _seeded(tmp_path)
    audit_path = store.directory / "audit.jsonl"
    audit_path.write_bytes(audit_path.read_bytes().replace(b"EVALUATED", b"REFUSED  "))
    code, _, err = _run(store.directory, "reset", "--latch", "halt")
    assert code == 1 and "audit" in err
    assert store.load().halt is True  # unchanged
    assert _run(store.directory, "verify-audit")[0] == 1


def test_admin_reset_fails_closed_on_corrupt_state(tmp_path):
    store = _seeded(tmp_path)
    store.path.write_text("{broken")
    assert _run(store.directory, "reset", "--latch", "halt")[0] == 1
    assert _run(store.directory, "status")[0] == 1


def _halted_store(tmp_path: Path) -> StateStore:
    store = _store(tmp_path)
    store.save(_halted_state())
    AuditLog(store.directory / "audit.jsonl", anchor=store).append(
        {"decision": "EVALUATED", "route": "seed"}
    )
    return store


def test_admin_halt_reset_refuses_while_drawdown_is_at_the_threshold(tmp_path):
    store = _halted_store(tmp_path)
    before_state, before_audit = store.path.read_bytes(), (store.directory / "audit.jsonl").read_bytes()
    code, out, err = _run(store.directory, "reset", "--latch", "halt")
    assert code == 1 and out == "" and "--rebase-halt-anchor" in err
    assert store.path.read_bytes() == before_state  # nothing cleared, anchor not touched
    assert (store.directory / "audit.jsonl").read_bytes() == before_audit  # and not audited as a reset
    assert store.load().halt is True


def test_admin_halt_reset_with_the_flag_is_refused_once_the_pilot_has_ended(tmp_path):
    store = _halted_store(tmp_path)
    state = store.load()
    _close(state, 12, "425")  # exactly -15%: ended, halt stays latched
    assert state.ended and state.halt
    store.save(state)
    before_state, before_audit = store.path.read_bytes(), (store.directory / "audit.jsonl").read_bytes()
    for args in (("reset", "--latch", "halt"), ("reset", "--latch", "halt", "--rebase-halt-anchor")):
        code, out, err = _run(store.directory, *args)
        assert code == 1 and out == "" and "pilot is ended" in err
    assert store.path.read_bytes() == before_state  # no anchor set, halt not cleared
    assert (store.directory / "audit.jsonl").read_bytes() == before_audit  # and no RESET entry
    assert store.load().halt_anchor is None


def test_admin_halt_reset_with_the_flag_sets_the_anchor_and_audits_it(tmp_path):
    store = _halted_store(tmp_path)
    code, out, _ = _run(store.directory, "reset", "--latch", "halt", "--rebase-halt-anchor")
    assert code == 0 and json.loads(out)["halt_anchor_rebased"] is True
    state = store.load()
    assert state.halt is False and state.halt_anchor == Decimal("46000") and state.peak == Decimal("50000")
    last = AuditLog(store.directory / "audit.jsonl").entries_after(0)[-1]
    assert (last["decision"], last["route"], last["codes"]) == ("RESET", "admin", ["halt", "rebase_halt_anchor"])
    assert last["limits_sha256"] == LIMITS.sha256
    code, out, _ = _run(store.directory, "status")
    assert json.loads(out)["halt_anchor"] == "46000" and json.loads(out)["latches"] == []
    # The next evaluated close, from the persisted state, does not latch it again.
    _close(state, 12, "455")
    assert not state.halt
    assert _run(store.directory, "verify-audit")[0] == 0


def test_admin_plain_halt_reset_above_the_threshold_records_no_anchor(tmp_path):
    store = _halted_store(tmp_path)
    state = store.load()
    state.drawdown = Decimal("-0.05")
    store.save(state)
    code, out, _ = _run(store.directory, "reset", "--latch", "halt")
    assert code == 0 and json.loads(out)["halt_anchor_rebased"] is False
    assert store.load().halt_anchor is None
    last = AuditLog(store.directory / "audit.jsonl").entries_after(0)[-1]
    assert last["codes"] == ["halt"]


def test_admin_rebase_flag_on_another_latch_is_refused(tmp_path):
    store = _seeded(tmp_path)
    code, _, err = _run(store.directory, "reset", "--latch", "mac_halt", "--rebase-halt-anchor")
    assert code == 1 and "halt only" in err and store.load().mac_halt is True


def test_admin_reset_without_a_readable_limits_file_is_refused(tmp_path):
    store = _seeded(tmp_path)
    missing = tmp_path / "no-limits.json"
    code, _, err = _run(store.directory, "reset", "--latch", "mac_halt", limits=None)
    assert code == 1 and "limits" in err and store.load().mac_halt is True
    out, errs = io.StringIO(), io.StringIO()
    assert admin.main([str(store.directory), "reset", "--latch", "mac_halt", "--limits", str(missing)], out=out, err=errs) == 1
    assert store.load().mac_halt is True


def test_admin_reset_audit_entry_carries_the_real_limits_hash(tmp_path):
    store = _seeded(tmp_path)
    assert _run(store.directory, "reset", "--latch", "mac_halt")[0] == 0
    last = AuditLog(store.directory / "audit.jsonl").entries_after(0)[-1]
    assert last["limits_sha256"] == LIMITS.sha256


def test_admin_verify_audit_ok(tmp_path):
    store = _seeded(tmp_path)
    code, out, _ = _run(store.directory, "verify-audit")
    assert code == 0 and json.loads(out)["entries"] == 1 and json.loads(out)["torn_tail"] is False


def _entries(store: StateStore) -> int:
    return AuditLog(store.directory / "audit.jsonl", anchor=store).verify()[0]


@pytest.mark.parametrize("damage", ["deleted", "emptied"])
def test_admin_refuses_every_command_that_trusts_the_log_when_it_is_gone(tmp_path, damage):
    store = _seeded(tmp_path)
    audit_path = store.directory / "audit.jsonl"
    before_state = store.path.read_bytes()
    if damage == "deleted":
        audit_path.unlink()
    else:
        audit_path.write_bytes(b"")
    code, _, err = _run(store.directory, "reset", "--latch", "halt")
    assert code == 1 and "audit" in err
    assert store.path.read_bytes() == before_state  # no latch cleared, nothing rewritten
    assert (audit_path.read_bytes() if audit_path.exists() else b"") == b""  # not restarted
    assert _run(store.directory, "verify-audit")[0] == 1
    code, out, _ = _run(store.directory, "status")
    assert code == 1 and json.loads(out)["audit_ok"] is False
    assert _run(store.directory, "session-end-check", clock=Clock("2026-10-12T15:30:00+05:30"), port=Port())[0] == 1


def test_admin_refuses_reset_when_whole_tail_lines_were_dropped(tmp_path):
    store = _seeded(tmp_path)
    assert _run(store.directory, "reset", "--latch", "mac_halt")[0] == 0  # a second entry, anchored
    audit_path = store.directory / "audit.jsonl"
    first = audit_path.read_bytes().splitlines(keepends=True)[0]
    audit_path.write_bytes(first)  # drop the RESET entry: a valid prefix
    before = store.path.read_bytes()
    code, _, err = _run(store.directory, "reset", "--latch", "account_mismatch")
    assert code == 1 and "audit" in err
    assert store.path.read_bytes() == before


def test_admin_reset_keeps_the_anchor_in_step_with_the_log(tmp_path):
    store = _seeded(tmp_path)
    for latch in ("mac_halt", "account_mismatch", "halt"):
        assert _run(store.directory, "reset", "--latch", latch)[0] == 0
    assert store.audit_anchor()[0] == 4 == _entries(store)
    assert _run(store.directory, "verify-audit")[0] == 0


def test_admin_session_end_check_keeps_the_anchor_in_step_with_the_log(tmp_path):
    store = _seeded(tmp_path)
    clock = Clock("2026-10-12T15:30:00+05:30")
    assert _run(store.directory, "session-end-check", clock=clock, port=Port())[0] == 0
    # The alert path audits, then saves state that was loaded before the audit.
    assert store.audit_anchor()[0] == 2 == _entries(store)
    assert _run(store.directory, "verify-audit")[0] == 0


def test_admin_verify_audit_reports_and_recovers_a_torn_tail(tmp_path):
    store = _seeded(tmp_path)
    audit_path = store.directory / "audit.jsonl"
    audit_path.write_bytes(audit_path.read_bytes() + b'{"seq":2,"prev')
    code, out, _ = _run(store.directory, "verify-audit")
    assert code == 0 and json.loads(out)["torn_tail"] is True and json.loads(out)["entries"] == 1
    assert _run(store.directory, "reset", "--latch", "mac_halt")[0] == 0  # next append truncates it
    assert audit_path.read_bytes().endswith(b"\n")
    code, out, _ = _run(store.directory, "verify-audit")
    body = json.loads(out)
    assert (body["ok"], body["entries"], body["torn_tail"]) == (True, 2, False)


def test_admin_session_end_check_sends_records_and_does_not_repeat(tmp_path):
    store = _seeded(tmp_path)
    clock, port = Clock("2026-10-12T15:30:00+05:30"), Port()
    code, out, _ = _run(store.directory, "session-end-check", clock=clock, port=port)
    assert code == 0 and json.loads(out)["alerts_sent"] == 1 and len(port.alerts) == 1
    assert _run(store.directory, "session-end-check", clock=clock, port=port)[0] == 0
    assert len(port.alerts) == 1  # persisted: the second process run does not repeat
    assert store.load().alerts_sent == [f"2026-10-12:{ISIN}"]


def test_admin_session_end_check_audits_the_real_limits_hash(tmp_path):
    store = _seeded(tmp_path)
    code, out, _ = _run(store.directory, "session-end-check",
                        clock=Clock("2026-10-12T15:30:00+05:30"), port=Port())
    assert code == 0 and json.loads(out)["limits_ok"] is True
    last = AuditLog(store.directory / "audit.jsonl").entries_after(0)[-1]
    assert (last["route"], last["codes"]) == ("session_end", ["stop_exit_open"])
    assert last["limits_sha256"] == LIMITS.sha256 and last["limits_sha256"] is not None


def test_admin_session_end_check_audits_the_hash_on_a_failed_alert_too(tmp_path):
    store = _seeded(tmp_path)
    code, _, _ = _run(store.directory, "session-end-check",
                      clock=Clock("2026-10-12T15:30:00+05:30"), port=Port(fail=True))
    assert code == 1
    last = AuditLog(store.directory / "audit.jsonl").entries_after(0)[-1]
    assert last["codes"] == ["alert_failed"] and last["limits_sha256"] == LIMITS.sha256


def test_admin_session_end_check_alerts_even_when_the_limits_cannot_be_read(tmp_path):
    store = _seeded(tmp_path)
    port = Port()
    code, out, _ = _run(store.directory, "session-end-check", clock=Clock("2026-10-12T15:30:00+05:30"),
                        port=port, limits=None)  # no limits file on this machine
    assert code == 1 and json.loads(out)["limits_ok"] is False  # loud, but the alert still went out
    assert len(port.alerts) == 1
    last = AuditLog(store.directory / "audit.jsonl").entries_after(0)[-1]
    assert last["limits_sha256"] is None


def test_admin_session_end_check_exits_nonzero_when_the_alert_fails(tmp_path):
    store = _seeded(tmp_path)
    clock = Clock("2026-10-12T15:30:00+05:30")
    code, out, _ = _run(store.directory, "session-end-check", clock=clock, port=Port(fail=True))
    assert code == 1 and json.loads(out)["alerts_failed"] == 1
    assert store.load().stops  # nothing blocked or unblocked
    # No port bound at all (63-05 not landed): also non-zero, never silent.
    assert _run(store.directory, "session-end-check", clock=clock)[0] == 1


def test_admin_usage_errors_exit_2(tmp_path):
    assert _run(tmp_path)[0] == 2
    assert _run(tmp_path, "reset")[0] == 2
    assert _run(tmp_path, "reset", "--latch", "nonsense")[0] == 2


def test_admin_imports_no_http_or_web_framework():
    import ast

    tree = ast.parse((ROOT / "gateway/vm/gateway_vm/orders/admin.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported.isdisjoint({"http", "urllib", "fastapi", "starlette", "requests", "socket"})
