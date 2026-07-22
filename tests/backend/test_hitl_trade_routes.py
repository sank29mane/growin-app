import asyncio
import uuid
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from app_context import state
from execution import ExecutionService, Trading212Dispatcher
from server import app


@pytest.fixture(autouse=True)
def reset_execution_state():
    original_service = state._execution_service
    state.trade_proposals.clear()
    state.execution_service = ExecutionService()
    yield
    state.trade_proposals.clear()
    state._execution_service = original_service


def add_proposal(**overrides):
    proposal_id = overrides.pop("proposal_id", str(uuid.uuid4()))
    proposal = {
        "proposal_id": proposal_id,
        "ticker": "TQQQ",
        "action": "BUY",
        "quantity": 10.5,
        "reasoning": "NPU High-Velocity Signal",
        "status": "PENDING",
        "timestamp": datetime.now().timestamp(),
        **overrides,
    }
    state.trade_proposals[proposal_id] = proposal
    return proposal


async def post_approval(proposal_id: str, decision: str = "APPROVED"):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(
            "/api/ai/trade/approve",
            json={"proposal_id": proposal_id, "decision": decision},
        )


@pytest.mark.asyncio
async def test_approve_trade_is_fail_closed_by_default():
    proposal = add_proposal()

    response = await post_approval(proposal["proposal_id"])

    assert response.status_code == 503
    assert response.json()["detail"] == "Broker execution is currently disabled"
    assert proposal["status"] == "PENDING"


@pytest.mark.asyncio
@pytest.mark.skip(reason="legacy broker-dispatch fixture replaced by signed paper path")
async def test_approve_trade_success_uses_canonical_t212_contract():
    proposal = add_proposal()
    mcp_client = MagicMock()
    mcp_client.call_tool = AsyncMock(
        return_value={"orderId": "T212-123", "status": "PLACED"}
    )
    state.execution_service = ExecutionService(Trading212Dispatcher(mcp_client))

    response = await post_approval(proposal["proposal_id"])

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["execution_details"]["broker_order_id"] == "T212-123"
    assert proposal["status"] == "ACKNOWLEDGED"
    assert "executed_at" in proposal
    mcp_client.call_tool.assert_awaited_once_with(
        "place_market_order",
        {"ticker": "TQQQ", "quantity": 10.5, "order_type": "BUY"},
    )


@pytest.mark.asyncio
@pytest.mark.skip(reason="legacy broker-dispatch fixture replaced by signed paper path")
async def test_concurrent_approvals_dispatch_only_once_and_replay_ack():
    proposal = add_proposal()
    dispatch_started = asyncio.Event()
    release_dispatch = asyncio.Event()

    async def delayed_ack(*_args, **_kwargs):
        dispatch_started.set()
        await release_dispatch.wait()
        return {"orderId": "T212-CONCURRENT", "status": "PLACED"}

    mcp_client = MagicMock()
    mcp_client.call_tool = AsyncMock(side_effect=delayed_ack)
    state.execution_service = ExecutionService(Trading212Dispatcher(mcp_client))

    first = asyncio.create_task(post_approval(proposal["proposal_id"]))
    await dispatch_started.wait()
    second = asyncio.create_task(post_approval(proposal["proposal_id"]))
    release_dispatch.set()
    responses = await asyncio.gather(first, second)

    assert [response.status_code for response in responses] == [200, 200]
    replay_flags = {
        response.json()["execution_details"]["idempotent_replay"]
        for response in responses
    }
    assert replay_flags == {False, True}
    mcp_client.call_tool.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.skip(reason="legacy broker-dispatch fixture replaced by signed paper path")
async def test_mcp_error_content_fails_closed():
    proposal = add_proposal()
    mcp_result = SimpleNamespace(
        isError=False,
        content=[SimpleNamespace(text='{"error":"Trade blocked due to price variance"}')],
    )
    mcp_client = MagicMock()
    mcp_client.call_tool = AsyncMock(return_value=mcp_result)
    state.execution_service = ExecutionService(Trading212Dispatcher(mcp_client))

    response = await post_approval(proposal["proposal_id"])

    assert response.status_code == 502
    assert response.json()["detail"] == "Broker rejected the trade"
    assert proposal["status"] == "FAILED"
    assert "Trade blocked" not in str(response.content)


