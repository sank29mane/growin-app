"""
vMLX Memory Pressure & Caching Validation Script
SOTA 2026: Hardware-aware verification for M4 Pro (48GB)
"""

import asyncio
import time
import os
import psutil
import logging
import json
import aiohttp

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

VMLX_URL = os.getenv("VMLX_BASE_URL", "http://127.0.0.1:8000/v1")
MEMORY_LIMIT_GB = 28.0 # 60% of 48GB (Weights + Active Memory)
KV_CACHE_LIMIT_GB = 12.0 # 25% of 48GB (Dedicated KV Pool)

async def check_vmlx_heartbeat():
    """Verify vMLX server is reachable."""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{VMLX_URL}/models", timeout=2) as resp:
                return resp.status == 200
    except Exception:
        return False

def get_process_memory_gb(name_filter="vmlx"):
    """Find vmlx process memory usage."""
    total_mem = 0.0
    for proc in psutil.process_iter(['name', 'memory_info']):
        try:
            if name_filter.lower() in proc.info['name'].lower():
                total_mem += proc.info['memory_info'].rss / (1024**3)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return total_mem

async def run_stress_test(num_concurrent=5):
    """Simulate concurrent chat sessions to test memory stability."""
    prompt = "Analyze the historical correlation between NVDA and the Nasdaq-100 over the last 5 years, providing a detailed breakdown of alpha generation and risk-adjusted returns."
    
    async def single_request(session, id):
        start = time.time()
        payload = {
            "model": "nemotron-3-30b-moe",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 1024
        }
        try:
            async with session.post(f"{VMLX_URL}/chat/completions", json=payload) as resp:
                data = await resp.json()
                duration = time.time() - start
                logger.info(f"Request {id} completed in {duration:.2f}s")
                return True
        except Exception as e:
            logger.error(f"Request {id} failed: {e}")
            return False

    logger.info(f"🚀 Starting vMLX Stress Test ({num_concurrent} concurrent sessions)")
    
    async with aiohttp.ClientSession() as session:
        tasks = [single_request(session, i) for i in range(num_concurrent)]
        results = await asyncio.gather(*tasks)
        
    success_rate = sum(results) / len(results)
    logger.info(f"📊 Stress Test Complete. Success Rate: {success_rate:.0%}")
    return success_rate == 1.0

async def main():
    logger.info("🔍 Initializing vMLX Verification...")
    
    if not await check_vmlx_heartbeat():
        logger.error("❌ vMLX server not found. Ensure 'vmlx serve' is running.")
        return

    # 1. Baseline Memory
    initial_mem = get_process_memory_gb()
    logger.info(f"Baseline Memory: {initial_mem:.2f} GB")
    
    # 2. Performance & Caching Test
    logger.info("Testing Prefix Caching...")
    async with aiohttp.ClientSession() as session:
        # First call (Cold)
        start = time.time()
        await session.post(f"{VMLX_URL}/chat/completions", json={
            "model": "nemotron-3-30b-moe",
            "messages": [{"role": "user", "content": "Cold start test prompt."}],
            "max_tokens": 10
        })
        cold_time = time.time() - start
        
        # Second call (Warm - should be faster due to prefix cache)
        start = time.time()
        await session.post(f"{VMLX_URL}/chat/completions", json={
            "model": "nemotron-3-30b-moe",
            "messages": [{"role": "user", "content": "Cold start test prompt."}],
            "max_tokens": 10
        })
        warm_time = time.time() - start
        
        logger.info(f"Cold Time: {cold_time:.3f}s | Warm Time: {warm_time:.3f}s")
        if warm_time < cold_time:
            logger.info("✅ Prefix Caching verified (Warm < Cold)")
        else:
            logger.warning("⚠️ Prefix Caching might not be active")

    # 3. Stress Test
    stable = await run_stress_test()
    
    # 4. Final Memory Check
    peak_mem = get_process_memory_gb()
    logger.info(f"Peak Memory: {peak_mem:.2f} GB")
    
    if peak_mem > MEMORY_LIMIT_GB:
        logger.warning(f"❌ Memory Limit Exceeded! {peak_mem:.2f}GB > {MEMORY_LIMIT_GB}GB")
    else:
        logger.info(f"✅ Memory usage stable within {MEMORY_LIMIT_GB}GB limit.")

if __name__ == "__main__":
    asyncio.run(main())
