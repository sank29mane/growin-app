"""Phase 64-03 Task 2 part A: the shadow portfolio with Option B (D-05, D-08, D-15, Q-6).

Two kinds of rows. Rows on a ``FakeAccount`` drive the portfolio over exact synthetic numbers and the shared 63-02
drawdown vectors; rows on a real simulation ledger (``make_rig``) prove the integration: the cash the ledger holds,
the real admission guard refusing a buy while a latch is open, a real expiry of an INCONCLUSIVE exit, and a real
reopen. Every value is synthetic. Nothing here reaches a broker, the relay, the VM or ``private/``.
"""

from __future__ import annotations

import ast
import json
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from execution.ledger import simulated_booking_id
from execution.models import AdmissionDecision
from india_limits_support import NOW, make_evidence
from risk_india import drawdown, exits, rules
from risk_india.state import LatchStore, state_path_for, write_state
from shadow_india import policy
from shadow_india.admission import SIMULATOR_DRAWDOWN_BASIS, QuoteSample
from shadow_india.evidence import EvidenceStore
from shadow_india.portfolio import (
    MARK_CARRIED_EX_DATE,
    MARK_RAW_CLOSE,
    PEAK_SOURCE,
    Instrument,
    PortfolioAborted,
    PortfolioError,
    ShadowPortfolio,
)
from strategy_india.portfolio import Book
from strategy_india.portfolio import Position as BookPosition
from test_shadow_india_admission import (
    RUN_ID,
    SEED,
    Rig,
    frozen_service_clock,  # noqa: F401 - a fixture, used through pytestmark
    make_rig,
    reopen,
    run,
    sha,
)
from test_strategy_india_support import limits as limits62
from test_strategy_india_support import params as params62
from test_strategy_india_support import weekday_sessions

pytestmark = pytest.mark.usefixtures("frozen_service_clock")

ROOT = Path(__file__).resolve().parents[2]
VECTORS = json.loads((ROOT / "tests/backend/fixtures/relay_orders/limits_vectors.json").read_text(encoding="utf-8"))
LIMITS = rules.Limits.from_fields(VECTORS["limits"])
CAP = LIMITS.capital_cap
FIXED = datetime(2026, 10, 22, 4, 0, tzinfo=timezone.utc)
SCHEDULE = sha("schedule")
D1, D2, D3, D4 = date(2026, 10, 9), date(2026, 10, 12), date(2026, 10, 13), date(2026, 10, 14)

ISINS = {
    "AAA": "INE000A01012",
    "BBB": "INE111B01023",
    "RELIANCE": "INE002A01018",
    "TCS": "INE467B01029",
    "INFY": "INE009A01021",
    "HDFCBANK": "INE040A01034",
    "SBIN": "INE062A01020",
}
A, B = ISINS["AAA"], ISINS["BBB"]
CLEAR_ISIN = "INE999A01010"


def instruments(*symbols: str) -> list[Instrument]:
    return [Instrument(ISINS[s], s, s) for s in symbols]


# --- a fake simulation account, for exact numbers -------------------------------------------------------


class FakeAccount:
    """The two reads the portfolio takes, over numbers a test sets. ``cash`` already carries charges."""

    def __init__(self, seed: Decimal = CAP, cash: Decimal | None = None) -> None:
        self.seed = seed
        self.cash = seed if cash is None else cash
        self.reserved = Decimal(0)
        self.held: dict[str, tuple[int, Decimal]] = {}
        self.buys: list[tuple[str, Decimal, Decimal]] = []
        self.sells: dict[str, Decimal] = {}

    def buy(self, symbol: str, quantity: int, price: str, charges: str = "0") -> None:
        held_q, held_c = self.held.get(symbol, (0, Decimal(0)))
        cost = quantity * Decimal(price)
        self.held[symbol] = (held_q + quantity, held_c + cost)
        self.cash -= cost + Decimal(charges)

    def sell(self, symbol: str, quantity: int, price: str, charges: str = "0") -> None:
        held_q, held_c = self.held[symbol]
        left = held_q - quantity
        self.held[symbol] = (left, held_c * left / held_q if held_q else Decimal(0))
        self.cash += quantity * Decimal(price) - Decimal(charges)

    def simulated_cash_view(self) -> Any:
        return SimpleNamespace(seed=self.seed, reserved=self.reserved, net_cash_out=self.seed - self.cash)

    def india_account_view(self, *, exclude_proposal_id: str = "") -> Any:
        return SimpleNamespace(
            positions=tuple((f"NSE:CASH:{s}", Decimal(q), c) for s, (q, c) in sorted(self.held.items())),
            open_buys=tuple((f"NSE:CASH:{s}", Decimal(q), p) for s, q, p in self.buys),
            open_sells={f"NSE:CASH:{s}": Decimal(q) for s, q in self.sells.items()},
        )


def make_evidence_store(root: Path) -> EvidenceStore:
    return EvidenceStore.create(
        root, run_id="run1", run_class="plumbing", session_id="s-1", hashes={"strategy_sha256": sha("x")},
        clock=lambda: FIXED,
    )


def reopen_evidence(root: Path) -> EvidenceStore:
    return EvidenceStore.open(root, run_id="run1", session_id="s-1", clock=lambda: FIXED)


