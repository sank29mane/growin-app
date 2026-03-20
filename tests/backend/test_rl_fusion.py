
import pytest
import numpy as np
from backend.agents.decision_agent import DecisionAgent
from backend.market_context import MarketContext, PriceData, TimeSeriesItem, ForecastData
from decimal import Decimal

@pytest.mark.asyncio
async def test_decision_agent_regime_detection():
    # 1. Setup mock context with high volatility history
    agent = DecisionAgent(model_name="mock")
    
    # Create high vol returns (alternating +/- 5%)
    history = []
    base_price = 100.0
    for i in range(100):
        change = 1.05 if i % 2 == 0 else 0.95
        base_price *= change
        history.append(TimeSeriesItem(
            timestamp=i * 1000,
            open=Decimal(str(base_price)),
            high=Decimal(str(base_price * 1.01)),
            low=Decimal(str(base_price * 0.99)),
            close=Decimal(str(base_price)),
            volume=Decimal("1000")
        ))
        
    context = MarketContext(
        query="Should I trade TQQQ?",
        ticker="TQQQ",
        price=PriceData(
            ticker="TQQQ",
            current_price=Decimal(str(base_price)),
            history_series=history
        ),
        forecast=ForecastData(
            ticker="TQQQ",
            forecast_24h=Decimal(str(base_price * 1.02)),
            algorithm="NeuralJMCE"
        )
    )
    
    # Set shadow mode to avoid real LLM call during logic test
    import os
    os.environ["USE_SHADOW_LLM"] = "1"
    
    # 2. Execute
    result = await agent.make_decision(context, "Should I trade TQQQ?")
    
    # 3. Verify
    regime = context.user_context.get("market_regime")
    print(f"DEBUG: Regime detected: {regime}")
    assert regime is not None
    assert regime["label"] in ["HIGH_VOL", "EXTREME_VOL"]
    print(f"✅ Regime Detection Verified: {regime['label']} (Z: {regime['z_score']:.2f})")

if __name__ == "__main__":
    import asyncio
    asyncio.run(test_decision_agent_regime_detection())
