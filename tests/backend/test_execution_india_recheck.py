"""Fix round 1 for 63-04: atomic caps, approval rechecks, no-guard denial, fill evidence.

C1  two admissions can both pass the cap check before either reserves; the reservation
    transaction itself enforces both caps at limit-price notional.
C2  a pending India BUY is re-validated (latches, state file, session cutoff, quote age, caps
    without its own reservation) at challenge and at claim; a failed recheck releases it.
C3  an India admission with no guard is denied whatever else the service was built with.
O3  fill evidence alone (no position row) counts as a fill for the latch file.

Synthetic values, injected clock, tmp dirs, the paper dispatcher. No broker, no real ledger.
"""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

import india_limits_support as ils
from execution import (
    AdmissionDecision,
    ApprovalConflict,
    ApprovalService,
    ExecutionConflictError,
    ExecutionService,
    PaperDispatcher,
)
from execution.ledger import IndiaCapExceeded
from risk_india import rules
from risk_india.exits import Position
from risk_india.state import state_path_for
from venue_seam_testkit import enroll, private_key, sign

TCS = "NSE:CASH:TCS"
TCS_ISIN = "INE467B01029"


class CountingPaper(PaperDispatcher):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def dispatch(self, intent):
        self.calls += 1
        return await super().dispatch(intent)


@pytest.fixture
def world(tmp_path):
    private = ils.india_private_dir(tmp_path)  # capital cap 1000.00, per-position cap 600.00
    ledger = ils.open_ledger(tmp_path)
    clock = ils.MutableClock()
    guard = ils.make_guard(ledger, private, now=clock)
    approval = ApprovalService(ledger)
    key = private_key()
    enroll(approval, key, workspace="india")
    dispatcher = CountingPaper()
    service = ExecutionService(
        dispatcher, ledger, require_approval=True, approval_service=approval, india_guard=guard
    )
    try:
        yield SimpleNamespace(
            ledger=ledger, guard=guard, service=service, approval=approval, key=key,
            clock=clock, dispatcher=dispatcher, private=private,
        )
    finally:
        ledger.close()


def tcs(**overrides):
    return ils.make_evidence(stock_code="TCS", isin=TCS_ISIN, **overrides)


def admit_only(world, proposal_id, quantity, *, ticker=ils.TICKER, evidence=None, price=None, limit="100.00"):
    intent = ils.make_intent(proposal_id, quantity=quantity, ticker=ticker, limit_price=limit)
    admission = ils.admit(world.service, intent, evidence=evidence, price=price)
    assert admission.decision is AdmissionDecision.ADMITTED, admission.reason_code
    return admission


def reserved(world, proposal_id) -> bool:
    row = world.ledger.get_reservation(proposal_id)
    return row is not None and row.state == "ACTIVE"


def released(world, proposal_id):
    assert world.ledger.get_order(proposal_id).state == "REJECTED"
    row = world.ledger.get_reservation(proposal_id)
    assert row is not None and row.state == "SETTLED" and row.outstanding == 0


def fresh():
    return ils.make_evidence()


# ----------------------------------------------------- C1: caps inside the reservation


def test_two_same_position_buys_that_both_passed_admission_cannot_both_reserve(world):
    admit_only(world, "a", 6)  # 600.00 on a 600.00 per-position cap
    admit_only(world, "b", 6)  # admitted before "a" reserved: the admission check saw nothing
    world.service.reserve("a")
    with pytest.raises(IndiaCapExceeded) as lost:
        world.service.reserve("b")
    assert lost.value.code == "capital_cap"  # 1,200 against 1,000 (and 1,200 against 600)
    assert world.ledger.get_reservation("b") is None
    assert world.ledger.get_order("b").state == "REJECTED"
    assert world.ledger.get_paper_budget("paper", "INR", workspace="india").reserved == Decimal("600.00")
    with pytest.raises(ApprovalConflict):
        world.service.create_approval_challenge("b", workspace="india", india_quote=fresh())


def test_the_per_position_cap_alone_is_enforced_at_reservation(world):
    admit_only(world, "a", 4)
    admit_only(world, "b", 4)  # 800 total fits the 1,000 capital cap, not the 600 position cap
    world.service.reserve("a")
    with pytest.raises(IndiaCapExceeded) as lost:
        world.service.reserve("b")
    assert lost.value.code == "per_position_cap"
    assert world.ledger.get_reservation("b") is None