class Fake:
    def __init__(self, tmp_path: Path, symbols=("AAA", "BBB"), *, start_peak: str | None = None, account=None):
        self.tmp = tmp_path
        self.root = tmp_path / "evidence"
        self.account = account or FakeAccount()
        self.store = make_evidence_store(self.root)
        self.latch_path = tmp_path / "india-risk-state.json"
        self.symbols = symbols
        self.latch = LatchStore(self.latch_path, LIMITS, has_fills=lambda: False)
        if start_peak is not None:
            write_state(self.latch_path, drawdown.RiskState(peak=Decimal(start_peak)))
        self.portfolio = ShadowPortfolio(self.account, self.latch, self.store, instruments(*symbols))

    def restart(self) -> "Fake":
        """A new process: the files stay, every object is rebuilt."""
        again = Fake.__new__(Fake)
        again.tmp, again.root, again.account, again.symbols = self.tmp, self.root, self.account, self.symbols
        again.store = reopen_evidence(self.root)
        again.latch_path = self.latch_path
        again.latch = LatchStore(self.latch_path, LIMITS, has_fills=lambda: False)
        again.portfolio = ShadowPortfolio(self.account, again.latch, again.store, instruments(*self.symbols))
        return again

    def mark(self, day: date, **closes: str):
        return self.portfolio.mark_session(day, {ISINS[s]: Decimal(c) for s, c in closes.items()})

    def records(self, kind: str) -> list[dict[str, Any]]:
        return [r for r in self.store.records() if r["kind"] == kind]


def at_peak(tmp_path: Path, **kw: Any) -> Fake:
    """A 100-share AAA position bought at 500 with no cash left: equity = 100 x close."""
    world = Fake(tmp_path, **kw)
    world.account.buy("AAA", 100, "500")
    return world


# --- the account is read, not guessed -------------------------------------------------------------------


def test_equity_is_cash_plus_raw_closes_with_charges_in_the_cash(tmp_path):
    world = Fake(tmp_path)
    world.account.buy("AAA", 20, "500", charges="25")  # cash 50000 - 10000 - 25
    result = world.mark(D1, AAA="520")
    assert result.equity == Decimal("39975") + Decimal("10400") == Decimal("50375")
    (mark,) = world.records("mark")
    assert (mark["cash"], mark["held_capital"], mark["exposure"], mark["equity"]) == ("39975", "10000", "10400", "50375")
    assert (mark["peak"], mark["drawdown"], mark["peak_source"]) == ("50375", "0.000000", PEAK_SOURCE)
    (position,) = world.records("position_mark")
    assert (position["isin"], position["quantity"], position["close"], position["cost"], position["mark_basis"]) == (
        A, "20", "520", "10000", MARK_RAW_CLOSE,
    )


def test_a_charge_lowers_equity_by_exactly_the_charge(tmp_path):
    free, charged = Fake(tmp_path / "a"), Fake(tmp_path / "b")
    free.account.buy("AAA", 20, "500")
    charged.account.buy("AAA", 20, "500", charges="37.5")
    assert free.mark(D1, AAA="500").equity - charged.mark(D1, AAA="500").equity == Decimal("37.5")


def test_open_exposure_is_in_the_exposure_figure_and_reservations_stay_inside_equity(tmp_path):
    world = Fake(tmp_path)
    world.account.buy("AAA", 20, "500")
    world.account.reserved = Decimal("3000")
    world.account.buys = [("BBB", 30, Decimal("100"))]
    result = world.mark(D1, AAA="500")
    (mark,) = world.records("mark")
    assert mark["exposure"] == "13000" and result.equity == Decimal("50000")
    snap = world.portfolio.snapshot()
    assert snap.reserved == Decimal("3000") and snap.open_buys[0].quantity == 30
    assert snap.holdings[0].quantity == 20 and snap.holdings[0].cost == Decimal("10000")


def test_a_ticker_the_run_never_admitted_aborts_the_run_and_refuses_the_mark(tmp_path):
    world = Fake(tmp_path, symbols=("AAA",))
    world.account.buy("BBB", 5, "100")  # a position nobody explained
    with pytest.raises(PortfolioAborted) as caught:
        world.mark(D1, AAA="500")
    assert caught.value.code == "unexplained_account_position"
    assert policy.run_state(world.store).abort_events == ("unexplained_account_position",)
    with pytest.raises(policy.RunAborted):
        world.mark(D2, AAA="500")


def test_an_unexplained_open_order_aborts_too(tmp_path):
    world = Fake(tmp_path, symbols=("AAA",))
    world.account.sells = {"BBB": Decimal(3)}
    with pytest.raises(PortfolioAborted):
        world.portfolio.snapshot()


def test_a_seed_that_is_not_the_capital_cap_is_refused(tmp_path):
    with pytest.raises(PortfolioError) as caught:
        Fake(tmp_path, account=FakeAccount(seed=Decimal("40000")))
    assert caught.value.code == "seed_capital_cap_mismatch"


def test_a_held_name_without_a_close_pauses_that_isin_and_touches_no_latch(tmp_path):
    world = Fake(tmp_path)
    world.account.buy("AAA", 20, "500")
    world.account.buy("BBB", 20, "500")
    with pytest.raises(PortfolioError) as caught:
        world.mark(D1, AAA="500")
    assert caught.value.code == "mark_missing"
    assert [(p.event, p.scope) for p in policy.run_state(world.store).open_pauses] == [("missing_bar", f"isin:{B}")]
    assert world.latch.load().last_session is None and world.records("mark") == []
    with pytest.raises(PortfolioError):
        world.mark(D1, AAA="500")  # asking again does not stack a second pause
    assert len(policy.run_state(world.store).open_pauses) == 1


@pytest.mark.parametrize("bad", ["0", "-5", "NaN", "Infinity"])
def test_a_close_that_is_not_a_positive_number_is_refused(tmp_path, bad):
    world = at_peak(tmp_path)
    with pytest.raises(PortfolioError) as caught:
        world.mark(D1, AAA=bad)
    assert caught.value.code == "bad_close" and world.latch.load().last_session is None


