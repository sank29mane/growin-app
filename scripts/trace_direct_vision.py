
import asyncio
import os
import sys
from PIL import Image
from datetime import datetime, timezone

# Ensure backend is in path
sys.path.append(os.path.join(os.getcwd(), 'backend'))

from agents.vision_agent import VisionAgent
from agents.decision_agent import DecisionAgent
from market_context import MarketContext, VisionData, VisualPattern
from unittest.mock import patch, MagicMock, AsyncMock

async def run_direct_trace():
    print("🚀 Initiating Direct Phase 35 Trace: Vision -> Decision Chain")
    
    # 1. Setup VisionAgent with Mocked MLX Engine
    agent = VisionAgent()
    mock_description = "The TSLA chart shows a clear Bull Flag forming near the 200-day moving average. RSI is at 45."
    
    # We mock Magentic extraction to be deterministic for the trace
    mock_patterns = [
        VisualPattern(name="Bull Flag", confidence=0.92, reasoning="Consolidation after sharp upward move.")
    ]
    
    print("👁️  Executing VisionAgent...")
    with patch("mlx_vlm_engine.get_vlm_engine") as mock_get_engine:
        mock_engine = mock_get_engine.return_value
        mock_engine.is_loaded.return_value = True
        mock_engine.generate = AsyncMock(return_value=mock_description)
        
        with patch("agents.vision_agent.extract_visual_patterns", return_value=MagicMock(patterns=mock_patterns, raw_description=mock_description)):
            response = await agent.analyze({"image": "dummy.png", "ticker": "TSLA"})
            
    if not response.success:
        print(f"❌ VisionAgent Failed: {response.error}")
        return

    print(f"✅ VisionAgent Success: Detected {len(response.data['patterns'])} patterns")

    # 2. Setup MarketContext with Vision Data
    context = MarketContext(ticker="TSLA", intent="visual_analysis", query="Analyze this TSLA chart for patterns")
    context.vision = VisionData(**response.data)
    
    # 3. Execute DecisionAgent
    print("🧠 Executing DecisionAgent Synthesis...")
    decision_agent = DecisionAgent()
    
    # We use Shadow Mode to see how it formats the final response with vision data
    os.environ["USE_SHADOW_LLM"] = "1"
    
    final_decision = await decision_agent.make_decision(context, "Should I trade TSLA based on the chart?")
    
    print("\n--- 📊 TRACE RESULTS ---")
    content = final_decision.get("content", "")
    print(content)
    
    if "Vision" in content or "Pattern" in content or "Bull Flag" in content:
        print("\n✅ SUCCESS: Phase 35 Integrated Reasoning Verified!")
    else:
        # Check shadow mode logic in decision_agent.py - it might need update to show vision
        print("\n⚠️  Note: Shadow mode might need update to explicitly show vision data labels.")

if __name__ == "__main__":
    asyncio.run(run_direct_trace())
