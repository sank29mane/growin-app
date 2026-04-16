import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../')))

import pytest
from decimal import Decimal
from quant_engine import QuantEngine
from utils.financial_math import create_decimal

def test_portfolio_metrics_precision():
    engine = QuantEngine()
    
    # Test with values that usually cause float errors
    # 0.1 + 0.2 in float is 0.30000000000000004
    positions = [
        {"qty": 1, "current_price": 0.1, "avg_cost": 0.05},
        {"qty": 1, "current_price": 0.2, "avg_cost": 0.15}
    ]
    
    metrics = engine.calculate_portfolio_metrics(positions)
    
    # Check total value: 0.1 + 0.2 = 0.3 EXACTLY
    assert metrics["total_value"] == Decimal('0.3')
    assert str(metrics["total_value"]) == "0.3"
    
    # Check P&L: (0.1 - 0.05) + (0.2 - 0.15) = 0.05 + 0.05 = 0.1
    assert metrics["total_pnl"] == Decimal("0.10")
    
    # Check Return: 0.1 / 0.2 = 0.5
    assert metrics["portfolio_return"] == Decimal('0.5')

def test_rebalancing_precision():
    engine = QuantEngine()
    
    # Target 50/50
    target = {"AAPL": 0.5, "TSLA": 0.5}
    # Current 40/60
    current = {"AAPL": "40%", "TSLA": "60%"}
    total_value = 1000.0
    
    result = engine.analyze_rebalancing_opportunity(current, target, total_value)
    
    # Deviation for AAPL: 0.5 - 0.4 = 0.1
    assert result["deviations_pct"]["AAPL"] == 10.0
    
    # Rebalance action for AAPL: buy 0.1 * 1000 = 100
    aapl_action = next(a for a in result["rebalance_actions"] if a["symbol"] == "AAPL")
    assert aapl_action["action"] == "BUY"
    assert aapl_action["amount"] == 100.0

def test_extreme_precision_edge_cases():
    """Test very large and very small numbers to ensure Decimal stability."""
    engine = QuantEngine()
    
    # 1. Very large quantity with tiny price
    positions = [
        {"qty": 1000000000, "current_price": 0.00000001, "avg_cost": 0.000000005}
    ]
    metrics = engine.calculate_portfolio_metrics(positions)
    assert metrics["total_value"] == Decimal('10')
    assert metrics["total_pnl"] == Decimal('5')
    
    # 2. Rebalancing with many small assets
    target = {f"T{i}": "1%" for i in range(100)}
    current = {f"T{i}": "0.5%" for i in range(100)}
    total_value = 1000000.0
    
    result = engine.analyze_rebalancing_opportunity(current, target, total_value)
    # Total deviation should be 0.005 * 100 = 0.5
    # Each asset needs 0.005 * 1000000 = 5000
    # Note: 1% threshold is 0.01 deviation. Our deviation is 0.01 - 0.005 = 0.005. This does not trigger rebalance.
    # To trigger rebalance action, we need a deviation > 0.01. Let's adjust target to 2% and current to 0.5%
    target_adjusted = {f"T{i}": "2%" for i in range(50)}
    current_adjusted = {f"T{i}": "0.5%" for i in range(50)}
    result_adjusted = engine.analyze_rebalancing_opportunity(current_adjusted, target_adjusted, total_value)

    # Total deviation: 0.02 - 0.005 = 0.015 (> 0.01 threshold)
    # 0.015 * 1000000 = 15000
    assert result_adjusted["rebalance_actions"][0]["amount"] == 15000.0
    assert len(result_adjusted["rebalance_actions"]) == 50

if __name__ == "__main__":
    pytest.main([__file__])