# --- the -8% halt, the halves and the -15% flatten ----------------------------------------------------------


def test_minus_7_99_percent_latches_nothing_and_minus_8_exactly_halts_entries(tmp_path):
    near = at_peak(tmp_path / "near")
    result = near.mark(D1, AAA="460.05")
    assert result.drawdown == Decimal("-0.079900") and not result.state.halt
    assert near.records("latch") == [] and policy.run_state(near.store).open_pauses == ()
    assert near.portfolio.entry_block_reasons() == ()

    edge = at_peak(tmp_path / "edge")
    result = edge.mark(D1, AAA="460")
    assert result.state.halt and not result.state.ended and result.drawdown == Decimal("-0.080000")
    assert [(p.event, p.scope) for p in policy.run_state(edge.store).open_pauses] == [("drawdown_halt", "entries")]
    assert edge.portfolio.entry_block_reasons() == ("halt_latch", "run_paused")
    assert [(r["latch"], r["scope"]) for r in edge.records("latch")] == [("halt", "entries")]


@pytest.mark.parametrize(("quantity", "halve"), [(1, None), (2, 1), (3, 1), (7, 3), (8, 4), (99, 49), (100, 50)])
def test_the_halt_emits_floor_q_over_2_and_nothing_for_a_one_share_name(tmp_path, quantity, halve):
    world = Fake(tmp_path)
    world.account.buy("AAA", quantity, "100")
    world.account.cash = Decimal("46000") - quantity * Decimal("100")  # equity lands exactly on -8% at close 100
    result = world.mark(D1, AAA="100")
    assert result.state.halt
    if halve is None:
        assert result.batches == () and world.portfolio.exit_proposals(D2).proposals == ()
        return
    (batch,) = result.batches
    assert batch.reason == "halve" and batch.intents[0].quantity == halve
    (proposal,) = world.portfolio.exit_proposals(D2).proposals
    assert (proposal.reason, proposal.quantity, proposal.reproposed) == ("halve", halve, False)


def test_a_halt_halves_every_name_and_stops_buys_at_the_real_gate(tmp_path):
    rig = make_rig(tmp_path)
    store = make_evidence_store(tmp_path / "evidence")
    try:
        names = (("RELIANCE", 190), ("TCS", 190), ("INFY", 99), ("HDFCBANK", 7), ("SBIN", 1))
        for symbol, quantity in names:
            own(rig, symbol, quantity)
        portfolio = ShadowPortfolio(rig.ledger, rig.guard.store, store, instruments(*[s for s, _ in names]))
        result = portfolio.mark_session(D1, {ISINS[s]: Decimal("91.70") for s, _ in names})
        assert result.state.halt and not result.state.ended and result.state.stops == {}
        legs = {i.isin: i.quantity for b in result.batches for i in b.intents}
        assert legs == {ISINS["RELIANCE"]: 95, ISINS["TCS"]: 95, ISINS["INFY"]: 49, ISINS["HDFCBANK"]: 3}
        denied = run(rig.adapter.prepare(buy_intent(rig, "next", "RELIANCE", 1), samples_for("RELIANCE")))
        assert denied.decision is AdmissionDecision.DENIED and denied.reason_code == "halt_latch"
        assert portfolio.buys_blocked(ISINS["RELIANCE"])
    finally:
        rig.close()


def test_minus_15_flattens_everything_ends_the_run_and_blocks_entries_for_good(tmp_path):
    world = at_peak(tmp_path)
    result = world.mark(D1, AAA="425")
    assert result.state.ended and result.state.halt
    (batch,) = result.batches  # the flatten batch only: no halve, no stop batch
    assert batch.reason == "flatten" and batch.intents[0].quantity == 100
    run_state = policy.run_state(world.store)
    assert run_state.ended and run_state.ended_events == ("drawdown_flatten_15",)
    assert [r["event"] for r in world.records("run_ended")] == ["drawdown_flatten_15"]
    assert {"pilot_ended", "run_ended", "halt_latch"} <= set(world.portfolio.entry_block_reasons())
    for step in (policy.STEP_ADMIT, policy.STEP_MINT):
        assert run_state.blocks(step, isin=CLEAR_ISIN)
    for step in (policy.STEP_BOOK, policy.STEP_EXPIRE, policy.STEP_READ_ONLY):
        assert not run_state.blocks(step)


def test_a_gap_from_above_minus_8_to_below_minus_15_emits_the_flatten_batch_only(tmp_path):
    world = Fake(tmp_path)
    world.account.buy("AAA", 100, "500")
    result = world.mark(D1, AAA="300")
    assert [b.reason for b in result.batches] == ["flatten"]
    assert world.latch.load().open_exits[A].reason == "flatten"


def test_the_run_end_survives_marks_and_restarts_and_the_wind_down_keeps_running(tmp_path):
    world = at_peak(tmp_path)
    world.mark(D1, AAA="425")
    assert world.mark(D2, AAA="700").state.ended  # a full recovery does not undo it
    assert [r["event"] for r in world.records("run_ended")] == ["drawdown_flatten_15"]  # recorded once
    again = world.restart()
    assert policy.run_state(again.store).ended and "run_ended" in again.portfolio.entry_block_reasons()
    again.mark(D3, AAA="700")  # marks still run
    (proposal,) = again.portfolio.exit_proposals(D4).proposals
    assert (proposal.reason, proposal.quantity, proposal.reproposed) == ("flatten", 100, True)
    again.account.sell("AAA", 100, "700")  # the simulated flatten books
    again.portfolio.on_booked_sell(A, 100)
    assert again.portfolio.exit_proposals(D4).proposals == () and again.portfolio.is_flat()
    assert policy.run_state(again.store).ended and again.latch.load().ended


