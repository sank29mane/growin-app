
import sys
import os
import time
import pandas as pd
import numpy as np

# Add repo root to path so we can import backend
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from backend.forecast_bridge import _process_forecast_data

def original_logic(forecast_df, timeframe, last_real_ts, avg_delta, robust_std, robust_mean, last_vol, ohlcv_data, anchor_bias=0.0):
    forecast = []

    # Original logic adapted to include anchor_bias inside loop equivalent
    # In original code, anchor_bias was applied AFTER the loop.
    # Here we simulate the end result for comparison.

    for idx, row in forecast_df.iterrows():
        step_num = idx + 1

        if timeframe == "1Day":
            current_ts = last_real_ts
            days_added = 0
            while days_added < step_num:
                current_ts += pd.Timedelta(days=1)
                if current_ts.dayofweek >= 5:
                    continue
                days_added += 1
            future_ts = current_ts
        else:
            future_ts = last_real_ts + (avg_delta * step_num)

        scaled_price = row.get('close', 0)

        try:
            unscaled_price = float((scaled_price * robust_std) + robust_mean)
        except:
            unscaled_price = float(ohlcv_data[-1]['c'])

        if np.isnan(unscaled_price) or np.isinf(unscaled_price):
            unscaled_price = float(ohlcv_data[-1]['c'])

        # Apply anchor bias (simulating the post-loop update)
        unscaled_price += anchor_bias

        forecast.append({
            "open": float(unscaled_price * 0.999),
            "high": float(unscaled_price * 1.005),
            "low": float(unscaled_price * 0.995),
            "close": float(unscaled_price),
            "volume": float(last_vol),
            "timestamp": int(future_ts.timestamp() * 1000)
        })
    return forecast

def test_forecast_vectorization_1hour():
    N = 100
    forecast_df = pd.DataFrame({'close': np.random.randn(N)})
    last_real_ts = pd.Timestamp("2023-10-27 15:30:00")
    avg_delta = pd.Timedelta(hours=1)
    robust_std = 1.5
    robust_mean = 100.0
    last_vol = 5000
    ohlcv_data = [{'c': 100.0, 'v': 5000}]
    anchor_bias = 5.0

    # Run Original
    f1 = original_logic(forecast_df, "1Hour", last_real_ts, avg_delta, robust_std, robust_mean, last_vol, ohlcv_data, anchor_bias)

    # Run Optimized
    f2 = _process_forecast_data(
        forecast_df=forecast_df,
        timeframe="1Hour",
        last_real_ts=last_real_ts,
        avg_delta=avg_delta,
        robust_std=robust_std,
        robust_mean=robust_mean,
        last_vol=last_vol,
        fallback_price=ohlcv_data[-1]['c'],
        anchor_bias=anchor_bias
    )

    # Compare
    assert len(f1) == len(f2)
    for i in range(len(f1)):
        # Allow small float differences
        assert abs(f1[i]['close'] - f2[i]['close']) < 1e-9
        assert f1[i]['timestamp'] == f2[i]['timestamp']

def test_forecast_vectorization_1day_business_logic():
    # Test Weekend Skipping
    N = 20 # Enough to cross multiple weekends
    forecast_df = pd.DataFrame({'close': np.random.randn(N)})
    last_real_ts = pd.Timestamp("2023-10-27 15:30:00") # Friday
    # Next business days should be: Mon, Tue, Wed...

    avg_delta = pd.Timedelta(days=1)
    robust_std = 1.0
    robust_mean = 100.0
    last_vol = 5000
    ohlcv_data = [{'c': 100.0, 'v': 5000}]
    anchor_bias = 0.0

    # Run Original
    f1 = original_logic(forecast_df, "1Day", last_real_ts, avg_delta, robust_std, robust_mean, last_vol, ohlcv_data, anchor_bias)

    # Run Optimized
    f2 = _process_forecast_data(
        forecast_df=forecast_df,
        timeframe="1Day",
        last_real_ts=last_real_ts,
        avg_delta=avg_delta,
        robust_std=robust_std,
        robust_mean=robust_mean,
        last_vol=last_vol,
        fallback_price=ohlcv_data[-1]['c'],
        anchor_bias=anchor_bias
    )

    # Check correctness of timestamps
    assert len(f1) == len(f2)
    for i in range(len(f1)):
        ts1 = f1[i]['timestamp']
        ts2 = f2[i]['timestamp']
        assert ts1 == ts2, f"Timestamp mismatch at index {i}: {ts1} vs {ts2}"

        # Verify it is not a weekend
        dt = pd.to_datetime(ts1, unit='ms')
        assert dt.dayofweek < 5, f"Date {dt} is a weekend!"

def test_performance_benchmark():
    # Only run if we want to log perf stats (optional for CI, but good for Journal)
    N = 1000
    forecast_df = pd.DataFrame({'close': np.random.randn(N)})
    last_real_ts = pd.Timestamp("2023-10-27 15:30:00")
    avg_delta = pd.Timedelta(hours=1)
    robust_std = 1.5
    robust_mean = 100.0
    last_vol = 5000
    ohlcv_data = [{'c': 100.0, 'v': 5000}]
    anchor_bias = 0.0

    start = time.time()
    original_logic(forecast_df, "1Hour", last_real_ts, avg_delta, robust_std, robust_mean, last_vol, ohlcv_data, anchor_bias)
    t1 = time.time() - start

    start = time.time()
    _process_forecast_data(
        forecast_df=forecast_df,
        timeframe="1Hour",
        last_real_ts=last_real_ts,
        avg_delta=avg_delta,
        robust_std=robust_std,
        robust_mean=robust_mean,
        last_vol=last_vol,
        fallback_price=ohlcv_data[-1]['c'],
        anchor_bias=anchor_bias
    )
    t2 = time.time() - start

    print(f"\nBenchmark N={N}: Original={t1:.5f}s, Optimized={t2:.5f}s, Speedup={t1/t2:.2f}x")
    assert t2 < t1 # Should be faster

if __name__ == "__main__":
    # Allow running directly
    test_forecast_vectorization_1hour()
    test_forecast_vectorization_1day_business_logic()
    test_performance_benchmark()
