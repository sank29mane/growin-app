import asyncio
import json
import os
import sys
from mcp_client import Trading212MCPClient

async def check_symbols():
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
        tickers = ["LQQ3.L", "QQQ3.L", "3QQQ.L", "LQQS.L"]
        for t in tickers:
            res = await mcp.call_tool("get_symbol_details", {"ticker": t})
            print(f"\n--- {t} ---")
            if res.content and res.content[0].text:
                print(res.content[0].text)
            else:
                print("No details found")

if __name__ == "__main__":
    asyncio.run(check_symbols())
