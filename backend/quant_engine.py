import pandas as pd
try:
    import pandas_ta as ta
    PANDAS_TA_AVAILABLE = True
except ImportError:
    ta = None
    PANDAS_TA_AVAILABLE = False

# Bolt Optimization: Import optional dependencies at module level
try:
    from scipy.signal import argrelextrema
    SCIPY_AVAILABLE = True
except ImportError:
    argrelextrema = None
    SCIPY_AVAILABLE = False

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
from utils.financial_math import create_decimal, safe_div, PRECISION_CURRENCY

class TechnicalIndicators(TypedDict, total=False):
    rsi: Optional[Decimal]
    macd: Optional[Decimal]
    macd_signal: Optional[Decimal]
    macd_hist: Optional[Decimal]
    bb_upper: Optional[Decimal]
    bb_middle: Optional[Decimal]
    bb_lower: Optional[Decimal]
    ema_50: Optional[Decimal]
    ema_200: Optional[Decimal]
    volume_sma: Optional[Decimal]

class AnalysisResult(TypedDict):
    indicators: TechnicalIndicators
    signals: Dict[str, str]
    current_price: Decimal
    data_points: int

class PortfolioMetrics(TypedDict):
    total_value: Decimal
    total_cost: Decimal
    total_pnl: Decimal
    portfolio_return: Decimal
    position_count: int


class TimeFrame(Enum):
    MINUTE_1 = "1Min"
    MINUTE_5 = "5Min"
    MINUTE_15 = "15Min"
    HOUR_1 = "1Hour"
    DAY = "1Day"


