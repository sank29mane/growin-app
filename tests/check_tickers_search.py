import asyncio
import json
import os
import sys
from backend.mcp_client import Trading212MCPClient

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
        queries = ["3QQS", "3SQU", "QQ3S", "3SQE"]
        for q in queries:
            res = await mcp.call_tool("search_instruments", {"query": q})
            print(f"\n--- Search: {q} ---")
            if res.content and res.content[0].text:
                results = json.loads(res.content[0].text)
                for r in results:
                    print(f"Ticker: {r.get('ticker')}, Name: {r.get('name')}, Currency: {r.get('currencyCode')}")
            else:
                print("No results found")

if __name__ == "__main__":
    asyncio.run(check_symbols())
