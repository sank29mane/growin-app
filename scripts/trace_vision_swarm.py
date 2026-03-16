
import asyncio
import os
import sys
from PIL import Image
import io
from decimal import Decimal

# Ensure backend is in path
sys.path.append(os.path.join(os.getcwd(), 'backend'))

from agents.coordinator_agent import CoordinatorAgent
from market_context import MarketContext
from unittest.mock import patch, MagicMock, AsyncMock

async def run_live_trace():
    print("🚀 Initiating Phase 35 Live Trace: Multi-Modal Swarm Integration")
    
    # 1. Create a dummy chart image
    img = Image.new('RGB', (800, 600), color=(30, 30, 30)) # Dark mode chart
    # Save to a temporary file
    img_path = "trace_chart.png"
    img.save(img_path)
    
    # 2. Setup Coordinator
    # We mock the MCP client and LLM to avoid needing live API keys for this trace
    # but we let the internal logic flow.
    coordinator = CoordinatorAgent()
    
    # Mock Classify Intent to force visual analysis
    coordinator._classify_intent = AsyncMock(return_value={
        "type": "visual_analysis",
        "needs": ["vision", "quant", "forecast"],
        "primary_ticker": "TSLA",
        "reason": "User provided a chart for analysis"
    })

    # Mock DataFabricator to prevent hanging on live APIs
    mock_context = MarketContext(ticker="TSLA", intent="visual_analysis")
    coordinator.data_fabricator.fabricate_context = AsyncMock(return_value=mock_context)

    # Mock VLM Engine response to simulate real local inference
    # (Running the actual 7B model in a trace might be too slow/memory intensive for this environment)
    mock_description = "The TSLA chart shows a clear Bull Flag forming near the 200-day moving average. RSI is at 45, suggesting room for upside."
    
    with patch("agents.vision_agent.get_vlm_engine") as mock_get_engine:
        mock_engine = mock_get_engine.return_value
        mock_engine.is_loaded.return_value = True
        mock_engine.generate = AsyncMock(return_value=mock_description)
        
        # 3. Process Query
        print("📥 Sending Multi-Modal Query to Swarm...")
        context = await coordinator.process_query(
            query="Analyze this TSLA chart for patterns",
            ticker="TSLA",
            image=img_path
        )
        
        # 4. Results Verification
        print("\n--- 📊 TRACE RESULTS ---")
        print(f"Ticker: {context.ticker}")
        
        if context.vision:
            print(f"✅ VisionAgent Infusion: SUCCESS")
            print(f"   Patterns Detected: {[p.name for p in context.vision.patterns]}")
            print(f"   Raw Description: {context.vision.raw_description[:50]}...")
        else:
            print(f"❌ VisionAgent Infusion: FAILED")

        if "final_answer" in context.user_context:
            answer = context.user_context["final_answer"]
            print(f"\n--- 🧠 DECISION AGENT SYNTHESIS ---")
            print(answer.get("content", "No content"))
            
            # Check for visual confirmation in the reasoning
            content = str(answer.get("content", "")).lower()
            if "visual" in content or "pattern" in content or "flag" in content:
                print("\n✅ EVIDENCE: DecisionAgent successfully utilized visual context!")
            else:
                print("\n⚠️ WARNING: DecisionAgent might have ignored visual context.")

    # Cleanup
    if os.path.exists(img_path):
        os.remove(img_path)

if __name__ == "__main__":
    # Ensure we use the right environment
    os.environ["USE_SHADOW_LLM"] = "1" # Use shadow mode for faster trace synthesis
    asyncio.run(run_live_trace())