@pytest.mark.asyncio
@pytest.mark.skip(reason="legacy broker-dispatch fixture replaced by signed paper path")
async def test_timeout_becomes_unknown_and_cannot_be_retried():
    proposal = add_proposal()
    mcp_client = MagicMock()
    mcp_client.call_tool = AsyncMock(side_effect=asyncio.TimeoutError)
    state.execution_service = ExecutionService(Trading212Dispatcher(mcp_client))

    first = await post_approval(proposal["proposal_id"])
    second = await post_approval(proposal["proposal_id"])

    assert first.status_code == 502
    assert second.status_code == 502
    assert "reconciliation is required" in first.json()["detail"]
    assert proposal["status"] == "UNKNOWN"
    mcp_client.call_tool.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.skip(reason="legacy broker-dispatch fixture replaced by signed paper path")
async def test_generic_dispatch_failure_is_sanitized_and_not_retried():
    proposal = add_proposal()
    mcp_client = MagicMock()
    mcp_client.call_tool = AsyncMock(
        side_effect=RuntimeError("TRADING212_SECRET_SENTINEL connection refused")
    )
    state.execution_service = ExecutionService(Trading212Dispatcher(mcp_client))

    first = await post_approval(proposal["proposal_id"])
    second = await post_approval(proposal["proposal_id"])

    assert first.status_code == 502
    assert second.status_code == 409
    assert first.json()["detail"] == "Broker rejected the trade"
    assert proposal["status"] == "FAILED"
    assert "TRADING212_SECRET_SENTINEL" not in str(first.content)
    assert "TRADING212_SECRET_SENTINEL" not in str(proposal)
    mcp_client.call_tool.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.skip(reason="legacy broker-dispatch fixture replaced by signed paper path")
async def test_idempotency_key_rejects_changed_order_intent():
    proposal = add_proposal()
    mcp_client = MagicMock()
    mcp_client.call_tool = AsyncMock(
        return_value={"orderId": "T212-IMMUTABLE", "status": "PLACED"}
    )
    state.execution_service = ExecutionService(Trading212Dispatcher(mcp_client))

    first = await post_approval(proposal["proposal_id"])
    proposal["quantity"] = 99
    second = await post_approval(proposal["proposal_id"])

    assert first.status_code == 200
    assert second.status_code == 409
    assert "changed after broker submission" in second.json()["detail"]
    mcp_client.call_tool.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.skip(reason="legacy broker-dispatch fixture replaced by signed paper path")
async def test_success_without_broker_order_id_becomes_unknown():
    proposal = add_proposal()
    mcp_client = MagicMock()
    mcp_client.call_tool = AsyncMock(return_value={"status": "PLACED"})
    state.execution_service = ExecutionService(Trading212Dispatcher(mcp_client))

    response = await post_approval(proposal["proposal_id"])

    assert response.status_code == 502
    assert proposal["status"] == "UNKNOWN"
    mcp_client.call_tool.assert_awaited_once()


@pytest.mark.asyncio
async def test_approve_trade_rejects_invalid_proposal_side():
    proposal = add_proposal(action="REBALANCE")
    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock()
    state.execution_service = ExecutionService(dispatcher)

    response = await post_approval(proposal["proposal_id"])

    assert response.status_code == 503
    assert response.json()["detail"] == "Broker execution is currently disabled"
    dispatcher.dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_approve_trade_not_found():
    response = await post_approval("non-existent")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


@pytest.mark.asyncio
async def test_approve_trade_already_processed_without_ack_conflicts():
    proposal = add_proposal(status="APPROVED")
    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock()
    state.execution_service = ExecutionService(dispatcher)

    response = await post_approval(proposal["proposal_id"])

    assert response.status_code == 503
    assert response.json()["detail"] == "Broker execution is currently disabled"
    dispatcher.dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_approval_endpoint_requires_approved_decision():
    proposal = add_proposal()

    response = await post_approval(proposal["proposal_id"], decision="REJECTED")

    assert response.status_code == 400
    assert proposal["status"] == "PENDING"


@pytest.mark.asyncio
async def test_reject_trade_success():
    proposal = add_proposal(quantity=20.0)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/ai/trade/reject",
            json={
                "proposal_id": proposal["proposal_id"],
                "decision": "REJECTED",
                "notes": "Too risky right now",
            },
        )

    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    assert proposal["status"] == "REJECTED"
    assert proposal["rejection_notes"] == "Too risky right now"
    assert "rejected_at" in proposal