def test_the_run_end_cannot_be_released_or_resumed(tmp_path):
    world = at_peak(tmp_path)
    world.mark(D1, AAA="425")
    before = world.store.records()
    with pytest.raises(policy.PolicyError) as released:
        world.portfolio.release_halt("operator", "reviewed the flatten", rebase_halt_anchor=True)
    assert released.value.code == "resume_refused_run_ended"
    with pytest.raises(policy.PolicyError) as resumed:
        policy.resume(world.store, "reviewed the flatten")
    assert resumed.value.code in {"drawdown_resume_requires_latch"}
    assert world.store.records() == before and world.latch.load().ended and world.latch.load().halt


# --- the -12% stop and the stop-open buy block --------------------------------------------------------------


def test_the_stop_is_raised_at_the_close_and_the_exit_is_proposed_at_the_next_decide_only(tmp_path):
    world = Fake(tmp_path)
    world.account.buy("AAA", 20, "500")
    world.account.buy("BBB", 20, "500")
    world.mark(D1, AAA="440", BBB="500")  # 440 = 500 x 0.88 exactly
    assert sorted(world.latch.load().stops) == [A]
    with pytest.raises(PortfolioError) as same_day:
        world.portfolio.exit_proposals(D1)  # a decide on the stop's own session would read its close
    assert same_day.value.code == "marks_not_before_decide"
    (proposal,) = world.portfolio.exit_proposals(D2).proposals
    assert (proposal.reason, proposal.isin, proposal.quantity, proposal.since) == ("stop", A, 20, D1)
    assert [(r["latch"], r["scope"], r["reason"]) for r in world.records("latch")] == [("stop", f"isin:{A}", "raised_2026-10-09")]


def test_440_01_does_not_stop_a_20_share_500_position(tmp_path):
    world = Fake(tmp_path)
    world.account.buy("AAA", 20, "500")
    assert world.mark(D1, AAA="440.01").state.stops == {}


def test_a_vol_scaled_stop_is_honoured_only_when_tighter(tmp_path):
    tight, loose = Fake(tmp_path / "t"), Fake(tmp_path / "l")
    for world in (tight, loose):
        world.account.buy("AAA", 20, "500")
    tighter = tight.portfolio.mark_session(D1, {A: Decimal("470")}, vol_stops={A: Decimal("-0.05")})
    assert sorted(tighter.state.stops) == [A] and tighter.config_errors == ()
    looser = loose.portfolio.mark_session(D1, {A: Decimal("470")}, vol_stops={A: Decimal("-0.20")})
    assert looser.state.stops == {} and looser.config_errors == (f"vol_stop_looser_than_position_stop:{A}",)
    assert [r["reason"] for r in loose.records("latch")] == [f"vol_stop_looser_than_position_stop:{A}"]
    assert loose.mark(D2, AAA="440").state.stops.keys() == {A}  # the fixed -12% still applies


def test_any_open_stop_exit_blocks_every_buy_at_the_real_gate_and_writes_the_session_end_line(tmp_path):
    rig = make_rig(tmp_path)
    store = make_evidence_store(tmp_path / "evidence")
    try:
        own(rig, "RELIANCE", 100)
        portfolio = ShadowPortfolio(rig.ledger, rig.guard.store, store, instruments("RELIANCE", "TCS"))
        result = portfolio.mark_session(D1, {ISINS["RELIANCE"]: Decimal("87")})  # 8700 <= 10000 x 0.88
        assert sorted(result.stop_open) == [ISINS["RELIANCE"]] and not result.state.halt
        # A different name is blocked too: the block is account-wide.
        denied = run(rig.adapter.prepare(buy_intent(rig, "other", "TCS", 5), samples_for("TCS")))
        assert denied.decision is AdmissionDecision.DENIED and denied.reason_code == "stop_open"
        assert portfolio.entry_block_reasons(ISINS["TCS"]) == ("stop_open",)
        (report,) = [r for r in store.records() if r["kind"] == "stop_open_report"]
        assert report["isins"] == [ISINS["RELIANCE"]] and report["buys_blocked"] is True
        assert report["as_of_date"] == D1.isoformat()
        portfolio.session_end_report(D1)  # once per session
        assert len([r for r in store.records() if r["kind"] == "stop_open_report"]) == 1
        # The exit fills in full: the stop clears and the block lifts. That is fill evidence, not a reset.
        sell_and_fill(rig, "exit-1", "RELIANCE", 100)
        portfolio.on_booked_sell(ISINS["RELIANCE"], 100)
        assert portfolio.entry_block_reasons(ISINS["TCS"]) == ()
        admitted = run(rig.adapter.prepare(buy_intent(rig, "after", "TCS", 5), samples_for("TCS")))
        assert admitted.decision is AdmissionDecision.ADMITTED
        assert [r["reason"] for r in store.records() if r["kind"] == "latch"][-1] == "cleared_by_fill"
    finally:
        rig.close()


def test_a_partial_exit_fill_leaves_the_stop_and_the_block_in_place(tmp_path):
    world = Fake(tmp_path)
    world.account.buy("AAA", 20, "500")
    world.mark(D1, AAA="400")
    world.account.sell("AAA", 8, "400")
    world.portfolio.on_booked_sell(A, 8)
    assert sorted(world.latch.load().stops) == [A] and world.portfolio.buys_blocked()
    (proposal,) = world.portfolio.exit_proposals(D2).proposals
    assert proposal.quantity == 12


