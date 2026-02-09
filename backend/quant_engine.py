import pandas as pd
try:
    import pandas_ta as ta
    PANDAS_TA_AVAILABLE = True
except ImportError:
    ta = None
    PANDAS_TA_AVAILABLE = False

# Bolt Optimization: Import optional dependencies at module level to avoid repeated ImportErrors
try:
    import growin_core
    RUST_CORE_AVAILABLE = True
except ImportError:
    growin_core = None
    RUST_CORE_AVAILABLE = False

try:
    import mlx.core as mx
    import mlx.nn as nn
    MLX_AVAILABLE = True
except ImportError:
    mx = None
    MLX_AVAILABLE = False

from typing import Dict, List, Any, Optional, TypedDict, Union
from enum import Enum
from decimal import Decimal
from backend.utils.financial_math import create_decimal, safe_div, PRECISION_CURRENCY

class TechnicalIndicators(TypedDict, total=False):
    rsi: Optional[float]
    macd: Optional[float]
    macd_signal: Optional[float]
    macd_hist: Optional[float]
    bb_upper: Optional[float]
    bb_middle: Optional[float]
    bb_lower: Optional[float]
    ema_50: Optional[float]
    ema_200: Optional[float]
    volume_sma: Optional[float]

class AnalysisResult(TypedDict):
    indicators: TechnicalIndicators
    signals: Dict[str, str]
    current_price: float
    data_points: int

class PortfolioMetrics(TypedDict):
    total_value: float
    total_cost: float
    total_pnl: float
    portfolio_return: float
    position_count: int


class TimeFrame(Enum):
    MINUTE_1 = "1Min"
    MINUTE_5 = "5Min"
    MINUTE_15 = "15Min"
    HOUR_1 = "1Hour"
    DAY = "1Day"


