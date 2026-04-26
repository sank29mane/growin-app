"""
Financial Math Utilities - Precision-safe calculations for Growin App
Uses Python's decimal module to avoid floating point errors.
Standard: 2026 SOTA Financial Best Practices
"""

from decimal import Decimal, getcontext, ROUND_HALF_UP
from typing import Union, Any, List, Dict, Optional, Tuple
import numpy as np
import pandas as pd

# Bolt Optimization: Import optional dependencies at module level
try:
    import growin_core
    RUST_CORE_AVAILABLE = True
except ImportError:
    growin_core = None
    RUST_CORE_AVAILABLE = False

from utils.mlx_loader import mx, HAS_MLX as MLX_AVAILABLE

# Standard Financial Precision (4 decimal places for intermediate, 2 for display)
PRECISION_INTERNAL = 4
PRECISION_DISPLAY = 2
PRECISION_CURRENCY = Decimal('0.01')

# Set global context for financial calculations
getcontext().rounding = ROUND_HALF_UP

def create_decimal(value: Any) -> Decimal:
    """Safe conversion to Decimal, handling strings, floats, ints, and NaN."""
    if value is None:
        return Decimal('0')
    if isinstance(value, float):
        import math
        if math.isnan(value) or math.isinf(value):
            return Decimal('0')
    if isinstance(value, str):
        # Remove currency symbols or commas if present
        clean_val = value.replace('£', '').replace('$', '').replace(',', '').strip()
        if clean_val.lower() in ['nan', 'inf', '-inf']:
            return Decimal('0')
        try:
            return Decimal(clean_val)
        except Exception:
            return Decimal('0')
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal('0')

def safe_div(numerator: Union[Decimal, float, str], denominator: Union[Decimal, float, str]) -> Decimal:
    """Divide with zero-check and return Decimal."""
    n = create_decimal(numerator)
    d = create_decimal(denominator)
    if d == 0:
        return Decimal('0')
    return n / d

def quantize_currency(value: Union[Decimal, float, str]) -> Decimal:
    """Round to 2 decimal places using ROUND_HALF_UP."""
    return create_decimal(value).quantize(PRECISION_CURRENCY, rounding=ROUND_HALF_UP)

def calculate_pnl_percent(current_value: Decimal, total_invested: Decimal) -> float:
    """Calculate PnL percentage safely."""
    if total_invested == 0:
        return 0.0
    return float((current_value - total_invested) / total_invested)