def test_an_exit_closed_inconclusive_stays_unfilled_and_is_proposed_again_at_the_next_decide(tmp_path):
    """Row ``inconclusive_exit_reproposed_next_decide`` (D-15), on a real ledger and a real expiry."""
    rig = make_rig(tmp_path)
    store = make_evidence_store(tmp_path / "evidence")
    try:
        own(rig, "RELIANCE", 100)
        portfolio = ShadowPortfolio(rig.ledger, rig.guard.store, store, instruments("RELIANCE"))
        isin = ISINS["RELIANCE"]
        portfolio.mark_session(D1, {isin: Decimal("87")})
        (first,) = portfolio.exit_proposals(D2).proposals
        assert (first.reason, first.quantity, first.reproposed) == ("stop", 100, False)
        # Decide D2: the exit is admitted and reserved, then its attempt closes INCONCLUSIVE.
        admitted = run(rig.adapter.prepare(sell_intent(rig, "exit-1", "RELIANCE", 100), samples_for("RELIANCE")))
        assert admitted.decision is AdmissionDecision.ADMITTED
        assert portfolio.exit_proposals(D3).waiting == (isin,)  # still reserved: never offer the shares twice
        portfolio.mark_session(D2, {isin: Decimal("86")})
        rig.ledger.expire_simulated_order("exit-1", "close:exit-1", "inconclusive_no_fill_assumed")
        # Nothing filled, so nothing moved: same holding, same stop, buys still blocked.
        assert [h.quantity for h in portfolio.snapshot().holdings] == [100]
        assert sorted(rig.guard.store.load().stops) == [isin] and portfolio.buys_blocked()
        plan = portfolio.exit_proposals(D3)
        (again,) = plan.proposals
        assert plan.waiting == ()
        assert (again.reason, again.isin, again.quantity, again.since) == ("stop", isin, 100, D1)
        assert again.reproposed is True and again.batch_id != first.batch_id and again.decided_on == D2
        assert policy.run_state(store).open_pauses == ()
    finally:
        rig.close()


def test_exit_proposals_refuse_marks_that_are_missing_or_not_before_the_decide(tmp_path):
    world = at_peak(tmp_path)
    world.latch.load()  # pilot start, nothing evaluated
    assert world.portfolio.exit_proposals(D1).proposals == ()  # no open exit, nothing to refuse
    world.mark(D1, AAA="425")
    for decide in (D1, date(2026, 10, 8)):
        with pytest.raises(PortfolioError) as caught:
            world.portfolio.exit_proposals(decide)
        assert caught.value.code == "marks_not_before_decide"


def test_an_exit_for_a_name_that_is_gone_from_the_account_is_closed_by_that_fact(tmp_path):
    world = at_peak(tmp_path)
    world.mark(D1, AAA="400")
    world.account.sell("AAA", 100, "400")  # the booking landed, the latch call did not
    assert world.portfolio.exit_proposals(D2).proposals == ()
    assert world.latch.load().stops == {} and world.latch.load().open_exits == {}
    assert world.records("latch")[-1]["reason"] == "cleared_by_fill"


# --- no automatic release, a peak that never rebases ------------------------------------------------------------


def test_recovery_releases_nothing_by_itself_and_only_the_latch_backed_release_clears_the_halt(tmp_path):
    world = at_peak(tmp_path)
    world.mark(D1, AAA="460")
    for day, close in ((D2, "520"), (D3, "700")):
        world.mark(day, AAA=close)
        assert world.latch.load().halt
        assert [p.event for p in policy.run_state(world.store).open_pauses] == ["drawdown_halt"]
        assert world.portfolio.entry_block_reasons() == ("halt_latch", "run_paused")
    # The generic resume still refuses the drawdown pause.
    with pytest.raises(policy.PolicyError) as generic:
        policy.resume(world.store, "equity is back")
    assert generic.value.code == "drawdown_resume_requires_latch"
    assert world.latch.load().halt
    # Equity is far above the peak now (drawdown 0): the 63 reset is allowed, and so is the release.
    world.portfolio.release_halt("operator", "reviewed the recovery")
    assert not world.latch.load().halt
    assert policy.run_state(world.store).open_pauses == ()
    assert world.portfolio.entry_block_reasons() == ()
    assert world.records("resume")[-1]["reason"] == "reviewed the recovery"
    assert [r["reason"] for r in world.records("latch")] == ["raised_2026-10-09", "released_by_operator"]


def test_release_is_refused_while_drawdown_is_at_or_below_the_halt_line_unless_the_loss_is_accepted(tmp_path):
    world = at_peak(tmp_path)
    world.mark(D1, AAA="460")
    before = world.store.records()
    with pytest.raises(PortfolioError) as refused:
        world.portfolio.release_halt("operator", "I looked at it")
    assert refused.value.code == "halt_release_refused"
    assert world.latch.load().halt and world.store.records() == before
    with pytest.raises(policy.PolicyError) as no_reason:
        world.portfolio.release_halt("operator", "  ")
    assert no_reason.value.code == "reason_required"
    peak_before = world.latch.load().peak
    world.portfolio.release_halt("operator", "accept the loss", rebase_halt_anchor=True)
    state = world.latch.load()
    assert not state.halt and state.halt_anchor == Decimal("46000") and state.peak == peak_before == CAP
    assert world.portfolio.entry_block_reasons() == ()