def test_two_buys_on_different_names_cannot_break_the_capital_cap(world):
    admit_only(world, "a", 6)
    admit_only(world, "b", 5, ticker=TCS, evidence=tcs())  # 600 + 500 = 1100 > 1000
    world.service.reserve("a")
    with pytest.raises(IndiaCapExceeded) as lost:
        world.service.reserve("b")
    assert lost.value.code == "capital_cap"
    assert world.ledger.get_reservation("b") is None
    # Exactly the cap still reserves: 600 + 400 = 1000.
    admit_only(world, "c", 4, ticker=TCS, evidence=tcs())
    world.service.reserve("c")
    assert reserved(world, "c")


def test_the_reservation_check_counts_limit_price_notional_not_the_admission_price(world):
    # The admission price (a mid) is half the limit. At the mid the new order is 250 and the
    # total 850, inside the 1,000 cap. At its limit price the order is 500 and the total 1,100.
    admit_only(world, "a", 6, price="50")
    admit_only(world, "b", 5, ticker=TCS, evidence=tcs(), price="50")
    world.service.reserve("a")
    with pytest.raises(IndiaCapExceeded) as lost:
        world.service.reserve("b")
    assert lost.value.code == "capital_cap"


def test_concurrent_reservations_let_exactly_one_through(world):
    for name in ("a", "b"):
        admit_only(world, name, 6)
    outcomes = []
    barrier = threading.Barrier(2)

    def attempt(name):
        barrier.wait()
        try:
            world.service.reserve(name)
            outcomes.append("reserved")
        except IndiaCapExceeded as exc:
            outcomes.append(exc.code)

    threads = [threading.Thread(target=attempt, args=(name,)) for name in ("a", "b")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["capital_cap", "reserved"]
    assert world.ledger.get_paper_budget("paper", "INR", workspace="india").reserved == Decimal("600.00")


def test_the_india_ledger_refuses_a_reservation_without_its_caps(world):
    admit_only(world, "a", 1)
    with pytest.raises(ApprovalConflict, match="india limits are required"):
        world.ledger.reserve_buying_power("a")
    assert world.ledger.get_reservation("a") is None


def test_a_filled_position_and_open_reservations_both_count(world):
    ils.seed_position(world.ledger, ils.TICKER, 3, "300.00", guard=world.guard)
    admit_only(world, "a", 3)  # 300 held + 300 = 600: exactly the per-position cap
    admit_only(world, "b", 1)
    world.service.reserve("a")
    with pytest.raises(IndiaCapExceeded) as lost:
        world.service.reserve("b")
    assert lost.value.code == "per_position_cap"


# ------------------------------------------------- C2: recheck at challenge and at claim


def latch_halt(world):
    world.guard.store.evaluate_session({}, ils.SESSION, cash=Decimal("900"), positions=[])


def latch_ended(world):
    world.guard.store.evaluate_session({}, ils.SESSION, cash=Decimal("800"), positions=[])


def latch_stop(world):
    ils.seed_position(world.ledger, TCS, 1, "100.00", guard=world.guard)
    world.guard.store.evaluate_session(
        {TCS: Decimal("80")}, ils.SESSION, cash=Decimal("900"),
        positions=[Position(TCS, "TCS", 1, Decimal("100.00"))],
    )


def latch_unreadable(world):
    ils.seed_position(world.ledger, TCS, 1, "100.00", guard=world.guard)
    state_path_for(world.ledger.path).unlink()


CHANGES = [
    ("halt_latch", latch_halt),
    ("pilot_ended", latch_ended),
    ("stop_open", latch_stop),
    ("state_unreadable", latch_unreadable),
]


@pytest.mark.parametrize("code,change", CHANGES, ids=[c for c, _ in CHANGES])
def test_a_fresh_challenge_is_refused_after_the_latches_changed(world, code, change):
    admit_only(world, "a", 2)
    world.service.reserve("a")
    change(world)
    with pytest.raises(ApprovalConflict) as refused:
        world.service.create_approval_challenge("a", workspace="india", india_quote=fresh())
    assert str(refused.value) == code
    released(world, "a")
    assert world.ledger.get_paper_budget("paper", "INR", workspace="india").reserved == 0
    assert world.ledger.approval_evidence_count("a") == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("code,change", CHANGES, ids=[c for c, _ in CHANGES])
async def test_a_signed_claim_is_refused_after_the_latches_changed(world, code, change):
    admit_only(world, "a", 2)
    world.service.reserve("a")
    challenge = world.service.create_approval_challenge("a", workspace="india", india_quote=fresh())
    signature = sign(world.key, challenge.signed_payload)
    change(world)
    with pytest.raises(ExecutionConflictError) as refused:
        await world.service.approve_signed(
            "a", challenge.challenge_id, signature, workspace="india", india_quote=fresh()
        )
    assert str(refused.value) == code
    assert world.dispatcher.calls == 0 and world.ledger.list_attempts("a") == []
    released(world, "a")


def test_a_challenge_after_the_1510_cutoff_is_refused(world):
    admit_only(world, "a", 2)
    world.service.reserve("a")
    world.clock.now = datetime(2026, 10, 8, 15, 10, 0, tzinfo=rules.IST)
    with pytest.raises(ApprovalConflict) as refused:
        world.service.create_approval_challenge(
            "a", workspace="india", india_quote=ils.make_evidence(observed_at=world.clock.now)
        )
    assert str(refused.value) == "session_closed"
    released(world, "a")


@pytest.mark.asyncio
async def test_a_claim_after_the_1510_cutoff_is_refused(world):
    admit_only(world, "a", 2)
    world.service.reserve("a")
    challenge = world.service.create_approval_challenge("a", workspace="india", india_quote=fresh())
    signature = sign(world.key, challenge.signed_payload)
    world.clock.now = datetime(2026, 10, 8, 15, 10, 0, tzinfo=rules.IST)
    with pytest.raises(ExecutionConflictError) as refused:
        await world.service.approve_signed(
            "a", challenge.challenge_id, signature, workspace="india",
            india_quote=ils.make_evidence(observed_at=world.clock.now),
        )
    assert str(refused.value) == "session_closed"
    assert world.dispatcher.calls == 0
    released(world, "a")


@pytest.mark.parametrize("age,ok", [(0, True), (30, True), (31, False), (-1, False)])
def test_the_challenge_needs_a_quote_no_older_than_30_seconds(world, age, ok):
    admit_only(world, "a", 2)
    world.service.reserve("a")
    quote = ils.make_evidence(observed_at=ils.NOW - timedelta(seconds=age))
    if ok:
        assert world.service.create_approval_challenge("a", workspace="india", india_quote=quote)
    else:
        with pytest.raises(ApprovalConflict) as refused:
            world.service.create_approval_challenge("a", workspace="india", india_quote=quote)
        assert str(refused.value) == "quote_unavailable"
        released(world, "a")


def test_the_challenge_without_any_quote_is_refused_and_releases(world):
    admit_only(world, "a", 2)
    world.service.reserve("a")
    with pytest.raises(ApprovalConflict) as refused:
        world.service.create_approval_challenge("a", workspace="india")
    assert str(refused.value) == "quote_unavailable"
    released(world, "a")


@pytest.mark.asyncio
async def test_a_stale_or_missing_quote_at_claim_is_refused(world):
    for name, quote in (("a", None), ("b", ils.make_evidence(observed_at=ils.NOW - timedelta(seconds=31)))):
        admit_only(world, name, 1)
        world.service.reserve(name)
        challenge = world.service.create_approval_challenge(name, workspace="india", india_quote=fresh())
        signature = sign(world.key, challenge.signed_payload)
        with pytest.raises(ExecutionConflictError) as refused:
            await world.service.approve_signed(
                name, challenge.challenge_id, signature, workspace="india", india_quote=quote
            )
        assert str(refused.value) == "quote_unavailable"
        released(world, name)
    assert world.dispatcher.calls == 0


@pytest.mark.asyncio
async def test_a_still_valid_buy_goes_through_and_a_replay_needs_no_new_quote(world):
    admit_only(world, "a", 2)
    world.service.reserve("a")
    challenge = world.service.create_approval_challenge("a", workspace="india", india_quote=fresh())
    signature = sign(world.key, challenge.signed_payload)
    ack = await world.service.approve_signed(
        "a", challenge.challenge_id, signature, workspace="india", india_quote=fresh()
    )
    assert ack.status == "ACKNOWLEDGED" and world.dispatcher.calls == 1
    again = await world.service.approve_signed(
        "a", challenge.challenge_id, signature, workspace="india"
    )
    assert again.idempotent_replay and world.dispatcher.calls == 1


def test_the_recheck_leaves_the_orders_own_reservation_out_of_the_caps(world):
    admit_only(world, "a", 6)  # 600.00: exactly the per-position cap, reserved
    world.service.reserve("a")
    assert world.service.create_approval_challenge("a", workspace="india", india_quote=fresh())


def test_the_recheck_still_applies_the_caps_to_what_others_hold(world):
    admit_only(world, "a", 4)
    world.service.reserve("a")
    # A fill the admission never saw takes room: 300 held + this order's 400 > 600.
    ils.seed_position(world.ledger, ils.TICKER, 3, "300.00", guard=world.guard)
    with pytest.raises(ApprovalConflict) as refused:
        world.service.create_approval_challenge("a", workspace="india", india_quote=fresh())
    assert str(refused.value) == "per_position_cap"
    released(world, "a")


@pytest.mark.asyncio
async def test_sells_stay_approvable_while_halted_and_need_no_quote(world):
    ils.seed_position(world.ledger, ils.TICKER, 7, "700.00", guard=world.guard)
    latch_halt(world)
    sell = ils.make_intent("s", side="SELL", quantity=3, limit_price="100.00")
    assert ils.admit(world.service, sell).decision is AdmissionDecision.ADMITTED
    challenge = world.service.create_approval_challenge("s", workspace="india")
    ack = await world.service.approve_signed(
        "s", challenge.challenge_id, sign(world.key, challenge.signed_payload), workspace="india"
    )
    assert ack.status == "ACKNOWLEDGED"


# -------------------------------------------------------- C3: no guard, no India admission


def test_the_default_constructor_denies_every_india_admission(tmp_path):
    ledger = ils.open_ledger(tmp_path)
    try:
        service = ExecutionService(PaperDispatcher(), ledger)  # no flags, no guard
        admission = service.admit(
            ils.make_intent("plain"),
            currency="INR",
            price="100",
            simulator_evidence={"simulated_fill_price": "100"},
            risk_evidence={"scaled_size": "1"},
        )
        assert admission.decision is AdmissionDecision.DENIED
        assert admission.reason_code == "india_limits_unavailable"
        assert ledger.get_reservation("plain") is None
        with pytest.raises(ApprovalConflict):
            service.reserve("plain")
    finally:
        ledger.close()


def test_a_service_with_no_guard_cannot_challenge_an_india_buy_another_service_admitted(world):
    admit_only(world, "a", 2)
    world.service.reserve("a")
    bare = ExecutionService(
        PaperDispatcher(), world.ledger, require_approval=True, approval_service=world.approval
    )
    with pytest.raises(ApprovalConflict) as refused:
        bare.create_approval_challenge("a", workspace="india", india_quote=fresh())
    assert str(refused.value) == "india_limits_unavailable"
    released(world, "a")


# ----------------------------------------------------------------- O3: fill evidence


def _record_fill_evidence_only(ledger, proposal_id: str, quantity: str) -> None:
    intent = ils.make_intent(proposal_id)
    ledger.register_intent(intent)
    with ledger._transaction() as connection:  # noqa: SLF001 - a reconciliation row, no position row
        connection.execute(
            "INSERT INTO reconciliation_evidence (proposal_id, broker_order_id, source, "
            "cumulative_quantity, cumulative_notional, status, evidence_fingerprint, observed_at, created_at) "
            "VALUES (?, 'b-1', 'test', ?, '300', 'PARTIALLY_FILLED', 'fp-1', "
            "'2026-10-08T00:00:00+00:00', '2026-10-08T00:00:00+00:00')",
            (proposal_id, quantity),
        )


def test_fill_evidence_without_a_position_row_still_counts_as_a_fill(world):
    assert world.ledger.has_fills() is False
    assert world.guard.store.load()  # the pilot start: file created, no fills yet
    _record_fill_evidence_only(world.ledger, "evid", "3")
    assert world.ledger.get_paper_position("paper", "INR", ils.TICKER, workspace="india") is None
    assert world.ledger.has_fills() is True
    state_path_for(world.ledger.path).unlink()
    admission = ils.admit(world.service, ils.make_intent("after-evidence"))
    assert admission.decision is AdmissionDecision.DENIED
    assert admission.reason_code == "state_unreadable"


def test_zero_quantity_evidence_is_not_a_fill(world):
    _record_fill_evidence_only(world.ledger, "zero", "0")
    assert world.ledger.has_fills() is False
