"""India SELL admission and halve or flatten batch registration (Phase 63-04, D-06, D-07, T-63-22).

A SELL needs no buying power. It is admitted within the held quantity minus the open sells,
stays admissible while the latches block buys, and is checked again, atomically, when its
approval is claimed. Halve and flatten batches register one proposal per position under one
batch id. No ledger schema change, no real ledger, no broker: tmp dirs, injected clocks,
the paper dispatcher.
"""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

import india_limits_support as ils
from app_context import AppState
from execution import (
    AdmissionDecision,
    ApprovalConflict,
    ApprovalService,
    ExecutionConflictError,
    ExecutionLedger,
    ExecutionService,
    OrderIntent,
    PaperDispatcher,
)
from execution.ledger import SCHEMA_VERSION, IntentConflict, intent_hash
from execution.models import ExecutionAdmission
from risk_india.exits import Position
from venue_seam_testkit import enroll, private_key, sign

SESSION_1 = date(2026, 10, 8)
SESSION_2 = date(2026, 10, 9)
NAMES = {
    "AAA": ("NSE:CASH:AAA", "INE000000011"),
    "BBB": ("NSE:CASH:BBB", "INE000000022"),
    "CCC": ("NSE:CASH:CCC", "INE000000033"),
}


def evidence_for(symbol: str):
    ticker, isin = NAMES[symbol]
    return ils.make_evidence(
        stock_code=symbol,
        isin=isin,
        ltp=Decimal("10.00"),
        lower_circuit=Decimal("9.00"),
        upper_circuit=Decimal("11.00"),
        previous_close=Decimal("10.00"),
        tick_reference=Decimal("10.00"),
        bid=Decimal("10.00"),
        ask=Decimal("10.00"),
    )


@pytest.fixture
def world(tmp_path):
    private = ils.india_private_dir(tmp_path, capital_cap="100000.00", per_position_cap="100000.00")
    ledger = ils.open_ledger(tmp_path)
    guard = ils.make_guard(ledger, private)
    approval = ApprovalService(ledger)
    key = private_key()
    enroll(approval, key, workspace="india")
    service = ExecutionService(
        PaperDispatcher(), ledger, require_approval=True, approval_service=approval, india_guard=guard
    )
    try:
        yield SimpleNamespace(
            ledger=ledger, guard=guard, service=service, approval=approval, key=key, private=private
        )
    finally:
        ledger.close()


def sell(world, proposal_id, quantity, *, symbol="AAA", fill="10.00", scaled=None):
    ticker, _isin = NAMES[symbol]
    intent = ils.make_intent(
        proposal_id, side="SELL", quantity=quantity, limit_price="10.00", ticker=ticker
    )
    return world.service.admit(
        intent,
        currency="INR",
        price="10.00",
        simulator_evidence={"simulated_fill_price": fill},
        risk_evidence={"scaled_size": str(scaled if scaled is not None else quantity)},
        india_quote=evidence_for(symbol),
    )


def hold(world, symbol, quantity, cost):
    ils.seed_position(world.ledger, NAMES[symbol][0], quantity, cost, guard=world.guard)


async def approve(world, proposal_id):
    challenge = world.service.create_approval_challenge(proposal_id, workspace="india")
    signature = sign(world.key, challenge.signed_payload)
    return await world.service.approve_signed(
        proposal_id, challenge.challenge_id, signature, workspace="india"
    )


# ------------------------------------------------------------------ admission


def test_a_sell_within_the_held_quantity_is_admitted_without_a_reservation(world):
    hold(world, "AAA", 7, "70.00")
    admission = sell(world, "s-7", 7)
    assert admission.decision is AdmissionDecision.ADMITTED
    world.service.reserve("s-7")  # a no-op for an India paper SELL, never buying power
    assert world.ledger.get_reservation("s-7") is None
    budget = world.ledger.get_paper_budget("paper", "INR", workspace="india")
    assert budget.reserved == 0 and budget.consumed == 0


