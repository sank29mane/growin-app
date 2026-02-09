import numpy as np
import sys
import os
from unittest.mock import MagicMock

# Add backend to path (parent directory)
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Mock heavy dependencies to avoid import crashes in unrelated agents
sys.modules["agents.forecasting_agent"] = MagicMock()
sys.modules["agents.portfolio_agent"] = MagicMock()
sys.modules["agents.research_agent"] = MagicMock()
sys.modules["agents.social_agent"] = MagicMock()
sys.modules["agents.whale_agent"] = MagicMock()
sys.modules["agents.goal_planner_agent"] = MagicMock()

# Also mock sklearn/scipy if they are still hit
sys.modules["sklearn"] = MagicMock()
sys.modules["scipy"] = MagicMock()
sys.modules["scipy.stats"] = MagicMock()

# Now import QuantAgent
# We need to make sure we import it correctly.
# Since we are in backend/tests, and added backend/ to sys.path, we can import agents.quant_agent
from agents.quant_agent import QuantAgent

def test_rsi_calculation_wilders_smoothing():
    """
    Test that QuantAgent._calculate_rsi matches standard Wilder's Smoothing RSI.
    This validates the fix for the divergence issue.
    """
    agent = QuantAgent()

    # Test data (same as used in verify_rsi_bug.py)
    prices = np.array([
        100.0, 102.0, 101.0, 103.0, 104.0, 105.0, 106.0, 105.0, 104.0, 103.0,
        102.0, 101.0, 100.0, 99.0, 98.0, 97.0, 96.0, 95.0, 96.0, 97.0,
        98.0, 99.0, 100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0
    ]) # 30 points

    # Expected values from a correct Wilder's implementation
    # (Verified in verify_rsi_bug.py)
    expected_rsi = np.array([
        50.00, 50.00, 50.00, 50.00, 50.00, 50.00, 50.00, 50.00, 50.00, 50.00,
        50.00, 50.00, 50.00, 50.00, 43.75, 40.99, 38.38, 35.92, 40.06, 43.96,
        47.63, 51.07, 54.31, 57.35, 60.20, 62.88, 65.38, 67.73, 69.92, 71.97
    ])

    calculated_rsi = agent._calculate_rsi(prices, period=14)

    # Check length
    assert len(calculated_rsi) == len(prices)

    # Check values (allow small float error)
    # The first 14 values are fillers (50.0)
    np.testing.assert_allclose(calculated_rsi, expected_rsi, rtol=1e-2, atol=0.05,
                               err_msg="Calculated RSI does not match expected Wilder's RSI")

def test_ema_calculation():
    """
    Test EMA calculation against expected Pandas behavior.
    """
    agent = QuantAgent()
    prices = np.arange(100.0, 130.0) # 30 points

    period = 10
    ema = agent._calculate_ema(prices, period)

    # Calculate expected EMA using pandas directly
    import pandas as pd

    # Manual verification of expected logic:
    # First `period` elements are used for initial SMA at index `period-1`
    initial_sma = prices[:period].mean()

    # Then EWM continues
    rest = prices[period:]
    concat = np.concatenate(([initial_sma], rest))
    expected_series = pd.Series(concat).ewm(span=period, adjust=False).mean()
    expected_values = expected_series.values

    # Reconstruct full array for comparison
    expected_full = np.zeros(len(prices))
    expected_full[period-1:] = expected_values
    # Note: the test just checks if agent._calculate_ema does what we expect it to do (match pandas)

    np.testing.assert_allclose(ema[period-1:], expected_full[period-1:], rtol=1e-5)

def test_macd_calculation():
    """
    Test MACD calculation structure.
    """
    agent = QuantAgent()
    prices = np.random.randn(100) + 100

    macd, signal, hist = agent._calculate_macd(prices)

    assert len(macd) == len(prices)
    assert len(signal) == len(prices)
    assert len(hist) == len(prices)

    # Basic relationship check
    np.testing.assert_allclose(hist, macd - signal, rtol=1e-5, atol=1e-8)

def test_insufficient_data():
    """Test behavior with insufficient data"""
    agent = QuantAgent()
    prices = np.array([100.0, 101.0, 102.0])

    rsi = agent._calculate_rsi(prices, 14)
    assert np.all(rsi == 50.0)

    ema = agent._calculate_ema(prices, 10)
    assert np.all(ema == prices.mean())
