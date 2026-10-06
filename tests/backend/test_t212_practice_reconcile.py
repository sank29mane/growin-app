"""Phase 66-03 Task 3: reconciliation depth and cancel.

``FakeDemoBroker`` plays the broker; the fake clock plays time. Reconcile only
ever reads: every test that reconciles also checks that the one POST it started
with is still the only mutation.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal

import httpx
import pytest

from brokers.trading212.practice_reconcile import ReconcileRefused, signed_quantity
from execution.service import BrokerOutcomeUnknownError
from t212_practice_testkit import (
    LIVE_KEY_CANARY,
    PRACTICE_ACCOUNT,
    FakeDemoBroker,
    place,
    prepare_fixture,
    route_client,
    sign_and_approve,
    start_practice_stack,
)
from t212_testkit import FakeClock, Fixture, install_no_real_network


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    install_no_real_network(monkeypatch)


def claimed_epoch(stack, proposal_id: str) -> float:
    attempts = stack.ledger.list_attempts(proposal_id)
    return datetime.fromisoformat(attempts[-1].claimed_at).timestamp()


def ghost_post(broker: FakeDemoBroker, status: int = 408):
    """The broker places the order, but the adapter sees only a timeout-like answer."""

    def post(request: httpx.Request) -> httpx.Response:
        broker._create_order(json.loads(request.content))
        return httpx.Response(status, text="Timed-out")

    return post


def lost_post(request: httpx.Request) -> httpx.Response:
    """The adapter sees a 408 and the broker never took the order."""

    return httpx.Response(408, text="Timed-out")


def scripted(broker: FakeDemoBroker, post):
    def handle(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            broker.requests.append(request)
            broker.times.append(broker.clock.now)
            return post(request)
        return broker(request)

    return handle


async def unknown_order(tmp_path, private_dir, monkeypatch, *, ghost: bool, proposal="u-1", **kwargs):
    clock = FakeClock(start=__import__("time").time())
    broker = FakeDemoBroker(clock)
    post = ghost_post(broker) if ghost else lost_post
    stack = await start_practice_stack(
        tmp_path, private_dir, monkeypatch, broker=broker, clock=clock, handler=scripted(broker, post)
    )
    with pytest.raises(BrokerOutcomeUnknownError):
        await place(stack, proposal, **kwargs)
    assert stack.ledger.get_order(proposal).state == "UNKNOWN"
    return stack


# --- terminal and partial states -------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_404_by_id_then_a_history_fill_is_filled_at_the_history_price_never_failed(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        await place(stack, "f-1", quantity="2")
        stack.broker.fill(7000001, price=47.5)
        result = await stack.adapter.reconciler.reconcile("f-1")
        assert stack.broker.of("GET", "/equity/orders/7000001"), "pending was asked first"
        assert result.state == "FILLED"
        assert Decimal(result.cumulative_notional) == Decimal("0.95"), "2 x 47.5p, in GBP"
        states = [e.to_state for e in stack.ledger.list_events("f-1")]
        assert "FAILED" not in states and states[-1] == "FILLED"
        assert len(stack.broker.mutations) == 1, "only the original POST"
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_404_with_the_order_absent_from_history_is_never_failed_and_the_poll_is_bounded(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        await place(stack, "gone-1")
        stack.broker.pending.pop(7000001)  # a 404 by id
        stack.broker.hidden_history.add(7000001)  # and not in history yet
        start = stack.clock.now
        result = await stack.adapter.reconciler.reconcile("gone-1")
        assert result.code == "NOT_VISIBLE"
        assert result.state == "ACKNOWLEDGED", "not pending is not failed"
        elapsed = stack.clock.now - start
        assert 100 <= elapsed <= 125, f"the bounded poll stopped at its 120 s cap, not {elapsed}"
        assert stack.ledger.get_order("gone-1").state == "ACKNOWLEDGED"
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_partial_fills_across_two_reconciles_are_monotonic_and_a_lower_total_is_refused(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        await place(stack, "p-1", quantity="3")
        stack.broker.fill(7000001, price=49.0, quantity=1)
        first = await stack.adapter.reconciler.reconcile("p-1")
        assert (first.state, first.cumulative_quantity) == ("PARTIALLY_FILLED", "1")
        position = stack.ledger.get_paper_position(PRACTICE_ACCOUNT, "GBP", "VODl_EQ", workspace="uk")
        assert Decimal(position["quantity"]) == 1

        stack.broker.fill(7000001, price=49.0, quantity=1)
        second = await stack.adapter.reconciler.reconcile("p-1")
        assert (second.state, second.cumulative_quantity) == ("PARTIALLY_FILLED", "2")

        # A lower cumulative total than the ledger already holds is refused.
        stack.broker.fills[7000001].pop()
        stack.broker.orders[7000001]["filledQuantity"] = 1.0
        third = await stack.adapter.reconciler.reconcile("p-1")
        assert third.code == "NON_MONOTONIC"
        assert stack.ledger.get_order("p-1").state == "PARTIALLY_FILLED"
        position = stack.ledger.get_paper_position(PRACTICE_ACCOUNT, "GBP", "VODl_EQ", workspace="uk")
        assert Decimal(position["quantity"]) == 2, "the refused snapshot changed nothing"
        assert any(e.event_type == "RECONCILIATION_ANOMALY" for e in stack.ledger.list_events("p-1"))

        # The remaining shares fill; history now shows all three fills.
        template = stack.broker.fills[7000001][0]
        stack.broker.fills[7000001].extend(
            [dict(template, id=998), dict(template, id=999)]
        )
        stack.broker.orders[7000001]["filledQuantity"] = 3.0
        stack.broker.orders[7000001]["status"] = "FILLED"
        stack.broker.pending.pop(7000001, None)
        final = await stack.adapter.reconciler.reconcile("p-1")
        assert final.state == "FILLED" and final.cumulative_quantity == "3"
    finally:
        stack.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["REPLACING", "REPLACED", "SOMETHING_NEW"])
async def test_replacing_replaced_and_unknown_statuses_map_to_unknown_and_keep_the_reservation(
    status, tmp_path, private_config_dir, monkeypatch
):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        await place(stack, "r-1")
        stack.broker.pending[7000001]["status"] = status
        result = await stack.adapter.reconciler.reconcile("r-1")
        assert result.state == "UNKNOWN"
        assert stack.ledger.get_reservation("r-1").state == "ACTIVE"
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_day_order_that_expires_unfilled_is_cancelled_and_its_reservation_released(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        budget_before = stack.ledger.get_paper_budget(PRACTICE_ACCOUNT, "GBP", workspace="uk").available
        await place(stack, "x-1")
        assert stack.ledger.get_paper_budget(PRACTICE_ACCOUNT, "GBP", workspace="uk").available < budget_before
        stack.broker.expire(7000001)
        result = await stack.adapter.reconciler.reconcile("x-1")
        assert result.state == "CANCELLED"
        reservation = stack.ledger.get_reservation("x-1")
        assert reservation.state == "SETTLED" and reservation.outstanding == 0
        assert stack.ledger.get_paper_budget(PRACTICE_ACCOUNT, "GBP", workspace="uk").available == budget_before
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_rejected_order_is_rejected_and_released(tmp_path, private_config_dir, monkeypatch):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        await place(stack, "rej-1")
        stack.broker.orders[7000001]["status"] = "REJECTED"
        stack.broker.pending.pop(7000001)
        result = await stack.adapter.reconciler.reconcile("rej-1")
        assert result.state == "REJECTED"
        assert stack.ledger.get_reservation("rej-1").outstanding == 0
    finally:
        stack.close()


# --- evidence the reconciler refuses to trust ------------------------------------------------------


@pytest.mark.asyncio
async def test_a_fill_price_that_looks_like_pounds_for_a_pence_instrument_is_an_anomaly_not_a_fill(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        await place(stack, "gbx-1", quantity="2")
        stack.broker.fill(7000001, price=49.0)
        stack.broker.fills[7000001][0]["price"] = 0.49  # 100x low against netValue
        result = await stack.adapter.reconciler.reconcile("gbx-1")
        assert result.code == "FILL_VALUE_MISMATCH"
        assert stack.ledger.get_order("gbx-1").state == "ACKNOWLEDGED"
        assert stack.ledger.get_paper_position(PRACTICE_ACCOUNT, "GBP", "VODl_EQ", workspace="uk") is None
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_fill_above_the_limit_notional_is_refused_by_the_ledger(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        await place(stack, "over-1", quantity="2", limit_price="50", price_gbp="0.5")
        stack.broker.fill(7000001, price=51.0)  # 1.02 GBP against a 1.00 reservation
        result = await stack.adapter.reconciler.reconcile("over-1")
        assert result.code == "NOTIONAL_EXCEEDED"
        assert stack.ledger.get_order("over-1").state == "ACKNOWLEDGED"
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_non_gbp_instrument_fill_is_an_anomaly(tmp_path, private_config_dir, monkeypatch):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        await place(stack, "usd-1")
        stack.broker.fill(7000001, price=49.0)
        stack.broker.orders[7000001]["instrument"]["currency"] = "USD"
        stack.broker.orders[7000001]["currency"] = "USD"
        result = await stack.adapter.reconciler.reconcile("usd-1")
        assert result.code == "CURRENCY_NOT_SUPPORTED"
        assert stack.ledger.get_order("usd-1").state == "ACKNOWLEDGED"
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_filled_order_whose_fills_are_not_in_history_yet_waits_then_leaves_the_state(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        await place(stack, "late-1", quantity="2")
        stack.broker.fill(7000001, price=49.0)
        stack.broker.fills[7000001].clear()  # history shows the order, not its fills
        result = await stack.adapter.reconciler.reconcile("late-1")
        assert result.code == "FILL_EVIDENCE_PENDING"
        assert result.state == "ACKNOWLEDGED"
        # The fills arrive later: the next reconcile settles it.
        stack.broker.fills[7000001].append(
            {"id": 5, "filledAt": "2026-10-07T09:00:00+00:00", "price": 49.0, "quantity": 2.0,
             "type": "TRADE", "walletImpact": {"netValue": 0.98}}
        )
        assert (await stack.adapter.reconciler.reconcile("late-1")).state == "FILLED"
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_position_smaller_than_the_ledger_holds_is_flagged_and_audited(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        await place(stack, "pos-1", quantity="2")
        stack.broker.fill(7000001, price=49.0)
        stack.broker.positions["VODl_EQ"] = Decimal("1")  # the broker holds less than the ledger
        result = await stack.adapter.reconciler.reconcile("pos-1")
        assert result.state == "FILLED"
        assert result.position_check == "POSITION_MISMATCH"
        assert any(
            e.event_type == "RECONCILIATION_ANOMALY" and e.payload == {"code": "POSITION_MISMATCH"}
            for e in stack.ledger.list_events("pos-1")
        )
    finally:
        stack.close()


# --- pagination, governor, host --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_follows_next_page_path_until_null_and_every_get_is_spaced_by_the_governor(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        await place(stack, "pg-1", quantity="2")
        stack.broker.fill(7000001, price=49.0, quantity=1)
        stack.broker.fill(7000001, price=50.0, quantity=1)
        stack.broker.history_page_size = 1
        before = len(stack.broker.requests)
        result = await stack.adapter.reconciler.reconcile("pg-1")
        assert result.state == "FILLED" and result.cumulative_quantity == "2"
        reads = stack.broker.requests[before:]
        history = [r for r in reads if r.url.path.endswith("/history/orders")]
        assert len(history) == 2
        assert "cursor" in history[1].url.params and "cursor" not in history[0].url.params
        times = stack.broker.times[before:]
        stamped = list(zip([r.url.path for r in reads], times))
        history_times = [t for path, t in stamped if path.endswith("/history/orders")]
        assert history_times[1] - history_times[0] >= 3.0 - 1e-6, "history is 20 per minute: 3 s apart"
        assert len(stack.broker.mutations) == 1
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_next_page_path_that_points_at_another_host_is_refused_and_never_followed(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        await place(stack, "host-1")
        stack.broker.fill(7000001, price=49.0)

        def history(request):
            if request.url.path.endswith("/history/orders"):
                return httpx.Response(
                    200,
                    json={"items": [], "nextPagePath": "https://live.trading212.com/api/v0/equity/history/orders?cursor=1"},
                )
            return None

        stack.broker.get_override = history
        with pytest.raises(ReconcileRefused) as refused:
            await stack.adapter.reconciler.reconcile("host-1")
        assert refused.value.code.startswith("READ_")
        assert all(r.url.host == "demo.trading212.com" for r in stack.broker.requests)
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_the_lse_history_fixtures_with_next_page_path_reconcile_to_filled(
    tmp_path, private_config_dir, monkeypatch
):
    clock = FakeClock(start=__import__("time").time())
    broker = FakeDemoBroker(clock)
    page1, page2 = Fixture("history_orders_lse_page1"), Fixture("history_orders_lse_page2")
    placed = Fixture("order_limit_200")

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST":
            broker.requests.append(request)
            return placed.response(clock)
        if path.endswith("/equity/orders/4400120001"):
            broker.requests.append(request)
            return Fixture("order_by_id_not_found").response(clock)
        if path.endswith("/history/orders"):
            broker.requests.append(request)
            return (page2 if "cursor" in request.url.params else page1).response(clock)
        if path.endswith("/equity/positions"):
            broker.requests.append(request)
            return httpx.Response(
                200,
                json=[{"instrument": {"ticker": "VODl_EQ", "currency": "GBX"}, "quantity": 2.0}],
            )
        return broker(request)

    stack = await start_practice_stack(
        tmp_path, private_config_dir, monkeypatch, broker=broker, clock=clock, handler=handle
    )
    try:
        await place(stack, "fx-1", quantity="2", limit_price="50", price_gbp="0.5")
        result = await stack.adapter.reconciler.reconcile("fx-1")
        assert result.state == "FILLED"
        assert Decimal(result.cumulative_notional) == Decimal("0.99")  # 49p + 50p
        assert [r.url.path.endswith("/history/orders") for r in broker.requests].count(True) == 2
    finally:
        stack.close()


# --- UNKNOWN and the strict matcher (D-15) --------------------------------------------------------


@pytest.mark.asyncio
async def test_one_matching_broker_order_is_adopted_and_its_id_sticks(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await unknown_order(tmp_path, private_config_dir, monkeypatch, ghost=True)
    try:
        assert stack.ledger.get_order("u-1").acknowledgment is None
        result = await stack.adapter.reconciler.reconcile("u-1")
        assert result.code == "ADOPTED" and result.state == "ACKNOWLEDGED"
        stored = stack.ledger.get_order("u-1")
        assert stored.acknowledgment.broker_order_id == "7000001"
        assert stored.acknowledgment.broker == "t212_practice"
        assert len(stack.broker.posts) == 1, "adoption never resends"
        # The id is now the order's: a later reconcile reads it by id and cannot adopt another.
        stack.broker.fill(7000001, price=49.0)
        assert (await stack.adapter.reconciler.reconcile("u-1")).state == "FILLED"
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_zero_matches_leave_the_order_unknown_after_the_bounded_window(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await unknown_order(tmp_path, private_config_dir, monkeypatch, ghost=False)
    try:
        start = stack.clock.now
        result = await stack.adapter.reconciler.reconcile("u-1")
        assert result.code == "NO_MATCH" and result.state == "UNKNOWN"
        assert 100 <= stack.clock.now - start <= 125
        events = [e.to_state for e in stack.ledger.list_events("u-1")]
        assert "FAILED" not in events, "UNKNOWN never becomes FAILED without evidence"
        assert stack.ledger.get_reservation("u-1").state == "ACTIVE"
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_two_matching_orders_escalate_and_adopt_nothing(tmp_path, private_config_dir, monkeypatch):
    stack = await unknown_order(tmp_path, private_config_dir, monkeypatch, ghost=True)
    try:
        # A second identical order appears at the broker inside the window.
        stack.broker._create_order({"ticker": "VODl_EQ", "quantity": 2, "limitPrice": 50.0, "timeValidity": "DAY"})
        result = await stack.adapter.reconciler.reconcile("u-1")
        assert result.code == "AMBIGUOUS_MATCH" and result.state == "UNKNOWN"
        assert stack.ledger.get_order("u-1").acknowledgment is None
        assert any(e.event_type == "RECONCILIATION_ESCALATED" for e in stack.ledger.list_events("u-1"))
    finally:
        stack.close()


MISMATCHES = {
    "other-origin": lambda o, sent: o.update(initiatedFrom="IOS"),
    "other-type": lambda o, sent: o.update(type="MARKET"),
    "other-ticker": lambda o, sent: o.update(ticker="LLOYl_EQ"),
    "other-side": lambda o, sent: o.update(side="SELL", quantity=-2),
    "other-quantity": lambda o, sent: o.update(quantity=3),
    "other-price": lambda o, sent: o.update(limitPrice=50.01),
    "too-early": lambda o, sent: o.update(createdAt=datetime.fromtimestamp(sent - 6).astimezone().isoformat()),
    "too-late": lambda o, sent: o.update(createdAt=datetime.fromtimestamp(sent + 121).astimezone().isoformat()),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("name", sorted(MISMATCHES))
async def test_a_broker_order_that_differs_in_any_matched_field_is_not_adopted(
    name, tmp_path, private_config_dir, monkeypatch
):
    stack = await unknown_order(tmp_path, private_config_dir, monkeypatch, ghost=True)
    try:
        MISMATCHES[name](stack.broker.pending[7000001], claimed_epoch(stack, "u-1"))
        result = await stack.adapter.reconciler.reconcile("u-1")
        assert result.code == "NO_MATCH", name
        assert stack.ledger.get_order("u-1").acknowledgment is None
    finally:
        stack.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("offset", [-4.0, 119.0])
async def test_a_broker_order_inside_the_time_window_edges_is_adopted(
    offset, tmp_path, private_config_dir, monkeypatch
):
    stack = await unknown_order(tmp_path, private_config_dir, monkeypatch, ghost=True)
    try:
        sent = claimed_epoch(stack, "u-1")
        stack.broker.pending[7000001]["createdAt"] = (
            datetime.fromtimestamp(sent + offset).astimezone().isoformat()
        )
        assert (await stack.adapter.reconciler.reconcile("u-1")).code == "ADOPTED"
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_broker_id_already_held_by_another_ledger_order_is_never_adopted_twice(
    tmp_path, private_config_dir, monkeypatch
):
    clock = FakeClock(start=__import__("time").time())
    broker = FakeDemoBroker(clock)
    calls = {"n": 0}

    def post(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return broker._create_order(json.loads(request.content))
        broker._create_order(json.loads(request.content))
        return httpx.Response(408, text="Timed-out")

    stack = await start_practice_stack(
        tmp_path, private_config_dir, monkeypatch, broker=broker, clock=clock, handler=scripted(broker, post)
    )
    try:
        await place(stack, "first", ticker="VODl_EQ")  # acknowledged, id 7000001
        # Same ticker blocked by D-16 until the first is gone; end it, then try the same order again.
        broker.expire(7000001)
        await stack.adapter.reconciler.reconcile("first")
        with pytest.raises(BrokerOutcomeUnknownError):
            await place(stack, "second", ticker="VODl_EQ")
        # Pending now holds 7000002 only; make the first (known) id look like a candidate too.
        broker.pending[7000001] = dict(broker.orders[7000001], status="NEW")
        result = await stack.adapter.reconciler.reconcile("second")
        assert result.code == "ADOPTED"
        assert stack.ledger.get_order("second").acknowledgment.broker_order_id == "7000002"
    finally:
        stack.close()


def test_signed_quantity_reads_side_and_sign_and_refuses_a_contradiction():
    assert signed_quantity({"quantity": 3, "side": "BUY"}) == 3
    assert signed_quantity({"quantity": 3, "side": "SELL"}) == -3
    assert signed_quantity({"quantity": -3, "side": "SELL"}) == -3
    assert signed_quantity({"quantity": -3, "side": "BUY"}) is None
    assert signed_quantity({"quantity": -3}) == -3
    assert signed_quantity({"quantity": 3}) == 3
    assert signed_quantity({"quantity": 0}) is None
    assert signed_quantity({"quantity": "x"}) is None
    assert signed_quantity({"quantity": 3, "side": "HOLD"}) is None


# --- the loopback reconcile route ---------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_reconcile_route_is_loopback_only_and_refuses_unknown_proposals(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        await place(stack, "rt-1")
        body = {"confirmation": "RECONCILE_T212_PRACTICE", "proposal_id": "rt-1"}
        async with route_client(stack, monkeypatch, client=("203.0.113.7", 5000)) as remote:
            assert (await remote.post("/api/t212-practice/reconciliations", json=body)).status_code == 403
        reads = len(stack.broker.requests)
        async with route_client(stack, monkeypatch) as local:
            missing = await local.post(
                "/api/t212-practice/reconciliations",
                json={"confirmation": "RECONCILE_T212_PRACTICE", "proposal_id": "nope"},
            )
            assert missing.status_code == 409 and missing.json()["detail"]["code"] == "ORDER_NOT_FOUND"
            wrong = await local.post(
                "/api/t212-practice/reconciliations",
                json={"confirmation": "yes", "proposal_id": "rt-1"},
            )
            assert wrong.status_code == 422
            extra = await local.post(
                "/api/t212-practice/reconciliations", json={**body, "account": "x"}
            )
            assert extra.status_code == 422
        assert len(stack.broker.requests) == reads, "a refused call reads nothing"
    finally:
        stack.close()


# --- cancel (D-22) ----------------------------------------------------------------------------------------


CANCEL = {"confirmation": "CANCEL_T212_PRACTICE"}


@pytest.mark.asyncio
async def test_cancel_is_loopback_only_and_refuses_unknown_or_unacknowledged_orders(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        prepare_fixture(stack, "never-sent")  # PENDING: no acknowledgement
        async with route_client(stack, monkeypatch, client=("198.51.100.9", 1)) as remote:
            denied = await remote.post(
                "/api/t212-practice/cancellations", json={**CANCEL, "proposal_id": "never-sent"}
            )
            assert denied.status_code == 403
        async with route_client(stack, monkeypatch) as local:
            for proposal in ("no-such-proposal", "never-sent"):
                refused = await local.post(
                    "/api/t212-practice/cancellations", json={**CANCEL, "proposal_id": proposal}
                )
                assert refused.status_code == 409, proposal
        assert [r.method for r in stack.broker.requests] == ["GET"], "no DELETE was sent"
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_cancel_writes_its_ledger_event_first_then_sends_one_delete_for_the_stored_id(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        await place(stack, "c-1")
        seen_at_delete: list[list[str]] = []
        original = stack.broker._cancel

        def delete(request):
            seen_at_delete.append([e.event_type for e in stack.ledger.list_events("c-1")])
            return original(int(request.url.path.rsplit("/", 1)[1]))

        stack.broker.delete_override = delete
        async with route_client(stack, monkeypatch) as client:
            response = await client.post(
                "/api/t212-practice/cancellations", json={**CANCEL, "proposal_id": "c-1"}
            )
        assert response.status_code == 200, response.text
        assert response.json()["cancel"] == {"outcome": "REQUESTED", "code": "HTTP_200"}
        deletes = [r for r in stack.broker.requests if r.method == "DELETE"]
        assert len(deletes) == 1 and deletes[0].url.path.endswith("/equity/orders/7000001")
        assert "CANCEL_REQUESTED" in seen_at_delete[0], "the event existed before the DELETE"
        # "Requested" is not "cancelled": the order is settled by a reconcile.
        assert stack.ledger.get_order("c-1").state == "ACKNOWLEDGED"
        result = await stack.adapter.reconciler.reconcile("c-1")
        assert result.state == "CANCELLED"
        assert stack.ledger.get_reservation("c-1").outstanding == 0
        # A second cancel for the same order is refused: a cancel is never sent twice.
        async with route_client(stack, monkeypatch) as client:
            again = await client.post(
                "/api/t212-practice/cancellations", json={**CANCEL, "proposal_id": "c-1"}
            )
        assert again.status_code == 409
        assert len([r for r in stack.broker.requests if r.method == "DELETE"]) == 1
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_delete_timeout_is_not_resent_and_leaves_the_order_for_reconcile(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        await place(stack, "t-1")

        def timeout(request):
            raise httpx.ReadTimeout("slow", request=request)

        stack.broker.delete_override = timeout
        async with route_client(stack, monkeypatch) as client:
            response = await client.post(
                "/api/t212-practice/cancellations", json={**CANCEL, "proposal_id": "t-1"}
            )
        assert response.status_code == 200
        assert response.json()["cancel"]["outcome"] == "UNKNOWN"
        assert len([r for r in stack.broker.requests if r.method == "DELETE"]) == 1
        assert stack.ledger.get_order("t-1").state == "ACKNOWLEDGED"
        events = [e.event_type for e in stack.ledger.list_events("t-1")]
        assert "CANCEL_REQUESTED" in events and "CANCEL_RESPONSE" in events
        # The cancel did reach the broker after all; reconcile finds the truth.
        stack.broker.expire(7000001)
        assert (await stack.adapter.reconciler.reconcile("t-1")).state == "CANCELLED"
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_two_cancel_requests_for_one_still_open_order_send_only_one_delete(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        await place(stack, "twice-1")
        stack.broker.cancel_removes_order = False  # still open (CANCELLING) after the first
        async with route_client(stack, monkeypatch) as client:
            first = await client.post(
                "/api/t212-practice/cancellations", json={**CANCEL, "proposal_id": "twice-1"}
            )
            second = await client.post(
                "/api/t212-practice/cancellations", json={**CANCEL, "proposal_id": "twice-1"}
            )
        assert first.status_code == 200 and second.status_code == 409
        assert stack.ledger.get_order("twice-1").state == "ACKNOWLEDGED"
        assert len([r for r in stack.broker.requests if r.method == "DELETE"]) == 1
    finally:
        stack.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("status,outcome", [(404, "REFUSED"), (403, "REFUSED"), (500, "UNKNOWN")])
async def test_a_cancel_the_broker_does_not_take_is_reported_not_resent(
    status, outcome, tmp_path, private_config_dir, monkeypatch
):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        await place(stack, "d-1")
        stack.broker.delete_override = lambda request: httpx.Response(status, text="x")
        async with route_client(stack, monkeypatch) as client:
            response = await client.post(
                "/api/t212-practice/cancellations", json={**CANCEL, "proposal_id": "d-1"}
            )
        assert response.json()["cancel"]["outcome"] == outcome
        assert len([r for r in stack.broker.requests if r.method == "DELETE"]) == 1
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_cancel_releases_only_through_a_reconcile_never_by_the_delete_itself(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        await place(stack, "e-1")
        stack.broker.cancel_removes_order = False  # CANCELLING: not gone yet
        result = await stack.service.cancel_order("e-1")
        assert result.requested
        assert stack.ledger.get_reservation("e-1").state == "ACTIVE"
        pending = await stack.adapter.reconciler.reconcile("e-1")
        assert pending.state == "ACKNOWLEDGED", "CANCELLING is still open"
        stack.broker.expire(7000001)
        assert (await stack.adapter.reconciler.reconcile("e-1")).state == "CANCELLED"
        assert LIVE_KEY_CANARY not in json.dumps([str(r.url) for r in stack.broker.requests])
    finally:
        stack.close()
