
import pytest
import asyncio
from decimal import Decimal
from unittest.mock import patch, MagicMock, AsyncMock
from backend.quant_engine import QuantEngine
from backend.data_fabricator import DataFabricator
from backend.market_context import MarketContext, RiskGovernanceData, PriceData, TimeSeriesItem
from backend.agents.decision_agent import DecisionAgent

@pytest.mark.asyncio
async def test_square_root_slippage_model():
    """Verify QuantEngine slippage calculation follows the square-root model."""
    qe = QuantEngine()
    
    # Order size: 1000 shares, ADV: 1,000,000 shares (0.1% of ADV)
    # sqrt(0.001) = 0.0316
    # sigma = 0.02, impact = 0.02 * 0.1 * 0.0316 = 0.0000632
    # bps = 0.63 bps
    order_size = Decimal('1000')
    adv = Decimal('1000000')
    slippage = qe.calculate_slippage_estimate(order_size, adv)
    
    assert float(slippage) > 0.5
    assert float(slippage) < 0.7
    
    # Larger order: 10% of ADV
    # sqrt(0.1) = 0.316
    # impact = 0.02 * 0.1 * 0.316 = 0.000632
    # bps = 6.32 bps
    order_size_large = Decimal('100000')
    slippage_large = qe.calculate_slippage_estimate(order_size_large, adv)
    assert float(slippage_large) > 6.0
    assert float(slippage_large) < 7.0

@pytest.mark.asyncio
async def test_data_fabricator_liquidity_injection():
    """Verify DataFabricator injects liquidity metrics into context."""
    fabricator = DataFabricator()
    
    # Mock price data with history for ADV calculation
    mock_history = [TimeSeriesItem(timestamp=i, open=100, high=101, low=99, close=100, volume=1000000) for i in range(30)]
    mock_price = PriceData(ticker="AAPL", current_price=Decimal("150.00"), history_series=mock_history)
    
    with patch.object(fabricator, "_fetch_price_data", new_callable=AsyncMock) as mock_fetch_price:
        mock_fetch_price.return_value = mock_price
        
        # Mock macro and geopolitical to avoid network
        with patch.object(fabricator, "_fetch_macro_indicators", new_callable=AsyncMock) as mock_macro, \
             patch.object(fabricator, "_fetch_geopolitical_data", new_callable=AsyncMock):
            
            mock_macro.return_value = {"vix_level": Decimal("20"), "yield_spread_10y2y": Decimal("1.0"), "systemic_risk_level": "NORMAL"}
            
            context = await fabricator.fabricate_context(intent="market_analysis", ticker="AAPL", account_type="invest")
            
            assert context.risk_governance is not None
            assert context.risk_governance.adv_30d == Decimal("1000000")
            assert context.risk_governance.slippage_bps is not None
            assert context.risk_governance.liquidity_status == "SAFE"
            assert "Est. Slippage:" in context.get_summary() # Actually summary might not have it yet, let's check

@pytest.mark.asyncio
async def test_decision_agent_liquidity_awareness():
    """Verify DecisionAgent prompt includes institutional liquidity data."""
    agent = DecisionAgent(model_name="mock-trader")
    agent._initialized = True
    
    context = MarketContext(
        query="Buy TSLA",
        ticker="TSLA",
        risk_governance=RiskGovernanceData(
            liquidity_status="THIN",
            slippage_bps=Decimal("15.5"),
            pov_participation=Decimal("0.05")
        )
    )
    
    # 2. Test Prompt Injection
    prompt = agent._build_prompt(context, "Buy TSLA")
    print(f"\n--- DEBUG PROMPT ---\n{prompt}\n------------------")
    assert "**LIQUIDITY**: Status: THIN" in prompt
    assert "Est. Slippage: 15.5 bps" in prompt
    assert "POV: 5.00%" in prompt

if __name__ == "__main__":
    pytest.main([__file__])
