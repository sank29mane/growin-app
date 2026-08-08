import pytest
import numpy as np
from backend.simulation.models import MarketImpactModel

def test_market_impact_model():
    model = MarketImpactModel()

    # 2D depth [price, size]
    bid_depth = np.array([[99.0, 10.0], [98.0, 20.0]])
    ask_depth = np.array([[101.0, 10.0], [102.0, 20.0]])

    # Small trade (fully within first level)
    slippage1 = model.calculate_slippage(5.0, bid_depth, ask_depth, 100.0)
    assert slippage1 > 0

    # Large trade (exceeds total depth)
    slippage2 = model.calculate_slippage(100.0, bid_depth, ask_depth, 100.0)
    assert slippage2 > slippage1

def test_market_impact_model_1d():
    model = MarketImpactModel()

    bid_depth = np.array([10.0, 20.0])
    ask_depth = np.array([10.0, 20.0])

    slippage = model.calculate_slippage(5.0, bid_depth, ask_depth, 100.0)
    assert slippage > 0

def test_market_impact_model_no_depth():
    model = MarketImpactModel()

    slippage = model.calculate_slippage(5.0, None, None, 100.0)
    assert slippage > 0
