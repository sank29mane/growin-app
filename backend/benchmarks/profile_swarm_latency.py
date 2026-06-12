import asyncio
import time
import logging
import sys
import os
import psutil
import datetime
from typing import Dict, Any, List
from dotenv import load_dotenv

# Add parent directory to path to allow imports from backend
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Load environment variables from backend/.env
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

# Set up minimal logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("latency_profiler")

from agents.quant_agent import QuantAgent
from agents.research_agent import ResearchAgent
from agents.forecasting_agent import ForecastingAgent
from agents.social_agent import SocialAgent

async def profile_agent(agent, context: Dict[str, Any], iterations: int = 5) -> Dict[str, Any]:
    """Profile a single agent's latency over multiple iterations."""
    latencies = []
    success_count = 0
    errors = []

    logger.info(f"Profiling {agent.config.name} (Cache Disabled)...")
    
    for i in range(iterations):
        # Clear cache every iteration for accurate raw performance
        agent.clear_cache()
        start_time = time.time()
        try:
            response = await agent.execute(context)
            latency = (time.time() - start_time) * 1000
            
            if response.success:
                latencies.append(latency)
                success_count += 1
            else:
                errors.append(response.error)
                logger.warning(f"  Iteration {i+1}: Failed - {response.error}")
        except Exception as e:
            logger.error(f"  Iteration {i+1}: Exception - {e}")
            errors.append(str(e))

    if not latencies:
        return {
            "name": agent.config.name,
            "avg_latency": 0,
            "min_latency": 0,
            "max_latency": 0,
            "success_rate": 0,
            "errors": list(set(errors))
        }

    return {
        "name": agent.config.name,
        "avg_latency": sum(latencies) / len(latencies),
        "min_latency": min(latencies),
        "max_latency": max(latencies),
        "success_rate": (success_count / iterations) * 100,
        "errors": list(set(errors))
    }

async def main():
    logger.info("🚀 Starting Swarm Latency Profiling for M4 Pro...")
    
    # 1. Hardware Snapshot
    mem = psutil.virtual_memory()
    available_gb = mem.available / 1024**3
    logger.info(f"Hardware Context: {available_gb:.2f}GB RAM available.")

    # 2. Prepare Context (Standardized)
    # Use unix timestamps in milliseconds for QuantEngine compatibility
    now_ms = int(time.time() * 1000)
    context = {
        "ticker": "AAPL",
        "company_name": "Apple Inc.",
        "ohlcv_data": [
            {
                "t": now_ms - (i * 60 * 1000), # 1 minute bars
                "o": 100.0, "h": 105.0, "l": 95.0, "c": 102.0, "v": 1000000
            } for i in range(100)
        ],
        "intent": "analytical"
    }

    # 3. Initialize Agents
    agents = []
    try:
        agents.append(QuantAgent())
        agents.append(ResearchAgent())
        # Add others if possible, but these are the critical ones for calibration
    except Exception as e:
        logger.error(f"Failed to initialize agents: {e}")

    # 4. Profile
    results = []
    for agent in agents:
        # Clear cache first for clean run
        agent.clear_cache()
        res = await profile_agent(agent, context)
        results.append(res)

    # 5. Output Summary
    print("\n" + "="*50)
    print("📈 SWARM LATENCY PROFILE SUMMARY")
    print("="*50)
    print(f"{'Agent':<20} | {'Avg (ms)':<10} | {'Min (ms)':<10} | {'Max (ms)':<10} | {'Rate %':<10}")
    print("-" * 75)
    
    for r in results:
        print(f"{r['name']:<20} | {r['avg_latency']:>10.2f} | {r['min_latency']:>10.2f} | {r['max_latency']:>10.2f} | {r['success_rate']:>10.1f}%")
    
    print("="*75)
    
    # Suggested Calibration Constants
    print("\n💡 SUGGESTED CALIBRATION CONSTANTS (v0.42.0):")
    quant_avg = next((r['avg_latency'] for r in results if r['name'] == 'QuantAgent'), 100)
    research_avg = next((r['avg_latency'] for r in results if r['name'] == 'ResearchAgent'), 1000)
    
    reflex_suggestion = (quant_avg * 1.5) / 1000 # To seconds
    synthesis_suggestion = (research_avg * 1.2) / 1000 # To seconds
    
    print(f"  reflex_timeout:    {reflex_suggestion:.2f}s  (Target: Quant + Buffer Overhead)")
    print(f"  synthesis_timeout: {synthesis_suggestion:.2f}s  (Target: Research/News Parallel Peak)")
    print("-" * 50)

if __name__ == "__main__":
    asyncio.run(main())