def test_the_peak_never_rebases_and_the_recorded_drawdown_is_measured_from_it(tmp_path):
    world = at_peak(tmp_path)
    world.mark(D1, AAA="460")
    world.portfolio.release_halt("operator", "accept the loss", rebase_halt_anchor=True)
    world.mark(D2, AAA="465")
    assert world.latch.load().peak == CAP  # a rebased halt anchor never touches the true peak
    last = world.records("mark")[-1]
    assert (last["peak"], last["equity"], last["drawdown"]) == ("50000", "46500", "-0.070000")
    world.mark(D3, AAA="520")
    assert world.records("mark")[-1]["peak"] == "52000"
    world.mark(D4, AAA="450")
    assert world.records("mark")[-1]["peak"] == "52000"  # lower equity later: still the high-water mark


def test_62_releases_a_halt_on_recovery_and_the_shadow_portfolio_does_not_expected_divergence(tmp_path):
    days = weekday_sessions(date(2026, 10, 9), 6)
    book = Book(capital=CAP, limits=limits62(), params=params62(), fold="parity")
    book.cash, book.peak = Decimal(0), CAP
    book.positions[A] = BookPosition(
        anchor_isin=A, isin=A, stock_code=A, quantity=100, bought_qty=100, cost_total=Decimal(50000),
        buy_cost_total=Decimal(50000), entry_date=days[0], entry_index=0, entry_score=Decimal(1),
        last_price=Decimal(500),
    )
    book.mark(days[0], {A: Decimal("460")})
    book.update_risk(days[0], 1, regime_cash=False)
    assert book.halted
    book.mark(days[1], {A: Decimal("700")})
    book.update_risk(days[1], 2, regime_cash=False)
    assert book.halted is False, "62 (backtest) releases the halt on recovery"

    world = at_peak(tmp_path)
    world.mark(days[0], AAA="460")
    world.mark(days[1], AAA="700")
    assert world.latch.load().halt, "the shadow portfolio (D-05, D-08) releases only through release_halt"
    assert world.portfolio.buys_blocked()


# --- parity with the 63-02 drawdown vectors ------------------------------------------------------------------------


def _replay_vector(tmp_path: Path, case: dict) -> Fake:
    start = case.get("start")
    account = FakeAccount(cash=Decimal(start["cash"]) if start else CAP)
    world = Fake(tmp_path, symbols=("AAA", "BBB"), start_peak=start["peak"] if start else None, account=account)
    by_isin = {v: k for k, v in ISINS.items()}
    for fill in case["fills"]:
        assert fill["side"] == "buy"
        account.buy(by_isin[fill["isin"]], fill["quantity"], fill["price"], fill["charges"])
    sessions = case["sessions"] or [{"session": "2026-10-09", "closes": {}}]
    for item in sessions:
        world.portfolio.mark_session(
            date.fromisoformat(item["session"]), {k: Decimal(v) for k, v in item["closes"].items()}
        )
    return world


@pytest.mark.parametrize("case", VECTORS["drawdown_cases"], ids=[c["name"] for c in VECTORS["drawdown_cases"]])
def test_the_shadow_portfolio_matches_the_63_drawdown_vector_rows(tmp_path, case):
    world = _replay_vector(tmp_path, case)
    state = world.latch.load()
    assert state.halt is case["expected"]["halt"]
    assert state.ended is case["expected"]["ended"]
    assert sorted(state.stops) == sorted(case["expected"]["stops"])
    run_state = policy.run_state(world.store)
    assert run_state.ended is case["expected"]["ended"]
    halted = any(p.event == "drawdown_halt" for p in run_state.open_pauses)
    assert halted is case["expected"]["halt"]


def test_every_vector_row_is_replayed_and_the_vectors_reach_each_trigger():
    cases = VECTORS["drawdown_cases"]
    assert len(cases) == 12
    assert any(c["expected"]["halt"] for c in cases) and any(c["expected"]["ended"] for c in cases)
    assert any(c["expected"]["stops"] for c in cases)


# --- Q-6: a structural ex-date pauses that ISIN's marks and stops for the session ----------------------------------


def test_a_split_ex_date_does_not_fire_a_stop_or_the_drawdown_but_the_same_close_does_without_it(tmp_path):
    control = Fake(tmp_path / "control")
    control.account.buy("AAA", 100, "500")
    control.mark(D1, AAA="500")
    fired = control.mark(D2, AAA="250")  # raw close halves on a 1:2 split: looks like -50%
    assert A in fired.state.stops and fired.state.ended  # what would happen without the pause

    world = Fake(tmp_path / "world")
    world.account.buy("AAA", 100, "500")
    world.mark(D1, AAA="510")
    paused = world.portfolio.mark_session(D2, {A: Decimal("255")}, structural_actions={A: "split"})
    assert paused.state.stops == {} and not paused.state.halt and not paused.state.ended
    assert paused.paused_isins == (A,) and paused.equity == Decimal("51000")  # the last raw close, carried
    day2 = [r for r in world.records("position_mark") if r["as_of_date"] == D2.isoformat()]
    assert [(r["mark_basis"], r["close"], r["corporate_action"]) for r in day2] == [(MARK_CARRIED_EX_DATE, "510", "split")]
    day1 = [r for r in world.records("position_mark") if r["as_of_date"] == D1.isoformat()]
    assert [r["mark_basis"] for r in day1] == [MARK_RAW_CLOSE] and "corporate_action" not in day1[0]
    assert world.portfolio.entry_block_reasons() == ()


def test_a_bonus_on_a_never_marked_name_carries_its_cost_per_share(tmp_path):
    world = Fake(tmp_path)
    world.account.buy("AAA", 100, "500")
    result = world.portfolio.mark_session(D1, {}, structural_actions={A: "bonus"})
    assert result.equity == Decimal("50000") and result.state.stops == {}
    assert world.records("position_mark")[0]["close"] == "500"