def test_one_share_over_the_holding_is_denied_sell_exceeds_holding(world):
    hold(world, "AAA", 7, "70.00")
    over = sell(world, "s-8", 8)
    assert over.decision is AdmissionDecision.DENIED and over.reason_code == "sell_exceeds_holding"
    assert world.ledger.get_order("s-8").state == "REJECTED"
    assert sell(world, "s-7", 7).decision is AdmissionDecision.ADMITTED


def test_open_sells_reduce_what_can_be_sold(world):
    hold(world, "AAA", 7, "70.00")
    assert sell(world, "s-4", 4).decision is AdmissionDecision.ADMITTED
    assert sell(world, "s-4b", 4).reason_code == "sell_exceeds_holding"  # 7 - 4 = 3 left
    assert sell(world, "s-3", 3).decision is AdmissionDecision.ADMITTED
    assert sell(world, "s-1", 1).reason_code == "sell_exceeds_holding"
    world.ledger.reject("s-4", "operator")  # a rejected sell frees its shares
    assert sell(world, "s-2", 2).decision is AdmissionDecision.ADMITTED


def test_a_sell_with_nothing_held_is_denied(world):
    world.guard.store.load()
    assert sell(world, "s-none", 1).reason_code == "sell_exceeds_holding"


def test_sells_stay_admissible_while_halted_and_ended_and_buys_are_denied(world):
    hold(world, "AAA", 7, "70.00")
    world.guard.store.evaluate_session({}, SESSION_1, cash=Decimal("90000"), positions=[])  # -10%: halt
    assert world.guard.store.load().halt
    assert sell(world, "s-halt", 3).decision is AdmissionDecision.ADMITTED
    buy = ils.admit(world.service, ils.make_intent("b-halt", limit_price="10.00", ticker=NAMES["AAA"][0]), evidence=evidence_for("AAA"), fill="10.00")
    assert buy.reason_code == "halt_latch"
    world.guard.store.evaluate_session({}, SESSION_2, cash=Decimal("80000"), positions=[])  # -20%: ended
    assert world.guard.store.load().ended
    assert sell(world, "s-end", 2).decision is AdmissionDecision.ADMITTED
    buy2 = ils.admit(world.service, ils.make_intent("b-end", limit_price="10.00", ticker=NAMES["AAA"][0]), evidence=evidence_for("AAA"), fill="10.00")
    assert buy2.reason_code == "pilot_ended"


def test_an_unreadable_latch_file_never_traps_a_position(world):
    hold(world, "AAA", 7, "70.00")
    ils.seed_position(world.ledger, NAMES["BBB"][0], 1, "10.00")  # fills exist
    from risk_india.state import state_path_for

    state_path_for(world.ledger.path).unlink()
    assert sell(world, "s-free", 3).decision is AdmissionDecision.ADMITTED
    buy = ils.admit(world.service, ils.make_intent("b-trapped", limit_price="10.00", ticker=NAMES["AAA"][0]), evidence=evidence_for("AAA"), fill="10.00")
    assert buy.reason_code == "state_unreadable"


def test_a_sell_still_obeys_collar_band_tick_session_and_quote(world):
    hold(world, "AAA", 7, "70.00")
    ticker, _ = NAMES["AAA"]
    wide = ils.make_intent("s-wide", side="SELL", quantity=1, limit_price="10.25", ticker=ticker)
    denied = world.service.admit(
        wide, currency="INR", price="10.25",
        simulator_evidence={"simulated_fill_price": "10.00"}, risk_evidence={"scaled_size": "1"},
        india_quote=evidence_for("AAA"),
    )
    assert denied.reason_code == "collar"
    no_quote = world.service.admit(
        ils.make_intent("s-noq", side="SELL", quantity=1, limit_price="10.00", ticker=ticker),
        currency="INR", price="10.00",
        simulator_evidence={"simulated_fill_price": "10.00"}, risk_evidence={"scaled_size": "1"},
    )
    assert no_quote.reason_code == "quote_unavailable"