class QuantEngine:
    """
    Quantitative Analysis Engine using pandas-ta for technical indicators
    and portfolio metrics. Processes OHLCV data for trading insights.
    """

    def __init__(self):
        pass

    def calculate_technical_indicators(self, ohlcv_data: List[Dict[str, Any]]) -> Union[AnalysisResult, Dict[str, str]]:
        """
        Calculate technical indicators from OHLCV data using Rust-optimized core.
        Input: List of dicts with keys 't', 'o', 'h', 'l', 'c', 'v'
        Output: Dict with indicators and signals
        """
        if not ohlcv_data:
            return {"error": "No OHLCV data provided"}

        # Convert to pandas DataFrame for lightweight preprocessing (sorting/filling)
        # We still use pandas for data alignment convenience, but heavy calc goes to Rust
        df = pd.DataFrame(ohlcv_data)
        df['timestamp'] = pd.to_datetime(df['t'], unit='ms')
        df.set_index('timestamp', inplace=True)
        df.rename(columns={'o': 'open', 'h': 'high', 'l': 'low', 'c': 'close', 'v': 'volume'}, inplace=True)
        df.sort_index(inplace=True)

        # Extract vectors for Rust
        close_prices = df['close'].fillna(0).tolist()
        
        # Calculate indicators via Rust Core
        indicators = {}
        
        # Priority: MLX (M-series NPU) > Rust > Pandas-TA > Pure Pandas
        # Hybrid Approach: Use MLX for vectorized ops (SMA, BBands), allow fallback to Rust/Pandas for recursive ops (RSI, EMI)
        if MLX_AVAILABLE and len(close_prices) > 0:
            try:
                 # convert to MLX array (float32 for NPU optimization)
                 price_array = mx.array(close_prices).astype(mx.float32)
                 
                 # --- Bollinger Bands & SMA (Convolution-based) ---
                 # We use 1D convolution for rolling mean, which is highly parallelizable on NPU
                 window_size = 20
                 weight_sma = mx.full((window_size,), 1.0/window_size).astype(mx.float32)
                 
                 # Reshape for conv1d: [Batch, Length, Channels] = [1, L, 1]
                 # Weights: [Out_Channels, Kernel_Size, In_Channels] = [1, 20, 1]
                 x_reshaped = price_array.reshape(1, -1, 1)
                 w_reshaped = weight_sma.reshape(1, window_size, 1)
                 
                 # Compute SMA20 via convolution
                 # padding=0 gives 'valid' output (size reduces by window-1)
                 sma20_conv = mx.conv1d(x_reshaped, w_reshaped, stride=1, padding=0)
                 
                 if sma20_conv.size > 0:
                     # Access element: [0, -1, 0] -> Last time step
                     indicators['bb_middle'] = float(sma20_conv[0, -1, 0].item())
                     
                     # Standard Deviation Calculation
                     # Var(X) = E[X^2] - (E[X])^2
                     x2 = price_array ** 2
                     x2_reshaped = x2.reshape(1, -1, 1)
                     
                     # E[X^2] windowed
                     sma20_x2 = mx.conv1d(x2_reshaped, w_reshaped, stride=1, padding=0)
                     
                     # Variance = Mean(Square) - Square(Mean)
                     # Note: sma20_conv ** 2 is element-wise square of the mean
                     var20 = sma20_x2 - (sma20_conv ** 2)
                     var20 = mx.maximum(var20, 0.0) # Clip negative optimized errors
                     std20 = mx.sqrt(var20)
                     
                     std_val = std20[0, -1, 0]
                     mean_val = sma20_conv[0, -1, 0]
                     
                     indicators['bb_upper'] = float((mean_val + 2 * std_val).item())
                     indicators['bb_lower'] = float((mean_val - 2 * std_val).item())

                 # --- Volume SMA (Vectorized) ---
                 if 'volume' in df:
                     vol_array = mx.array(df['volume'].fillna(0).tolist()).astype(mx.float32)
                     vol_reshaped = vol_array.reshape(1, -1, 1)
                     # Reuse kernel since window is same (20)
                     vol_sma_conv = mx.conv1d(vol_reshaped, w_reshaped, stride=1, padding=0)
                     if vol_sma_conv.size > 0:
                         indicators['volume_sma'] = float(vol_sma_conv[0, -1, 0].item())

            except Exception as e:
                # Log usage failure but continue seamlessly to Rust/Pandas fallback
                print(f"MLX NPU Calculation Warning: {e} - Falling back to CPU.")
                pass
        if RUST_CORE_AVAILABLE:
            try:
                # RSI
                if 'rsi' not in indicators:
                    rsi_vals = growin_core.calculate_rsi(close_prices, 14)
                    indicators['rsi'] = rsi_vals[-1] if rsi_vals else None

                # MACD
                if 'macd' not in indicators:
                    macd_line, macd_signal, macd_hist = growin_core.calculate_macd(close_prices, 12, 26, 9)
                    if macd_line:
                        indicators['macd'] = macd_line[-1]
                        indicators['macd_signal'] = macd_signal[-1]
                        indicators['macd_hist'] = macd_hist[-1]

                # Bollinger Bands
                if 'bb_upper' not in indicators:
                    upper, middle, lower = growin_core.calculate_bbands(close_prices, 20, 2.0)
                    if upper:
                        indicators['bb_upper'] = upper[-1]
                        indicators['bb_middle'] = middle[-1]
                        indicators['bb_lower'] = lower[-1]

                # EMA
                if 'ema_50' not in indicators:
                    ema_50_vals = growin_core.calculate_ema(close_prices, 50)
                    indicators['ema_50'] = ema_50_vals[-1] if len(ema_50_vals) >= 50 else None

                if 'ema_200' not in indicators:
                    ema_200_vals = growin_core.calculate_ema(close_prices, 200)
                    indicators['ema_200'] = ema_200_vals[-1] if len(ema_200_vals) >= 200 else None

                # Volume SMA
                if 'volume_sma' not in indicators:
                    vol_vals = growin_core.calculate_sma(df['volume'].fillna(0).tolist(), 20)
                    indicators['volume_sma'] = vol_vals[-1] if vol_vals else None
            except Exception as e:
                # If Rust fails, we might still have partials/MLX
                if not indicators: return {"error": f"Indicator calculation failed: {e}"}
        elif PANDAS_TA_AVAILABLE and ta is not None:
             # Fallback to pandas-ta if Rust is missing
             try:
                # RSI
                df.ta.rsi(length=14, append=True)
                indicators['rsi'] = df['RSI_14'].iloc[-1] if 'RSI_14' in df else None

                # MACD
                df.ta.macd(fast=12, slow=26, signal=9, append=True)
                if 'MACD_12_26_9' in df:
                    indicators['macd'] = df['MACD_12_26_9'].iloc[-1]
                    indicators['macd_signal'] = df['MACDs_12_26_9'].iloc[-1]
                    indicators['macd_hist'] = df['MACDh_12_26_9'].iloc[-1]

                # Bollinger Bands
                df.ta.bbands(length=20, std=2.0, append=True)
                if 'BBU_20_2.0' in df:
                    indicators['bb_upper'] = df['BBU_20_2.0'].iloc[-1]
                    indicators['bb_middle'] = df['BBM_20_2.0'].iloc[-1]
                    indicators['bb_lower'] = df['BBL_20_2.0'].iloc[-1]

                # EMA
                df.ta.ema(length=50, append=True)
                indicators['ema_50'] = df['EMA_50'].iloc[-1] if 'EMA_50' in df else None

                df.ta.ema(length=200, append=True)
                indicators['ema_200'] = df['EMA_200'].iloc[-1] if 'EMA_200' in df else None

                # Volume SMA
                vol_sma = df['volume'].rolling(window=20).mean()
                indicators['volume_sma'] = vol_sma.iloc[-1] if not vol_sma.empty else None

             except Exception as e:
                 return {"error": f"pandas-ta calculation failed: {e}"}
        else:
            # Fallback to Pure Pandas (Vectorized)
            try:
                close = df['close']

                # RSI (Wilder's Smoothing)
                delta = close.diff()
                gain = (delta.where(delta > 0, 0)).ewm(alpha=1/14, adjust=False).mean()
                loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/14, adjust=False).mean()
                rs = gain / loss
                rsi_series = 100 - (100 / (1 + rs))
                indicators['rsi'] = rsi_series.iloc[-1] if not rsi_series.empty else None

                # MACD (12, 26, 9)
                exp1 = close.ewm(span=12, adjust=False).mean()
                exp2 = close.ewm(span=26, adjust=False).mean()
                macd_line = exp1 - exp2
                signal_line = macd_line.ewm(span=9, adjust=False).mean()
                macd_hist = macd_line - signal_line

                indicators['macd'] = macd_line.iloc[-1]
                indicators['macd_signal'] = signal_line.iloc[-1]
                indicators['macd_hist'] = macd_hist.iloc[-1]

                # Bollinger Bands (20, 2)
                sma20 = close.rolling(window=20).mean()
                std20 = close.rolling(window=20).std()
                indicators['bb_upper'] = (sma20 + (std20 * 2)).iloc[-1]
                indicators['bb_middle'] = sma20.iloc[-1]
                indicators['bb_lower'] = (sma20 - (std20 * 2)).iloc[-1]

                # EMA
                indicators['ema_50'] = close.ewm(span=50, adjust=False).mean().iloc[-1]
                indicators['ema_200'] = close.ewm(span=200, adjust=False).mean().iloc[-1]

                # Volume SMA
                indicators['volume_sma'] = df['volume'].rolling(window=20).mean().iloc[-1]

            except Exception as e:
                return {"error": f"Pure Pandas calculation failed: {e}"}

        # Generate signals
        signals = self._generate_signals(indicators, df['close'].iloc[-1])

        return {
            "indicators": indicators,
            "signals": signals,
            "current_price": df['close'].iloc[-1],
            "data_points": len(df)
        }

    def calculate_portfolio_metrics(self, positions: List[Dict[str, Any]], benchmark_returns: Optional[List[float]] = None) -> Union[PortfolioMetrics, Dict[str, str]]:
        """
        Calculate snapshot portfolio-level metrics.
        Input: List of position dicts with 'symbol', 'qty', 'current_price', 'avg_cost'
        Note: This does NOT calculate time-series metrics like Sharpe Ratio or Beta,
        as those require historical data which is not available in a snapshot.
        """
        if not positions:
            return {"error": "No positions provided"}

        total_value = Decimal(0)
        total_cost = Decimal(0)

        for pos in positions:
            qty = create_decimal(pos.get('qty', 0))
            price = create_decimal(pos.get('current_price', 0))
            avg_cost = create_decimal(pos.get('avg_cost', 0))
            
            total_value += qty * price
            total_cost += qty * avg_cost

        if total_value == Decimal(0):
            return {"error": "Portfolio value is zero"}

        total_pnl = total_value - total_cost
        portfolio_return = safe_div(total_pnl, total_cost)

        return {
            "total_value": float(total_value),  # Return float for JSON compatibility
            "total_cost": float(total_cost),
            "total_pnl": float(total_pnl),
            "portfolio_return": float(portfolio_return),
            "position_count": len(positions)
        }

    def _generate_signals(self, indicators: Dict[str, Any], current_price: float) -> Dict[str, Any]:
        """Generate buy/sell signals based on indicators"""
        signals = {
            "trend": "neutral",
            "momentum": "neutral",
            "volatility": "neutral",
            "overall_signal": "hold"
        }

        # Trend signals (EMA crossover)
        if indicators.get('ema_50') and indicators.get('ema_200'):
            if current_price > indicators['ema_50'] > indicators['ema_200']:
                signals['trend'] = "bullish"
            elif current_price < indicators['ema_50'] < indicators['ema_200']:
                signals['trend'] = "bearish"

        # Momentum signals (RSI)
        rsi = indicators.get('rsi')
        if rsi:
            if rsi > 70:
                signals['momentum'] = "overbought"
            elif rsi < 30:
                signals['momentum'] = "oversold"

        # Volatility signals (Bollinger Bands)
        bb_upper = indicators.get('bb_upper')
        bb_lower = indicators.get('bb_lower')
        if bb_upper and bb_lower:
            if current_price > bb_upper:
                signals['volatility'] = "high"
            elif current_price < bb_lower:
                signals['volatility'] = "low"

        # Overall signal logic
        if signals['trend'] == "bullish" and signals['momentum'] == "oversold":
            signals['overall_signal'] = "buy"
        elif signals['trend'] == "bearish" and signals['momentum'] == "overbought":
            signals['overall_signal'] = "sell"

        return signals

    def analyze_rebalancing_opportunity(self, current_allocation: Dict[str, str], target_allocation: Dict[str, float],
                                      total_portfolio_value: float) -> Dict[str, Any]:
        """
        Analyze if portfolio needs rebalancing based on target allocations.
        Input:
            current_allocation: dict of symbol: percentage as string (e.g., "50%")
            target_allocation: dict of symbol: percentage as float (e.g., 0.5)
            total_portfolio_value: total value of the portfolio in account currency
        """
        # Parse current allocation
        current_parsed: Dict[str, Decimal] = {}
        
        # Convert total portfolio value to Decimal
        total_value_dec = create_decimal(total_portfolio_value)
        if total_value_dec <= 0:
             return {"error": "Total portfolio value must be positive"}

        for symbol, pct_val in current_allocation.items():
            try:
                # Handle string "50%" or float 0.5 or 50
                val_str = str(pct_val).strip().replace('%', '')
                val_dec = create_decimal(val_str)
                
                # Heuristic: if > 1.0, assume it's a percentage (e.g. 50 -> 0.5)
                # Unless it's exactly 1.0 (100% or 1.0 ratio? Ambiguous, assume ratio if not formatted as %)
                if val_dec > 1:
                    val_dec = val_dec / 100
                
                current_parsed[symbol] = val_dec
            except Exception:
                current_parsed[symbol] = Decimal(0)

        # Calculate deviations
        deviations = {}
        rebalance_actions = []

        # Iterate through all unique symbols
        all_symbols = set(current_parsed.keys()) | set(target_allocation.keys())

        for symbol in all_symbols:
            current_pct = current_parsed.get(symbol, Decimal(0))
            target_pct = create_decimal(target_allocation.get(symbol, 0))
            
            deviation = target_pct - current_pct
            deviations[symbol] = float(deviation)

            # Threshold: 5% deviation (0.05)
            if abs(deviation) > Decimal("0.05"):
                current_val_amt = current_pct * total_value_dec
                target_val_amt = target_pct * total_value_dec
                
                value_change = target_val_amt - current_val_amt
                
                rebalance_actions.append({
                    "symbol": symbol,
                    "current_pct": float(current_pct),
                    "target_pct": float(target_pct),
                    "deviation": float(deviation),
                    "action": "buy" if value_change > 0 else "sell",
                    "value_change": float(abs(value_change))
                })

        return {
            "total_value": float(total_value_dec),
            "deviations": deviations,
            "rebalance_actions": rebalance_actions,
            "needs_rebalancing": len(rebalance_actions) > 0
        }


# --- Utility Functions ---

def get_quant_engine():
    return QuantEngine()


if __name__ == '__main__':
    print("QuantEngine module loaded.")