def test_only_the_paused_name_is_exempt_and_other_names_are_still_marked_and_stopped(tmp_path):
    world = Fake(tmp_path)
    world.account.buy("AAA", 20, "500")
    world.account.buy("BBB", 20, "500")
    result = world.portfolio.mark_session(
        D1, {A: Decimal("250"), B: Decimal("400")}, structural_actions={A: "split"}
    )
    assert sorted(result.state.stops) == [B]


def test_a_structural_kind_the_portfolio_does_not_know_is_refused(tmp_path):
    world = at_peak(tmp_path)
    with pytest.raises(PortfolioError) as caught:
        world.portfolio.mark_session(D1, {A: Decimal("500")}, structural_actions={A: "dividend"})
    assert caught.value.code == "structural_action_unknown" and world.latch.load().last_session is None


def test_documented_residual_the_session_after_the_ex_date_marks_the_unadjusted_quantity_at_the_raw_close(tmp_path):
    """Provisional Q-6 pauses ONE session and makes no factor adjustment, so a split leaves the quantity
    unadjusted. This row pins that residual so a later quantity-adjust step has to change it on purpose."""
    world = at_peak(tmp_path)
    world.portfolio.mark_session(D1, {A: Decimal("250")}, structural_actions={A: "split"})
    after = world.mark(D2, AAA="250")
    assert A in after.state.stops


# --- the evidence mirrors the latch, and a restart changes nothing (T-64-16) ------------------------------------------


def test_a_latch_written_before_the_evidence_is_repaired_by_the_next_call(tmp_path):
    world = at_peak(tmp_path)
    # The crash window: the latch file took the evaluation, the evidence write never happened.
    world.latch.evaluate_session(
        {A: Decimal("460")}, D1, cash=Decimal(0), positions=[exits.Position(A, "AAA", 100, Decimal("50000"))]
    )
    assert policy.run_state(world.store).open_pauses == ()
    again = world.restart()
    again.mark(D2, AAA="460")
    assert [(p.event, p.scope) for p in policy.run_state(again.store).open_pauses] == [("drawdown_halt", "entries")]
    assert [r["event"] for r in again.records("pause")] == ["drawdown_halt"]


def test_the_repair_runs_before_anything_that_can_raise_so_a_refused_mark_still_leaves_the_pause(tmp_path):
    world = at_peak(tmp_path)
    world.latch.evaluate_session(
        {A: Decimal("460")}, D1, cash=Decimal(0), positions=[exits.Position(A, "AAA", 100, Decimal("50000"))]
    )
    again = world.restart()
    with pytest.raises(PortfolioError) as caught:
        again.portfolio.mark_session(D2, {})  # no close for the held name: refused before any evaluation
    assert caught.value.code == "mark_missing"
    assert {p.event for p in policy.run_state(again.store).open_pauses} == {"drawdown_halt", "missing_bar"}


def test_halt_stop_and_run_end_survive_a_restart_and_the_buy_block_stays(tmp_path):
    world = Fake(tmp_path)
    world.account.buy("AAA", 20, "500")
    world.mark(D1, AAA="440")
    again = world.restart()
    assert again.portfolio.entry_block_reasons() == ("stop_open",)
    assert sorted(again.latch.load().stops) == [A]
    again.mark(D2, AAA="600")  # recovery
    assert again.portfolio.entry_block_reasons() == ("stop_open",)


def test_a_second_mark_for_an_evaluated_session_changes_nothing(tmp_path):
    world = at_peak(tmp_path)
    world.mark(D1, AAA="460")
    before = world.store.records()
    again = world.mark(D1, AAA="300")
    assert again.evaluated is False and again.state.last_equity == Decimal("46000")
    assert world.store.records() == before
    with pytest.raises(PortfolioError) as older:
        world.mark(date(2026, 10, 8), AAA="500")
    assert older.value.code == "session_already_evaluated"


def test_an_abort_stops_marks_and_release(tmp_path):
    world = at_peak(tmp_path)
    world.mark(D1, AAA="460")
    policy.raise_event(world.store, "audit_corrupt", "chain break")
    with pytest.raises(policy.RunAborted):
        world.mark(D2, AAA="460")
    with pytest.raises(policy.PolicyError) as caught:
        world.portfolio.release_halt("operator", "reviewed")
    assert caught.value.code == "resume_refused_abort"
    assert "run_aborted" in world.portfolio.entry_block_reasons()


def test_an_unreadable_latch_blocks_buys_and_aborts_a_mark(tmp_path):
    world = at_peak(tmp_path)
    world.mark(D1, AAA="500")
    world.latch_path.write_text("{}")
    assert "latch_unreadable" in world.portfolio.entry_block_reasons()
    with pytest.raises(PortfolioAborted) as caught:
        world.mark(D2, AAA="500")
    assert caught.value.code == "state_corrupt"


# --- the adapter's figure is a separate, stamped field -------------------------------------------------------------------


