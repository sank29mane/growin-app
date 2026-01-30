import asyncio
import logging
import os
from dotenv import load_dotenv

# Load env vars
load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO)

async def test_arch():
    from agents.coordinator_agent import CoordinatorAgent
    # from mcp_client import MCPClient
    
    # Mock MCP client
    class MockMCP:
        async def call_tool(self, name, args):
            print(f"MCP Call: {name} {args}")
            return []

    print("--- Initializing Coordinator ---")
    coordinator = CoordinatorAgent(mcp_client=MockMCP(), model_name="granite-tiny")
    
    print("\n--- Running Query: Analyze AAPL ---")
    context = await coordinator.process_query("Analyze AAPL")
    
    print("\n--- Result ---")
    print(f"Ticker: {context.ticker}")
    print(f"Price: {context.price.current_price if context.price else 'NiL'}")
    print(f"Quant Signal: {context.quant.signal if context.quant else 'NiL'}")
    print(f"Decision: {context.user_context.get('final_answer', 'No decision')[:100]}...")

if __name__ == "__main__":
    asyncio.run(test_arch())
