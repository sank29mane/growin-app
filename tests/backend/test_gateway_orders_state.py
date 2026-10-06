"""VM risk state, latches, charge bound, durable store, session-end alert and
admin CLI (Phase 63-01, Task 2). Fakes and tmp dirs only.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import sys
from datetime import date, datetime
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
from gateway_vm.orders import store as store_mod  # noqa: E402
from gateway_vm.orders.audit import AuditBroken, AuditLog  # noqa: E402
from gateway_vm.orders.limits import (  # noqa: E402
    Holding,
    Limits,
    Trade,
)
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
    risk.reset_latch(state, "halt")
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
        "last_evaluated_session", "drawdown", "halt", "ended", "mac_halt",
        "account_mismatch", "stops", "fills", "consumed_intents", "alerts_sent",
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


def _run(directory: Path, *argv: str, clock=None, port=None):
    out, err = io.StringIO(), io.StringIO()
    kwargs = {}
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
    AuditLog(store.directory / "audit.jsonl").append({"decision": "EVALUATED", "route": "seed"})
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


def test_admin_verify_audit_ok(tmp_path):
    store = _seeded(tmp_path)
    code, out, _ = _run(store.directory, "verify-audit")
    assert code == 0 and json.loads(out)["entries"] == 1


def test_admin_session_end_check_sends_records_and_does_not_repeat(tmp_path):
    store = _seeded(tmp_path)
    clock, port = Clock("2026-10-12T15:30:00+05:30"), Port()
    code, out, _ = _run(store.directory, "session-end-check", clock=clock, port=port)
    assert code == 0 and json.loads(out)["alerts_sent"] == 1 and len(port.alerts) == 1
    assert _run(store.directory, "session-end-check", clock=clock, port=port)[0] == 0
    assert len(port.alerts) == 1  # persisted: the second process run does not repeat
    assert store.load().alerts_sent == [f"2026-10-12:{ISIN}"]


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
