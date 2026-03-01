
import pandas as pd
import numpy as np

# Bolt Optimization: Import optional dependencies at module level to avoid repeated ImportErrors (PR #48)
try:
    import growin_core
    GROWIN_CORE_AVAILABLE = True
except ImportError:
    growin_core = None
    GROWIN_CORE_AVAILABLE = False

def calculate_rsi(prices: pd.Series, period: int = 14) -> pd.Series:
    """Calculate Relative Strength Index using Rust core with pure Python fallback."""
    # Convert Series to list for Rust
    prices_list = prices.fillna(0).tolist()
    
    if GROWIN_CORE_AVAILABLE:
        try:
            # Call Rust extension
            rsi_values = growin_core.calculate_rsi(prices_list, period)
            # Return as Series with same index
            return pd.Series(rsi_values, index=prices.index, name="RSI")
        except Exception:
            pass # Fallback to python if rust fails

    # Pure Python Fallback
    delta = prices.diff()
    gain = (delta.where(delta > 0, 0)).ewm(alpha=1/period, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/period, adjust=False).mean()
    
    rs = gain / loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    rsi = rsi.fillna(50.0) # Default to 50 if undefined

    return pd.Series(rsi, index=prices.index, name="RSI")

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
