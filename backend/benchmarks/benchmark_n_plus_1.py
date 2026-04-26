import asyncio
import time
from unittest.mock import AsyncMock, patch

async def run_benchmark():
    tickers = [f"TICKER_{i}" for i in range(100)]
    
    # 1. Baseline: N+1 Query simulation
    # Using asyncio.gather limits the concurrency often due to rate limits or connection pools
    async def mock_fetch_ticker(t):
        await asyncio.sleep(0.05) # simulate network I/O per request
        return {t: {"bars": []}}
        
    # Simulate connection pool limit (e.g. 10 concurrent requests)
    sem = asyncio.Semaphore(10)
    async def limited_fetch(t):
        async with sem:
            return await mock_fetch_ticker(t)
            
    start_n1 = time.time()
    await asyncio.gather(*[limited_fetch(t) for t in tickers])
    end_n1 = time.time()
    n1_time = end_n1 - start_n1
    
    # 2. Optimized: Batch Query simulation
    async def mock_get_batch_bars(tickers):
        await asyncio.sleep(0.15) # single network I/O slightly longer for batch
        return {t: {"bars": []} for t in tickers}
        
    start_batch = time.time()
    await mock_get_batch_bars(tickers)
    end_batch = time.time()
    batch_time = end_batch - start_batch
    
    print(f"Baseline (N+1 with connection limits) Time: {n1_time:.4f}s")
    print(f"Optimized (Batch) Time: {batch_time:.4f}s")
    print(f"Improvement: {n1_time / batch_time:.2f}x faster")

asyncio.run(run_benchmark())
