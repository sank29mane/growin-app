"""PR #557 fix 4: an UNKNOWN (or already submitted) practice order is never approved twice.

An UNKNOWN BUY keeps its reservation ACTIVE, so the reservation check alone let
``create_challenge`` issue a fresh challenge for an order that may already be at
the broker. The only thing between a second click and a second order was one
ledger state check. The challenge now refuses the order up front, and the old
ledger check is proven to hold on its own.
"""

from __future__ import annotations

import numpy as np
import httpx
import pytest

from execution import ApprovalConflict
from execution.service import ExecutionConflictError
from execution.service import BrokerOutcomeUnknownError
from t212_practice_testkit import (
    FakeDemoBroker,
    prepare_fixture,
    sign,
    sign_and_approve,
    start_practice_stack,
)
from t212_testkit import FakeClock, install_no_real_network


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    install_no_real_network(monkeypatch)


@pytest.fixture(autouse=True)
def regime_zero(monkeypatch):
    import market_data.regime as regime_module

    monkeypatch.setattr(
        regime_module, "fast_gmm_predict_proba", lambda feature, **params: np.array([1.0, 0.0, 0.0, 0.0])
    )


async def unknown_buy_stack(tmp_path, private_dir, monkeypatch):
    clock = FakeClock(start=__import__("time").time())
    broker = FakeDemoBroker(clock)

    def handle(request):
        if request.method == "POST":
            broker.requests.append(request)
            return httpx.Response(408, text="timeout")
        return broker(request)

    stack = await start_practice_stack(
        tmp_path, private_dir, monkeypatch, broker=broker, clock=clock, handler=handle
    )
    assert stack.started, stack.app.execution_startup_error
    return stack


@pytest.mark.asyncio
async def test_a_second_approval_of_an_unknown_buy_never_dispatches(tmp_path, private_config_dir, monkeypatch):
    stack = await unknown_buy_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        prepare_fixture(stack, "u-1")
        challenge = stack.service.create_approval_challenge("u-1", workspace="uk")
        with pytest.raises(BrokerOutcomeUnknownError):
            await stack.service.approve_signed(
                "u-1", challenge.challenge_id, sign(stack.key, challenge.signed_payload), workspace="uk"
            )
        assert stack.ledger.get_order("u-1").state == "UNKNOWN"
        assert stack.ledger.get_reservation("u-1").state == "ACTIVE", "UNKNOWN keeps the reservation"
        assert len(stack.broker.posts) == 1

        # No fresh challenge is issued for it.
        with pytest.raises(ApprovalConflict, match="UNKNOWN"):
            stack.service.create_approval_challenge("u-1", workspace="uk")
        # The full signed path cannot be driven a second time, and no POST leaves.
        with pytest.raises(ApprovalConflict):
            await sign_and_approve(stack, "u-1")
        # The challenge that WAS issued before the first dispatch cannot be replayed either.
        with pytest.raises(ExecutionConflictError):
            await stack.service.approve_signed(
                "u-1", challenge.challenge_id, sign(stack.key, challenge.signed_payload), workspace="uk"
            )
        assert len(stack.broker.posts) == 1, "the broker saw exactly one order"
        assert stack.ledger.get_order("u-1").state == "UNKNOWN"
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_no_challenge_is_issued_for_an_order_that_is_already_acknowledged(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        prepare_fixture(stack, "a-1")
        await sign_and_approve(stack, "a-1")
        assert stack.ledger.get_order("a-1").state == "ACKNOWLEDGED"
        with pytest.raises(ApprovalConflict, match="ACKNOWLEDGED"):
            stack.service.create_approval_challenge("a-1", workspace="uk")
        assert len(stack.broker.posts) == 1
    finally:
        stack.close()
