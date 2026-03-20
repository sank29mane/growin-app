import asyncio
import json
import os
import sys
from mcp_client import Trading212MCPClient

async def check_portfolio():
    mcp = Trading212MCPClient()
    server_config = {
        "name": "Sim-Data-Fetcher",
        "type": "stdio",
        "command": "uv",
        "args": ["run", "backend/trading212_mcp_server.py"],
        "env": {
            "TRADING212_API_KEY": os.getenv("TRADING212_API_KEY"),
            "TRADING212_API_KEY_ISA": os.getenv("TRADING212_API_KEY_ISA"),
            "TRADING212_USE_DEMO": "false",
            "PYTHONPATH": "backend"
        }
    }

    async with mcp.connect_all([server_config]):
        res = await mcp.call_tool("analyze_portfolio", {"account_type": "all"})
        if res.content and res.content[0].text:
            data = json.loads(res.content[0].text)
            print("\n--- Portfolio Analysis ---")
            for pos in data.get("positions", []):
                print(f"Ticker: {pos.get('ticker')}, Quantity: {pos.get('quantity')}, PnL: {pos.get('ppl')}")
        else:
            print("No analysis found")

if __name__ == "__main__":
    asyncio.run(check_portfolio())
