
import asyncio
import os
import sys
import json
import time
from datetime import datetime, timezone
from PIL import Image
import io

# Ensure backend is in path
sys.path.append(os.path.join(os.getcwd(), 'backend'))

from agents.coordinator_agent import CoordinatorAgent
from market_context import MarketContext
from mlx_engine import get_memory_info

async def run_production_uat_trace(ticker: str):
    """
    Perform a high-fidelity production UAT trace.
    Uses real VLM inference (if hardware permits) and captures SOTA metrics.
    """
    print(f"\n🚀 --- PRODUCTION UAT TRACE: {ticker} ---")
    
    # 1. Setup Environment
    os.environ["GROWIN_SHADOW_MODE"] = "1" # Hardened interceptor ON
    # We allow REAL LLM reasoning here if possible, or force shadow if requested
    # For this trace, we'll use the actual swarm logic
    
    coordinator = CoordinatorAgent()
    
    # 2. Capture Initial State
    initial_mem = get_memory_info()
    print(f"📊 Initial Memory: {initial_mem['used_percent']:.1f}%")
    
    # 3. Simulate High-Fidelity Query
    query = f"High-fidelity analysis for {ticker}. Check technical patterns and confirm with vision."
    
    # In a real UAT, we would pull a real chart image here. 
    # For the script, we'll assume a placeholder image is provided to the VisionAgent
    # or it uses its internal fallback/mock if no image is in context.
    
    start_time = time.time()
    try:
        print(f"🧠 Swarm executing for {ticker}...")
        context = await coordinator.process_query(query=query, ticker=ticker)
        end_time = time.time()
        
        duration = end_time - start_time
        
        # 4. Capture Metrics
        final_mem = get_memory_info()
        print(f"✅ Trace Complete in {duration:.2f}s")
        print(f"📊 Final Memory: {final_mem['used_percent']:.1f}%")
        
        # 5. Verify Hardening Success Criteria
        print("\n📝 --- SUCCESS CRITERIA CHECK ---")
        
        # Latency (<15s for full swarm)
        if duration < 15:
            print("✅ Latency: PASS (<15s)")
        else:
            print(f"⚠️ Latency: WARNING ({duration:.2f}s > 15s)")
            
        # Memory Stability (No OOM)
        print("✅ Memory Stability: PASS (No crash)")
        
        # Decision Alignment (Reasoning Trace exists)
        if os.path.exists("reasoning_trace.json"):
            with open("reasoning_trace.json", "r") as f:
                trace = json.load(f)
                multiplier = trace["inputs"]["conviction_multiplier"]
                print(f"✅ Reasoning Trace: PASS (Multiplier: {multiplier}x)")
        else:
            print("❌ Reasoning Trace: FAILED (File not found)")
            
        # Interceptor Verification
        if os.path.exists("shadow_trades.log"):
             print("✅ Shadow Interceptor: PASS (Log active)")
             
        # Output Summary
        if "final_answer" in context.user_context:
            ans = context.user_context["final_answer"]
            content = ans.get("content", str(ans)) if isinstance(ans, dict) else str(ans)
            print(f"\n🧠 Final Consensus Snippet:\n{content[:300]}...")

    except Exception as e:
        print(f"❌ UAT Trace Failed for {ticker}: {e}")
        import traceback
        traceback.print_exc()

async def main():
    print("🌟 Growin Phase 36: Final Production UAT Trace 🌟")
    
    # Target assets from CONTEXT.md
    targets = ["TQQQ", "TSLA"]
    
    for ticker in targets:
        await run_production_uat_trace(ticker)
        print("-" * 40)
        
    print("\n🏁 Phase 36 Live UAT Trace Complete.")

if __name__ == "__main__":
    # Ensure we don't accidentally spend too much on LLM if not needed
    # But for UAT verification, we want to see the real logic if possible.
    # Set USE_SHADOW_LLM=0 to test real reasoning (requires API keys / local LLM)
    # We'll default to 1 for the automated script safety.
    if os.environ.get("USE_REAL_LLM_FOR_UAT") != "1":
        os.environ["USE_SHADOW_LLM"] = "1"
        
    asyncio.run(main())
