import asyncio
import pytest
from agents.coordinator_agent import CoordinatorAgent
from mcp_client import Trading212MCPClient
from dotenv import load_dotenv

@pytest.mark.asyncio
async def test_coordinator_account_detection():
    load_dotenv()
    c = CoordinatorAgent(Trading212MCPClient())
    # Test ISA detection
    res = await c.process_query('How is my ISA doing?')
    print(f"Query: 'How is my ISA doing?' -> Detected Account: {res.user_context.get('account_type')}")
    
    # Test Invest detection
    res = await c.process_query('Show my investment portfolio')
    print(f"Query: 'Show my investment portfolio' -> Detected Account: {res.user_context.get('account_type')}")

if __name__ == "__main__":
    asyncio.run(test())
