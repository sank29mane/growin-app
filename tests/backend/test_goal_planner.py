import asyncio
import logging
import sys
import pytest
from unittest.mock import MagicMock

pytestmark = pytest.mark.asyncio

# --- MOCKING START ---
# We need to mock app_context.state.mcp_client before importing the agent
# because the agent might use it (though it imports it inside the method).
# To be safe and test the "Real Data" path, we inject a mock state.

mock_mcp_client = MagicMock()
mock_mcp_client.session = True # specific truthy value

async def mock_call_tool(name, args):
    if name == "get_price_history":
        ticker = args.get("ticker")
        # Return different data based on ticker to verify logic
        if ticker in ["TQQQ", "BITO", "ARKK"]:
             # High return, High vol (2% daily ~ 32% annual)
             return MagicMock(content=[MagicMock(type='text', text='{"performance": {"total_change_percent": 50.0, "volatility_percent": 2.0}}')])
        elif ticker in ["BND"]:
             # Low return, Low vol (0.3% daily ~ 5% annual)
             return MagicMock(content=[MagicMock(type='text', text='{"performance": {"total_change_percent": 4.0, "volatility_percent": 0.3}}')])
        else:
             # Average (0.8% daily ~ 13% annual)
             return MagicMock(content=[MagicMock(type='text', text='{"performance": {"total_change_percent": 10.0, "volatility_percent": 0.8}}')])
    return MagicMock(content=[MagicMock(type='text', text='{}')])

mock_mcp_client.call_tool = mock_call_tool

from agents.goal_planner_agent import GoalPlannerAgent

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def test_goal_planner():
    print("--- Starting Goal Planner Agent Tests (Phase 2B with Pie Logic) ---")
    
    agent = GoalPlannerAgent()
    
    scenarios = [
        {
            "name": "Retirement (Conservative)",
            "context": {
                "initial_capital": 50000,
                "target_returns_percent": 5.0,
                "duration_years": 10,
                "risk_profile": "LOW"
            }
        },
        {
            "name": "Moonshot (Aggressive+ Momentum)",
            "context": {
                "initial_capital": 2000,
                "target_returns_percent": 25.0,
                "duration_years": 2,
                "risk_profile": "AGGRESSIVE_PLUS"
            }
        }
    ]
    
    for scenario in scenarios:
        response = await agent.analyze(scenario["context"])
        assert response.success, response.error
        data = response.data
        assert data.get("risk_profile") == scenario["context"]["risk_profile"]
        impl = data.get("implementation")
        assert impl and impl.get("action") and impl.get("name")
        assert data.get("optimal_weights"), scenario["name"]
        if data.get("risk_profile") == "AGGRESSIVE_PLUS":
            assert data.get("rebalancing_strategy")

if __name__ == "__main__":
    asyncio.run(test_goal_planner())
