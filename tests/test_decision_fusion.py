
import pytest
import os
import sys
import json
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock

# Ensure backend and project root are in path
project_root = os.getcwd()
sys.path.append(project_root)
sys.path.append(os.path.join(project_root, 'backend'))

from agents.decision_agent import DecisionAgent
from market_context import MarketContext, VisionData, VisualPattern

@pytest.mark.asyncio
async def test_conviction_multiplier_high_conf():
    agent = DecisionAgent()
    context = MarketContext(ticker="TSLA", query="Test")
    
    # 1. Add high-confidence visual patterns
    context.vision = VisionData(
        patterns=[
            VisualPattern(name="Bull Flag", confidence=0.92, reasoning="Clear consolidation")
        ],
        raw_description="High conf chart"
    )
    
    # Mock LLM and other methods to test the multiplier logic in make_decision
    with patch.object(agent, '_initialize_llm', new_callable=AsyncMock):
        with patch.object(agent, '_run_agentic_loop', new_callable=AsyncMock) as mock_loop:
            mock_loop.return_value = {"content": "Final recommendation", "response_id": "test-123"}
            with patch.object(agent, '_export_reasoning_trace', new_callable=AsyncMock):
                with patch.object(agent, '_validate_prices', new_callable=AsyncMock, side_effect=lambda x, y: x):
                    
                    await agent.make_decision(context, "Analyze TSLA")
                    
                    assert context.user_context.get("conviction_multiplier") == 1.2

@pytest.mark.asyncio
async def test_conviction_multiplier_low_conf():
    agent = DecisionAgent()
    context = MarketContext(ticker="TSLA", query="Test")
    
    # 2. Add low-confidence visual patterns
    context.vision = VisionData(
        patterns=[
            VisualPattern(name="Unclear", confidence=0.45, reasoning="Too much noise")
        ],
        raw_description="Low conf chart"
    )
    
    with patch.object(agent, '_initialize_llm', new_callable=AsyncMock):
        with patch.object(agent, '_run_agentic_loop', new_callable=AsyncMock) as mock_loop:
            mock_loop.return_value = {"content": "Final recommendation", "response_id": "test-123"}
            with patch.object(agent, '_export_reasoning_trace', new_callable=AsyncMock):
                with patch.object(agent, '_validate_prices', new_callable=AsyncMock, side_effect=lambda x, y: x):
                    
                    await agent.make_decision(context, "Analyze TSLA")
                    
                    # Should be 1.0 (no multiplier)
                    assert context.user_context.get("conviction_multiplier", 1.0) == 1.0

@pytest.mark.asyncio
async def test_reasoning_trace_export():
    agent = DecisionAgent()
    context = MarketContext(ticker="AAPL", query="Test Query")
    context.agents_executed = ["QuantAgent", "VisionAgent"]
    context.reasoning = "I think the market is bullish."
    
    trace_path = os.path.join(os.getcwd(), "reasoning_trace.json")
    if os.path.exists(trace_path):
        os.remove(trace_path)
        
    await agent._export_reasoning_trace(context, "Buy AAPL", "Test Query")
    
    assert os.path.exists(trace_path)
    with open(trace_path, "r") as f:
        trace = json.load(f)
        assert trace["query"] == "Test Query"
        assert trace["inputs"]["ticker"] == "AAPL"
        assert trace["agent_thoughts"]["chain_of_thought"] == context.reasoning
        assert trace["inputs"]["hybrid_weighting"]["quant"] == 0.4
