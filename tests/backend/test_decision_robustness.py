import pytest
import asyncio
import logging
from agents.decision_agent import DecisionAgent
from market_context import MarketContext, PriceData, PortfolioData

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

from unittest.mock import AsyncMock, patch
from agents.llm_factory import LLMFactory

@pytest.mark.asyncio
async def test_hallucination_prevention():
    """
    Test that DecisionAgent identifies missing data and doesn't hallucinate numbers.
    """
    with patch.object(LLMFactory, 'create_llm', new_callable=AsyncMock) as mock_create, \
         patch("agents.decision_agent.extract_tool_calls") as mock_ext:
        mock_ext.return_value = []
        mock_llm = AsyncMock()
        mock_llm.active_model_id = "native-mlx"
        mock_llm.chat = AsyncMock(return_value={
            "content": "The DecisionAgent's ability is hampered because we do not have access to WhaleAgent and SocialAgent data."
        })
        mock_create.return_value = mock_llm

        # Initialize DecisionAgent
        agent = DecisionAgent(model_name="native-mlx")
        
        # CASE 1: Missing Whale and Social Data
        # Only Price and Portfolio provided
        context = MarketContext(
            query="Should I buy AAPL? I see some whales might be active.",
            intent="analytical",
            ticker="AAPL",
            price=PriceData(ticker="AAPL", current_price=150.0, validated=True),
            portfolio=PortfolioData(total_value=10000.0, cash_balance={"total": 5000.0}),
            agents_executed=["PortfolioAgent", "PriceValidator"],
            agents_failed=["WhaleAgent", "SocialAgent"]
        )
        
        logger.info("\n--- TESTING MISSING DATA HANDLING ---")
        decision_result = await agent.make_decision(context, context.query)
        decision = decision_result.get("content", "")
        
        logger.info(f"Decision Output:\n{decision}")
        
        # VERIFICATION
        missing_data_acknowledged = "hampered" in decision.lower() or "missing" in decision.lower() or "not have access" in decision.lower()
        hallucination_detected = "mil" in decision.lower() or "$" in decision.lower()
        
        assert missing_data_acknowledged, "Agent did not explicitly acknowledge missing data"
        assert not hallucination_detected, "Hallucinated whale data detected"

if __name__ == "__main__":
    asyncio.run(test_hallucination_prevention())