def test_regime_scaling_never_shrinks_a_sell_but_a_veto_still_denies(world):
    hold(world, "AAA", 7, "70.00")
    scaled_down = sell(world, "s-scaled", 5, scaled=1)  # the gate would have sized it to 1
    assert scaled_down.decision is AdmissionDecision.ADMITTED
    assert scaled_down.final_quantity == Decimal("5")
    ticker, _ = NAMES["AAA"]
    vetoed = world.service.admit(
        ils.make_intent("s-veto", side="SELL", quantity=1, limit_price="10.00", ticker=ticker),
        currency="INR", price="10.00",
        simulator_evidence={"simulated_fill_price": "10.00"},
        risk_evidence={"scaled_size": "1", "allowed": False},
        india_quote=evidence_for("AAA"),
    )
    assert vetoed.decision is AdmissionDecision.DENIED
    zero = world.service.admit(
        ils.make_intent("s-zero", side="SELL", quantity=1, limit_price="10.00", ticker=ticker),
        currency="INR", price="10.00",
        simulator_evidence={"simulated_fill_price": "10.00"}, risk_evidence={"scaled_size": "0"},
        india_quote=evidence_for("AAA"),
    )
    assert zero.decision is AdmissionDecision.DENIED
    # BUY sizing is unchanged: the gate may still scale a BUY down.
    buy = ils.admit(
        world.service,
        ils.make_intent("b-scaled", quantity=5, limit_price="10.00", ticker=ticker),
        evidence=evidence_for("AAA"),
        fill="10.00",
    )
    assert buy.decision is AdmissionDecision.ADMITTED and buy.final_quantity == Decimal("5")


# ------------------------------------------------------------- challenge and claim


@pytest.mark.asyncio
async def test_an_admitted_sell_gets_a_challenge_and_dispatches_once(world):
    hold(world, "AAA", 7, "70.00")
    assert sell(world, "s-go", 3).decision is AdmissionDecision.ADMITTED
    ack = await approve(world, "s-go")
    assert ack.status == "ACKNOWLEDGED"
    assert world.ledger.get_order("s-go").state == "ACKNOWLEDGED"
    assert len(world.ledger.list_attempts("s-go")) == 1


def test_a_denied_sell_gets_no_challenge(world):
    hold(world, "AAA", 7, "70.00")
    assert sell(world, "s-no", 8).decision is AdmissionDecision.DENIED
    with pytest.raises(ApprovalConflict):
        world.service.create_approval_challenge("s-no", workspace="india")
    assert world.ledger.approval_evidence_count("s-no") == 0


def test_the_position_is_checked_again_at_challenge_time(world):
    hold(world, "AAA", 7, "70.00")
    assert sell(world, "s-late", 5).decision is AdmissionDecision.ADMITTED
    ils.seed_position(world.ledger, NAMES["AAA"][0], 3, "30.00")  # the holding shrank since admission
    with pytest.raises(ApprovalConflict, match="sell_exceeds_holding"):
        world.service.create_approval_challenge("s-late", workspace="india")


def _slip_in_a_second_admitted_sell(world, proposal_id: str, first_id: str, quantity: int):
    """What a race looks like: a second SELL admitted before the first was claimed.

    Sequential admission counts pending sells, so it cannot produce this on its own. The row
    is written straight to the ledger, as a second process that read the position at the same
    instant would have written it.
    """
    ticker, _ = NAMES["AAA"]
    intent = ils.make_intent(proposal_id, side="SELL", quantity=quantity, limit_price="10.00", ticker=ticker)
    world.ledger.register_intent(intent)
    first = world.ledger.get_admission(first_id)
    forged = ExecutionAdmission(
        **{**first.model_dump(), "proposal_id": proposal_id, "intent_hash": intent_hash(intent)}
    )
    world.ledger.record_admission(intent, forged)


def test_two_approvals_cannot_oversell_the_same_shares(world):
    hold(world, "AAA", 5, "50.00")
    assert sell(world, "s-first", 5).decision is AdmissionDecision.ADMITTED
    _slip_in_a_second_admitted_sell(world, "s-second", "s-first", 5)
    assert world.ledger.get_admission("s-second").decision is AdmissionDecision.ADMITTED
    first = world.service.create_approval_challenge("s-first", workspace="india")
    second = world.service.create_approval_challenge("s-second", workspace="india")
    claim = world.approval.approve_signed(
        "s-first", first.challenge_id, sign(world.key, first.signed_payload), workspace="india"
    )
    assert claim.status.value == "CLAIMED"
    with pytest.raises(ApprovalConflict, match="sell_exceeds_holding"):
        world.approval.approve_signed(
            "s-second", second.challenge_id, sign(world.key, second.signed_payload), workspace="india"
        )
    assert world.ledger.get_order("s-second").state == "PENDING"
    assert world.ledger.approval_evidence_count("s-second") == 0