class TechnicalIndicators:
    """
    Unified Technical Indicators Library with Multi-Backend Support.
    Prioritizes: MLX (GPU) > Rust (CPU Optimized) > NumPy/Pandas (Standard).
    """

    @staticmethod
    def calculate_rsi(prices: Union[List[float], np.ndarray], period: int = 14, backend: str = 'auto') -> np.ndarray:
        """
        Calculates Relative Strength Index (RSI).
        """
        data = np.array(prices)
        if len(data) < period:
            return np.full(len(data), 50.0)

        # 1. Rust Path
        if (backend == 'auto' or backend == 'rust') and RUST_CORE_AVAILABLE:
            try:
                return np.array(growin_core.calculate_rsi(data.tolist(), period))
            except Exception:
                if backend == 'rust': raise

        # 2. NumPy Path (Fallback)
        delta = np.diff(data, prepend=data[0])
        gain = np.where(delta > 0, delta, 0.0)
        loss = np.where(delta < 0, -delta, 0.0)

        avg_gain = np.zeros_like(data)
        avg_loss = np.zeros_like(data)

        # Initial averages
        avg_gain[period] = np.mean(gain[1:period+1])
        avg_loss[period] = np.mean(loss[1:period+1])

        # Wilder's Smoothing
        for i in range(period + 1, len(data)):
            avg_gain[i] = (avg_gain[i-1] * (period - 1) + gain[i]) / period
            avg_loss[i] = (avg_loss[i-1] * (period - 1) + loss[i]) / period

        rs = np.divide(avg_gain, avg_loss, out=np.full_like(avg_gain, 100.0), where=avg_loss != 0)
        rsi = 100.0 - (100.0 / (1.0 + rs))
        
        # Warm up period
        rsi[:period] = 50.0
        return rsi

    @staticmethod
    def calculate_sma(data: Union[List[float], np.ndarray], period: int = 20, backend: str = 'auto') -> np.ndarray:
        """
        Calculates Simple Moving Average (SMA).
        """
        arr = np.array(data)
        if len(arr) < period:
            return np.zeros_like(arr)

        # 1. MLX Path
        if (backend == 'auto' or backend == 'mlx') and MLX_AVAILABLE:
            try:
                x = mx.array(arr.astype(np.float32)).reshape(1, -1, 1)
                w = mx.full((period,), 1.0/period).astype(mx.float32).reshape(1, period, 1)
                conv = mx.conv1d(x, w, stride=1, padding=0)
                # Pad leading zeros to maintain length
                res = np.zeros_like(arr)
                res[period-1:] = np.array(conv).flatten()
                return res
            except Exception:
                if backend == 'mlx': raise

        # 2. Rust Path
        if (backend == 'auto' or backend == 'rust') and RUST_CORE_AVAILABLE:
            try:
                return np.array(growin_core.calculate_sma(arr.tolist(), period))
            except Exception:
                if backend == 'rust': raise

        # 3. NumPy Path
        weights = np.ones(period) / period
        sma = np.convolve(arr, weights, mode='valid')
        res = np.zeros_like(arr)
        res[period-1:] = sma
        return res

    @staticmethod
    def calculate_ema(data: Union[List[float], np.ndarray], period: int = 14, backend: str = 'auto') -> np.ndarray:
        """
        Calculates Exponential Moving Average (EMA).
        Uses SMA of first 'period' points for initialization to match Rust core.
        """
        arr = np.array(data)
        if len(arr) < period:
            return np.zeros_like(arr)

        # 1. Rust Path
        if (backend == 'auto' or backend == 'rust') and RUST_CORE_AVAILABLE:
            try:
                return np.array(growin_core.calculate_ema(arr.tolist(), period))
            except Exception:
                if backend == 'rust': raise

        # 2. NumPy Path (Aligned with Rust)
        res = np.zeros_like(arr)
        k = 2.0 / (period + 1.0)
        
        # Initial EMA is SMA of first 'period' elements
        res[period-1] = np.mean(arr[:period])
        
        for i in range(period, len(arr)):
            res[i] = (arr[i] * k) + (res[i-1] * (1.0 - k))
            
        return res

    @staticmethod
    def calculate_macd(data: Union[List[float], np.ndarray], fast: int = 12, slow: int = 26, signal: int = 9, backend: str = 'auto') -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Calculates MACD Line, Signal Line, and Histogram.
        """
        arr = np.array(data)

        # 1. Rust Path
        if (backend == 'auto' or backend == 'rust') and RUST_CORE_AVAILABLE:
            try:
                m, s, h = growin_core.calculate_macd(arr.tolist(), fast, slow, signal)
                return np.array(m), np.array(s), np.array(h)
            except Exception:
                if backend == 'rust': raise

        # 2. NumPy/Pandas Path
        ema_fast = TechnicalIndicators.calculate_ema(arr, fast, backend='numpy')
        ema_slow = TechnicalIndicators.calculate_ema(arr, slow, backend='numpy')
        macd_line = ema_fast - ema_slow
        signal_line = TechnicalIndicators.calculate_ema(macd_line, signal, backend='numpy')
        histogram = macd_line - signal_line
        
        return macd_line, signal_line, histogram

    @staticmethod
    def calculate_bbands(data: Union[List[float], np.ndarray], period: int = 20, std_dev: float = 2.0, backend: str = 'auto') -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Calculates Bollinger Bands (Upper, Middle, Lower).
        """
        arr = np.array(data)

        # 1. MLX Path
        if (backend == 'auto' or backend == 'mlx') and MLX_AVAILABLE and len(arr) >= period:
            try:
                x = mx.array(arr.astype(np.float32)).reshape(1, -1, 1)
                w = mx.full((period,), 1.0/period).astype(mx.float32).reshape(1, period, 1)
                
                # SMA (Middle Band)
                sma_conv = mx.conv1d(x, w, stride=1, padding=0)
                
                # STD
                x2 = mx.array((arr**2).astype(np.float32)).reshape(1, -1, 1)
                sma_x2 = mx.conv1d(x2, w, stride=1, padding=0)
                var = mx.maximum(sma_x2 - (sma_conv ** 2), 0.0)
                std = mx.sqrt(var)
                
                middle = np.zeros_like(arr)
                middle[period-1:] = np.array(sma_conv).flatten()
                
                upper = np.zeros_like(arr)
                upper[period-1:] = (np.array(sma_conv) + std_dev * np.array(std)).flatten()
                
                lower = np.zeros_like(arr)
                lower[period-1:] = (np.array(sma_conv) - std_dev * np.array(std)).flatten()
                
                return upper, middle, lower
            except Exception:
                if backend == 'mlx': raise

        # 2. Rust Path
        if (backend == 'auto' or backend == 'rust') and RUST_CORE_AVAILABLE:
            try:
                u, m, l = growin_core.calculate_bbands(arr.tolist(), period, std_dev)
                return np.array(u), np.array(m), np.array(l)
            except Exception:
                if backend == 'rust': raise

        # 3. Pandas Path
        series = pd.Series(arr)
        middle = series.rolling(window=period).mean()
        std = series.rolling(window=period).std()
        upper = middle + (std_dev * std)
        lower = middle - (std_dev * std)
        
        return upper.fillna(0).values, middle.fillna(0).values, lower.fillna(0).values
