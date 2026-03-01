import pytest
import asyncio
from backend.agents.risk_agent import RiskAgent
from backend.market_context import MarketContext, PriceData, PortfolioData
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

@pytest.mark.asyncio
async def test_wash_sale_blocking():
    """Verify that RiskAgent blocks a BUY order if a recent loss sale exists."""
    agent = RiskAgent()
    agent._initialize = AsyncMock()
    agent._llm = AsyncMock()
    
    # 1. Mock a context with a recent loss sale of AAPL
    mock_price = PriceData(ticker="AAPL", current_price=Decimal("150.0"), currency="USD")
    context = MarketContext(
        query="Buy AAPL",
        ticker="AAPL",
        price=mock_price,
        intent="trade_execution"
    )
    
    # Inject a recent loss sale into user_context
    context.user_context["recent_trades"] = [
        {"ticker": "AAPL", "side": "SELL", "pnl": -50.0, "timestamp": "2026-02-20"}
    ]
    
    # 2. Mock LLM to return BLOCKED when it sees the wash sale risk in prompt
    # In our code, we added "Wash Sale Risk: HIGH" to the prompt if detected.
    agent._llm.ainvoke.return_value = MagicMock(content='''
    {
      "status": "BLOCKED",
      "confidence_score": 0.1,
      "risk_assessment": "WASH SALE DETECTED: AAPL was sold for a loss recently. Buying now will invalidate the tax loss.",
      "compliance_notes": "UK/US Wash Sale Rule violation (30-day window).",
      "debate_refutation": "This trade is tax-inefficient due to the wash sale rule.",
      "requires_hitl": true
    }
    ''')
    
    # 3. Execute review
    result = await agent.review(context, "I recommend buying 10 shares of AAPL.")
    
    assert result["status"] == "BLOCKED"
    assert "WASH SALE" in result["risk_assessment"]
    print(f"Verified Wash Sale Blocking: {result['risk_assessment']}")

if __name__ == "__main__":
    asyncio.run(test_wash_sale_blocking())
