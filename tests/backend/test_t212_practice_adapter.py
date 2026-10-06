"""Phase 66-03 Task 1 (tracer): a signed practice BUY goes out to a mock demo host and back.

Everything runs against ``FakeDemoBroker`` through ``httpx.MockTransport``. No
test here contacts a Trading 212 host: an autouse guard fails the test on any
real transport or socket connect.
"""

from __future__ import annotations

import base64
import json
from decimal import Decimal

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from routes import t212_practice_routes
from t212_practice_testkit import (
    DEMO_LIMIT_URL,
    KEY_CANARY,
    PRACTICE_ACCOUNT,
    SECRET_CANARY,
    place,
    prepare_fixture,
    start_practice_stack,
)
from t212_testkit import install_no_real_network


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    install_no_real_network(monkeypatch)


def basic(key: str, secret: str) -> str:
    return "Basic " + base64.b64encode(f"{key}:{secret}".encode()).decode()


def route_client(stack, monkeypatch, *, client=("127.0.0.1", 4321)) -> AsyncClient:
    app = FastAPI()
    app.include_router(t212_practice_routes.router)
    monkeypatch.setattr(t212_practice_routes, "state", stack.app)
    return AsyncClient(transport=ASGITransport(app=app, client=client), base_url="http://test")


@pytest.mark.asyncio
async def test_tracer_signed_practice_buy_is_sent_once_and_reconciled_to_filled(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        assert stack.started, stack.app.execution_startup_error
        assert stack.app.execution_mode == "practice"
        # Starting only proved the account: one read, no order.
        assert [r.method for r in stack.broker.requests] == ["GET"]

        admission = prepare_fixture(stack, "tracer-1", quantity="2", limit_price="50", price_gbp="0.5")
        assert admission.decision.value == "ADMITTED"
        challenge = stack.service.create_approval_challenge("tracer-1", workspace="uk")
        assert stack.broker.posts == [], "nothing is sent before the signed approval"

        from venue_seam_testkit import sign

        ack = await stack.service.approve_signed(
            "tracer-1",
            challenge.challenge_id,
            sign(stack.key, challenge.signed_payload),
            workspace="uk",
        )

        # Exactly one POST, to exactly the demo limit endpoint, with the D-04 body.
        assert len(stack.broker.posts) == 1
        post = stack.broker.posts[0]
        assert str(post.url) == DEMO_LIMIT_URL
        assert json.loads(post.content) == {
            "ticker": "VODl_EQ",
            "quantity": 2,
            "limitPrice": 50.0,
            "timeValidity": "DAY",
        }
        assert post.headers["Authorization"] == basic(KEY_CANARY, SECRET_CANARY)
        assert ack.broker == "t212_practice"
        assert ack.broker_order_id == "7000001"
        assert stack.ledger.get_order("tracer-1").state == "ACKNOWLEDGED"

        # The broker fills it; the loopback reconcile route settles the ledger.
        stack.broker.fill(7000001, price=49.0)
        before = len(stack.broker.requests)
        async with route_client(stack, monkeypatch) as client:
            response = await client.post(
                "/api/t212-practice/reconciliations",
                json={"confirmation": "RECONCILE_T212_PRACTICE", "proposal_id": "tracer-1"},
            )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["state"] == "FILLED"
        assert body["cumulative_quantity"] == "2"
        assert body["position_check"] == "OK"

        # Pending, then history, then positions: the D-17 order, all GETs.
        order = [(r.method, r.url.path.removeprefix("/api/v0")) for r in stack.broker.requests[before:]]
        assert order == [
            ("GET", "/equity/orders/7000001"),
            ("GET", "/equity/history/orders"),
            ("GET", "/equity/positions"),
        ]
        assert len(stack.broker.posts) == 1, "reconcile never places an order"

        ledger = stack.ledger
        assert ledger.get_order("tracer-1").state == "FILLED"
        position = ledger.get_paper_position(PRACTICE_ACCOUNT, "GBP", "VODl_EQ", workspace="uk")
        assert Decimal(position["quantity"]) == Decimal("2")
        assert Decimal(position["notional"]) == Decimal("0.98")  # 2 x 49p, in GBP
        reservation = ledger.get_reservation("tracer-1")
        assert reservation.consumed == Decimal("0.98")
        assert reservation.state == "SETTLED"
        assert reservation.outstanding == 0
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_second_reconcile_of_a_settled_order_is_a_harmless_no_op(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        await place(stack, "again-1")
        stack.broker.fill(7000001, price=49.0)
        first = await stack.adapter.reconciler.reconcile("again-1")
        assert first.state == "FILLED"
        reads = len(stack.broker.requests)
        second = await stack.adapter.reconciler.reconcile("again-1")
        assert second.code == "NOT_RECONCILABLE"
        assert len(stack.broker.requests) == reads, "a filled order is not read again"
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_the_start_up_pin_makes_exactly_one_read(tmp_path, private_config_dir, monkeypatch):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        assert [(r.method, r.url.path) for r in stack.broker.requests] == [
            ("GET", "/api/v0/equity/account/summary")
        ]
    finally:
        stack.close()
