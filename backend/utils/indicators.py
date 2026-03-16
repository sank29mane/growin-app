
import pandas as pd
import numpy as np
from .indicator_engine import get_indicator_engine

def calculate_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    """Calculate Relative Strength Index using IndicatorEngine (AMX/Rust/CPU)."""
    engine = get_indicator_engine()
    rsi_values = engine.rsi(prices.values, period)
    return pd.Series(rsi_values, index=prices.index, name="RSI")

def calculate_volatility(high: pd.Series, low: pd.Series, period: int = 14) -> pd.Series:
    """Calculate price volatility (ATR smoothed)."""
    engine = get_indicator_engine()
    # IndicatorEngine requires high, low, close for ATR. We use close as fallback.
    atr_values = engine.atr(high.values, low.values, high.values, period)
    return pd.Series(atr_values, index=high.index, name="Volatility")

def add_intraday_features(df: pd.DataFrame) -> pd.DataFrame:
    """Adds a standard set of intraday technical features using IndicatorEngine."""
    engine = get_indicator_engine()
    return engine.add_all_indicators(df)
