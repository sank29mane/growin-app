"""Phase 64-03 Task 2 part A: the shadow portfolio with Option B (D-05, D-08, D-15, Q-6).

Two kinds of rows. Rows on a ``FakeAccount`` drive the portfolio over exact synthetic numbers and the shared 63-02
drawdown vectors; rows on a real simulation ledger (``make_rig``) prove the integration: the cash the ledger holds,
the real admission guard refusing a buy while a latch is open, a real expiry of an INCONCLUSIVE exit, and a real
reopen. Every value is synthetic. Nothing here reaches a broker, the relay, the VM or ``private/``.
"""

from __future__ import annotations

import ast
import json
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
from shadow_india import portfolio as portfolio_module
from shadow_india.admission import SIMULATOR_DRAWDOWN_BASIS, QuoteSample
from shadow_india.evidence import EvidenceStore
from shadow_india.portfolio import (
    MARK_CARRIED_EX_DATE,
    MARK_RAW_CLOSE,
    PEAK_SOURCE,
    ExitPlan,
    Holding,
    Instrument,
    PortfolioAborted,
    PortfolioError,
    ShadowPortfolio,
    exit_intent_id,
    exit_obligation_id,
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
        self.bookings: list[SimpleNamespace] = []
        self.adjustments: list[SimpleNamespace] = []

    def _book(self, side: str, symbol: str, quantity: int, proposal_id: str, price: str, charges: str) -> None:
        self.bookings.append(
            SimpleNamespace(
                booking_id=f"booking-{len(self.bookings) + 1}", proposal_id=proposal_id, side=side,
                ticker=f"NSE:CASH:{symbol}", delta_quantity=Decimal(quantity), price=Decimal(price),
                charges=Decimal(charges),
            )
        )

    def buy(self, symbol: str, quantity: int, price: str, charges: str = "0") -> None:
        held_q, held_c = self.held.get(symbol, (0, Decimal(0)))
        cost = quantity * Decimal(price)
        self.held[symbol] = (held_q + quantity, held_c + cost)
        self.cash -= cost + Decimal(charges)
        self._book("BUY", symbol, quantity, f"buy-{len(self.bookings) + 1}", price, charges)

    def sell(self, symbol: str, quantity: int, price: str, charges: str = "0", *, proposal_id: str | None = None) -> None:
        """A booked sell. ``proposal_id`` is the exit intent id it was booked under; none means a sell that is not
        any exit's."""
        held_q, held_c = self.held[symbol]
        left = held_q - quantity
        self.held[symbol] = (left, held_c * left / held_q if held_q else Decimal(0))
        self.cash += quantity * Decimal(price) - Decimal(charges)
        self._book("SELL", symbol, quantity, proposal_id or f"manual-{len(self.bookings) + 1}", price, charges)

    def adjust(self, symbol: str, quantity: int, kind: str = "bonus", ex_date: date | None = None) -> None:
        """A split or bonus booked: the quantity changes, the cost basis does not, and an explicit adjustment record
        is exposed (the real ledger has no such record). ``ex_date`` defaults to ``D2``, the ex-date the rows use."""
        self.held[symbol] = (quantity, self.held[symbol][1])
        self.adjustments.append(SimpleNamespace(ticker=f"NSE:CASH:{symbol}", kind=kind, ex_date=ex_date or D2))

    def corporate_action_adjustments(self) -> list[SimpleNamespace]:
        return list(self.adjustments)

    def simulated_bookings(self, proposal_id: str | None = None) -> list[SimpleNamespace]:
        return [b for b in self.bookings if proposal_id is None or b.proposal_id == proposal_id]

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
        """The evidence lines of ``kind``. The ``sell_baseline`` and ``booking_baseline`` latch lines are bookkeeping, read through ``store``."""
        return [r for r in self.store.records() if r["kind"] == kind and r.get("latch") not in ("sell_baseline", "booking_baseline")]

    def exit_id(self, isin: str, attempt: int = 1) -> str:
        """The intent id a runner builds for the open exit on ``isin`` (read from the latch, as a runner would)."""
        current = self.latch.load().open_exits[isin]
        return exit_intent_id(exit_obligation_id(current.reason, isin, current.since), attempt)

    def sell_for_exit(self, symbol: str, quantity: int, price: str, attempt: int = 1) -> None:
        self.account.sell(symbol, quantity, price, proposal_id=self.exit_id(ISINS[symbol], attempt))


def latch_evaluate(portfolio: ShadowPortfolio, latch: LatchStore, *args: Any, **kwargs: Any):
    """An evaluation as the portfolio runs it for a halve: the sell booking boundary is persisted, then the latch
    moves. Rows that write the latch with no boundary (an outside writer) call ``latch.evaluate_session`` directly."""
    session = args[1]
    portfolio._record_sell_boundaries(session, portfolio.snapshot())
    return latch.evaluate_session(*args, **kwargs)


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


def test_a_split_ex_date_stop_is_suppressed_even_when_a_tighter_vol_stop_would_fire_at_the_carried_close(tmp_path):
    """Review r1 P2: buy 500, mark 470, split ex-date with a -5% vol stop. The control proves the stop would fire."""
    control = Fake(tmp_path / "control")
    control.account.buy("AAA", 100, "500")
    control.mark(D1, AAA="470")
    fired = control.portfolio.mark_session(D2, {A: Decimal("470")}, vol_stops={A: Decimal("-0.05")})
    assert sorted(fired.state.stops) == [A]  # 470 x 100 <= 500 x 100 x 0.95

    world = Fake(tmp_path / "world")
    world.account.buy("AAA", 100, "500")
    world.mark(D1, AAA="470")
    held = world.portfolio.mark_session(
        D2, {A: Decimal("235")}, structural_actions={A: "split"}, vol_stops={A: Decimal("-0.05")}
    )
    assert held.state.stops == {} and held.state.open_exits == {} and held.batches == ()
    assert held.equity == Decimal("47000")  # the carried 470, not the raw 235


def test_the_carried_name_cannot_stop_on_a_cost_above_its_carried_value_either(tmp_path):
    """A buy that booked after the last mark can leave cost far above the carried close; still no stop."""
    world = Fake(tmp_path)
    world.account.buy("AAA", 100, "500")
    world.mark(D1, AAA="500")
    world.account.buy("AAA", 100, "700")  # cost 120000, carried close 500: far below the -12% line
    held = world.portfolio.mark_session(D2, {A: Decimal("250")}, structural_actions={A: "split"})
    assert held.state.stops == {}


def test_an_ex_date_on_a_held_name_raises_the_scoped_corporate_action_pause_once_and_blocks_its_buys(tmp_path):
    world = Fake(tmp_path)
    world.account.buy("AAA", 100, "500")
    world.mark(D1, AAA="500")
    world.portfolio.mark_session(D2, {A: Decimal("250")}, structural_actions={A: "bonus"})
    world.portfolio.mark_session(D3, {A: Decimal("250")}, structural_actions={A: "bonus"})  # same pause, no second line
    assert [(p.event, p.scope) for p in policy.run_state(world.store).open_pauses] == [
        ("unresolved_corporate_action", f"isin:{A}")
    ]
    assert len(world.records("pause")) == 1
    assert world.portfolio.entry_block_reasons() == ()  # another name is not blocked
    assert world.portfolio.entry_block_reasons(A) == ("instrument_paused",)
    run_state = policy.run_state(world.store)
    for step in (policy.STEP_ADMIT, policy.STEP_MINT):
        assert run_state.blocks(step, isin=A) and not run_state.blocks(step, isin=A, side=policy.SIDE_SELL)
        assert not run_state.blocks(step, isin=B)


def test_a_structural_action_on_a_name_that_is_not_held_raises_no_pause(tmp_path):
    world = Fake(tmp_path)
    world.account.buy("AAA", 100, "500")
    world.portfolio.mark_session(D1, {A: Decimal("500")}, structural_actions={B: "split"})
    assert policy.run_state(world.store).open_pauses == ()


def test_a_one_to_one_bonus_over_two_sessions_fires_no_false_stop_and_no_false_minus_15(tmp_path):
    """Review r1 Q-6 residual. The raw close halves on the ex-date and stays halved while the ledger still
    holds the unadjusted quantity; without the pause the second session looks like -50% (the next row is the control)."""
    world = Fake(tmp_path)
    world.account.buy("AAA", 100, "500")
    world.mark(D1, AAA="500")
    ex = world.portfolio.mark_session(D2, {A: Decimal("250")}, structural_actions={A: "bonus"})
    after = world.mark(D3, AAA="250")  # no structural flag this session: the open pause keeps the carry
    for result in (ex, after):
        assert result.state.stops == {} and not result.state.halt and not result.state.ended
        assert result.equity == Decimal("50000") and result.drawdown == Decimal(0)
        assert result.paused_isins == (A,) and result.batches == ()
    day3 = [r for r in world.records("position_mark") if r["as_of_date"] == D3.isoformat()]
    assert [(r["mark_basis"], r["close"]) for r in day3] == [(MARK_CARRIED_EX_DATE, "500")]
    assert "corporate_action" not in day3[0]
    assert policy.run_state(world.store).ended is False
    assert world.portfolio.entry_block_reasons(A) == ("instrument_paused",)


def test_without_the_pause_the_session_after_the_ex_date_would_read_minus_50_percent(tmp_path):
    """The control for the row above: a name that is not carried marks the unadjusted quantity at the raw close."""
    world = Fake(tmp_path)
    world.account.buy("AAA", 100, "500")
    world.mark(D1, AAA="500")
    fired = world.mark(D2, AAA="250")
    assert A in fired.state.stops and fired.state.ended


def test_only_an_operator_release_with_a_reason_after_the_adjustment_releases_the_carry_and_the_buy_block(tmp_path):
    world = Fake(tmp_path)
    world.account.buy("AAA", 100, "500")
    world.mark(D1, AAA="500")
    world.portfolio.mark_session(D2, {A: Decimal("250")}, structural_actions={A: "bonus"})
    world.mark(D3, AAA="250")  # time and marks release nothing
    assert world.portfolio.entry_block_reasons(A) == ("instrument_paused",)
    with pytest.raises(policy.PolicyError) as blank:
        world.portfolio.release_corporate_action(A, "   ")
    assert blank.value.code == "reason_required"
    assert world.portfolio.entry_block_reasons(A) == ("instrument_paused",)
    world.account.adjust("AAA", 200)  # the operator booked the bonus shares
    world.portfolio.release_corporate_action(A, "bonus quantity reconciled with the ledger")
    assert world.portfolio.entry_block_reasons(A) == ()
    released = world.mark(D4, AAA="250")
    assert released.paused_isins == () and released.state.stops == {} and released.equity == Decimal("50000")
    day4 = [r for r in world.records("position_mark") if r["as_of_date"] == D4.isoformat()]
    assert [(r["mark_basis"], r["close"], r["quantity"]) for r in day4] == [(MARK_RAW_CLOSE, "250", "200")]


def flatten_world(tmp_path: Path, *, structural: bool) -> tuple[Fake, Any]:
    """AAA 10 and BBB 80 bought at 500; D2 marks BBB at 400, which is -16% of the peak and ends the run."""
    world = Fake(tmp_path)
    world.account.buy("AAA", 10, "500")
    world.account.buy("BBB", 80, "500")
    world.mark(D1, AAA="500", BBB="500")
    actions = {A: "bonus"} if structural else None
    result = world.portfolio.mark_session(D2, {A: Decimal("250"), B: Decimal("400")}, structural_actions=actions)
    assert result.state.ended and sorted(result.state.open_exits) == [A, B]
    return world, result


def test_a_carried_name_is_flattened_with_the_rest_once_the_operator_releases_it(tmp_path):
    """Review r2 gap: the flatten of a carried name would sell the unadjusted quantity at the adjusted quote."""
    control, _ = flatten_world(tmp_path / "control", structural=False)
    plan = control.portfolio.exit_proposals(D3)
    assert sorted((p.isin, p.reason) for p in plan.proposals) == [(A, "flatten"), (B, "flatten")]  # fires when unpaused

    world, _ = flatten_world(tmp_path / "world", structural=True)
    held = world.portfolio.exit_proposals(D3)
    assert [(p.isin, p.reason) for p in held.proposals] == [(B, "flatten")] and held.held_out == (A,)
    assert sorted(world.latch.load().open_exits) == [A, B]  # the obligation is kept, only the proposal waits
    world.account.adjust("AAA", 20)
    world.portfolio.release_corporate_action(A, "bonus booked on the ledger, flatten both")
    released = world.portfolio.exit_proposals(D3)
    assert sorted((p.isin, p.reason, p.quantity) for p in released.proposals) == [
        (A, "flatten", 20), (B, "flatten", 80),
    ] and released.held_out == ()


# --- the evidence mirrors the latch, and a restart changes nothing (T-64-16) ------------------------------------------


def test_a_latch_written_before_the_evidence_is_repaired_by_the_next_call(tmp_path):
    world = at_peak(tmp_path)
    # The crash window: the latch file took the evaluation, the evidence write never happened.
    latch_evaluate(world.portfolio, world.latch,
        {A: Decimal("460")}, D1, cash=Decimal(0), positions=[exits.Position(A, "AAA", 100, Decimal("50000"))]
    )
    assert policy.run_state(world.store).open_pauses == ()
    again = world.restart()
    again.mark(D2, AAA="460")
    assert [(p.event, p.scope) for p in policy.run_state(again.store).open_pauses] == [("drawdown_halt", "entries")]
    assert [r["event"] for r in again.records("pause")] == ["drawdown_halt"]


def test_the_repair_runs_before_anything_that_can_raise_so_a_refused_mark_still_leaves_the_pause(tmp_path):
    world = at_peak(tmp_path)
    latch_evaluate(world.portfolio, world.latch,
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


# --- the terminal flatten covers every holding (review r1 P1) ----------------------------------------------------------------


def test_a_name_that_books_after_the_run_end_is_owed_a_flatten_every_decide_until_it_is_sold(tmp_path):
    world = Fake(tmp_path)
    world.account.buy("AAA", 100, "500")
    ended = world.mark(D1, AAA="425")
    assert ended.state.ended and sorted(ended.state.open_exits) == [A]
    world.account.buy("BBB", 10, "100")  # an earlier admitted buy books after the -15% close
    again = world.mark(D2, AAA="425", BBB="100")
    assert again.state.ended and B not in again.state.open_exits  # the latch cannot hold it: nothing to build
    plan = world.portfolio.exit_proposals(D3)
    assert sorted((p.isin, p.reason, p.quantity, p.since) for p in plan.proposals) == [
        (A, "flatten", 100, D1), (B, "flatten", 10, D2),
    ]
    assert [(r["latch"], r["scope"], r["reason"]) for r in world.records("latch") if r["latch"] == "flatten"] == [
        ("flatten", f"isin:{B}", "due_2026-10-12")
    ]
    world.account.sell("AAA", 100, "425")  # the first flatten books
    world.portfolio.on_booked_sell(A, 100)
    still = world.portfolio.exit_proposals(D3)
    assert [(p.isin, p.reason, p.quantity) for p in still.proposals] == [(B, "flatten", 10)]
    assert not world.portfolio.is_flat()
    restarted = world.restart()  # a derived obligation: a restart changes nothing
    restarted.mark(D3, BBB="100")
    (kept,) = restarted.portfolio.exit_proposals(D4).proposals
    assert (kept.isin, kept.reason, kept.quantity, kept.since, kept.reproposed) == (B, "flatten", 10, D2, True)
    restarted.account.sell("BBB", 10, "100")
    restarted.portfolio.on_booked_sell(B, 10)
    assert restarted.portfolio.exit_proposals(D4).proposals == () and restarted.portfolio.is_flat()
    assert len([r for r in restarted.records("latch") if r["latch"] == "flatten"]) == 1  # recorded once


def test_a_name_that_books_between_the_end_mark_and_the_decide_is_owed_a_flatten_too(tmp_path):
    world = Fake(tmp_path)
    world.account.buy("AAA", 100, "500")
    world.mark(D1, AAA="425")
    world.account.buy("BBB", 7, "100")  # no mark in between: the decide itself reconciles
    plan = world.portfolio.exit_proposals(D2)
    assert sorted((p.isin, p.reason, p.quantity) for p in plan.proposals) == [(A, "flatten", 100), (B, "flatten", 7)]


def test_a_weaker_exit_on_a_name_held_after_the_run_end_is_upgraded_to_a_flatten(tmp_path):
    world = Fake(tmp_path)
    world.account.buy("AAA", 100, "500")
    world.mark(D1, AAA="425")
    world.account.buy("BBB", 20, "100")
    world.mark(D2, AAA="425", BBB="80")  # BBB closes 20% under cost: the stop latch raises a STOP exit
    assert world.latch.load().open_exits[B].reason == "stop"
    plan = world.portfolio.exit_proposals(D3)
    assert sorted((p.isin, p.reason, p.quantity) for p in plan.proposals) == [(A, "flatten", 100), (B, "flatten", 20)]


def test_no_flatten_is_owed_before_the_run_ends_and_none_for_a_name_that_is_gone(tmp_path):
    world = Fake(tmp_path)
    world.account.buy("AAA", 100, "500")
    world.mark(D1, AAA="500")
    assert world.portfolio.exit_proposals(D2).proposals == ()
    assert world.records("latch") == []
    world.mark(D2, AAA="425")
    world.account.sell("AAA", 100, "425")
    assert world.portfolio.exit_proposals(D3).proposals == () and world.portfolio.is_flat()


# --- what is due comes from durable state, never from a notification (review r1 P1) ---------------------------------------------


def halved_world(tmp_path: Path) -> Fake:
    """100 AAA bought at 500; a -8% close halts and queues a halve of 50."""
    world = Fake(tmp_path)
    world.account.buy("AAA", 100, "500")
    result = world.mark(D1, AAA="460")
    assert result.state.halt and result.state.open_exits[A].quantity == 50
    return world


def test_a_booking_whose_hook_never_ran_is_netted_off_by_the_next_decide(tmp_path):
    world = halved_world(tmp_path)
    world.sell_for_exit("AAA", 20, "460")  # booked, then the process died before on_booked_sell
    again = world.restart()
    (proposal,) = again.portfolio.exit_proposals(D2).proposals
    assert (proposal.reason, proposal.quantity) == ("halve", 30)  # was 50: the 20 shares would be sold twice
    assert again.latch.load().open_exits[A].quantity == 30


def test_the_hook_arriving_late_or_twice_applies_the_booking_once(tmp_path):
    world = halved_world(tmp_path)
    world.sell_for_exit("AAA", 20, "460")
    for _ in range(3):
        world.portfolio.on_booked_sell(A, 20)
        assert world.latch.load().open_exits[A].quantity == 30  # was 10 after the second call
    (proposal,) = world.portfolio.exit_proposals(D2).proposals
    assert proposal.quantity == 30
    again = world.restart()
    again.portfolio.on_booked_sell(A, 20)
    assert again.latch.load().open_exits[A].quantity == 30


def test_a_hook_that_claims_more_than_was_sold_cannot_drop_a_required_exit(tmp_path):
    world = halved_world(tmp_path)
    world.sell_for_exit("AAA", 20, "460")
    world.portfolio.on_booked_sell(A, 50)  # the claim is not trusted: the holdings say 20
    assert world.latch.load().open_exits[A].quantity == 30
    (proposal,) = world.portfolio.exit_proposals(D2).proposals
    assert proposal.quantity == 30


def test_a_halve_that_booked_in_full_without_a_hook_is_closed_by_the_holdings(tmp_path):
    world = halved_world(tmp_path)
    world.sell_for_exit("AAA", 50, "460")
    assert world.portfolio.exit_proposals(D2).proposals == ()
    assert world.latch.load().open_exits == {} and world.latch.load().halt  # the exit is done, the halt stays


def test_a_partial_halve_booking_across_two_restarts_never_oversells(tmp_path):
    world = halved_world(tmp_path)
    world.sell_for_exit("AAA", 20, "460")
    second = world.restart()
    assert [p.quantity for p in second.portfolio.exit_proposals(D2).proposals] == [30]
    second.sell_for_exit("AAA", 25, "460", attempt=2)  # the next booking, again with no hook
    third = second.restart()
    assert [p.quantity for p in third.portfolio.exit_proposals(D2).proposals] == [5]
    third.sell_for_exit("AAA", 5, "460", attempt=3)
    assert third.restart().portfolio.exit_proposals(D2).proposals == ()
    assert third.portfolio.snapshot().holdings[0].quantity == 50  # exactly floor(100 / 2) went


def test_a_later_buy_does_not_grow_an_old_halve(tmp_path):
    world = halved_world(tmp_path)
    world.account.buy("AAA", 30, "460")  # an earlier admitted buy books after the halt
    (proposal,) = world.portfolio.exit_proposals(D2).proposals
    assert proposal.quantity == 50


def test_a_stop_exit_booked_in_part_without_a_hook_is_resynced_to_what_is_held(tmp_path):
    world = Fake(tmp_path)
    world.account.buy("AAA", 20, "500")
    world.mark(D1, AAA="400")
    world.account.sell("AAA", 8, "400")  # no hook
    (proposal,) = world.restart().portfolio.exit_proposals(D2).proposals
    assert (proposal.reason, proposal.quantity) == ("stop", 12)
    assert world.latch.load().open_exits[A].quantity == 12 and sorted(world.latch.load().stops) == [A]
    world.account.sell("AAA", 12, "400")
    world.portfolio.on_booked_sell(A, 12)
    world.portfolio.on_booked_sell(A, 12)
    assert world.latch.load().stops == {} and world.latch.load().open_exits == {}


@pytest.mark.parametrize("bad", [-1, True, 1.5, "20", None])
def test_a_sold_quantity_that_is_not_a_whole_number_is_refused_and_touches_nothing(tmp_path, bad):
    world = halved_world(tmp_path)
    world.account.sell("AAA", 20, "460")
    before = world.latch_path.read_bytes()
    with pytest.raises(PortfolioError) as caught:
        world.portfolio.on_booked_sell(A, bad)
    assert caught.value.code == "bad_fill_quantity" and world.latch_path.read_bytes() == before
    with pytest.raises(PortfolioError) as unknown:
        world.portfolio.on_booked_sell(CLEAR_ISIN, 1)
    assert unknown.value.code == "unknown_instrument"


def test_a_halve_the_evidence_never_recorded_falls_back_to_the_latch_quantity_capped_at_what_is_held(tmp_path):
    """Only a latch written outside the portfolio (no position mark for the raising session) takes this path:
    the target cannot be derived, so the exit is never larger than the latch says nor than the holdings."""
    world = at_peak(tmp_path)
    latch_evaluate(world.portfolio, world.latch,
        {A: Decimal("460")}, D1, cash=Decimal(0), positions=[exits.Position(A, "AAA", 100, Decimal("50000"))]
    )
    assert world.records("position_mark") == [] and world.latch.load().open_exits[A].quantity == 50
    assert [p.quantity for p in world.portfolio.exit_proposals(D2).proposals] == [50]
    world.account.adjust("AAA", 30)  # a reverse split booked: 30 held
    assert [p.quantity for p in world.portfolio.exit_proposals(D2).proposals] == [30]  # never more than held


# --- the evaluation inputs are durable before the latch moves (review r1 P2) -------------------------------------------------------------


class Died(Exception):
    """A simulated process death."""


def die_after_latch_write(world: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    real = world.latch.evaluate_session

    def evaluate_then_die(*args: Any, **kwargs: Any):
        real(*args, **kwargs)
        raise Died

    monkeypatch.setattr(world.latch, "evaluate_session", evaluate_then_die)


def test_a_crash_between_the_latch_write_and_the_evidence_write_keeps_the_raw_close_for_the_carry(tmp_path, monkeypatch):
    """Review r1 P2. Without the fix the lost close made the next carry read cost (500), so equity fell from
    60000 to 50000 and the run saw -16.67% and ended."""
    world = Fake(tmp_path)
    world.account.buy("AAA", 100, "500")
    die_after_latch_write(world, monkeypatch)
    with pytest.raises(Died):
        world.mark(D1, AAA="600")
    assert world.latch.load().last_session == D1 and world.latch.load().peak == Decimal("60000")
    assert [(r["close"], r["mark_basis"]) for r in world.records("position_mark")] == [("600", MARK_RAW_CLOSE)]
    again = world.restart()
    result = again.portfolio.mark_session(D2, {A: Decimal("300")}, structural_actions={A: "split"})
    assert result.equity == Decimal("60000") and result.drawdown == Decimal(0)
    assert not result.state.ended and not result.state.halt and result.state.stops == {}
    carried = [r for r in again.records("position_mark") if r["as_of_date"] == D2.isoformat()]
    assert [(r["close"], r["mark_basis"]) for r in carried] == [("600", MARK_CARRIED_EX_DATE)]
    assert policy.run_state(again.store).ended is False


def test_the_mark_line_the_crash_lost_is_written_by_the_retry_and_nothing_is_written_twice(tmp_path, monkeypatch):
    world = Fake(tmp_path)
    world.account.buy("AAA", 100, "500")
    die_after_latch_write(world, monkeypatch)
    with pytest.raises(Died):
        world.mark(D1, AAA="600")
    assert world.records("mark") == []
    again = world.restart()
    retried = again.mark(D1, AAA="600")
    assert retried.evaluated is False and retried.equity == Decimal("60000")
    assert len(again.records("position_mark")) == 1 and [m["equity"] for m in again.records("mark")] == ["60000"]


def test_a_crash_before_the_latch_write_leaves_the_inputs_and_the_retry_completes_it(tmp_path, monkeypatch):
    world = Fake(tmp_path)
    world.account.buy("AAA", 100, "500")

    def die_first(*args: Any, **kwargs: Any):
        raise Died

    monkeypatch.setattr(world.latch, "evaluate_session", die_first)
    with pytest.raises(Died):
        world.mark(D1, AAA="600")
    assert world.latch.load().last_session is None and len(world.records("position_mark")) == 1
    again = world.restart()
    result = again.mark(D1, AAA="600")
    assert result.evaluated is True and result.equity == Decimal("60000")
    assert len(again.records("position_mark")) == 1  # the same inputs are not written twice


def test_a_retry_with_different_inputs_supersedes_the_line_and_the_latest_one_is_what_the_carry_reads(tmp_path, monkeypatch):
    world = Fake(tmp_path)
    world.account.buy("AAA", 100, "500")

    def die_first(*args: Any, **kwargs: Any):
        raise Died

    monkeypatch.setattr(world.latch, "evaluate_session", die_first)
    with pytest.raises(Died):
        world.mark(D1, AAA="600")
    again = world.restart()
    again.mark(D1, AAA="610")  # the corrected close reaches the latch
    assert [r["close"] for r in again.records("position_mark")] == ["600", "610"]
    carried = again.portfolio.mark_session(D2, {A: Decimal("305")}, structural_actions={A: "split"})
    assert carried.equity == Decimal("61000")


# --- source rules for the anchors (review r1 gap 2) -------------------------------------------------------------------------------------


@pytest.mark.parametrize("isin", [ISINS["RELIANCE"] + "\n", ISINS["RELIANCE"] + "\r\n", "\n" + ISINS["RELIANCE"], "ine002a01018"])
def test_an_instrument_isin_with_a_trailing_newline_or_lower_case_is_refused(isin):
    with pytest.raises(PortfolioError) as caught:
        Instrument(isin, "RELIANCE", "RELIANCE")
    assert caught.value.code == "bad_instrument"


@pytest.mark.parametrize("symbol", ["RELIANCE\n", "\nRELIANCE", "reliance", "RELIANCE ", ""])
def test_an_instrument_symbol_with_a_trailing_newline_is_refused(symbol):
    with pytest.raises(PortfolioError) as caught:
        Instrument(ISINS["RELIANCE"], symbol, "RELIANCE")
    assert caught.value.code == "bad_instrument"


@pytest.mark.parametrize("isin", [A + "\n", "\n" + A, A.lower(), "short"])
def test_a_structural_action_isin_with_a_trailing_newline_is_refused_before_anything_is_written(tmp_path, isin):
    world = at_peak(tmp_path)
    before = world.store.records()
    with pytest.raises(PortfolioError) as caught:
        world.portfolio.mark_session(D1, {A: Decimal("500")}, structural_actions={isin: "split"})
    assert caught.value.code == "structural_action_unknown"
    assert world.latch.load().last_session is None and world.store.records() == before


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


# --- review r2 P1: a halve is reconciled against its own booked sells, never the aggregate holding -----------------------------------------


def interleaved_world(tmp_path: Path) -> Fake:
    """100 AAA; a -8% close queues a halve of 50; 20 are sold under the exit's intent id; an earlier admitted buy of 20
    books too. The aggregate holding is back at 100, so any rule read from holdings sees nothing sold."""
    world = halved_world(tmp_path)
    world.sell_for_exit("AAA", 20, "460")
    world.account.buy("AAA", 20, "460")
    assert world.portfolio.snapshot().holdings[0].quantity == 100
    return world


def test_a_booked_sell_and_a_later_buy_leave_exactly_30_owed_after_restart_and_duplicate_hooks(tmp_path):
    world = interleaved_world(tmp_path)
    again = world.restart()
    for quantity in (50, 20, 0, 20):  # duplicate, stale and out-of-order claims
        again.portfolio.on_booked_sell(A, quantity)
    (proposal,) = again.portfolio.exit_proposals(D2).proposals
    assert (proposal.reason, proposal.quantity) == ("halve", 30)  # was 50: 20 shares would be sold twice
    assert again.latch.load().open_exits[A].quantity == 30
    assert again.restart().portfolio.exit_proposals(D2).proposals[0].quantity == 30


def test_hooks_before_the_booking_and_after_it_in_any_order_change_nothing(tmp_path):
    world = halved_world(tmp_path)
    world.portfolio.on_booked_sell(A, 20)  # announced before anything booked
    assert world.latch.load().open_exits[A].quantity == 50
    world.sell_for_exit("AAA", 20, "460")
    world.account.buy("AAA", 20, "460")
    world.restart().portfolio.on_booked_sell(A, 20)
    world.portfolio.on_booked_sell(A, 20)
    assert world.portfolio.exit_proposals(D2).proposals[0].quantity == 30
    world.sell_for_exit("AAA", 30, "460", attempt=2)  # the retry order under the same obligation completes it
    world.portfolio.on_booked_sell(A, 30)
    assert world.portfolio.exit_proposals(D2).proposals == () and A not in world.latch.load().open_exits


def test_the_halve_never_exceeds_what_is_held_even_when_it_is_owed_more(tmp_path):
    world = halved_world(tmp_path)
    world.account.adjust("AAA", 30)  # a reverse split booked: 30 held, 50 still owed
    assert world.portfolio.exit_proposals(D2).proposals[0].quantity == 30


def test_a_booking_counts_once_by_its_booking_id(tmp_path):
    world = halved_world(tmp_path)
    world.sell_for_exit("AAA", 20, "460")
    world.account.bookings.append(world.account.bookings[-1])  # the same booking read twice
    assert world.portfolio.exit_proposals(D2).proposals[0].quantity == 30


def test_the_fallback_path_without_a_position_mark_nets_the_booked_sell_across_a_later_buy(tmp_path):
    """The latch was written outside the portfolio, so no position mark names the raising quantity. The first sight is
    recorded in the evidence once; a booked sell of 20 and a booked buy of 20 still leave 30."""
    world = at_peak(tmp_path)
    latch_evaluate(world.portfolio, world.latch,
        {A: Decimal("460")}, D1, cash=Decimal(0), positions=[exits.Position(A, "AAA", 100, Decimal("50000"))]
    )
    assert world.records("position_mark") == [] and world.latch.load().open_exits[A].quantity == 50
    assert world.portfolio.exit_proposals(D2).proposals[0].quantity == 50
    assert [r["reason"] for r in world.records("latch") if r["latch"] == "halve_baseline"] == [
        "since_2026-10-09_quantity_50"
    ]
    world.sell_for_exit("AAA", 20, "460")
    world.account.buy("AAA", 20, "460")
    for _ in range(2):
        again = world.restart()
        again.portfolio.on_booked_sell(A, 20)
        assert again.portfolio.exit_proposals(D2).proposals[0].quantity == 30
    assert len([r for r in world.records("latch") if r["latch"] == "halve_baseline"]) == 1


def test_the_exit_proposal_carries_the_obligation_id_a_runner_builds_its_intent_id_from(tmp_path):
    world = halved_world(tmp_path)
    (first,) = world.portfolio.exit_proposals(D2).proposals
    world.mark(D2, AAA="455")
    (again,) = world.portfolio.exit_proposals(D3).proposals
    assert first.batch_id != again.batch_id and first.obligation_id == again.obligation_id
    assert first.obligation_id == exit_obligation_id("halve", A, D1) == f"exit-halve-20261009-{A}"
    assert exit_intent_id(first.obligation_id, 2) == f"{first.obligation_id}-t2"
    for bad in (0, -1, True, "1", None):
        with pytest.raises(PortfolioError) as caught:
            exit_intent_id(first.obligation_id, bad)  # type: ignore[arg-type]
        assert caught.value.code == "bad_attempt"


def test_on_a_real_ledger_a_halve_booked_under_its_intent_id_is_netted_across_a_later_buy(tmp_path):
    rig = make_rig(tmp_path)
    store = make_evidence_store(tmp_path / "evidence")
    try:
        own(rig, "RELIANCE", 100)
        isin = ISINS["RELIANCE"]
        portfolio = ShadowPortfolio(rig.ledger, rig.guard.store, store, instruments("RELIANCE"))
        # An earlier admitted buy of 20 (admitted before the halt, so the gate lets it in).
        admitted = run(rig.adapter.prepare(buy_intent(rig, "late-buy", "RELIANCE", 20), samples_for("RELIANCE")))
        assert admitted.decision is AdmissionDecision.ADMITTED, admitted.reason_code
        # A halve of 50 raised by the latch (cash and close chosen so only the -8% line is crossed).
        latch_evaluate(portfolio, rig.guard.store,
            {isin: Decimal("95")}, D1, cash=Decimal("36000"),
            positions=[exits.Position(isin, "RELIANCE", 100, Decimal("10000"))],
        )
        assert rig.guard.store.load().open_exits[isin].quantity == 50
        obligation = exit_obligation_id("halve", isin, D1)
        # The halve sells 20 and books, then the earlier buy books.
        sell_and_fill(rig, exit_intent_id(obligation, 1), "RELIANCE", 20)
        book(rig, "late-buy", 20, "100.00", attempt="attempt-late")
        assert [h.quantity for h in portfolio.snapshot().holdings] == [100]
        for _ in range(2):  # duplicate hooks
            portfolio.on_booked_sell(isin, 20)
        (proposal,) = portfolio.exit_proposals(D2).proposals
        assert (proposal.reason, proposal.quantity) == ("halve", 30)
    finally:
        rig.close()


# --- review r2 P2: only a mark the latch committed can be carried -------------------------------------------------------------------------


def crash_before_latch(world: Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    def die(*args: Any, **kwargs: Any):
        raise Died

    monkeypatch.setattr(world.latch, "evaluate_session", die)


def test_an_unfinished_attempt_is_never_carried_even_when_the_retry_flags_the_bonus(tmp_path, monkeypatch):
    """Mark 100 @ 500 commits. The next session's attempt at 250 crashes before the latch. The retry carries the
    committed 500, not the unfinished 250 (which read as a terminal -50%)."""
    control = Fake(tmp_path / "control")
    control.account.buy("AAA", 100, "500")
    control.mark(D1, AAA="500")
    crash_before_latch(control, monkeypatch)
    with pytest.raises(Died):
        control.mark(D2, AAA="250")
    monkeypatch.undo()
    # The control: the retry WITHOUT the flag marks the raw 250 and the run ends, as it should for a real -50%.
    assert control.restart().mark(D2, AAA="250").state.ended

    world = Fake(tmp_path / "world")
    world.account.buy("AAA", 100, "500")
    world.mark(D1, AAA="500")
    crash_before_latch(world, monkeypatch)
    with pytest.raises(Died):
        world.mark(D2, AAA="250")
    monkeypatch.undo()
    assert [r["close"] for r in world.records("position_mark")] == ["500", "250"]
    again = world.restart()
    result = again.portfolio.mark_session(D2, {A: Decimal("250")}, structural_actions={A: "bonus"})
    assert result.equity == Decimal("50000") and result.drawdown == Decimal(0)
    assert not result.state.ended and not result.state.halt and result.state.stops == {}
    assert [(r["close"], r["mark_basis"]) for r in again.records("position_mark") if r["as_of_date"] == D2.isoformat()][-1] == (
        "500", MARK_CARRIED_EX_DATE,
    )
    assert policy.run_state(again.store).ended is False


def test_a_session_the_latch_never_evaluated_stays_uncommitted_after_later_sessions_commit(tmp_path, monkeypatch):
    """D2 crashed and was never retried, D3 commits. A later carry reads D3's committed mark, never the orphan D2 line
    (a pin of the seq binding; the discriminating row is the crash-then-retry one above)."""
    world = Fake(tmp_path)
    world.account.buy("AAA", 100, "500")
    world.mark(D1, AAA="500")
    crash_before_latch(world, monkeypatch)
    with pytest.raises(Died):
        world.mark(D2, AAA="250")
    monkeypatch.undo()
    again = world.restart()
    again.mark(D3, AAA="490")  # commits D3 at 490; the D2 line (250) is orphaned
    carried = again.portfolio.mark_session(D4, {A: Decimal("245")}, structural_actions={A: "split"})
    assert carried.equity == Decimal("49000")  # the committed 490, not the orphan 250 and not 500


def test_the_commit_names_the_lines_the_latch_read_and_is_written_once_and_repaired_after_a_crash(tmp_path, monkeypatch):
    world = Fake(tmp_path)
    world.account.buy("AAA", 100, "500")
    crash_before_latch(world, monkeypatch)
    with pytest.raises(Died):
        world.mark(D1, AAA="600")
    monkeypatch.undo()
    again = world.restart()
    again.mark(D1, AAA="610")  # supersedes the 600 line before the latch moves
    lines = again.records("position_mark")
    (commit,) = again.records("evaluation_commit")
    assert [r["close"] for r in lines] == ["600", "610"] and commit["position_mark_seqs"] == [lines[1]["seq"]]
    assert commit["as_of_date"] == D1.isoformat() and commit["equity"] == "61000"
    again.mark(D1, AAA="610")
    again.restart().mark(D2, AAA="610")
    assert len([r for r in again.records("evaluation_commit") if r["as_of_date"] == D1.isoformat()]) == 1


def test_a_commit_the_crash_lost_is_repaired_before_the_carry_reads_the_marks(tmp_path, monkeypatch):
    world = Fake(tmp_path)
    world.account.buy("AAA", 100, "500")
    die_after_latch_write(world, monkeypatch)
    with pytest.raises(Died):
        world.mark(D1, AAA="600")
    assert world.records("evaluation_commit") == []  # the latch moved, the commit line did not
    monkeypatch.undo()
    result = world.restart().portfolio.mark_session(D2, {A: Decimal("300")}, structural_actions={A: "split"})
    assert result.equity == Decimal("60000")


# --- review r2 P2: a reconciled quantity cannot create a phantom peak while paused -----------------------------------------------------------


def test_a_bonus_reconciled_on_the_ledger_while_paused_moves_neither_equity_nor_the_peak(tmp_path):
    """Carry 100 @ 500 through a 1:1 bonus, reconcile the ledger to 200 at the same cost, mark while paused (was 100000
    and a false peak), release, mark at 250: no false -50%."""
    world = Fake(tmp_path)
    world.account.buy("AAA", 100, "500")
    world.mark(D1, AAA="500")
    world.portfolio.mark_session(D2, {A: Decimal("250")}, structural_actions={A: "bonus"})
    world.account.adjust("AAA", 200)
    paused = world.mark(D3, AAA="250")
    assert paused.paused_isins == (A,) and paused.equity == Decimal("50000") and paused.peak == Decimal("50000")
    assert paused.state.stops == {} and not paused.state.ended
    (mark3,) = [r for r in world.records("mark") if r["as_of_date"] == D3.isoformat()]
    assert (mark3["equity"], mark3["peak"], mark3["exposure"]) == ("50000", "50000", "50000")
    day3 = [r for r in world.records("position_mark") if r["as_of_date"] == D3.isoformat()]
    assert [(r["quantity"], r["carried_quantity"], r["carried_value"]) for r in day3] == [("200", "200", "50000")]
    world.portfolio.release_corporate_action(A, "bonus shares booked")
    resumed = world.mark(D4, AAA="250")
    assert resumed.equity == Decimal("50000") and resumed.state.stops == {} and not resumed.state.ended
    assert not resumed.state.halt and resumed.drawdown == Decimal(0)


def test_a_split_reconciled_to_a_larger_quantity_while_paused_does_not_lift_the_peak_either(tmp_path):
    world = Fake(tmp_path)
    world.account.buy("AAA", 100, "500")
    world.mark(D1, AAA="500")
    world.portfolio.mark_session(D2, {A: Decimal("100")}, structural_actions={A: "split"})  # 1:5
    world.account.adjust("AAA", 500)
    world.mark(D3, AAA="100")
    again = world.mark(D4, AAA="100")
    assert again.equity == Decimal("50000") and again.peak == Decimal("50000")


def test_a_buy_that_books_while_paused_moves_the_value_by_its_cost_and_not_the_drawdown(tmp_path):
    """The cash a pre-admitted buy spends is not a loss: the carried value follows the ledger cost basis."""
    world = Fake(tmp_path)
    world.account.buy("AAA", 50, "500")
    world.mark(D1, AAA="500")
    world.portfolio.mark_session(D2, {A: Decimal("250")}, structural_actions={A: "bonus"})
    world.account.buy("AAA", 10, "500")
    result = world.mark(D3, AAA="250")
    assert result.equity == Decimal("50000") and result.drawdown == Decimal(0)


# --- review r2 gap: exits on a carried name are held out until the operator releases it ----------------------------------------------------


def probe_world(tmp_path: Path, *, structural: bool) -> Fake:
    """AAA 40 and BBB 60 bought at 500; D2 marks BBB at 430 (a halt) and AAA at its post-bonus 250. The control
    leaves AAA at 500 with no bonus, so the same -8% halt raises the same two halves."""
    world = Fake(tmp_path)
    world.account.buy("AAA", 40, "500")
    world.account.buy("BBB", 60, "500")
    world.mark(D1, AAA="500", BBB="500")
    result = world.portfolio.mark_session(
        D2, {A: Decimal("250" if structural else "500"), B: Decimal("430")},
        structural_actions={A: "bonus"} if structural else None,
    )
    assert result.state.halt and sorted(result.state.open_exits) == [A, B]
    return world


def test_a_halt_does_not_halve_a_carried_name_at_the_post_bonus_quote_until_it_is_released(tmp_path):
    """Review r2 gap probe. Without the hold the halve of 20 sells at 250, books a fake loss, marks 40800 (-18.4%)
    and ends the run."""
    control = probe_world(tmp_path / "control", structural=False)  # no pause: the same halve is proposed
    assert sorted((p.isin, p.reason, p.quantity) for p in control.portfolio.exit_proposals(D3).proposals) == [
        (A, "halve", 20), (B, "stop", 60),  # BBB at 430 is also past its -12% stop
    ]

    world = probe_world(tmp_path / "world", structural=True)
    plan = world.portfolio.exit_proposals(D3)
    assert [(p.isin, p.reason, p.quantity) for p in plan.proposals] == [(B, "stop", 60)] and plan.held_out == (A,)
    assert world.latch.load().open_exits[A].quantity == 20  # kept in the latch
    again = world.restart().portfolio.exit_proposals(D3)
    assert again.held_out == (A,) and [p.isin for p in again.proposals] == [B]  # a restart changes nothing
    world.account.adjust("AAA", 80)
    with pytest.raises(PortfolioError) as stale:  # the held-out 20 is in pre-bonus units: see the r3 row below
        world.portfolio.release_corporate_action(A, "bonus booked")
    assert stale.value.code == "exit_owed_in_pre_adjustment_units"
    world.account.sell("AAA", 80, "250")  # the operator flattens the name by hand: the halve closes with it
    world.portfolio.release_corporate_action(A, "flattened, nothing left to adjust")
    released = world.portfolio.exit_proposals(D3)
    assert [(p.isin, p.reason, p.quantity) for p in released.proposals] == [(B, "stop", 60)] and released.held_out == ()


def test_a_carried_name_that_owes_no_exit_has_nothing_held_out(tmp_path):
    world = Fake(tmp_path)
    world.account.buy("AAA", 20, "500")
    world.account.buy("BBB", 20, "500")
    world.mark(D1, AAA="500", BBB="500")
    world.portfolio.mark_session(D2, {A: Decimal("250"), B: Decimal("400")}, structural_actions={A: "bonus"})
    plan = world.portfolio.exit_proposals(D3)
    assert [(p.isin, p.reason) for p in plan.proposals] == [(B, "stop")]
    assert plan.held_out == ()  # AAA owes no exit, so nothing is held out and nothing is invented for it


# --- review r2 residual made fail-closed: a pause on a name whose adjustment is not booked cannot be resumed -------------------------------


def test_policy_resume_refuses_a_corporate_action_pause_and_names_the_portfolio_release(tmp_path):
    world = at_peak(tmp_path)
    world.mark(D1, AAA="500")
    world.portfolio.mark_session(D2, {A: Decimal("250")}, structural_actions={A: "bonus"})
    before = world.store.records()
    for seqs in (None, [p.seq for p in policy.run_state(world.store).open_pauses]):
        with pytest.raises(policy.PolicyError) as caught:
            policy.resume(world.store, "looks fine", pause_seqs=seqs)
        assert caught.value.code == "corporate_action_resume_requires_portfolio"
    assert world.store.records() == before and world.portfolio.entry_block_reasons(A) == ("instrument_paused",)


def test_release_is_refused_while_the_ledger_still_holds_the_pre_ex_quantity(tmp_path):
    world = at_peak(tmp_path)
    world.mark(D1, AAA="500")
    world.portfolio.mark_session(D2, {A: Decimal("250")}, structural_actions={A: "bonus"})
    world.mark(D3, AAA="250")
    before = world.store.records()
    with pytest.raises(PortfolioError) as caught:
        world.portfolio.release_corporate_action(A, "operator reviewed")
    assert caught.value.code == "corporate_action_not_booked"
    assert world.store.records() == before and world.portfolio.entry_block_reasons(A) == ("instrument_paused",)
    world.account.adjust("AAA", 200)  # the adjustment is booked: now it may go
    world.portfolio.release_corporate_action(A, "operator reviewed")
    assert world.portfolio.entry_block_reasons(A) == ()


def test_release_refuses_an_unknown_name_a_closed_pause_an_abort_and_a_name_never_marked(tmp_path):
    world = at_peak(tmp_path)
    with pytest.raises(PortfolioError) as unknown:
        world.portfolio.release_corporate_action(CLEAR_ISIN, "reviewed")
    assert unknown.value.code == "unknown_instrument"
    with pytest.raises(policy.PolicyError) as none_open:
        world.portfolio.release_corporate_action(A, "reviewed")
    assert none_open.value.code == "nothing_to_resume"
    policy.raise_event(world.store, "unresolved_corporate_action", "bonus", isin=A)  # a pause with no committed mark
    with pytest.raises(PortfolioError) as unmarked:
        world.portfolio.release_corporate_action(A, "reviewed")
    assert unmarked.value.code == "corporate_action_not_booked"  # fail closed: nothing says the quantity changed
    policy.raise_event(world.store, "audit_corrupt", "chain break")
    with pytest.raises(policy.PolicyError) as aborted:
        world.portfolio.release_corporate_action(A, "reviewed")
    assert aborted.value.code == "resume_refused_abort"


def test_a_name_that_is_no_longer_held_is_released_without_an_adjustment(tmp_path):
    world = at_peak(tmp_path)
    world.mark(D1, AAA="500")
    world.portfolio.mark_session(D2, {A: Decimal("250")}, structural_actions={A: "bonus"})
    world.account.sell("AAA", 100, "250")  # the operator flattened it by hand
    world.portfolio.release_corporate_action(A, "flattened, nothing left to adjust")
    assert world.portfolio.entry_block_reasons(A) == ()


def test_on_the_real_ledger_the_release_is_refused_because_it_cannot_book_an_adjustment(tmp_path):
    rig = make_rig(tmp_path)
    store = make_evidence_store(tmp_path / "evidence")
    try:
        own(rig, "RELIANCE", 100)
        isin = ISINS["RELIANCE"]
        portfolio = ShadowPortfolio(rig.ledger, rig.guard.store, store, instruments("RELIANCE"))
        portfolio.mark_session(D1, {isin: Decimal("100")})
        portfolio.mark_session(D2, {isin: Decimal("50")}, structural_actions={isin: "bonus"})
        assert portfolio.entry_block_reasons(isin) == ("instrument_paused",)
        with pytest.raises(PortfolioError) as caught:
            portfolio.release_corporate_action(isin, "operator reviewed the bonus")
        assert caught.value.code == "corporate_action_not_booked"
        with pytest.raises(policy.PolicyError) as plain:
            policy.resume(store, "operator reviewed the bonus")
        assert plain.value.code == "corporate_action_resume_requires_portfolio"
        assert [(p.event, p.scope) for p in policy.run_state(store).open_pauses] == [
            ("unresolved_corporate_action", f"isin:{isin}")
        ]
        # Flattening the name through the ledger is the way out: the pause then releases.
        sell_and_fill(rig, exit_intent_id("exit-flatten-by-hand", 1), "RELIANCE", 100)
        portfolio.release_corporate_action(isin, "flattened by hand")
        assert portfolio.entry_block_reasons(isin) == ()
    finally:
        rig.close()


# --- review r3: the release guard, the commit's seqs, an ambiguous halve fill, a held-out halve -------------------------------------------------

R_ISIN, T_ISIN = ISINS["RELIANCE"], ISINS["TCS"]


def real_world(tmp_path: Path, *symbols: str):
    rig = make_rig(tmp_path)
    store = make_evidence_store(tmp_path / "evidence")
    return rig, store, ShadowPortfolio(rig.ledger, rig.guard.store, store, instruments(*symbols))


def rebuy(rig: Rig, proposal_id: str, symbol: str, quantity: int, price: str = "100.00") -> None:
    admission = run(rig.adapter.prepare(buy_intent(rig, proposal_id, symbol, quantity, price), samples_for(symbol, price)))
    assert admission.decision is AdmissionDecision.ADMITTED, admission.reason_code
    book(rig, proposal_id, quantity, price, attempt=f"attempt-{proposal_id}")


def test_a_buy_booked_on_the_ex_date_session_is_not_an_adjustment_and_the_release_is_refused(tmp_path):
    """opus-r3-1. Own 150 marked at 100; a buy of 10 books before the ex-date close mark; the bonus mark carries. The
    ledger quantity (160) differs from the last pre-ex mark (150) only because of the buy. Released, the next mark read
    160 x 50 against a carried 16000 (-50%) and ended the run."""
    rig, store, portfolio = real_world(tmp_path, "RELIANCE")
    try:
        own(rig, "RELIANCE", 150)
        portfolio.mark_session(D1, {R_ISIN: Decimal("100")})
        rebuy(rig, "exdate-buy", "RELIANCE", 10)
        carried = portfolio.mark_session(D2, {R_ISIN: Decimal("50")}, structural_actions={R_ISIN: "bonus"})
        assert carried.equity == Decimal("50000") and carried.paused_isins == (R_ISIN,)
        before = store.records()
        with pytest.raises(PortfolioError) as caught:
            portfolio.release_corporate_action(R_ISIN, "operator reviewed")
        assert caught.value.code == "corporate_action_not_booked"
        assert store.records() == before and portfolio.entry_block_reasons(R_ISIN) == ("instrument_paused",)
        later = portfolio.mark_session(D3, {R_ISIN: Decimal("50")})
        assert later.equity == Decimal("50000") and not later.state.ended and not later.state.halt
    finally:
        rig.close()


def test_a_partial_non_exit_sell_booked_on_the_ex_date_session_is_not_an_adjustment_either(tmp_path):
    rig, store, portfolio = real_world(tmp_path, "RELIANCE")
    try:
        own(rig, "RELIANCE", 150)
        portfolio.mark_session(D1, {R_ISIN: Decimal("100")})
        sell_and_fill(rig, "trim-by-hand", "RELIANCE", 50)
        portfolio.mark_session(D2, {R_ISIN: Decimal("50")}, structural_actions={R_ISIN: "bonus"})
        with pytest.raises(PortfolioError) as caught:
            portfolio.release_corporate_action(R_ISIN, "operator reviewed")
        assert caught.value.code == "corporate_action_not_booked"
        later = portfolio.mark_session(D3, {R_ISIN: Decimal("50")})
        assert not later.state.ended and later.equity == Decimal("45000") + Decimal("5000")
    finally:
        rig.close()


def test_a_booked_adjustment_releases_before_and_after_a_further_carried_mark_but_not_after_a_buy_and_sell(tmp_path):
    world = Fake(tmp_path)
    world.account.buy("AAA", 100, "500")
    world.mark(D1, AAA="500")
    world.portfolio.mark_session(D2, {A: Decimal("250")}, structural_actions={A: "bonus"})
    world.account.adjust("AAA", 200)  # booked after the ex-date mark
    world.mark(D3, AAA="250")  # a later mark already records the adjusted quantity
    assert world.mark(D3, AAA="250").equity == Decimal("50000")
    world.portfolio.release_corporate_action(A, "bonus booked")
    assert world.portfolio.entry_block_reasons(A) == ()
    # A buy and a sell both move the cost basis: neither is an adjustment.
    other = Fake(tmp_path / "other")
    other.account.buy("AAA", 100, "500")
    other.mark(D1, AAA="500")
    other.account.buy("AAA", 10, "500")
    other.account.sell("AAA", 20, "500")
    other.portfolio.mark_session(D2, {A: Decimal("250")}, structural_actions={A: "bonus"})
    other.mark(D3, AAA="250")
    with pytest.raises(PortfolioError) as caught:
        other.portfolio.release_corporate_action(A, "operator reviewed")
    assert caught.value.code == "corporate_action_not_booked"


def test_a_line_of_a_name_the_retry_no_longer_held_is_not_committed_with_the_evaluation(tmp_path, monkeypatch):
    """opus-r3-2. Attempt 1 marks AAA 500 and BBB 300 and dies before the latch; BBB is sold; the retry marks AAA only.
    The commit names the retry's line, not the latest line per ISIN, which included BBB's stale 300."""
    w = Fake(tmp_path)
    w.account.buy("AAA", 50, "500")
    w.account.buy("BBB", 50, "500")
    w.mark(D1, AAA="500", BBB="500")
    crash_before_latch(w, monkeypatch)
    with pytest.raises(Died):
        w.mark(D2, AAA="500", BBB="300")
    monkeypatch.undo()
    w.account.sell("BBB", 50, "500")
    again = w.restart()
    again.mark(D2, AAA="500")
    (commit,) = [r for r in again.records("evaluation_commit") if r["as_of_date"] == D2.isoformat()]
    lines = [r for r in again.records("position_mark") if r["as_of_date"] == D2.isoformat()]
    stale = [r["seq"] for r in lines if r["isin"] == B]
    assert stale and commit["position_mark_seqs"] == [r["seq"] for r in lines if r["isin"] == A]
    # BBB is bought again and a bonus pauses it: the carry reads the committed 500, not the unfinished 300.
    again.account.buy("BBB", 50, "500")
    carried = again.portfolio.mark_session(D3, {A: Decimal("500"), B: Decimal("250")}, structural_actions={B: "bonus"})
    assert carried.equity == Decimal("50000") - Decimal("25000") + Decimal("25000") and not carried.state.ended


def test_the_commit_a_crash_after_the_latch_lost_is_repaired_with_the_inputs_of_the_attempt_that_moved_the_latch(
    tmp_path, monkeypatch
):
    w = Fake(tmp_path)
    w.account.buy("AAA", 50, "500")
    w.account.buy("BBB", 50, "500")
    w.mark(D1, AAA="500", BBB="500")
    crash_before_latch(w, monkeypatch)
    with pytest.raises(Died):
        w.mark(D2, AAA="500", BBB="300")
    monkeypatch.undo()
    w.account.sell("BBB", 50, "500")
    retry = w.restart()
    die_after_latch_write(retry, monkeypatch)
    with pytest.raises(Died):
        retry.mark(D2, AAA="500")  # the latch moved on AAA only, then the process died before the commit line
    monkeypatch.undo()
    repaired = w.restart()
    repaired.portfolio.on_booked_sell(A, 0)
    (commit,) = [r for r in repaired.records("evaluation_commit") if r["as_of_date"] == D2.isoformat()]
    bbb = [r["seq"] for r in repaired.records("position_mark") if r["isin"] == B and r["as_of_date"] == D2.isoformat()]
    assert bbb and not set(bbb) & set(commit["position_mark_seqs"]) and len(commit["position_mark_seqs"]) == 1


def test_on_a_real_ledger_a_stale_line_of_a_sold_name_never_becomes_a_carried_value(tmp_path, monkeypatch):
    """codex-r3-2, on the real ledger. TCS is marked at 60 by an attempt that never reached the latch, then sold; it is
    bought again and a bonus pauses it. The carry is the committed 100, so equity and the peak stay at 50000."""
    rig, store, portfolio = real_world(tmp_path, "RELIANCE", "TCS")
    try:
        own(rig, "RELIANCE", 150)
        own(rig, "TCS", 150)
        portfolio.mark_session(D1, {R_ISIN: Decimal("100"), T_ISIN: Decimal("100")})

        def die(*args: Any, **kwargs: Any):
            raise Died

        with monkeypatch.context() as patched:  # not monkeypatch.undo(): that would also undo the frozen service clock
            patched.setattr(rig.guard.store, "evaluate_session", die)
            with pytest.raises(Died):
                portfolio.mark_session(D2, {R_ISIN: Decimal("100"), T_ISIN: Decimal("60")})
        sell_and_fill(rig, "sell-tcs", "TCS", 150)
        portfolio.mark_session(D2, {R_ISIN: Decimal("100")})
        rebuy(rig, "rebuy-tcs", "TCS", 150)
        carried = portfolio.mark_session(
            D3, {R_ISIN: Decimal("100"), T_ISIN: Decimal("50")}, structural_actions={T_ISIN: "bonus"}
        )
        assert carried.equity == Decimal("50000") == carried.peak and carried.paused_isins == (T_ISIN,)
        assert not carried.state.ended and carried.state.stops == {}
    finally:
        rig.close()


def ambiguous_pauses(store: EvidenceStore) -> list[Any]:
    return [p for p in policy.run_state(store).open_pauses if p.event == "ambiguous_exit_fill"]


def halve_on_real_ledger(rig: Rig, portfolio: ShadowPortfolio):
    """Own 100 RELIANCE; the latch raises a halve of 50 at D1. The first decide records what the ledger held."""
    own(rig, "RELIANCE", 100)
    latch_evaluate(portfolio, rig.guard.store,
        {R_ISIN: Decimal("95")}, D1, cash=Decimal("36000"),
        positions=[exits.Position(R_ISIN, "RELIANCE", 100, Decimal("10000"))],
    )
    assert rig.guard.store.load().open_exits[R_ISIN].quantity == 50
    assert [p.quantity for p in portfolio.exit_proposals(D2).proposals] == [50]


def test_a_halve_part_filled_under_an_ordinary_order_id_is_never_proposed_again(tmp_path):
    """codex-r3-1. The runner books 20 of the halve's 50 under an ordinary order id and expires the rest. Reconciliation
    ignored that fill and proposed 50 again (70 sold, 30 held, 30 more proposed)."""
    rig, store, portfolio = real_world(tmp_path, "RELIANCE")
    try:
        halve_on_real_ledger(rig, portfolio)
        sell_and_fill(rig, "ordinary-order-1", "RELIANCE", 20)
        plan = portfolio.exit_proposals(D2)
        assert plan.proposals == () and plan.ambiguous == (R_ISIN,)
        assert [(p.event, p.scope) for p in ambiguous_pauses(store)] == [("ambiguous_exit_fill", f"isin:{R_ISIN}")]
        assert "instrument_paused" in portfolio.entry_block_reasons(R_ISIN)
        assert rig.guard.store.load().open_exits[R_ISIN].quantity == 50  # the latch still owes it
        portfolio.on_booked_sell(R_ISIN, 20)
        again = ShadowPortfolio(rig.ledger, rig.guard.store, store, instruments("RELIANCE"))
        assert again.exit_proposals(D2).proposals == () and again.exit_proposals(D2).ambiguous == (R_ISIN,)
        assert len(ambiguous_pauses(store)) == 1  # the pause is raised once, not on every call
        # The way out is to flatten the name by hand: the exit closes, and the pause can then be resumed.
        sell_and_fill(rig, "flatten-by-hand", "RELIANCE", 80)
        assert again.exit_proposals(D2) == ExitPlan((), ())
        policy.resume(store, "operator flattened the name", pause_seqs=[p.seq for p in ambiguous_pauses(store)])
        assert ambiguous_pauses(store) == []
    finally:
        rig.close()


@pytest.mark.parametrize("proposal_id", [
    "manual-sell",
    exit_intent_id(exit_obligation_id("halve", A, D2), 1),  # another day's halve
    exit_intent_id(exit_obligation_id("halve", A, D1), 1) + "x",
    "x" + exit_intent_id(exit_obligation_id("halve", A, D1), 1),
    exit_intent_id(exit_obligation_id("stop", A, D1), 1),  # another reason
])
def test_any_sell_not_booked_under_the_halves_own_id_makes_it_ambiguous(tmp_path, proposal_id):
    world = halved_world(tmp_path)
    assert [p.quantity for p in world.portfolio.exit_proposals(D2).proposals] == [50]
    world.account.sell("AAA", 5, "460", proposal_id=proposal_id)
    plan = world.portfolio.exit_proposals(D2)
    assert plan.proposals == () and plan.ambiguous == (A,)
    assert [p.event for p in ambiguous_pauses(world.store)] == ["ambiguous_exit_fill"]
    assert world.restart().portfolio.exit_proposals(D2).ambiguous == (A,)  # a restart changes nothing


def test_sells_before_the_halve_was_raised_and_the_halves_own_fills_are_not_ambiguous(tmp_path):
    world = Fake(tmp_path)
    world.account.buy("AAA", 120, "500")
    world.account.sell("AAA", 20, "500")  # an ordinary trim, long before the halt
    result = world.mark(D1, AAA="460")
    assert result.state.halt and result.state.open_exits[A].quantity == 50
    world.sell_for_exit("AAA", 20, "460")
    world.account.buy("AAA", 10, "460")
    plan = world.portfolio.exit_proposals(D2)
    assert plan.ambiguous == () and [p.quantity for p in plan.proposals] == [30]
    assert ambiguous_pauses(world.store) == []


def test_an_ambiguous_halve_does_not_hold_back_a_stop_on_another_name_and_marks_keep_evaluating(tmp_path):
    world = probe_world(tmp_path, structural=False)  # AAA owes a halve of 20, BBB a stop of 60
    world.account.sell("AAA", 5, "500")  # not the halve's
    plan = world.portfolio.exit_proposals(D3)
    assert [(p.isin, p.reason) for p in plan.proposals] == [(B, "stop")] and plan.ambiguous == (A,)
    result = world.mark(D3, AAA="500", BBB="430")  # marks never refuse because of it
    assert result.evaluated and world.latch.load().open_exits[A].quantity == 20


def test_a_halve_held_out_across_a_corporate_action_pause_is_not_released_in_pre_adjustment_units(tmp_path):
    """codex-r3-4. A halve of 20 of 40 is held out through a 1:1 bonus; released, it would stay 20 of 80 (a quarter)."""
    world = probe_world(tmp_path / "world", structural=True)
    plan = world.portfolio.exit_proposals(D3)
    assert plan.held_out == (A,) and world.latch.load().open_exits[A].quantity == 20
    world.account.adjust("AAA", 80)
    before = world.store.records()
    with pytest.raises(PortfolioError) as caught:
        world.portfolio.release_corporate_action(A, "bonus booked")
    assert caught.value.code == "exit_owed_in_pre_adjustment_units"
    assert world.store.records() == before and world.portfolio.exit_proposals(D3).held_out == (A,)
    # A stop owes what is held, in whatever units: it does not block the release.
    flatten = Fake(tmp_path / "flatten")
    flatten.account.buy("AAA", 40, "500")
    flatten.mark(D1, AAA="500")
    flatten.portfolio.mark_session(D2, {A: Decimal("250")}, structural_actions={A: "bonus"})
    flatten.account.adjust("AAA", 80)
    flatten.portfolio.release_corporate_action(A, "bonus booked")
    assert flatten.portfolio.entry_block_reasons(A) == ()


# --- review r4: the sell boundary is persisted first, a release needs an explicit record, a carried sell scales --------------------------------


def two_names_at_ninety(tmp_path: Path):
    """RELIANCE 200 and TCS 200 bought at 100 (cash 10000): a close of 89 on both is -8.8% (a halt, a halve of
    half the shares held each) while 89 stays above the -12% stop at 88."""
    rig = make_rig(tmp_path)
    store = make_evidence_store(tmp_path / "evidence")
    portfolio = ShadowPortfolio(rig.ledger, rig.guard.store, store, instruments("RELIANCE", "TCS"))
    own(rig, "RELIANCE", 200)
    own(rig, "TCS", 200)
    portfolio.mark_session(D1, {R_ISIN: Decimal("100"), T_ISIN: Decimal("100")})
    return rig, store, portfolio


def admit_ordinary_sell(rig: Rig, proposal_id: str, quantity: int) -> None:
    admission = run(rig.adapter.prepare(sell_intent(rig, proposal_id, "RELIANCE", quantity), samples_for("RELIANCE")))
    assert admission.decision is AdmissionDecision.ADMITTED, admission.reason_code


def book_pending_sell(rig: Rig, proposal_id: str, quantity: int) -> None:
    book(rig, proposal_id, quantity, "100.00", attempt="attempt-exit")


def test_codex_r4_1_an_ordinary_sell_admitted_before_the_halve_and_booked_after_a_crash_is_ambiguous(tmp_path, monkeypatch):
    """Admit an ordinary 20-share sell, raise the halve of 100, die after the latch write, book the pending sell before
    any reconciliation. Recovery recorded the baseline AFTER that booking (1), saw no ambiguity and proposed 100 of the
    remaining 180: 120 sold, 80 held. The boundary written before the latch (0) makes the booking ambiguous."""
    rig, store, portfolio = two_names_at_ninety(tmp_path)
    try:
        admit_ordinary_sell(rig, "ordinary-20", 20)
        real = rig.guard.store.evaluate_session

        def evaluate_then_die(*args: Any, **kwargs: Any):
            real(*args, **kwargs)
            raise Died

        monkeypatch.setattr(rig.guard.store, "evaluate_session", evaluate_then_die)
        with pytest.raises(Died):
            portfolio.mark_session(D2, {R_ISIN: Decimal("89"), T_ISIN: Decimal("89")})
        monkeypatch.undo()
        assert rig.guard.store.load().open_exits[R_ISIN].quantity == 100
        book_pending_sell(rig, "ordinary-20", 20)
        recovered = ShadowPortfolio(rig.ledger, rig.guard.store, store, instruments("RELIANCE", "TCS"))
        plan = recovered.exit_proposals(D3)
        assert [p.isin for p in plan.proposals] == [T_ISIN] and plan.ambiguous == (R_ISIN,)
        assert [(p.event, p.scope) for p in ambiguous_pauses(store)] == [("ambiguous_exit_fill", f"isin:{R_ISIN}")]
        assert recovered.exit_proposals(D3).ambiguous == (R_ISIN,)  # a restart changes nothing
    finally:
        rig.close()


@pytest.mark.parametrize(
    ("crash_at", "ambiguous"),
    [
        ("before_boundary", False),  # nothing moved: the sell booked before any evaluation, so it is history
        ("after_boundary_before_latch", False),  # the retry supersedes the boundary: the latch had not moved either
        ("after_latch", True),  # the halve was already open when the sell booked
    ],
)
def test_codex_r4_1_a_crash_at_each_boundary_never_proposes_a_halve_on_top_of_an_ordinary_sell(
    tmp_path, monkeypatch, crash_at, ambiguous
):
    rig, store, portfolio = two_names_at_ninety(tmp_path)
    try:
        admit_ordinary_sell(rig, "ordinary-20", 20)
        closes = {R_ISIN: Decimal("89"), T_ISIN: Decimal("89")}

        def die(*args: Any, **kwargs: Any):
            raise Died

        if crash_at == "before_boundary":
            monkeypatch.setattr(portfolio, "_record_sell_boundaries", die)
        elif crash_at == "after_boundary_before_latch":
            monkeypatch.setattr(rig.guard.store, "evaluate_session", die)
        else:
            real = rig.guard.store.evaluate_session

            def evaluate_then_die(*args: Any, **kwargs: Any):
                real(*args, **kwargs)
                raise Died

            monkeypatch.setattr(rig.guard.store, "evaluate_session", evaluate_then_die)
        with pytest.raises(Died):
            portfolio.mark_session(D2, closes)
        monkeypatch.undo()
        book_pending_sell(rig, "ordinary-20", 20)
        retry = ShadowPortfolio(rig.ledger, rig.guard.store, store, instruments("RELIANCE", "TCS"))
        retry.mark_session(D2, closes)
        plan = retry.exit_proposals(D3)
        reliance = [p for p in plan.proposals if p.isin == R_ISIN]
        if ambiguous:
            assert plan.ambiguous == (R_ISIN,) and reliance == []
        else:
            assert plan.ambiguous == () and [p.quantity for p in reliance] == [90]  # half of the 180 held then
    finally:
        rig.close()


def test_codex_r4_1_a_halve_with_no_recorded_boundary_is_never_sized_from_current_bookings(tmp_path):
    """A latch written outside the portfolio leaves no boundary line. The old code counted the bookings it saw at first
    sight, which hid every sell booked after the halve opened."""
    world = at_peak(tmp_path)
    world.latch.evaluate_session(
        {A: Decimal("460")}, D1, cash=Decimal(0), positions=[exits.Position(A, "AAA", 100, Decimal("50000"))]
    )
    world.account.sell("AAA", 20, "460")  # an ordinary sell booked after the halve opened
    plan = world.portfolio.exit_proposals(D2)
    assert plan.proposals == () and plan.ambiguous == (A,)
    assert [r for r in world.store.records() if r["kind"] == "latch" and r["latch"] == "sell_baseline"] == []


def test_codex_r4_2_equal_cost_with_another_quantity_is_not_an_adjustment(tmp_path):
    """Hold 100 costing 10000; a buy of 40 at 50 was admitted before the bonus pause; then a 20-share sell and the delayed
    buy book. Holdings 120 at cost 10000: the old cost-equality rule released it and the next mark read 45000, a halt and
    a 120-share stop. Without an explicit record the release is refused."""
    world = Fake(tmp_path)
    world.account.buy("AAA", 100, "100")
    world.mark(D1, AAA="100")
    world.portfolio.mark_session(D2, {A: Decimal("50")}, structural_actions={A: "bonus"})
    world.account.sell("AAA", 20, "50")
    world.account.buy("AAA", 40, "50")
    assert world.account.held["AAA"] == (120, Decimal("10000"))  # the probe's own numbers
    world.mark(D3, AAA="50")
    before = world.store.records()
    with pytest.raises(PortfolioError) as caught:
        world.portfolio.release_corporate_action(A, "operator reviewed")
    assert caught.value.code == "corporate_action_not_booked"
    assert world.store.records() == before and world.portfolio.entry_block_reasons(A) == ("instrument_paused",)


def test_codex_r4_2_only_an_explicit_record_for_this_kind_and_ex_date_releases(tmp_path):
    world = at_peak(tmp_path)
    world.mark(D1, AAA="500")
    world.portfolio.mark_session(D2, {A: Decimal("250")}, structural_actions={A: "bonus"})
    world.account.held["AAA"] = (200, world.account.held["AAA"][1])  # the quantity moved, no record booked
    with pytest.raises(PortfolioError) as bare:
        world.portfolio.release_corporate_action(A, "operator reviewed")
    assert bare.value.code == "corporate_action_not_booked"
    world.account.adjustments.append(SimpleNamespace(ticker="NSE:CASH:AAA", kind="split", ex_date=D2))  # wrong kind
    world.account.adjustments.append(SimpleNamespace(ticker="NSE:CASH:AAA", kind="bonus", ex_date=D3))  # wrong date
    world.account.adjustments.append(SimpleNamespace(ticker="NSE:CASH:BBB", kind="bonus", ex_date=D2))  # wrong name
    with pytest.raises(PortfolioError) as wrong:
        world.portfolio.release_corporate_action(A, "operator reviewed")
    assert wrong.value.code == "corporate_action_not_booked"
    world.account.adjustments.append(SimpleNamespace(ticker="NSE:CASH:AAA", kind="bonus", ex_date=D2))
    world.portfolio.release_corporate_action(A, "operator reviewed")
    assert world.portfolio.entry_block_reasons(A) == ()


def test_codex_r3_3_a_partial_sale_of_a_carried_name_scales_the_carried_value_by_the_shares_held(tmp_path):
    """Own 100 marked at 150 (value 15000, cost 10000); the bonus pause carries 15000; 40 are sold. Cost falls to 6000
    and the old cost delta left 11000 in the carry (the sold shares' value still in it). 60 of 100 shares are 9000."""
    rig, store, portfolio = real_world(tmp_path, "RELIANCE")
    try:
        own(rig, "RELIANCE", 100)
        portfolio.mark_session(D1, {R_ISIN: Decimal("150")})
        carried = portfolio.mark_session(D2, {R_ISIN: Decimal("75")}, structural_actions={R_ISIN: "bonus"})
        assert carried.paused_isins == (R_ISIN,) and carried.equity == Decimal("55000")
        sell_and_fill(rig, "trim-by-hand", "RELIANCE", 40)
        after = portfolio.mark_session(D3, {R_ISIN: Decimal("75")})
        (line,) = [r for r in store.records() if r["kind"] == "position_mark" and r["as_of_date"] == D3.isoformat()]
        assert (line["quantity"], line["carried_quantity"], Decimal(line["carried_value"])) == ("60", "60", Decimal("9000"))
        assert after.equity == Decimal("44000") + Decimal("9000")
    finally:
        rig.close()


# --- codex r5 residual: a carried value reconciles the sells and the buys booked while paused, separately -----------------------------------


def carried_world(tmp_path: Path) -> Fake:
    """100 AAA bought at 100 (cost 10000), marked at 150 (value 15000), carried through a bonus ex-date at 75."""
    world = Fake(tmp_path)
    world.account.buy("AAA", 100, "100")
    world.mark(D1, AAA="150")
    carried = world.portfolio.mark_session(D2, {A: Decimal("75")}, structural_actions={A: "bonus"})
    assert carried.paused_isins == (A,) and carried.equity == Decimal("55000")
    return world


def carried_line(world: Fake, day: date) -> dict[str, Any]:
    (line,) = [r for r in world.records("position_mark") if r["as_of_date"] == day.isoformat() and r["isin"] == A]
    return line


def test_codex_r5_1_a_sell_alone_while_carried_still_takes_its_shares_out_of_the_carry(tmp_path):
    """The r4 control: 100 at 15000, 40 sold, 60 x 150 = 9000. Nothing booked beside it."""
    world = carried_world(tmp_path)
    world.account.sell("AAA", 40, "75")
    after = world.mark(D3, AAA="75")
    line = carried_line(world, D3)
    assert (line["quantity"], line["carried_quantity"], line["carried_value"]) == ("60", "60", "9000")
    assert after.equity == world.account.cash + Decimal("9000")


def test_codex_r5_1_a_buy_booked_beside_a_hand_sell_adds_its_cost_and_the_sell_removes_only_its_shares(tmp_path):
    """The probe on the fake account: 100 shares carried at 10000, sell 40 at 100, an earlier admitted buy of 20 books
    at 101. The 80 shares carry 60 x 100 + 20 x 101 = 8020. Net-quantity scaling gave 8000 and dropped the 2020."""
    world = Fake(tmp_path)
    world.account.buy("AAA", 100, "100")
    world.mark(D1, AAA="100")
    world.portfolio.mark_session(D2, {A: Decimal("50")}, structural_actions={A: "bonus"})
    world.account.sell("AAA", 40, "100")
    world.account.buy("AAA", 20, "101")
    after = world.mark(D3, AAA="50")
    line = carried_line(world, D3)
    assert (line["quantity"], line["carried_quantity"], line["carried_value"]) == ("80", "80", "8020")
    assert after.equity == world.account.cash + Decimal("8020")


def test_codex_r5_1_the_order_of_the_sell_and_the_buy_does_not_change_the_value(tmp_path):
    for sell_first in (True, False):
        world = Fake(tmp_path / str(sell_first))
        world.account.buy("AAA", 100, "100")
        world.mark(D1, AAA="100")
        world.portfolio.mark_session(D2, {A: Decimal("50")}, structural_actions={A: "bonus"})
        if sell_first:
            world.account.sell("AAA", 40, "100")
            world.account.buy("AAA", 20, "101")
        else:
            world.account.buy("AAA", 20, "101")
            world.account.sell("AAA", 40, "100")
        world.mark(D3, AAA="50")
        assert carried_line(world, D3)["carried_value"] == "8020"


def test_codex_r5_1_a_buy_alone_while_carried_adds_its_cost(tmp_path):
    world = carried_world(tmp_path)
    world.account.buy("AAA", 20, "101")
    after = world.mark(D3, AAA="75")
    line = carried_line(world, D3)
    assert (line["quantity"], line["carried_quantity"], line["carried_value"]) == ("120", "120", "17020")
    assert after.equity == world.account.cash + Decimal("17020")


def test_codex_r5_1_the_next_carried_mark_starts_from_the_last_one_and_counts_only_new_bookings(tmp_path):
    world = Fake(tmp_path)
    world.account.buy("AAA", 100, "100")
    world.mark(D1, AAA="100")
    world.portfolio.mark_session(D2, {A: Decimal("50")}, structural_actions={A: "bonus"})
    world.account.sell("AAA", 40, "100")
    world.account.buy("AAA", 20, "101")
    world.mark(D3, AAA="50")
    world.account.sell("AAA", 10, "100")
    world.account.buy("AAA", 5, "102")
    world.mark(D4, AAA="50")
    line = carried_line(world, D4)
    # 8020 x 70 / 80 = 7017.5 from D3's value, plus the 510 the new buy cost. Counting D3's bookings again, or none, misses it.
    assert (line["carried_quantity"], line["carried_value"]) == ("75", "7527.5")


def test_codex_r5_1_a_full_sale_leaves_nothing_in_the_carry_and_the_pause_releases_by_flat(tmp_path):
    world = carried_world(tmp_path)
    world.account.sell("AAA", 100, "150")
    after = world.mark(D3, AAA="75")
    assert not [r for r in world.records("position_mark") if r["as_of_date"] == D3.isoformat()]
    assert after.equity == world.account.cash and after.paused_isins == ()
    world.portfolio.release_corporate_action(A, "sold flat")
    assert world.portfolio.entry_block_reasons(A) == ()


def test_codex_r5_1_a_sale_past_the_carried_lot_takes_the_rest_from_the_bought_shares(tmp_path):
    """100 carried at 10000, a buy of 20 at 101 books, 110 are sold: 10 shares left, all from the bought lot."""
    world = Fake(tmp_path)
    world.account.buy("AAA", 100, "100")
    world.mark(D1, AAA="100")
    world.portfolio.mark_session(D2, {A: Decimal("50")}, structural_actions={A: "bonus"})
    world.account.buy("AAA", 20, "101")
    world.account.sell("AAA", 110, "100")
    world.mark(D3, AAA="50")
    line = carried_line(world, D3)
    assert (line["carried_quantity"], line["carried_value"]) == ("10", "1010")


def test_codex_r5_1_on_the_real_ledger_a_buy_booked_beside_a_hand_sell_carries_its_cost_and_no_false_halt(tmp_path):
    """Codex's probe. 100 RELIANCE carried at 10000; while paused 40 are sold at 100 by hand and an admitted buy of 20
    books at 101. 80 shares carry 8020 (60 x 100 + 20 x 101); net-quantity scaling gave 8000. With TCS and INFY marked
    near the line the total is 46004 against the 46000 halt: 8000 read 45984 and halted the entries and halved three
    names."""
    rig, store, portfolio = real_world(tmp_path, "RELIANCE", "TCS", "INFY")
    try:
        for symbol in ("RELIANCE", "TCS", "INFY"):
            own(rig, symbol, 100)
        same = {R_ISIN: Decimal("100"), T_ISIN: Decimal("100"), ISINS["INFY"]: Decimal("100")}
        portfolio.mark_session(D1, same)
        admission = run(rig.adapter.prepare(buy_intent(rig, "late-buy", "RELIANCE", 20, "101.00"), samples_for("RELIANCE", "101.00")))
        assert admission.decision is AdmissionDecision.ADMITTED, admission.reason_code
        carried = portfolio.mark_session(D2, same, structural_actions={R_ISIN: "bonus"})
        assert carried.paused_isins == (R_ISIN,) and carried.equity == Decimal("50000")
        sell_and_fill(rig, "trim-by-hand", "RELIANCE", 40)
        book(rig, "late-buy", 20, "101", attempt="attempt-late")
        near = {**same, T_ISIN: Decimal("80.02"), ISINS["INFY"]: Decimal("80.02")}
        after = portfolio.mark_session(D3, near)
        (line,) = [r for r in store.records() if r["kind"] == "position_mark" and r["as_of_date"] == D3.isoformat() and r["isin"] == R_ISIN]
        assert (line["quantity"], line["carried_quantity"], Decimal(line["carried_value"])) == ("80", "80", Decimal("8020"))
        assert after.equity == Decimal("46004.00")
        # TCS and INFY at 80.02 against a cost of 100 stop out on their own; the carried name must not move the halt.
        assert not after.state.halt and not [b for b in after.batches if b.reason == "halve"]
        assert R_ISIN not in after.state.open_exits
    finally:
        rig.close()


# --- review r1 (Codex r1 + Opus r1 at 5c5c6bc5): the fallback is fail-closed, sales replay in order, buys carry charges ------------------------


def legacy_mark(world: Fake) -> None:
    """The last committed mark has no booking count (a line written before the count existed)."""
    world.portfolio._booking_boundary = lambda isin, session: None  # type: ignore[method-assign]


def pauses(world: Fake) -> list[str]:
    return [p.event for p in policy.run_state(world.store).open_pauses if p.scope == f"isin:{A}"]


def test_codex_r1_1_a_mark_with_no_booking_count_never_values_above_the_ledger_reading_and_pauses_the_name(tmp_path):
    """Codex probe on the fake ledger: 100 carried at 15000, sell 40 at 100, a pre-admitted buy of 20 books at 101. The
    ledger reading is 60 x 150 + 20 x 101 = 11020; the net-quantity fallback said 12000 and lifted the equity."""
    world = carried_world(tmp_path)
    legacy_mark(world)
    world.account.sell("AAA", 40, "100")
    world.account.buy("AAA", 20, "101")
    after = world.mark(D3, AAA="75")
    value = Decimal(carried_line(world, D3)["carried_value"])
    assert value <= Decimal("11020") and value > 0
    assert after.equity <= world.account.cash + Decimal("11020") and after.peak == Decimal("55000")
    assert "carry_unreconciled" in pauses(world) and world.portfolio.buys_blocked(A)


def test_opus_r1_1_a_bonus_with_no_booking_row_then_a_sell_never_lifts_the_peak(tmp_path):
    """Opus probe B: 100 carried at 15000, a 1:1 bonus booked as a quantity change only (no adjustment record), then 40
    sold at 75. Held 160, cost 8000. The fallback read 15000 + (8000 - 10000) = 13000 and moved the peak."""
    world = carried_world(tmp_path)
    world.account.held["AAA"] = (200, world.account.held["AAA"][1])  # quantity only: no booking, no adjustment record
    world.account.sell("AAA", 40, "75")
    after = world.mark(D3, AAA="75")
    value = Decimal(carried_line(world, D3)["carried_value"])
    assert 0 < value <= Decimal("12000")  # the fair reading (bonus first, then the sell) is 12000
    assert after.peak == Decimal("55000") and after.equity <= world.account.cash + Decimal("12000")
    assert "carry_unreconciled" in pauses(world)


def test_opus_r1_1_an_explicit_adjustment_record_explains_the_extra_shares_without_a_second_pause(tmp_path):
    world = carried_world(tmp_path)
    world.account.adjust("AAA", 200)
    world.account.sell("AAA", 40, "75")
    after = world.mark(D3, AAA="75")
    assert Decimal(carried_line(world, D3)["carried_value"]) <= Decimal("12000")
    assert after.peak == Decimal("55000")
    assert "carry_unreconciled" not in pauses(world)


def test_a_replay_that_misses_the_held_quantity_pauses_and_the_operator_clears_it_with_resume(tmp_path):
    world = carried_world(tmp_path)
    world.account.held["AAA"] = (90, world.account.held["AAA"][1])  # ten shares gone with no booking
    after = world.mark(D3, AAA="75")
    assert Decimal(carried_line(world, D3)["carried_value"]) <= Decimal("13500") and after.peak == Decimal("55000")
    assert "carry_unreconciled" in pauses(world)
    seq = next(p.seq for p in policy.run_state(world.store).open_pauses if p.event == "carry_unreconciled")
    policy.resume(world.store, "operator reviewed the ledger", pause_seqs=[seq])
    assert "carry_unreconciled" not in pauses(world)


def test_a_reconciled_carry_raises_no_unreconciled_pause(tmp_path):
    world = carried_world(tmp_path)
    world.account.sell("AAA", 40, "75")
    world.account.buy("AAA", 20, "101")
    world.mark(D3, AAA="75")
    assert Decimal(carried_line(world, D3)["carried_value"]) == Decimal("11020")
    assert "carry_unreconciled" not in pauses(world)


def test_codex_r1_2_a_sale_consumes_only_the_lots_held_when_it_books_not_a_later_buy(tmp_path):
    """Carry 100 at 10000; buy 20 at 101; sell 110 at 100; buy 20 at 99. The sale takes the carried lot and 10 of the
    first buy; the second buy was not held yet. Remaining: 10 x 101 + 20 x 99 = 2990. Pooling the buys said 3000."""
    world = Fake(tmp_path)
    world.account.buy("AAA", 100, "100")
    world.mark(D1, AAA="100")
    world.portfolio.mark_session(D2, {A: Decimal("50")}, structural_actions={A: "bonus"})
    world.account.buy("AAA", 20, "101")
    world.account.sell("AAA", 110, "100")
    world.account.buy("AAA", 20, "99")
    after = world.mark(D3, AAA="50")
    line = carried_line(world, D3)
    assert (line["carried_quantity"], Decimal(line["carried_value"])) == ("30", Decimal("2990"))
    assert after.equity == world.account.cash + Decimal("2990") and "carry_unreconciled" not in pauses(world)


def test_opus_r1_3_a_buy_charge_is_part_of_the_cost_a_carried_buy_adds_on_the_real_ledger(tmp_path):
    """Codex's probe with a 7.50 charge on the buy: 100 carried at 15000, 40 sold, 20 bought at 101 plus 7.50."""
    rig, store, portfolio = real_world(tmp_path, "RELIANCE")
    try:
        own(rig, "RELIANCE", 100)
        portfolio.mark_session(D1, {R_ISIN: Decimal("150")})
        admission = run(rig.adapter.prepare(buy_intent(rig, "late-buy", "RELIANCE", 20, "101.00"), samples_for("RELIANCE", "101.00")))
        assert admission.decision is AdmissionDecision.ADMITTED, admission.reason_code
        portfolio.mark_session(D2, {R_ISIN: Decimal("75")}, structural_actions={R_ISIN: "bonus"})
        sell_and_fill(rig, "trim-by-hand", "RELIANCE", 40)
        book(rig, "late-buy", 20, "101", charges="7.50", attempt="attempt-late")
        portfolio.mark_session(D3, {R_ISIN: Decimal("75")})
        (line,) = [r for r in store.records() if r["kind"] == "position_mark" and r["as_of_date"] == D3.isoformat() and r["isin"] == R_ISIN]
        assert Decimal(line["carried_value"]) == Decimal("11027.50")
    finally:
        rig.close()


# --- review r2 (Codex r2 + Opus r2 at 553eb8e6): the unreconciled search is bounded, a crashed attempt's boundary is read by session -----------


def spy_on_replays(monkeypatch: pytest.MonkeyPatch, ceiling: int) -> list[int]:
    """Count the replay steps (one booking applied = one step) and stop the row the moment the work passes ``ceiling``,
    so a search that lost its bound turns the row red in milliseconds instead of running for a minute."""

    real = ShadowPortfolio._replay
    steps = [0]

    def counted(base: Any, since: Any, **kwargs: Any):
        steps[0] += len(since) + 1
        assert steps[0] <= ceiling, f"the unreconciled search spent {steps[0]} replay steps, past {ceiling}"
        return real(base, since, **kwargs)

    monkeypatch.setattr(ShadowPortfolio, "_replay", staticmethod(counted))
    return steps


def many_bookings_world(tmp_path: Path, pairs: int) -> Fake:
    """100 carried at 15000 (unit 150); then ``pairs`` times a buy of 2 at 101 and a sell of 1 at 100, so the ledger holds
    100 + pairs shares. Every boundary but the first is short of the held quantity: the worst case for the search."""
    world = carried_world(tmp_path)
    for _ in range(pairs):
        world.account.buy("AAA", 2, "101")
        world.account.sell("AAA", 1, "100")
    return world


@pytest.mark.parametrize("known_count", [False, True], ids=["legacy_line_no_count", "count_known_but_short"])
def test_codex_r2_1_an_800_booking_unreconciled_row_is_valued_in_bounded_work_and_still_pauses(tmp_path, monkeypatch, known_count):
    """Codex r2 P2: 401 and 801 bookings each took over 50 seconds. The work is counted, not timed (timing rows flake
    under xdist): the replays stay inside the budget, the value is at or below a reading the exhaustive search would have
    had (the first boundary, 50500), above zero, and the name still pauses with its buys blocked."""
    world = many_bookings_world(tmp_path, 400)
    if known_count:
        world.portfolio._booking_boundary = lambda isin, session: 1 if session == D2 else None  # type: ignore[method-assign]
        world.account.held["AAA"] = (world.account.held["AAA"][0] + 7, world.account.held["AAA"][1])  # 7 shares no booking explains
    else:
        legacy_mark(world)
    assert len(world.account.bookings) == 801
    steps = spy_on_replays(monkeypatch, portfolio_module.CARRY_SEARCH_BUDGET)
    after = world.mark(D3, AAA="75")
    assert steps[0] <= 801  # at most the one plain replay
    value = Decimal(carried_line(world, D3)["carried_value"])
    assert 0 < value <= Decimal("50500")
    assert after.peak == Decimal("55000") and after.equity <= world.account.cash + Decimal("50500")
    assert "carry_unreconciled" in pauses(world) and world.portfolio.buys_blocked(A)


def test_codex_r2_1_the_floor_is_the_one_pass_answer_when_the_search_would_pass_the_budget(tmp_path, monkeypatch):
    """The legacy line with 801 bookings: the first boundary's reading is 50,500; the floor is the end boundary's
    (the base lot less its one sale): 15000 - 150 = 14850, found with no search at all."""
    world = many_bookings_world(tmp_path, 400)
    legacy_mark(world)
    steps = spy_on_replays(monkeypatch, 0)
    world.mark(D3, AAA="75")
    assert steps[0] == 0
    value = Decimal(carried_line(world, D3)["carried_value"])
    assert Decimal("14850") - Decimal("0.0001") <= value <= Decimal("14850.0001"), value


def small_scenarios() -> list[tuple[int, str, list[tuple[str, int, str]], int]]:
    """(base quantity, base value, bookings, held quantity): hand-picked shapes plus seeded random ones, all small."""
    import random

    rng = random.Random(611)
    out: list[tuple[int, str, list[tuple[str, int, str]], int]] = [
        (100, "15000", [("SELL", 40, "100"), ("BUY", 20, "101")], 80),
        (100, "15000", [("BUY", 20, "101"), ("SELL", 110, "100"), ("BUY", 20, "99")], 30),
        (100, "15000", [("SELL", 40, "75")], 160),
        (100, "15000", [("SELL", 40, "75")], 90),
        (100, "15000", [], 100),
        (100, "15000", [("SELL", 100, "75"), ("BUY", 3, "90")], 3),
    ]
    for _ in range(250):
        n = rng.randint(0, 5)
        rows = [(rng.choice(["BUY", "SELL"]), rng.randint(1, 40), str(rng.randint(60, 220))) for _ in range(n)]
        held = rng.randint(1, 200)
        out.append((rng.randint(1, 150), str(rng.randint(500, 30000)), rows, held))
    return out


@pytest.mark.parametrize("base_bookings", [None, 0], ids=["legacy_count", "known_count"])
def test_codex_r2_1_the_capped_value_is_never_above_the_exhaustive_search_and_never_zero(tmp_path, monkeypatch, base_bookings):
    """The cap path against the exhaustive one over the same ledgers: a budget of zero (floor only) never reads above the
    full search, and never reads zero (``mark_session`` would turn a zero into the ledger cost)."""
    world = Fake(tmp_path)
    holding = Holding(Instrument(A, "AAA", "AAA"), 1, Decimal(0))
    checked = 0
    for quantity, value, rows, held in small_scenarios():
        bookings = [
            SimpleNamespace(
                booking_id=f"b{i}", proposal_id=f"p{i}", side=side, ticker="NSE:CASH:AAA", delta_quantity=Decimal(q),
                price=Decimal(price), charges=Decimal("0.5"),
            )
            for i, (side, q, price) in enumerate(rows)
        ]
        holding = Holding(Instrument(A, "AAA", "AAA"), held, Decimal(held) * 100)
        base = portfolio_module._Carry(Decimal(quantity), Decimal(value), Decimal(quantity) * 100, base_bookings)
        monkeypatch.setattr(portfolio_module, "CARRY_SEARCH_BUDGET", 10**12)
        exhaustive, exhaustive_ok = world.portfolio._carried_value(holding, base, bookings)
        monkeypatch.setattr(portfolio_module, "CARRY_SEARCH_BUDGET", 0)
        capped, capped_ok = world.portfolio._carried_value(holding, base, bookings)
        if exhaustive_ok:
            continue  # reconciled to the ledger: the replay answers, the cap never applies
        assert not capped_ok
        assert 0 < capped <= exhaustive, (quantity, value, rows, held, capped, exhaustive)
        checked += 1
    assert checked > 100


def test_codex_r2_1_a_search_inside_the_budget_is_still_exhaustive(tmp_path, monkeypatch):
    """The r1 probe sits far under the budget: the search still runs (replays are counted) and still reads at or below
    the ledger reading, 11020."""
    world = carried_world(tmp_path)
    legacy_mark(world)
    steps = spy_on_replays(monkeypatch, portfolio_module.CARRY_SEARCH_BUDGET)
    world.account.sell("AAA", 40, "100")
    world.account.buy("AAA", 20, "101")
    world.mark(D3, AAA="75")
    assert steps[0] > 0
    assert 0 < Decimal(carried_line(world, D3)["carried_value"]) <= Decimal("11020")
