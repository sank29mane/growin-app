
import asyncio
import os
import sys
from dotenv import load_dotenv

# Add backend to path
sys.path.append(os.path.join("/Users/sanketmane/Codes/Growin App/", "backend"))

from app_context import state
from agents.portfolio_agent import PortfolioAgent

async def test_portfolio_agent():
    load_dotenv("/Users/sanketmane/Codes/Growin App/backend/.env")
    
    # Initialize state
    servers = [
        {
            "name": "Trading 212",
            "type": "stdio",
            "command": "python3",
            "args": ["/Users/sanketmane/Codes/Growin App/backend/trading212_mcp_server.py"],
            "env": {},
            "url": None,
        }
    ]
    
    print("Connecting to MCP...")
    try:
        async with state.mcp_client.connect_all(servers):
            print(f"Connected to: {list(state.mcp_client.sessions.keys())}")
            
            agent = PortfolioAgent()
            print("Executing PortfolioAgent...")
            response = await agent.execute({"account_type": "all"})
            
            print(f"Success: {response.success}")
            if response.success:
                print(f"Data summary: {response.data.get('summary')}")
                print(f"Number of positions: {len(response.data.get('positions', []))}")
            else:
                print(f"Error: {response.error}")
                
    except Exception as e:
        print(f"Exception: {e}")

if __name__ == "__main__":
    asyncio.run(test_portfolio_agent())
