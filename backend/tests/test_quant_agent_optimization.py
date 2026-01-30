
import pytest
import numpy as np
import pandas as pd
import time
from agents.quant_agent import QuantAgent

# We will patch QuantAgent to expose the original slow method for comparison
# or just implement it here as reference.

def original_calculate_ema(data: np.ndarray, period: int) -> np.ndarray:
    """Exponential Moving Average (Original Loop Implementation)"""
    if len(data) < period:
        return np.full(len(data), data.mean() if len(data) > 0 else 0)
    ema = np.zeros(len(data))
    ema[period-1] = data[:period].mean()
    multiplier = 2 / (period + 1)
    for i in range(period, len(data)):
        ema[i] = (data[i] - ema[i-1]) * multiplier + ema[i-1]
    return ema

def test_ema_vectorization_correctness():
    """Verify that the new vectorized implementation matches the original one."""
    np.random.seed(42)
    data = np.random.rand(1000) * 100
    period = 20

    # Original implementation
    expected = original_calculate_ema(data, period)

    # New implementation (we will mock this into the agent or just test the logic)
    # But for this test, we want to test the *actual* agent method after modification.
    # Since we haven't modified it yet, this test is expected to fail if we asserted equality against a broken implementation,
    # but here we want to assert equality against the Reference Implementation.

    # Ideally, we call QuantAgent()._calculate_ema(data, period)
    # and assert it matches expected.
    # BEFORE optimization: it matches because it IS the original code.
    # AFTER optimization: it MUST still match.

    agent = QuantAgent()
    # Force use of fallback (disable talib if present, or just call _calculate_ema directly)
    # QuantAgent._calculate_ema is the python fallback.

    actual = agent._calculate_ema(data, period)

    # Compare
    # The first period-1 elements are 0 in original.
    diff = np.abs(expected[period-1:] - actual[period-1:])
    max_diff = diff.max()

    assert max_diff < 1e-10, f"Max difference {max_diff} exceeds tolerance"

def test_ema_performance():
    """Benchmark performance improvement."""
    np.random.seed(42)
    data = np.random.rand(100000) * 100
    period = 50

    start = time.time()
    original_calculate_ema(data, period)
    original_time = time.time() - start

    agent = QuantAgent()
    start = time.time()
    agent._calculate_ema(data, period)
    current_time = time.time() - start

    print(f"\nOriginal Time: {original_time:.6f}s")
    print(f"Current Time:  {current_time:.6f}s")

    # Only assert speedup if we expect it (i.e. after optimization)
    # For now, just logging it.