def test_concurrent_claims_let_exactly_one_sell_through(world):
    hold(world, "AAA", 5, "50.00")
    assert sell(world, "c-first", 5).decision is AdmissionDecision.ADMITTED
    _slip_in_a_second_admitted_sell(world, "c-second", "c-first", 5)
    prepared = {}
    for pid in ("c-first", "c-second"):
        challenge = world.service.create_approval_challenge(pid, workspace="india")
        prepared[pid] = (challenge.challenge_id, sign(world.key, challenge.signed_payload))

    def claim(pid):
        challenge_id, signature = prepared[pid]
        try:
            world.approval.approve_signed(pid, challenge_id, signature, workspace="india")
            return "claimed"
        except ApprovalConflict:
            return "refused"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(claim, ("c-first", "c-second")))
    assert sorted(outcomes) == ["claimed", "refused"]
    states = {pid: world.ledger.get_order(pid).state for pid in ("c-first", "c-second")}
    assert sorted(states.values()) == ["PENDING", "SUBMITTING"]


@pytest.mark.asyncio
async def test_a_claimed_sell_keeps_its_shares_out_of_the_next_admission(world):
    hold(world, "AAA", 7, "70.00")
    assert sell(world, "k-1", 4).decision is AdmissionDecision.ADMITTED
    await approve(world, "k-1")  # ACKNOWLEDGED, still open (no fill evidence in 63)
    assert sell(world, "k-2", 4).reason_code == "sell_exceeds_holding"
    assert sell(world, "k-3", 3).decision is AdmissionDecision.ADMITTED


def test_the_paper_ledger_cannot_reconcile_an_india_sell_so_nothing_is_invented(world):
    # SELL fills are not booked in the Mac paper ledger (follow-up); the call refuses.
    hold(world, "AAA", 7, "70.00")
    sell(world, "r-1", 3)
    from execution.models import ReconciliationSnapshot, ReconciliationStatus
    from datetime import datetime, timezone

    with pytest.raises(Exception):
        world.service.reconcile(
            ReconciliationSnapshot(
                proposal_id="r-1", broker_order_id="b", source="t", cumulative_quantity=Decimal(1),
                cumulative_notional=Decimal(10), status=ReconciliationStatus.PARTIALLY_FILLED,
                evidence_fingerprint="f", observed_at=datetime.now(timezone.utc),
            )
        )
    assert world.ledger.get_paper_position("paper", "INR", NAMES["AAA"][0], workspace="india")["quantity"] == "7"


# --------------------------------------------------------- no schema change, batch tags

_BASE_TABLES = {
    "order_intents", "order_projection", "dispatch_attempts", "approval_keys", "approval_challenges",
    "execution_approvals", "execution_events", "execution_admissions", "paper_budgets",
    "buying_power_reservations", "workspace_controls", "workspace_control_events",
    "reconciliation_evidence", "paper_positions", "requote_intents", "requote_events",
    "ledger_identity",
}


