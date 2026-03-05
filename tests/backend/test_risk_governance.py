import pytest
from decimal import Decimal
from backend.quant_engine import get_quant_engine
from backend.market_context import MarketContext, RiskGovernanceData, PriceData, TimeSeriesItem
from backend.agents.ace_evaluator import ACEEvaluator

def test_adv_calculation():
    engine = get_quant_engine()
    # Mock OHLCV data for 5 days
    ohlcv = [
        {"t": 1, "v": 100},
        {"t": 2, "v": 200},
        {"t": 3, "v": 300},
        {"t": 4, "v": 400},
        {"t": 5, "v": 500}
    ]
    adv = engine.calculate_adv_30d(ohlcv)
    # Average: (100+200+300+400+500) / 5 = 1500 / 5 = 300
    assert adv == Decimal("300")

def test_liquidity_impact():
    engine = get_quant_engine()
    adv = Decimal("1000000")
    order_size = Decimal("20000") # 2% of ADV
    impact = engine.estimate_liquidity_impact(order_size, adv)
    assert impact == Decimal("0.02")

def test_ace_macro_penalties():
    evaluator = ACEEvaluator(base_score=1.0)
    
    # 1. Normal Risk
    risk_normal = RiskGovernanceData(vix_level=Decimal("15"), yield_spread_10y2y=Decimal("1.5"))
    score_normal = evaluator.calculate_score([], "APPROVED", risk_normal)
    assert score_normal == 1.0
    
    # 2. High VIX Penalty
    risk_high_vix = RiskGovernanceData(vix_level=Decimal("35"), yield_spread_10y2y=Decimal("1.5"))
    score_vix = evaluator.calculate_score([], "APPROVED", risk_high_vix)
    assert score_vix == 0.8
    
    # 3. Yield Inversion Penalty
    risk_inversion = RiskGovernanceData(vix_level=Decimal("15"), yield_spread_10y2y=Decimal("-0.2"))
    score_inv = evaluator.calculate_score([], "APPROVED", risk_inversion)
    assert score_inv == 0.8
    
    # 4. Combined Extreme Risk
    risk_extreme = RiskGovernanceData(vix_level=Decimal("35"), yield_spread_10y2y=Decimal("-0.2"))
    score_ext = evaluator.calculate_score([], "APPROVED", risk_extreme)
    # 1.0 * 0.8 * 0.8 = 0.64
    assert pytest.approx(float(score_ext)) == 0.64

def test_risk_governance_serialization():
    data = RiskGovernanceData(
        vix_level=Decimal("25.5"),
        yield_spread_10y2y=Decimal("0.1"),
        adv_30d=Decimal("1000000"),
        systemic_risk_level="ELEVATED"
    )
    context = MarketContext(
        query="Test",
        risk_governance=data
    )
    dump = context.model_dump(mode='json')
    assert dump["risk_governance"]["vix_level"] == "25.5"
    assert dump["risk_governance"]["systemic_risk_level"] == "ELEVATED"

def test_ace_no_governance():
    evaluator = ACEEvaluator(base_score=1.0)
    # Should not crash and should return base score
    score = evaluator.calculate_score([], "APPROVED", None)
    assert score == 1.0

@pytest.mark.asyncio
async def test_data_fabricator_macro_integration():
    from backend.data_fabricator import DataFabricator
    fabricator = DataFabricator()
    # Mock _fetch_macro_indicators to avoid network hit
    import asyncio
    from decimal import Decimal
    
    context = await fabricator.fabricate_context("market_analysis", "AAPL", "invest")
    assert context.risk_governance is not None
    assert context.risk_governance.vix_level is not None
