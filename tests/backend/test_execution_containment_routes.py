from unittest.mock import AsyncMock, MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from app_context import state
from lm_studio_client import LMStudioClient
from server import app


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name",
    [
        "place_market_order",
        "place_limit_order",
        "place_stop_order",
        "place_stop_limit_order",
        "cancel_order",
        "update_pie",
        "update_investment_pie",
    ],
)
async def test_generic_mcp_route_blocks_sensitive_tools_even_with_legacy_token(tool_name):
    session = MagicMock()
    session.call_tool = AsyncMock()
    original_sessions = state.mcp_client.sessions
    state.mcp_client.sessions = {"Trading 212": session}
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/mcp/tool/call",
                json={
                    "server_name": "Trading 212",
                    "tool_name": tool_name,
                    "arguments": {"ticker": "TQQQ"},
                    "approval_token": "legacy-token-must-not-bypass",
                },
            )
    finally:
        state.mcp_client.sessions = original_sessions

    assert response.status_code == 503
    session.call_tool.assert_not_awaited()


@pytest.mark.asyncio
async def test_goal_execution_route_is_fail_closed_before_broker_dispatch():
    mcp_client = state.mcp_client
    original_call_tool = mcp_client.call_tool
    mcp_client.call_tool = AsyncMock()
    payload = {
        "implementation": {"type": "TRADING212_PIE", "name": "Growth"},
        "suggested_instruments": [{"ticker": "VUSA", "weight": 100}],
    }
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/goal/execute", json=payload)
    finally:
        mcp_client.call_tool = original_call_tool

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Goal execution is disabled until it uses the execution service"
    )


@pytest.mark.asyncio
async def test_lm_studio_model_tool_callback_cannot_execute_sensitive_tool():
    original_client = state._mcp_client
    mcp_client = MagicMock()
    mcp_client.call_tool = AsyncMock()
    state.mcp_client = mcp_client
    try:
        result = await LMStudioClient()._execute_mcp_tool(
            "place_market_order",
            {"ticker": "TQQQ", "quantity": 10, "order_type": "BUY"},
        )
    finally:
        state._mcp_client = original_client

    assert "unavailable to model tool calls" in result["error"]
    mcp_client.call_tool.assert_not_awaited()
