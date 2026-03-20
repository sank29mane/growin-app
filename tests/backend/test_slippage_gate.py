import sys
import os
import asyncio
from decimal import Decimal
from datetime import datetime, timezone

# Add backend to path
sys.path.append(os.path.join(os.getcwd(), 'backend'))

from backend.market_context import MarketContext, RiskGovernanceData, PriceData
from backend.agents.risk_agent import RiskAgent

async def test_risk_gate():
    print("Testing Risk Agent Slippage Hard-Gate...")
    
    # 1. Normal Liquidity Case (10 bps slippage)
    context_normal = MarketContext(
        query="Buy 100 shares of AAPL",
        ticker="AAPL",
        price=PriceData(ticker="AAPL", current_price=Decimal("150.0")),
        risk_governance=RiskGovernanceData(
            slippage_bps=Decimal("10.0"),
            liquidity_status="LIQUID"
        )
    )
    agent = RiskAgent(model_name="mistral")
    res_normal = await agent.review(context_normal, "Suggest buying 100 shares of AAPL.")
    print(f"\n[NORMAL CASE (10 bps)]\nSuccess: {res_normal.get('status', 'N/A')}\nContent: {res_normal.get('risk_assessment', '')[:200]}...")

    # 2. High Slippage Case (150 bps slippage)
    context_high = MarketContext(
        query="Buy 1,000,000 shares of ILLIQ",
        ticker="ILLIQ",
        price=PriceData(ticker="ILLIQ", current_price=Decimal("1.0")),
        risk_governance=RiskGovernanceData(
            slippage_bps=Decimal("150.0"),
            liquidity_status="ILLIQUID"
        )
    )

    res_high = await agent.review(context_high, "Suggest buying 1,000,000 shares of ILLIQ.")
    print(f"\n[HIGH SLIPPAGE CASE (150 bps)]\nSuccess: {res_high.get('status', 'N/A')}\nContent: {res_high.get('risk_assessment', '')[:200]}...")

    
    # Verify if blocked or flagged
    feedback = res_high.get('risk_assessment', '').lower()
    if res_high.get('status') == "BLOCKED" or "slippage" in feedback:
         print("\n✅ Risk Agent correctly identified and flagged/blocked high slippage.")
    else:
         print("\n❌ Risk Agent FAILED to flag/block high slippage.")

if __name__ == "__main__":
    asyncio.run(test_risk_gate())
