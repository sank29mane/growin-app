
import pandas as pd
import numpy as np
from typing import List, Dict, Any

def calculate_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    """Calculate Relative Strength Index."""
    delta = prices.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    
    # Avoid division by zero
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50)  # Default to neutral

def calculate_volatility(high: pd.Series, low: pd.Series, period: int = 14) -> pd.Series:
    """Calculate price volatility (High-Low spread smoothed)."""
    spread = high - low
    return spread.rolling(window=period).mean().bfill().fillna(0)

def add_intraday_features(df: pd.DataFrame) -> pd.DataFrame:
    """Adds a standard set of intraday technical features to a dataframe."""
    df = df.copy()
    
    # Technical Indicators
    df['rsi'] = calculate_rsi(df['close'])
    df['vol_ma'] = calculate_volatility(df['high'], df['low'])
    
    # Moving Average Deviations
    df['ma_20'] = df['close'].rolling(window=20).mean()
    df['ma_dist'] = (df['close'] - df['ma_20']) / df['ma_20'].replace(0, 1) # Normalize dist
    
    # Simple Momentum
    df['roc_3'] = df['close'].pct_change(3)
    
    return df.fillna(0)
