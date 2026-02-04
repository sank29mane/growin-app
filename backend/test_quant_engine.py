import sys
import os
from typing import List, Dict, Any

# Ensure we can import modules from the current directory
sys.path.append(os.getcwd())

from quant_engine import get_quant_engine

def test_quant_engine():
    engine = get_quant_engine()
    
    # Mock data
    ohlcv = [
        {"t": 1625097600000 + i * 86400000, "o": 100 + i, "h": 105 + i, "l": 95 + i, "c": 102 + i, "v": 1000}
        for i in range(30)
    ]
    
    print("Testing technical indicators...")
    indicators = engine.calculate_technical_indicators(ohlcv)
    if "error" in indicators:
        print(f"Error in indicators: {indicators['error']}")
    else:
        print(f"RSI: {indicators['indicators'].get('rsi')}")
        print(f"MACD: {indicators['indicators'].get('macd')}")
        print(f"Signals: {indicators['signals']}")

    print("\nTesting portfolio metrics...")
    positions = [
        {"symbol": "AAPL", "qty": 10, "current_price": 150.0, "avg_cost": 140.0},
        {"symbol": "MSFT", "qty": 5, "current_price": 300.0, "avg_cost": 280.0}
    ]
    metrics = engine.calculate_portfolio_metrics(positions)
    if "error" in metrics:
        print(f"Error in metrics: {metrics['error']}")
    else:
        print(f"Total Value: {metrics.get('total_value')}")
        print(f"Portfolio Return: {metrics.get('portfolio_return')}")
        print(f"Sharpe Ratio: {metrics.get('sharpe_ratio')}")

if __name__ == "__main__":
    test_quant_engine()
