
import pytest
import asyncio
from decimal import Decimal
from unittest.mock import patch, MagicMock, AsyncMock
from backend.agents.decision_agent import DecisionAgent
from backend.agents.risk_agent import RiskAgent
from backend.market_context import MarketContext, PriceData, QuantData, ResearchData, ForecastData, RiskGovernanceData, PortfolioData
from backend.utils.financial_math import create_decimal

@pytest.mark.asyncio
async def test_profit_maximizing_coordinates_format():
    """
    Step 2 & 5: Verify DecisionAgent outputs explicit Entry/TP/SL coordinates.
    """
    # Use dry_run mode to simulate a profit-maximizing response
    from backend.lm_studio_client import LMStudioClient
    mock_llm = LMStudioClient(dry_run=True)
    
    agent = DecisionAgent(model_name="mock-trader")
    agent.llm = mock_llm
    agent._initialized = True
    
    context = MarketContext(
        query="Analyze TSLA for an intraday trade.",
        ticker="TSLA",
        intent="intraday_trade",
        price=PriceData(ticker="TSLA", current_price=Decimal("250.00")),
        quant=QuantData(ticker="TSLA", rsi=Decimal("35"), signal="BUY"),
        research=ResearchData(ticker="TSLA", sentiment_score=Decimal("0.6"), sentiment_label="BULLISH"),
        forecast=ForecastData(ticker="TSLA", forecast_24h=Decimal("265.00"), trend="BULLISH", algorithm="TTM-Zero")
    )
    
    # Mock the dry_run to return a specific formatted response
    with patch.object(mock_llm, "chat", new_callable=AsyncMock) as mock_chat:
        mock_chat.return_value = {
            "content": """
            🚀 PROFIT-DRIVEN TRADE RECOMMENDATION 🚀
            
            **Target Entry**: 250.00
            **Take Profit**: 262.50 (Targeting 5% gain)
            **Stop Loss**: 245.00 (Max 2% loss)
            **Conviction Level**: 9/10
            
            Strategic Synthesis: TSLA is showing oversold RSI at 35 with strong news sentiment. 
            The TTM model predicts a 6% move within 24h.
            """,
            "sessionId": "test-session"
        }
        
        result = await agent.make_decision(context, "Analyze TSLA for an intraday trade.")
        content = result["content"]
        
        assert "**Target Entry**" in content
        assert "**Take Profit**" in content
        assert "**Stop Loss**" in content
        assert "**Conviction Level**" in content
        # Verify decimal precision in coordinates
        assert "250.00" in content
        assert "262.50" in content
        assert "245.00" in content

@pytest.mark.asyncio
async def test_risk_governance_calibration_vix_high():
    """
    Step 3: Verify RiskAgent allows volatility plays even when VIX > 30.
    """
    risk_agent = RiskAgent(model_name="mock-risk")
    risk_agent._llm = AsyncMock()
    
    context = MarketContext(
        query="Short TSLA due to macro headwind.",
        ticker="TSLA",
        trade_horizon="short", # Intraday
        portfolio=PortfolioData(total_value=Decimal("10000")),
        risk_governance=RiskGovernanceData(
            vix_level=Decimal("35.0"), # High VIX
            yield_spread_10y2y=Decimal("-0.5"), # Inverted
            systemic_risk_level="EXTREME"
        )
    )
    
    suggestion = "SELL 10 TSLA at 250.00"
    
    # Mock LLM to return an APPROVED status for a short-term volatility play
    mock_resp = MagicMock()
    mock_resp.content = '{"status": "APPROVED", "risk_assessment": "High VIX environment suitable for short-term volatility plays.", "requires_hitl": true}'
    risk_agent._llm.ainvoke.return_value = mock_resp
    
    result = await risk_agent.review(context, suggestion)
    
    assert result["status"] == "APPROVED"
    assert "suit" in result["risk_assessment"].lower()
    assert result["requires_hitl"] is True

if __name__ == "__main__":
    pytest.main([__file__])
