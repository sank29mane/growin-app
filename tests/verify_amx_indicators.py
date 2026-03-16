"""
Verification script for hardware-accelerated IndicatorEngine.
Compares MLX output with NumPy/Pandas baseline.
"""

import numpy as np
import pandas as pd
import time
import logging
from backend.utils.indicator_engine import IndicatorEngine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("VerifyAMX")

def test_indicators():
    # 1. Generate Synthetic Data (100,000 points)
    n = 100000
    t = np.linspace(0, 10, n)
    close = 100 + 10 * np.sin(t) + np.random.normal(0, 1, n)
    high = close + np.random.uniform(0, 2, n)
    low = close - np.random.uniform(0, 2, n)
    
    df = pd.DataFrame({
        'close': close,
        'high': high,
        'low': low
    })
    
    engine_mlx = IndicatorEngine(force_backend="mlx")
    engine_cpu = IndicatorEngine(force_backend="numpy")
    
    logger.info("🚀 Starting AMX/MLX vs CPU Comparison")
    
    # --- SMA ---
    start = time.time()
    sma_mlx = engine_mlx.sma(close, 20)
    mlx_dur = (time.time() - start) * 1000
    
    start = time.time()
    sma_cpu = engine_cpu.sma(close, 20)
    cpu_dur = (time.time() - start) * 1000
    
    diff = np.abs(sma_mlx - sma_cpu).max()
    logger.info(f"SMA (20): MLX={mlx_dur:.2f}ms, CPU={cpu_dur:.2f}ms | Max Diff: {diff:.6f}")
    
    # --- RSI ---
    start = time.time()
    rsi_mlx = engine_mlx.rsi(close, 14)
    mlx_dur = (time.time() - start) * 1000
    
    start = time.time()
    rsi_cpu = engine_cpu.rsi(close, 14)
    cpu_dur = (time.time() - start) * 1000
    
    diff = np.abs(rsi_mlx - rsi_cpu).max()
    logger.info(f"RSI (14): MLX={mlx_dur:.2f}ms, CPU={cpu_dur:.2f}ms | Max Diff: {diff:.6f}")
    
    # --- ATR ---
    start = time.time()
    atr_mlx = engine_mlx.atr(high, low, close, 14)
    mlx_dur = (time.time() - start) * 1000
    
    start = time.time()
    atr_cpu = engine_cpu.atr(high, low, close, 14)
    cpu_dur = (time.time() - start) * 1000
    
    diff = np.abs(atr_mlx - atr_cpu).max()
    logger.info(f"ATR (14): MLX={mlx_dur:.2f}ms, CPU={cpu_dur:.2f}ms | Max Diff: {diff:.6f}")

    # --- Full Enrichment ---
    start = time.time()
    enriched = engine_mlx.add_all_indicators(df)
    logger.info(f"Full enrichment (1000 rows): {(time.time() - start)*1000:.2f}ms")
    
    logger.info("✅ Verification complete")

if __name__ == "__main__":
    test_indicators()
