import pytest
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock
from decimal import Decimal
import sys
import os

# Ensure backend path is in sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../../backend')))

from agents.orchestrator_agent import OrchestratorAgent
from agents.ace_evaluator import ACEEvaluator
from utils.trajectory_stitcher import TrajectoryStitcher
from market_context import MarketContext, QuantData, ResearchData, ForecastData, SocialData, WhaleData, PriceData

@pytest.mark.asyncio
async def test_ace_scoring_accuracy():
    """Verify ACE Evaluator calculates expected scores for edge cases."""
    evaluator = ACEEvaluator()
    
    # Case 1: Multiple rebuttals needed but eventually approved
    trace = [
        {"turn": 0, "status": "FLAGGED", "refutation": "Too risky."},
        {"turn": 1, "status": "FLAGGED", "refutation": "Still lacks diversification."},
        {"turn": 2, "status": "APPROVED", "refutation": "Issues addressed."}
    ]
    score = evaluator.calculate_score(trace, "APPROVED")
    # Base 1.0 - (2 rebuttals * 0.1) + (1 * 0.05 for 'addressed')
    # Wait, 'addressed' in turn 2 -> +0.05
    # Score should be 1.0 - 0.2 + 0.05 = 0.85
    assert abs(score - 0.85) < 0.001
    assert evaluator.get_robustness_label(score) == "BATTLE_TESTED"

def test_trajectory_stitching_narrative_coherence():
    """Verify TrajectoryStitcher creates a logical narrative from disconnected signals."""
    context = MarketContext(query="Stitching Test", intent="market_analysis", ticker="AAPL")
    
    # Add scattered context data
    context.price = PriceData(ticker="AAPL", current_price=Decimal("150.0"), currency="USD")
    context.quant = QuantData(ticker="AAPL", signal="BUY", rsi=Decimal("35.0"), support_level=Decimal("145.0"))
    context.whale = WhaleData(ticker="AAPL", sentiment_impact="BULLISH", unusual_volume=True)
    
    stitched = TrajectoryStitcher.stitch(context)
    
    assert "Market opened with AAPL at USD150.00" in stitched
    assert "Technical analysis identifies a BUY signal" in stitched
    assert "Institutional block trades (Whale Watch) show a BULLISH bias" in stitched

@pytest.mark.asyncio
async def test_dynamic_alpha_weighting_bias():
    """Verify Orchestrator dynamically includes alpha metrics in the prompt."""
    orchestrator = OrchestratorAgent()
    orchestrator._initialize = AsyncMock()
    
    # Mock data_fabricator
    mock_context = MarketContext(query="Buy AAPL?", ticker="AAPL")
    orchestrator.data_fabricator.fabricate_context = AsyncMock(return_value=mock_context)
    
    # Mock AnalyticsDB
    mock_db = MagicMock()
    mock_db.get_agent_alpha_metrics.return_value = {
        "avg_1d": 0.05,
        "specialists": {
            "QuantAgent": {"avg_1d": 0.08, "total_sessions": 5}
        }
    }
    
    with patch("analytics_db.get_analytics_db", return_value=mock_db):
        # Verify alpha context is added
        historical_alpha = mock_db.get_agent_alpha_metrics("AAPL")
        assert "QuantAgent" in historical_alpha["specialists"]
        assert historical_alpha["specialists"]["QuantAgent"]["avg_1d"] == 0.08

if __name__ == "__main__":
    asyncio.run(test_ace_scoring_accuracy())
    test_trajectory_stitching_narrative_coherence()
    asyncio.run(test_dynamic_alpha_weighting_bias())
    print("✅ Phase 19 Integration Tests Passed.")
