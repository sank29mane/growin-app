import time
import pytest
import numpy as np
from backend.features.online_vol import OnlineVolatility
from backend.features.online_spread import RelativeSpread
from backend.features.welford import WelfordStandardizer

def test_online_vol_correctness():
    vol = OnlineVolatility(alpha=0.1)
    
    # First update should return 0.0 (not enough data to calculate returns)
    assert vol.update(100.0) == 0.0
    
    # Second update
    # price = 100.0 -> price = 110.0, return = (110 - 100)/100 = 0.1
    # var = 0.1 * (0.1^2) + 0.9 * 0.0 = 0.001
    # vol = sqrt(0.001) ≈ 0.03162277
    v2 = vol.update(110.0)
    assert pytest.approx(v2, abs=1e-6) == np.sqrt(0.001)
    
    # Third update
    # price = 110.0 -> price = 99.0, return = (99 - 110)/110 = -0.1
    # var = 0.1 * ((-0.1)^2) + 0.9 * 0.001 = 0.001 + 0.0009 = 0.0019
    # vol = sqrt(0.0019) ≈ 0.043588989
    v3 = vol.update(99.0)
    assert pytest.approx(v3, abs=1e-6) == np.sqrt(0.0019)

def test_relative_spread_correctness():
    rs = RelativeSpread()
    # bid=100.0, ask=102.0
    # mid = 101.0
    # spread = 2.0 / 101.0
    s1 = rs.update(100.0, 102.0)
    assert pytest.approx(s1, abs=1e-6) == (2.0 / 101.0)
    
    # Test boundary condition (mid is 0)
    assert rs.update(0.0, 0.0) == 0.0

def test_welford_standardizer_correctness():
    w = WelfordStandardizer(ddof=0)
    
    # Test values: [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
    # Mean: 5.0, Population Var: 4.0, Population Std: 2.0
    values = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
    z_scores = []
    for val in values:
        z_scores.append(w.update(val))
        
    assert pytest.approx(w.mean, abs=1e-6) == 5.0
    assert pytest.approx(w.variance, abs=1e-6) == 4.0
    assert pytest.approx(w.std, abs=1e-6) == 2.0
    
    # Z-scores computed online. Final value 9.0 should have Z-score (9.0 - 5.0) / 2.0 = 2.0
    assert pytest.approx(z_scores[-1], abs=1e-6) == 2.0

def test_welford_initialization():
    w = WelfordStandardizer(ddof=0)
    w.initialize_state(mean=10.0, variance=4.0, count=1000)
    
    assert w.mean == 10.0
    assert w.variance == 4.0
    assert w.std == 2.0
    assert w.count == 1000
    
    # Check that Z-scoring uses the initialized state
    assert pytest.approx(w.zscore(12.0), abs=1e-6) == 1.0

def test_performance_microbenchmarks():
    vol = OnlineVolatility(alpha=0.1)
    rs = RelativeSpread()
    w = WelfordStandardizer(ddof=0)
    
    # Warm up JIT compilers so we don't measure compile overhead
    vol.update(100.0)
    vol.update(101.0)
    rs.update(100.0, 101.0)
    w.update(1.0)
    
    N = 100000
    
    # Benchmark OnlineVolatility
    t0 = time.perf_counter()
    for i in range(N):
        vol.update(100.0 + (i % 10))
    t1 = time.perf_counter()
    vol_time_per_tick = (t1 - t0) / N
    
    # Benchmark RelativeSpread
    t0 = time.perf_counter()
    for i in range(N):
        rs.update(100.0 + (i % 5), 101.0 + (i % 5))
    t1 = time.perf_counter()
    spread_time_per_tick = (t1 - t0) / N
    
    # Benchmark WelfordStandardizer
    t0 = time.perf_counter()
    for i in range(N):
        w.update(1.0 + (i % 10))
    t1 = time.perf_counter()
    welford_time_per_tick = (t1 - t0) / N
    
    print(f"\nVolatility update time: {vol_time_per_tick * 1e6:.4f} us per tick")
    print(f"Spread update time: {spread_time_per_tick * 1e6:.4f} us per tick")
    print(f"Welford update time: {welford_time_per_tick * 1e6:.4f} us per tick")
    
    # Assert latency is < 1us (1e-6 seconds)
    assert vol_time_per_tick < 1e-6, f"Volatility latency too high: {vol_time_per_tick * 1e6:.2f} us"
    assert spread_time_per_tick < 1e-6, f"Spread latency too high: {spread_time_per_tick * 1e6:.2f} us"
    assert welford_time_per_tick < 1e-6, f"Welford latency too high: {welford_time_per_tick * 1e6:.2f} us"
