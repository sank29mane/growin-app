import asyncio
import logging
import sys
import os

# Add parent directory to path to import backend modules
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), 'backend')))

from backend.agents.decision_agent import DecisionAgent
from backend.market_context import MarketContext
from backend.app_context import state

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def test_decision_agent_math():
    # Mock MarketContext
    context = MarketContext(ticker="AAPL")
    
    # Initialize DecisionAgent with a mock/lite model if possible, 
    # but here we just want to see if the delegation code is reached.
    agent = DecisionAgent(model_name="gpt-4o") # Or any model
    
    # We need to mock the LLM factory to avoid real API calls if we just want to test logic
    # But actually, the task says "check logs for delegation". 
    # The delegation happens BEFORE the agentic loop.
    
    query = "Run a 10,000 path Monte Carlo for AAPL"
    
    print(f"Testing query: {query}")
    
    # We'll just call make_decision. It might fail at the LLM step but we should see logs from the math delegation.
    try:
        await agent.make_decision(context, query)
    except Exception as e:
        # We expect it might fail if LLM is not configured, but check logs
        print(f"Execution finished (possibly with error): {e}")

if __name__ == "__main__":
    asyncio.run(test_decision_agent_math())
