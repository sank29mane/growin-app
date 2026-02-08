
import asyncio
import time
import sys
import os
from unittest.mock import patch
import json

# Add backend to path
sys.path.append(os.path.abspath("backend"))

from trading212_mcp_server import call_tool

async def benchmark_parallel_fetching():
    print("🚀 Starting Portfolio Benchmark...")
    
    # Mock Client
    class MockClient:
        def __init__(self, name):
            self.name = name

        async def get_all_positions(self):
            await asyncio.sleep(1.0) # Simulate 1s latency
            return [{"ticker": "AAPL", "quantity": 10, "averagePrice": 150, "currentPrice": 160, "ppl": 100}]

        async def get_account_cash(self):
            await asyncio.sleep(1.0) # Simulate 1s latency (parallel with positions)
            return {"total": 1000, "free": 500}
            
        async def get_instruments(self):
            return [{"ticker": "AAPL", "name": "Apple Inc", "currencyCode": "USD"}]

    # Setup 2 accounts
    mock_invest = MockClient("invest")
    mock_isa = MockClient("isa")
    
    mock_clients = {"invest": mock_invest, "isa": mock_isa}

    # Patch get_clients to return our mocks
    with patch('trading212_mcp_server.get_clients', return_value=mock_clients):
        # Patch get_active_client just in case
        with patch('trading212_mcp_server.get_active_client', return_value=mock_invest):
             # Patch normalize logic since we use simple mocks
             with patch('trading212_mcp_server.normalize_all_positions', side_effect=lambda p, m: p):
                 with patch('trading212_mcp_server.calculate_portfolio_value', return_value=1600):
                    
                    start_time = time.perf_counter()
                    
                    # Request "all" accounts
                    result = await call_tool("analyze_portfolio", {"account_type": "all"})
                    
                    end_time = time.perf_counter()
                    duration = end_time - start_time
                    
                    # Verify result content
                    content = json.loads(result[0].text)
                    summary = content["summary"]
                    
                    print(f"⏱️  Duration: {duration:.4f}s")
                    print(f"📊 Accounts Processed: {len(summary['accounts'])}")
                    
                    # Validation
                    if duration < 1.5:
                        print("✅ PASS: Execution time < 1.5s (Parallel verified)")
                    else:
                        print("❌ FAIL: Execution time > 1.5s (Likely sequential)")
                    
                    if len(summary['accounts']) == 2:
                         print("✅ PASS: Both accounts processed")
                    else:
                         print("❌ FAIL: Missing accounts")

if __name__ == "__main__":
    asyncio.run(benchmark_parallel_fetching())