class QuantEngine:
    """
    Quantitative Analysis Engine using optimized backends.
    Priority: MLX (Apple Silicon) > Rust > Pandas-TA > Pure Pandas.
    Now using Decimal for all financial precision.
    """

    def __init__(self):
        pass

    def calculate_technical_indicators(self, ohlcv_data: List[Dict[str, Any]]) -> Union[AnalysisResult, Dict[str, str]]:
        """
        Calculate technical indicators from OHLCV data using optimized backends.
        """
        if not ohlcv_data:
            return {"error": "No OHLCV data provided"}

        # 0. Preprocessing
        try:
            df = pd.DataFrame(ohlcv_data)
            df['timestamp'] = pd.to_datetime(df['t'], unit='ms')
            df.set_index('timestamp', inplace=True)
            df.rename(columns={'o': 'open', 'h': 'high', 'l': 'low', 'c': 'close', 'v': 'volume'}, inplace=True)
            df.sort_index(inplace=True)
            close_prices = df['close'].fillna(0).tolist()
        except Exception as e:
            return {"error": f"Preprocessing failed: {e}"}

        indicators = {}
        
        # 1. MLX Path (Vectorized Ops)
        if MLX_AVAILABLE and len(close_prices) >= 20:
            try:
                 import logging
                 logger = logging.getLogger("quant_engine")
                 logger.debug("Using MLX for vectorized indicators")
                 
                 price_array = mx.array(close_prices).astype(mx.float32)
                 window_size = 20
                 weight_sma = mx.full((window_size,), 1.0/window_size).astype(mx.float32)
                 x_reshaped = price_array.reshape(1, -1, 1)
                 w_reshaped = weight_sma.reshape(1, window_size, 1)
                 
                 sma20_conv = mx.conv1d(x_reshaped, w_reshaped, stride=1, padding=0)
                 
                 if sma20_conv.size > 0:
                     indicators['bb_middle'] = create_decimal(sma20_conv[0, -1, 0].item())
                     x2 = price_array ** 2
                     x2_reshaped = x2.reshape(1, -1, 1)
                     sma20_x2 = mx.conv1d(x2_reshaped, w_reshaped, stride=1, padding=0)
                     var20 = sma20_x2 - (sma20_conv ** 2)
                     var20 = mx.maximum(var20, 0.0)
                     std20 = mx.sqrt(var20)
                     
                     std_val = std20[0, -1, 0]
                     mean_val = sma20_conv[0, -1, 0]
                     
                     indicators['bb_upper'] = create_decimal((mean_val + 2 * std_val).item())
                     indicators['bb_lower'] = create_decimal((mean_val - 2 * std_val).item())

                 if 'volume' in df:
                     vol_array = mx.array(df['volume'].fillna(0).tolist()).astype(mx.float32)
                     vol_reshaped = vol_array.reshape(1, -1, 1)
                     vol_sma_conv = mx.conv1d(vol_reshaped, w_reshaped, stride=1, padding=0)
                     if vol_sma_conv.size > 0:
                         indicators['volume_sma'] = create_decimal(vol_sma_conv[0, -1, 0].item())

            except Exception as e:
                import logging
                logging.getLogger("quant_engine").warning(f"MLX Path Error: {e}")

        # 2. Rust Path (Recursive & Heavy Math)
        if RUST_CORE_AVAILABLE:
            try:
                import logging
                logger = logging.getLogger("quant_engine")
                logger.debug("Using Rust Core for recursive indicators")
                
                if 'rsi' not in indicators:
                    if all(p == close_prices[0] for p in close_prices):
                        indicators['rsi'] = Decimal('50.0')
                    else:
                        rsi_vals = growin_core.calculate_rsi(close_prices, 14)
                        indicators['rsi'] = create_decimal(rsi_vals[-1]) if rsi_vals else None

                if 'macd' not in indicators:
                    macd_line, macd_signal, macd_hist = growin_core.calculate_macd(close_prices, 12, 26, 9)
                    if macd_line:
                        indicators['macd'] = create_decimal(macd_line[-1])
                        indicators['macd_signal'] = create_decimal(macd_signal[-1])
                        indicators['macd_hist'] = create_decimal(macd_hist[-1])

                if 'bb_upper' not in indicators:
                    upper, middle, lower = growin_core.calculate_bbands(close_prices, 20, 2.0)
                    if upper:
                        indicators['bb_upper'] = create_decimal(upper[-1])
                        indicators['bb_middle'] = create_decimal(middle[-1])
                        indicators['bb_lower'] = create_decimal(lower[-1])

                if 'ema_50' not in indicators:
                    ema_50_vals = growin_core.calculate_ema(close_prices, 50)
                    indicators['ema_50'] = create_decimal(ema_50_vals[-1]) if len(ema_50_vals) >= 50 else None

                if 'ema_200' not in indicators:
                    ema_200_vals = growin_core.calculate_ema(close_prices, 200)
                    indicators['ema_200'] = create_decimal(ema_200_vals[-1]) if len(ema_200_vals) >= 200 else None

                if 'volume_sma' not in indicators:
                    vol_vals = growin_core.calculate_sma(df['volume'].fillna(0).tolist(), 20)
                    indicators['volume_sma'] = create_decimal(vol_vals[-1]) if vol_vals else None
            except Exception as e:
                import logging
                logging.getLogger("quant_engine").warning(f"Rust Path Error: {e}")

        # 3. Pandas-TA Path (Fallback)
        if (len(indicators) < 5) and PANDAS_TA_AVAILABLE and ta is not None:
             try:
                if 'rsi' not in indicators:
                    df.ta.rsi(length=14, append=True)
                    indicators['rsi'] = create_decimal(df['RSI_14'].iloc[-1]) if 'RSI_14' in df else None

                if 'macd' not in indicators:
                    df.ta.macd(fast=12, slow=26, signal=9, append=True)
                    if 'MACD_12_26_9' in df:
                        indicators['macd'] = create_decimal(df['MACD_12_26_9'].iloc[-1])
                        indicators['macd_signal'] = create_decimal(df['MACDs_12_26_9'].iloc[-1])
                        indicators['macd_hist'] = create_decimal(df['MACDh_12_26_9'].iloc[-1])

                if 'bb_upper' not in indicators:
                    df.ta.bbands(length=20, std=2.0, append=True)
                    if 'BBU_20_2.0' in df:
                        indicators['bb_upper'] = create_decimal(df['BBU_20_2.0'].iloc[-1])
                        indicators['bb_middle'] = create_decimal(df['BBM_20_2.0'].iloc[-1])
                        indicators['bb_lower'] = create_decimal(df['BBL_20_2.0'].iloc[-1])

                if 'ema_50' not in indicators:
                    df.ta.ema(length=50, append=True)
                    indicators['ema_50'] = create_decimal(df['EMA_50'].iloc[-1]) if 'EMA_50' in df else None

                if 'ema_200' not in indicators:
                    df.ta.ema(length=200, append=True)
                    indicators['ema_200'] = create_decimal(df['EMA_200'].iloc[-1]) if 'EMA_200' in df else None

             except Exception as e:
                 import logging
                 logging.getLogger("quant_engine").warning(f"Pandas-TA Path Error: {e}")

        # 4. Pure Pandas Path (Last Resort)
        if len(indicators) < 5:
            try:
                import numpy as np
                close = df['close']
                if 'rsi' not in indicators:
                    delta = close.diff()
                    gain = (delta.where(delta > 0, 0.0)).ewm(alpha=1/14, adjust=False).mean()
                    loss = (-delta.where(delta < 0, 0.0)).ewm(alpha=1/14, adjust=False).mean()
                    with np.errstate(divide='ignore', invalid='ignore'):
                        rs = gain / loss
                        rsi_series = 100.0 - (100.0 / (1.0 + rs))
                        rsi_series[loss == 0] = 100.0
                        rsi_series[(gain == 0) & (loss == 0)] = 50.0
                    indicators['rsi'] = create_decimal(rsi_series.iloc[-1]) if not rsi_series.empty else None

                if 'macd' not in indicators:
                    exp1 = close.ewm(span=12, adjust=False).mean()
                    exp2 = close.ewm(span=26, adjust=False).mean()
                    macd_line = exp1 - exp2
                    signal_line = macd_line.ewm(span=9, adjust=False).mean()
                    indicators['macd'] = create_decimal(macd_line.iloc[-1])
                    indicators['macd_signal'] = create_decimal(signal_line.iloc[-1])
                    indicators['macd_hist'] = create_decimal((macd_line - signal_line).iloc[-1])

                if 'bb_upper' not in indicators:
                    sma20 = close.rolling(window=20).mean()
                    std20 = close.rolling(window=20).std()
                    indicators['bb_upper'] = create_decimal((sma20 + (std20 * 2)).iloc[-1])
                    indicators['bb_middle'] = create_decimal(sma20.iloc[-1])
                    indicators['bb_lower'] = create_decimal((sma20 - (std20 * 2)).iloc[-1])
            except Exception as e:
                import logging
                logging.getLogger("quant_engine").error(f"Pure Pandas Fallback Error: {e}")

        # Generate signals
        signals = self._generate_signals(indicators, df['close'].iloc[-1])

        return {
            "indicators": indicators,
            "signals": signals,
            "current_price": create_decimal(df['close'].iloc[-1]),
            "data_points": len(df)
        }

    def _generate_signals(self, indicators: Dict[str, Any], current_price: Any) -> Dict[str, Any]:
        curr_price = create_decimal(current_price)
        signals = {"trend": "neutral", "momentum": "neutral", "volatility": "neutral", "overall_signal": "hold"}
        if indicators.get('ema_50') and indicators.get('ema_200'):
            if curr_price > indicators['ema_50'] > indicators['ema_200']: signals['trend'] = "bullish"
            elif curr_price < indicators['ema_50'] < indicators['ema_200']: signals['trend'] = "bearish"
        rsi = indicators.get('rsi')
        if rsi:
            if rsi > 70: signals['momentum'] = "overbought"
            elif rsi < 30: signals['momentum'] = "oversold"
        bb_upper, bb_lower = indicators.get('bb_upper'), indicators.get('bb_lower')
        if bb_upper and bb_lower:
            if curr_price > bb_upper: signals['volatility'] = "high"
            elif curr_price < bb_lower: signals['volatility'] = "low"
        if signals['trend'] == "bullish" and signals['momentum'] == "oversold": signals['overall_signal'] = "buy"
        elif signals['trend'] == "bearish" and signals['momentum'] == "overbought": signals['overall_signal'] = "sell"
        return signals

    def calculate_pivot_levels(self, ohlcv_data: List[Dict[str, Any]], order: int = 5) -> Dict[str, Decimal]:
        if not ohlcv_data: return {"support": Decimal('0'), "resistance": Decimal('0')}
        import numpy as np
        highs = np.array([float(d.get('h', d.get('high', 0))) for d in ohlcv_data], dtype=np.float64)
        lows = np.array([float(d.get('l', d.get('low', 0))) for d in ohlcv_data], dtype=np.float64)
        closes = np.array([float(d.get('c', d.get('close', 0))) for d in ohlcv_data], dtype=np.float64)
        current_price = closes[-1]
        if SCIPY_AVAILABLE and argrelextrema is not None:
            peak_idx = argrelextrema(highs, np.greater, order=order)[0]
            trough_idx = argrelextrema(lows, np.less, order=order)[0]
            peaks, troughs = highs[peak_idx], lows[trough_idx]
        else:
            peaks, troughs = [], []
            for i in range(order, len(closes) - order):
                if all(highs[i] > highs[i-j] for j in range(1, order+1)) and all(highs[i] > highs[i+order]): peaks.append(highs[i])
                if all(lows[i] < lows[i-j] for j in range(1, order+1)) and all(lows[i] < lows[i+order]): troughs.append(lows[i])
            peaks, troughs = np.array(peaks), np.array(troughs)
        if len(peaks) == 0: peaks = np.array([np.max(highs[-50:])])
        if len(troughs) == 0: troughs = np.array([np.min(lows[-50:])])
        res = np.min(peaks[peaks > current_price]) if np.any(peaks > current_price) else np.max(peaks)
        sup = np.max(troughs[troughs < current_price]) if np.any(troughs < current_price) else np.min(troughs)
        return {"support": create_decimal(sup), "resistance": create_decimal(res)}

    def calculate_portfolio_metrics(self, positions: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not positions: return {"error": "No positions provided"}
        total_value, total_cost = Decimal('0'), Decimal('0')
        for pos in positions:
            qty = create_decimal(pos.get('qty') or pos.get('quantity') or 0)
            price = create_decimal(pos.get('current_price') or pos.get('currentPrice') or 0)
            avg_cost = create_decimal(pos.get('avg_cost') or pos.get('averagePrice') or 0)
            total_value += qty * price
            total_cost += qty * avg_cost
        total_pnl = total_value - total_cost
        return {"total_value": total_value, "total_cost": total_cost, "total_pnl": total_pnl, "portfolio_return": safe_div(total_pnl, total_cost), "position_count": len(positions)}

    def analyze_rebalancing_opportunity(self, current_allocation: Dict[str, str], target_allocation: Dict[str, float],
                                      total_portfolio_value: Any) -> Dict[str, Any]:
        """
        Analyze if portfolio needs rebalancing based on target allocations.
        Now precision-safe using Decimal.
        """
        current_parsed: Dict[str, Decimal] = {}
        total_value_dec = create_decimal(total_portfolio_value)
        if total_value_dec <= 0:
             return {"error": "Total portfolio value must be positive"}

        for symbol, pct_val in current_allocation.items():
            try:
                val_str = str(pct_val).strip().replace('%', '')
                val_dec = create_decimal(val_str)
                if val_dec > 1: val_dec = val_dec / 100
                current_parsed[symbol] = val_dec
            except Exception:
                current_parsed[symbol] = Decimal(0)

        deviations = {}
        rebalance_actions = []
        all_symbols = set(current_parsed.keys()) | set(target_allocation.keys())

        for symbol in all_symbols:
            current_pct = current_parsed.get(symbol, Decimal(0))
            target_pct = create_decimal(target_allocation.get(symbol, 0))
            deviation = target_pct - current_pct
            deviations[symbol] = deviation

            if abs(deviation) > Decimal("0.001"):
                current_val_amt = current_pct * total_value_dec
                target_val_amt = target_pct * total_value_dec
                value_change = target_val_amt - current_val_amt
                rebalance_actions.append({
                    "symbol": symbol,
                    "current_pct": current_pct,
                    "target_pct": target_pct,
                    "deviation": deviation,
                    "action": "buy" if value_change > 0 else "sell",
                    "value_change": abs(value_change)
                })

        return {
            "total_value": total_value_dec,
            "deviations": deviations,
            "rebalance_actions": rebalance_actions,
            "needs_rebalancing": len(rebalance_actions) > 0
        }

def get_quant_engine(): return QuantEngine()