def test_the_peak_and_drawdown_are_the_latch_high_water_mark_and_the_adapter_figure_carries_its_basis(tmp_path):
    rig = make_rig(tmp_path)
    store = make_evidence_store(tmp_path / "evidence")
    try:
        own(rig, "RELIANCE", 100)  # 10000 at cost
        portfolio = ShadowPortfolio(rig.ledger, rig.guard.store, store, instruments("RELIANCE"))
        isin = ISINS["RELIANCE"]
        portfolio.mark_session(D1, {isin: Decimal("130")})  # equity 53000
        adapter_state = rig.adapter.portfolio_state()
        assert adapter_state == {"equity": 50000.0, "peak_equity": 50000.0}  # at cost: it never saw the gain
        figure = Decimal(1) - Decimal(str(adapter_state["equity"])) / Decimal(str(adapter_state["peak_equity"]))
        portfolio.mark_session(D2, {isin: Decimal("120")}, simulator_drawdown_pct=figure)
        marks = [r for r in store.records() if r["kind"] == "mark"]
        assert (Decimal(marks[0]["peak"]), marks[0]["peak_source"]) == (Decimal("53000"), PEAK_SOURCE)
        assert "simulator_drawdown_pct" not in marks[0]
        second = marks[1]
        assert (Decimal(second["equity"]), Decimal(second["peak"]), second["drawdown"]) == (
            Decimal("52000"), Decimal("53000"), "-0.018868",
        )
        assert Decimal(second["peak"]) != Decimal(str(adapter_state["peak_equity"]))
        assert second["simulator_drawdown_pct"] == "0" and second["simulator_drawdown_basis"] == SIMULATOR_DRAWDOWN_BASIS
        assert SIMULATOR_DRAWDOWN_BASIS == "seed_relative_at_cost"
    finally:
        rig.close()


def test_an_adapter_figure_that_is_not_a_finite_decimal_is_refused(tmp_path):
    world = at_peak(tmp_path)
    for bad in (0.1, Decimal("NaN")):
        with pytest.raises(PortfolioError) as caught:
            world.portfolio.mark_session(D1, {A: Decimal("500")}, simulator_drawdown_pct=bad)  # type: ignore[arg-type]
        assert caught.value.code == "bad_simulator_figure"
    assert world.records("mark") == []


# --- a real reopen: the real gate still refuses from the files --------------------------------------------------------------


def test_after_a_reopen_the_real_gate_still_refuses_from_the_latch_file(tmp_path):
    rig = make_rig(tmp_path)
    store = make_evidence_store(tmp_path / "evidence")
    own(rig, "RELIANCE", 100)
    ShadowPortfolio(rig.ledger, rig.guard.store, store, instruments("RELIANCE", "TCS")).mark_session(
        D1, {ISINS["RELIANCE"]: Decimal("87")}
    )
    rig = reopen(rig)
    try:
        portfolio = ShadowPortfolio(
            rig.ledger, rig.guard.store, reopen_evidence(tmp_path / "evidence"), instruments("RELIANCE", "TCS")
        )
        assert portfolio.entry_block_reasons() == ("stop_open",)
        denied = run(rig.adapter.prepare(buy_intent(rig, "after-reopen", "TCS", 5), samples_for("TCS")))
        assert denied.reason_code == "stop_open"
        assert state_path_for(rig.ledger.path).exists()
    finally:
        rig.close()


# --- source rules -------------------------------------------------------------------------------------------------------------


def test_the_module_has_no_float_no_sql_and_no_clock():
    tree = ast.parse((ROOT / "backend/shadow_india/portfolio.py").read_text())
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "float" not in names
    attributes = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert not attributes & {"now", "utcnow", "today", "time", "monotonic", "sleep", "execute"}
    constants = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, float)]
    assert constants == []


# --- helpers for the real-ledger rows -----------------------------------------------------------------------------------------


def samples_for(symbol: str, price: str = "100.00") -> list[QuoteSample]:
    """Three distinct relay readings for ``symbol`` ending two seconds before ``NOW``, ask at ``price``."""
    out = []
    for index in range(3):
        step = Decimal(2 - index) * Decimal("0.01")
        fetched = NOW - timedelta(seconds=2 * (3 - index))
        ask, bid = Decimal(price) - step, Decimal(price) - Decimal("0.10") - step
        out.append(
            QuoteSample(
                sha(f"{symbol}-raw-{index}"), fetched, bid, ask,
                make_evidence(
                    observed_at=fetched, ask=ask, bid=bid, isin=ISINS[symbol], stock_code=symbol,
                ),
            )
        )
    return out


def buy_intent(rig: Rig, proposal_id: str, symbol: str, quantity: int, price: str = "100.00"):
    return rig.adapter.build_intent(proposal_id, symbol=symbol, side="BUY", quantity=quantity, limit_price=price)


def sell_intent(rig: Rig, proposal_id: str, symbol: str, quantity: int, price: str = "100.00"):
    return rig.adapter.build_intent(proposal_id, symbol=symbol, side="SELL", quantity=quantity, limit_price=price)


def book(rig: Rig, proposal_id: str, quantity: int, price: str, charges: str = "0", attempt: str = "attempt-1"):
    scenario = sha(f"scenario-{proposal_id}-{attempt}")
    return rig.ledger.book_simulated_fill(
        simulated_booking_id(attempt, scenario), proposal_id, quantity, price, charges, SCHEDULE, scenario, attempt
    )


def own(rig: Rig, symbol: str, quantity: int, price: str = "100.00") -> None:
    proposal_id = f"buy-{symbol}"
    admission = run(rig.adapter.prepare(buy_intent(rig, proposal_id, symbol, quantity, price), samples_for(symbol, price)))
    assert admission.decision is AdmissionDecision.ADMITTED, admission.reason_code
    book(rig, proposal_id, quantity, price)


def sell_and_fill(rig: Rig, proposal_id: str, symbol: str, quantity: int, price: str = "100.00") -> None:
    admission = run(rig.adapter.prepare(sell_intent(rig, proposal_id, symbol, quantity, price), samples_for(symbol, price)))
    assert admission.decision is AdmissionDecision.ADMITTED, admission.reason_code
    book(rig, proposal_id, quantity, price, attempt="attempt-exit")
