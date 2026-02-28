import asyncio
import time
import cProfile
import pstats
import io
from typing import Optional
import sys
import os

# Add backend to path if not present
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from data_fabricator import DataFabricator

class BenchmarkSuite:
    def __init__(self):
        self.fabricator = DataFabricator()
        
    async def benchmark_latency(self, ticker: str = "AAPL", intent: str = "price_check", runs: int = 3):
        """
        Measure latency of fabricator context generation.
        """
        print(f"Starting Latency Benchmark for {ticker} (Intent: {intent}, Runs: {runs})...")
        latencies = []
        
        for i in range(runs):
            start = time.perf_counter()
            try:
                # We are testing the real data fetch here to see actual bottlenecks
                # For CI/CD we might want to mock this, but for profiling we want real data
                # passing None for account_type and user_settings as they are optional
                context = await self.fabricator.fabricate_context(intent, ticker, None, {})
                duration = (time.perf_counter() - start) * 1000  # ms
                latencies.append(duration)
                print(f"Run {i+1}: {duration:.2f}ms")
            except Exception as e:
                print(f"Run {i+1} failed: {e}")

        if latencies:
            avg_latency = sum(latencies) / len(latencies)
            print(f"\nAverage Latency: {avg_latency:.2f}ms")
            print(f"Min: {min(latencies):.2f}ms, Max: {max(latencies):.2f}ms")
            return avg_latency
        return 0.0

    async def profile_cpu(self, ticker: str = "AAPL"):
        """
        Profile CPU usage using cProfile to identify bottlenecks.
        """
        print(f"\nRunning CPU Profiler for {ticker}...")
        pr = cProfile.Profile()
        pr.enable()
        
        await self.fabricator.fabricate_context("market_analysis", ticker, None, {})
        
        pr.disable()
        s = io.StringIO()
        sortby = pstats.SortKey.CUMULATIVE
        ps = pstats.Stats(pr, stream=s).sort_stats(sortby)
        ps.print_stats(20) # Print top 20 functions
        print(s.getvalue())

async def main():
    suite = BenchmarkSuite()
    
    print("--- BENCHMARK SUITE ---")
    
    # Warmup
    print("Warming up...")
    try:
        await suite.fabricator.fabricate_context("price_check", "AAPL", None, {})
    except Exception as e:
        print(f"Warmup failed (expected if offline/no keys): {e}")

    # Latency Test
    await suite.benchmark_latency("AAPL", "price_check", runs=3)
    
    # CPU Profile
    await suite.profile_cpu("TSLA")

if __name__ == "__main__":
    asyncio.run(main())
