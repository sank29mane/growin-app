
import asyncio
import logging
import sys
from unittest.mock import MagicMock

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

# Create a mock module for app_context
mock_app_context = MagicMock()
mock_app_context.state.mcp_client = mock_mcp_client
sys.modules['app_context'] = mock_app_context
# --- MOCKING END ---

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
        print(f"\n--- Testing Scenario: {scenario['name']} ---")
        try:
            response = await agent.analyze(scenario["context"])
            
            if response.success:
                data = response.data
                print(f"✅ Success! (Latency: {response.latency_ms:.2f}ms)")
                print(f"   Risk Profile: {data.get('risk_profile')}")
                print(f"   Expected Return: {data.get('expected_annual_return'):.2%}")
                
                # Verify Pie Implementation
                impl = data.get('implementation')
                if impl:
                    print(f"   🥧 Pie Action: {impl.get('action')} - {impl.get('type')}")
                    print(f"   🥧 Pie Name: {impl.get('name')}")
                else:
                    print("❌ Error: Missing Pie Implementation details!")

                if data.get('risk_profile') == 'AGGRESSIVE_PLUS':
                    print(f"   Rebalancing: {data.get('rebalancing_strategy')}")
                    
                # Basic validation
                if len(data.get('optimal_weights', {})) == 0:
                    print("❌ Error: No weights generated!")
                else:
                    print(f"   Weights: {data.get('optimal_weights')}")
                    
            else:
                print(f"❌ Failed: {response.error}")
                
        except Exception as e:
            print(f"❌ Exception: {e}")
            import traceback
            traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_goal_planner())
