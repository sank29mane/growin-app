
import asyncio
import json
from mcp_client import Trading212MCPClient

async def test_t212_mcp():
    client = Trading212MCPClient()
    
    # 1. Config server
    server = {
        "name": "Trading 212",
        "type": "stdio",
        "command": "python3",
        "args": ["trading212_mcp_server.py"],
        "env": {},
        "url": None,
    }
    
    print(f"Connecting to {server['name']}...")
    try:
        async with client.connect_all([server]):
            print(f"Connected to {len(client.sessions)} servers.")
            
            # 2. Call analyze_portfolio
            print("Calling analyze_portfolio...")
            result = await client.call_tool("analyze_portfolio", {"account_type": "all"})
            
            if result and result.content:
                data = json.loads(result.content[0].text)
                print(f"Success! Found {len(data.get('positions', []))} positions.")
                print(f"Total Value: {data.get('summary', {}).get('current_value')}")
            else:
                print("Failed: Empty response from MCP.")
                
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(test_t212_mcp())
