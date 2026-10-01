from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.decision_agent import DecisionAgent, ToolCall
from market_context import MarketContext


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_name",
    [
        "place_market_order",
        "place_limit_order",
        "place_stop_order",
        "place_stop_limit_order",
        "cancel_order",
    ],
)
async def test_high_conviction_cannot_call_broker_execution_tools(tool_name):
    """Agent conviction must never grant direct broker execution authority."""
    mcp_client = MagicMock()
    mcp_client.call_tool = AsyncMock()

    llm = MagicMock()
    llm.chat = AsyncMock(
        side_effect=[
            {"content": f"[TOOL:{tool_name}(...) ]"},
            {"content": "I created a trade proposal for human review."},
        ]
    )

    agent = DecisionAgent(model_name="native-mlx", mcp_client=mcp_client)
    agent.llm = llm
    agent._initialized = True
    context = MarketContext(query="Create a high-conviction trade", intent="analytical")
    tool_call = ToolCall(
        tool_name=tool_name,
        arguments={"ticker": "AAPL", "action": "BUY", "quantity": 1},
        conviction_level=10,
        is_high_conviction=True,
    )

    with patch(
        "agents.decision_agent.extract_tool_calls",
        side_effect=[[tool_call], []],
    ):
        result = await agent._run_agentic_loop("system", "prompt", context)

    assert result["content"] == "I created a trade proposal for human review."
    mcp_client.call_tool.assert_not_awaited()

    follow_up_messages = llm.chat.await_args_list[1].kwargs["messages"]
    assert "Requires UI confirmation" in follow_up_messages[-1]["content"]


def test_high_conviction_proposal_still_requires_human_approval():
    agent = DecisionAgent(model_name="native-mlx", mcp_client=MagicMock())
    context = MarketContext(
        query="Buy AAPL",
        intent="analytical",
        ticker="AAPL",
        reasoning="HIGH CONVICTION",
    )

    proposal = agent._extract_trade_proposal(
        "BUY 1 share of AAPL. Conviction Level: 10",
        context,
    )

    assert proposal is not None
    assert proposal["status"] == "PENDING"
    assert proposal["bypass_confirmation"] is False
