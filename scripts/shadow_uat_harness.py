
import asyncio
import os
import sys
import json
import time
from datetime import datetime, timezone

# Ensure backend is in path
sys.path.append(os.path.join(os.getcwd(), 'backend'))

from agents.coordinator_agent import CoordinatorAgent
from market_context import MarketContext

async def run_shadow_uat_cycle(ticker: str):
    """Run a single analysis cycle in shadow mode."""
    print(f"🕵️ Starting Shadow UAT Cycle for {ticker}...")
    
    # Force shadow mode
    os.environ["GROWIN_SHADOW_MODE"] = "1"
    
    coordinator = CoordinatorAgent()
    
    # For UAT, we might want to provide a real chart image if available, 
    # but for the harness test, we'll use a placeholder or skip if not testing vision specifically
    query = f"Analyze {ticker} for potential trades"
    
    start_time = time.time()
    try:
        context = await coordinator.process_query(query=query, ticker=ticker)
        latency = (time.time() - start_time)
        
        print(f"✅ Cycle Complete ({latency:.2f}s)")
        print(f"📊 Intent: {context.intent}")
        
        if "final_answer" in context.user_context:
            answer = context.user_context["final_answer"]
            content = answer.get("content", str(answer)) if isinstance(answer, dict) else str(answer)
            print(f"🧠 Decision: {content[:200]}...")
            
        # Check shadow_trades.log for intercepts
        if os.path.exists("shadow_trades.log"):
            with open("shadow_trades.log", "r") as f:
                last_line = f.readlines()[-1]
                if ticker in last_line:
                    print(f"🚨 CONFIRMED: Trade for {ticker} was INTERCEPTED and logged.")
                else:
                    print(f"ℹ️ No trade signals generated for {ticker} in this cycle.")
        
        # Verify Reasoning Trace
        if os.path.exists("reasoning_trace.json"):
            print("✅ Reasoning Trace Exported successfully.")
            
    except Exception as e:
        print(f"❌ Shadow Cycle Failed: {e}")

async def main():
    print("🚀 Growin Shadow Mode UAT Harness (Wave 3)")
    target_assets = ["TQQQ", "SQQQ", "TSLA"]
    
    # For the harness verification, we'll just run one cycle
    for asset in target_assets[:1]:
        await run_shadow_uat_cycle(asset)
        
    print("\n🏁 Shadow Harness verification finished.")

if __name__ == "__main__":
    # Ensure shadow mode env is set for any subprocesses
    os.environ["GROWIN_SHADOW_MODE"] = "1"
    # We use shadow LLM for the harness verification to avoid cost/latency
    os.environ["USE_SHADOW_LLM"] = "1" 
    
    asyncio.run(main())