def test_there_is_no_ledger_schema_change(world):
    assert SCHEMA_VERSION == 6
    connection = sqlite3.connect(world.ledger.path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    finally:
        connection.close()
    assert tables == _BASE_TABLES


def test_an_exit_batch_tag_is_idempotent_and_never_rewritten(world):
    hold(world, "AAA", 7, "70.00")
    sell(world, "t-1", 3)
    world.ledger.record_exit_batch("t-1", "halve-20261008-abcdef012345", "halve")
    world.ledger.record_exit_batch("t-1", "halve-20261008-abcdef012345", "halve")
    assert world.ledger.get_exit_batch("t-1") == {"batch_id": "halve-20261008-abcdef012345", "reason": "halve"}
    with pytest.raises(IntentConflict):
        world.ledger.record_exit_batch("t-1", "flatten-20261008-abcdef012345", "flatten")
    with pytest.raises(ValueError):
        world.ledger.record_exit_batch("t-1", "x", "halve")
    assert world.ledger.get_exit_batch("missing") is None


# ------------------------------------------------------- halve and flatten batches


@pytest.fixture
def app(tmp_path):
    private = ils.india_private_dir(tmp_path, capital_cap="100000.00", per_position_cap="100000.00")
    state = AppState()
    assert state.start_execution(
        tmp_path / "india.sqlite3", workspace="india", private_dir=private, india_clock=lambda: ils.NOW
    )
    guard = state.execution_service.india_guard
    for symbol, quantity in (("AAA", 7), ("BBB", 4), ("CCC", 1)):
        ils.seed_position(
            state._execution_ledger, NAMES[symbol][0], quantity, f"{quantity * 10}.00", guard=guard
        )
    try:
        yield state
    finally:
        state.close_execution()


MARKS = {NAMES[s][0]: Decimal("10.00") for s in NAMES}  # close at cost: no stop
PRICES = {NAMES[s][0]: Decimal("10.00") for s in NAMES}


def test_a_halt_registers_halve_sells_of_3_and_2_and_none_for_the_one_share(app):
    result = app.evaluate_india_session(MARKS, SESSION_1, cash=Decimal("90000"))  # ~ -9.9%: halt only
    assert result.state.halt and not result.state.ended
    assert [b.reason for b in result.batches] == ["halve"]
    proposals = app.register_india_exit_batches(result.batches, limit_prices=PRICES)
    by_ticker = {p["ticker"]: p for p in proposals}
    assert {t: p["quantity"] for t, p in by_ticker.items()} == {NAMES["AAA"][0]: "3", NAMES["BBB"][0]: "2"}
    assert NAMES["CCC"][0] not in by_ticker  # floor(1 / 2) = 0: no sell
    assert {p["action"] for p in proposals} == {"SELL"}
    batch_ids = {p["batch_id"] for p in proposals}
    assert len(batch_ids) == 1 and batch_ids == {result.batches[0].batch_id}
    assert {p["reason"] for p in proposals} == {"halve"}
    # The tag is returned with the proposal and is not part of the immutable intent.
    ledger = app._execution_ledger
    for proposal in proposals:
        stored = ledger.get_order(proposal["proposal_id"])
        assert "batch_id" not in stored.intent and "reason" not in stored.intent
        assert stored.intent_hash == intent_hash(OrderIntent.model_validate(dict(stored.intent)))
        again = app.execution_service.get_proposal(proposal["proposal_id"])
        assert (again["batch_id"], again["reason"]) == (result.batches[0].batch_id, "halve")
        assert again["status"] == "PENDING"


def test_registering_the_same_batch_twice_changes_nothing(app):
    result = app.evaluate_india_session(MARKS, SESSION_1, cash=Decimal("90000"))
    first = app.register_india_exit_batches(result.batches, limit_prices=PRICES)
    second = app.register_india_exit_batches(result.batches, limit_prices=PRICES)
    assert [p["proposal_id"] for p in first] == [p["proposal_id"] for p in second]
    assert len(app._execution_ledger.list_orders()) == 2
    assert len([e for e in app._execution_ledger.list_events() if e.event_type == "EXIT_BATCH_REGISTERED"]) == 2


def test_a_flatten_registers_every_full_position_under_one_batch(app):
    result = app.evaluate_india_session(MARKS, SESSION_1, cash=Decimal("80000"))  # ~ -19.9%: ended
    assert result.state.ended
    assert [b.reason for b in result.batches] == ["flatten"]  # and no halve batch beside it
    proposals = app.register_india_exit_batches(result.batches, limit_prices=PRICES)
    assert {p["ticker"]: p["quantity"] for p in proposals} == {
        NAMES["AAA"][0]: "7", NAMES["BBB"][0]: "4", NAMES["CCC"][0]: "1",
    }
    assert len({p["batch_id"] for p in proposals}) == 1
    assert {p["reason"] for p in proposals} == {"flatten"}


def test_a_missing_limit_price_registers_nothing(app):
    result = app.evaluate_india_session(MARKS, SESSION_1, cash=Decimal("90000"))
    partial = {NAMES["AAA"][0]: Decimal("10.00")}
    with pytest.raises(ValueError):
        app.register_india_exit_batches(result.batches, limit_prices=partial)
    with pytest.raises(ValueError):
        app.register_india_exit_batches(
            result.batches, limit_prices={**PRICES, NAMES["BBB"][0]: Decimal("0")}
        )
    assert app._execution_ledger.list_orders() == []


def test_an_unfilled_halve_is_rebuilt_for_the_next_session_with_a_new_batch(app):
    first = app.evaluate_india_session(MARKS, SESSION_1, cash=Decimal("90000"))
    pending = app.pending_india_exit_batches(SESSION_2)
    assert [b.reason for b in pending] == ["halve"]
    assert pending[0].batch_id != first.batches[0].batch_id  # a new decision date, a new batch
    assert sorted(i.quantity for i in pending[0].intents) == [2, 3]


@pytest.mark.asyncio
async def test_each_registered_sell_is_admitted_and_approved_on_its_own_signature(app):
    from datetime import timezone

    from market_data import IndiaInstrument, TopOfBook

    result = app.evaluate_india_session(MARKS, SESSION_1, cash=Decimal("90000"))
    proposals = app.register_india_exit_batches(result.batches, limit_prices=PRICES)
    instruments = {symbol: IndiaInstrument(symbol=symbol) for symbol in ("AAA", "BBB")}
    now = datetime.now(timezone.utc)
    await app.start_market_data_replay(
        tuple(instruments.values()),
        tuple(
            TopOfBook(
                instrument=instrument, source="local-replay", bid="9.99", ask="10.01",
                observed_at=now, received_at=now, sequence=sequence,
            )
            for instrument in instruments.values()
            for sequence in (1, 2, 3)
        ),
    )
    service = app.execution_service
    key = private_key()
    enroll(service._approval_service, key, workspace="india")
    signatures = []
    try:
        for proposal in proposals:
            symbol = proposal["ticker"].rsplit(":", 1)[-1]
            # The real path: regime classifier, simulator, risk gate and the India limits.
            admission = app.admit_india_paper_proposal(
                app.get_trade_proposal(proposal["proposal_id"]),
                instrument=instruments[symbol],
                portfolio_state={"equity": 100000.0, "peak_equity": 100000.0},
                quote=evidence_for(symbol),
            )
            assert admission.decision is AdmissionDecision.ADMITTED, admission.reason_code
            assert admission.final_quantity == Decimal(proposal["quantity"])
            challenge = service.create_approval_challenge(proposal["proposal_id"], workspace="india")
            signature = sign(key, challenge.signed_payload)
            signatures.append(signature)
            await service.approve_signed(
                proposal["proposal_id"], challenge.challenge_id, signature, workspace="india"
            )
    finally:
        await app.close_market_data()
    assert len(set(signatures)) == len(proposals) == 2  # one signature and one challenge each
    states = [app._execution_ledger.get_order(p["proposal_id"]).state for p in proposals]
    assert states == ["ACKNOWLEDGED"] * 2


def test_the_guard_less_india_ledger_still_denies_a_sell(tmp_path):
    ledger = ils.open_ledger(tmp_path)
    try:
        service = ExecutionService(PaperDispatcher(), ledger)
        ils.seed_position(ledger, NAMES["AAA"][0], 5, "50.00")
        ticker, _ = NAMES["AAA"]
        admission = service.admit(
            ils.make_intent("g-1", side="SELL", quantity=1, limit_price="10.00", ticker=ticker),
            currency="INR", price="10.00",
            simulator_evidence={"simulated_fill_price": "10.00"}, risk_evidence={"scaled_size": "1"},
        )
        assert admission.decision is AdmissionDecision.DENIED
        assert admission.reason_code == "SELL_ADMISSION_REQUIRES_A_POSITION_RESERVATION"
    finally:
        ledger.close()
