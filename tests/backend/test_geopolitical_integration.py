
import pytest
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock
from decimal import Decimal
from market_context import MarketContext, GeopoliticalData, GeopoliticalEvent, ResearchData
from backend.data_fabricator import DataFabricator
from backend.agents.decision_agent import DecisionAgent

@pytest.mark.asyncio
async def test_geopolitical_data_fabrication():
    """Verify DataFabricator correctly fetches and injects geopolitical data."""
    fabricator = DataFabricator()
    
    mock_geo_data = GeopoliticalData(
        gpr_score=Decimal('0.85'),
        global_sentiment_label="CRISIS",
        top_events=[GeopoliticalEvent(title="Global Trade War", impact="HIGH", region="Global")],
        summary="Extreme risk detected due to trade war."
    )
    
    with patch.object(fabricator.geopolitical_agent, "execute", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = MagicMock(
            success=True,
            data=mock_geo_data.model_dump()
        )
        
        # Mock other dependencies to speed up test
        with patch.object(fabricator, "_fetch_price_data", new_callable=AsyncMock), \
             patch.object(fabricator, "_fetch_news_data", new_callable=AsyncMock), \
             patch.object(fabricator, "_fetch_social_data", new_callable=AsyncMock), \
             patch.object(fabricator, "_fetch_macro_indicators", new_callable=AsyncMock):
            
            context = await fabricator.fabricate_context(intent="market_analysis", ticker="AAPL", account_type="invest")
            
            assert context.geopolitical is not None
            assert context.geopolitical.gpr_score == Decimal('0.85')
            assert context.geopolitical.global_sentiment_label == "CRISIS"
            assert "GPR: CRISIS" in context.get_summary()

@pytest.mark.asyncio
async def test_decision_agent_geopolitical_awareness():
    """Verify DecisionAgent prompt includes geopolitical context and identifies contradictions."""
    agent = DecisionAgent(model_name="mock-model")
    agent.llm = AsyncMock()
    agent._initialized = True
    
    context = MarketContext(
        query="Should I buy AAPL?",
        ticker="AAPL",
        research=ResearchData(ticker="AAPL", sentiment_score=Decimal('0.4'), sentiment_label="BULLISH"),
        geopolitical=GeopoliticalData(
            gpr_score=Decimal('0.9'),
            global_sentiment_label="CRISIS",
            top_events=[GeopoliticalEvent(title="Escalating Conflict", impact="HIGH", region="Middle East")]
        )
    )
    
    # 1. Test Contradiction Detection
    contradictions = agent._identify_contradictions(context)
    assert any("Global macro/geopolitical risk is CRISIS" in c for c in contradictions)
    context.user_context["contradictions"] = contradictions
    
    # 2. Test Prompt Injection
    prompt = agent._build_prompt(context, "Should I buy AAPL?")
    # Check for presence of key geopolitical info
    assert "GPR (Geopolitical)" in prompt
    assert "Score: 0.90" in prompt
    assert "(CRISIS)" in prompt
    assert "Escalating Conflict" in prompt
    assert "AGENT CONTRADICTIONS" in prompt

if __name__ == "__main__":
    pytest.main([__file__])
