import json
import pytest
import asyncio
from agents.risk_agent import RiskAgent
from market_context import MarketContext, PriceData, PortfolioData
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
import model_registry_testkit as kit
from model_registry_testkit import key_env, network_guard, registry_factory, stub  # noqa: F401

@pytest.mark.asyncio
async def test_wash_sale_blocking(stub, registry_factory):
    """Verify that RiskAgent surfaces the risk critic's BLOCKED verdict for a wash-sale BUY.

    The critic is the registry's risk_critic role served by the loopback stub; the
    test checks the wash-sale alert reaches the prompt and the verdict comes back.
    """
    registry_factory()
    stub.tool_arguments["return_riskassessment"] = {
        "status": "BLOCKED",
        "confidence_score": 0.1,
        "risk_assessment": "WASH SALE DETECTED: AAPL was sold for a loss recently.",
        "compliance_notes": "30-day window.",
        "recommendation_adjustment": "Wait out the window.",
        "debate_refutation": "This trade is tax-inefficient due to the wash sale rule.",
        "requires_hitl": True,
    }
    from app_logging import correlation_id_ctx
    correlation_id_ctx.set("test-correlation-id")

    agent = RiskAgent()

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

    # 2. Execute review through the registry's risk_critic role
    result = await agent.review(context, "I recommend buying 10 shares of AAPL.")

    assert result["status"] == "BLOCKED"
    assert result["requires_hitl"] is True
    [request] = stub.snapshot()
    assert request.model == kit.model_id_for("risk_critic")
    assert "Wash Sale Risk Alert: True" in json.dumps(request.json["messages"])
    print(f"Verified Wash Sale Blocking: {result['risk_assessment']}")

if __name__ == "__main__":
    asyncio.run(test_wash_sale_blocking())
