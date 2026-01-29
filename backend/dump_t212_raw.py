
import asyncio
import os
import json
from trading212_mcp_server import Trading212Client

async def dump_raw_data():
    isa_key = os.environ.get("TRADING212_API_KEY_ISA")
    if not isa_key:
        print("Error: ISA Key not found")
        return

    client = Trading212Client(isa_key, "", False)
    
    print("--- Account Info ---")
    try:
        info = await client.get_account_info()
        print(json.dumps(info, indent=2))
    except Exception as e:
        print(f"Info Error: {e}")

    print("\n--- Account Cash ---")
    try:
        cash = await client.get_account_cash()
        print(json.dumps(cash, indent=2))
    except Exception as e:
        print(f"Cash Error: {e}")

    print("\n--- Portfolio (First 1 item) ---")
    try:
        portfolio = await client.get_all_positions()
        if portfolio:
            print(json.dumps(portfolio[0], indent=2))
        else:
            print("Empty portfolio")
    except Exception as e:
        print(f"Portfolio Error: {e}")
        
    await client.close()

if __name__ == "__main__":
    asyncio.run(dump_raw_data())
