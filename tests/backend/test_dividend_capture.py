import pytest
import numpy as np
from datetime import date
from decimal import Decimal
from data_models import DividendData, PriceData
from dividend_bridge import LeveragedDividendEngine
from quant_engine import QuantEngine

def test_leveraged_dividend_engine_leverage():
    engine = LeveragedDividendEngine()
    # High anomaly, low volatility -> high leverage
    leverage_high = engine.calculate_optimal_leverage("AAPL", 0.05, 0.1)
    # Low anomaly, high volatility -> low leverage
    leverage_low = engine.calculate_optimal_leverage("AAPL", 0.01, 0.3)
    
    assert leverage_high > leverage_low
    assert 1.0 <= leverage_high <= 4.0
    assert 1.0 <= leverage_low <= 4.0

def test_solve_execution_routing():
    engine = LeveragedDividendEngine()
    order_size = Decimal("100000")
    liquidity = {
        "IEX": 1000000.0,
        "NASDAQ": 5000000.0,
        "NYSE": 4000000.0
    }
    
    routing = engine.solve_execution_routing(order_size, liquidity)
    
    assert len(routing) == 3
    total_routed = sum(Decimal(str(r["amount"])) for r in routing)
    assert abs(total_routed - order_size) < Decimal("0.01")
    
    # NASDAQ has most liquidity, should get most volume in quadratic impact model
    nasdaq_route = next(r for r in routing if r["venue"] == "NASDAQ")
    iex_route = next(r for r in routing if r["venue"] == "IEX")
    assert nasdaq_route["amount"] > iex_route["amount"]

def test_generate_capture_plan():
    engine = LeveragedDividendEngine()
    div = DividendData(ticker="AAPL", amount=Decimal("0.25"), ex_date=date(2024, 5, 15))
    prices = [
        PriceData(ticker="AAPL", timestamp="2024-05-14T16:00:00Z", open=Decimal("190"), high=Decimal("192"), low=Decimal("189"), close=Decimal("191"), volume=1000000)
    ]
    
    plan = engine.generate_capture_plan(div, prices)
    
    assert plan["ticker"] == "AAPL"
    assert plan["leverage"] >= 1.0
    assert plan["entry_trigger"] == "CLOSE_T_MINUS_1"
    assert plan["exit_target"] > Decimal("191")

def test_quant_engine_hedging():
    engine = QuantEngine()
    
    # Test Delta-Neutral Overlay
    overlay = engine.calculate_delta_neutral_overlay("AAPL", Decimal("1000"), Decimal("190"), 0.25)
    assert overlay["ticker"] == "AAPL"
    assert overlay["recommended_strike"] == Decimal("190") * Decimal("1.05")
    # 1000 shares / (0.3 * 100) = 33.33 -> 34 contracts
    assert overlay["contracts_to_sell"] == Decimal("34")
    
    # Test Index Netting
    positions = [
        {"ticker": "AAPL", "qty": 100, "current_price": 190, "beta": 1.2},
        {"ticker": "MSFT", "qty": 50, "current_price": 400, "beta": 1.1}
    ]
    netting = engine.calculate_index_netting(positions)
    
    expected_value = (100 * 190) + (50 * 400) # 19000 + 20000 = 39000
    expected_beta_weighted = (19000 * 1.2) + (20000 * 1.1) # 22800 + 22000 = 44800
    
    assert netting["portfolio_value"] == Decimal(str(expected_value))
    assert netting["beta_weighted_value"] == Decimal(str(expected_beta_weighted))
    assert netting["index_short_amount"] == Decimal(str(expected_beta_weighted))
