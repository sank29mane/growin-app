import pytest
import numpy as np
from backend.simulation.models import MarketImpactModel

def test_slippage_calculation():
    model = MarketImpactModel(impact_exponent=0.5)

    bid_depth = np.array([
        [99.0, 10.0],
        [98.0, 20.0],
        [97.0, 30.0]
    ])

    ask_depth = np.array([
        [101.0, 10.0],
        [102.0, 20.0],
        [103.0, 30.0]
    ])

    mid_price = 100.0

    # Small trade (should fit in first level)
    trade_size = 5.0
    slippage = model.calculate_slippage(trade_size, bid_depth, ask_depth, mid_price)

    assert slippage > 0
    assert slippage < 2.0  # Should be close to 1.0 (difference from mid to 101.0)
