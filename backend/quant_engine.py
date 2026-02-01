import pandas as pd
import numpy as np
try:
    import pandas_ta as ta
    PANDAS_TA_AVAILABLE = True
except ImportError:
    ta = None
    PANDAS_TA_AVAILABLE = False
from typing import Dict, List, Any, Optional
from enum import Enum


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

    def calculate_technical_indicators(self, ohlcv_data: List[Dict[str, Any]]) -> Dict[str, Any]:
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
        
        try:
            import growin_core
            
            # RSI
            rsi_vals = growin_core.calculate_rsi(close_prices, 14)
            indicators['rsi'] = rsi_vals[-1] if rsi_vals else None

            # MACD
            # Rust returns tuple (macd_line, signal_line, histogram)
            macd_line, macd_signal, macd_hist = growin_core.calculate_macd(close_prices, 12, 26, 9)
            if macd_line:
                indicators['macd'] = macd_line[-1]
                indicators['macd_signal'] = macd_signal[-1]
                indicators['macd_hist'] = macd_hist[-1]

            # Bollinger Bands
            upper, middle, lower = growin_core.calculate_bbands(close_prices, 20, 2.0)
            if upper:
                indicators['bb_upper'] = upper[-1]
                indicators['bb_middle'] = middle[-1]
                indicators['bb_lower'] = lower[-1]

            # EMA
            ema_50_vals = growin_core.calculate_ema(close_prices, 50)
            indicators['ema_50'] = ema_50_vals[-1] if len(ema_50_vals) >= 50 else None
            
            ema_200_vals = growin_core.calculate_ema(close_prices, 200)
            indicators['ema_200'] = ema_200_vals[-1] if len(ema_200_vals) >= 200 else None
            
            # Volume SMA
            vol_vals = growin_core.calculate_sma(df['volume'].fillna(0).tolist(), 20)
            indicators['volume_sma'] = vol_vals[-1] if vol_vals else None

        except ImportError as e:
            return {"error": f"growin_core (Rust extension) not available: {e}"}
        except Exception as e:
            return {"error": f"Indicator calculation failed: {e}"}

        # Generate signals
        signals = self._generate_signals(indicators, df['close'].iloc[-1])

        return {
            "indicators": indicators,
            "signals": signals,
            "current_price": df['close'].iloc[-1],
            "data_points": len(df)
        }

    def calculate_portfolio_metrics(self, positions: List[Dict[str, Any]], benchmark_returns: Optional[List[float]] = None) -> Dict[str, Any]:
        """
        Calculate portfolio-level metrics using MLX for Apple Silicon acceleration.
        Input: List of position dicts with 'symbol', 'qty', 'current_price', 'avg_cost'
        """
        if not positions:
            return {"error": "No positions provided"}

        total_value = sum(pos['qty'] * pos['current_price'] for pos in positions if 'qty' in pos and 'current_price' in pos)
        if total_value == 0:
            return {"error": "Portfolio value is zero"}

        # Calculate returns for each position
        position_returns = []
        for pos in positions:
            if 'avg_cost' in pos and pos['avg_cost'] > 0:
                ret = (pos['current_price'] - pos['avg_cost']) / pos['avg_cost']
                position_returns.append(ret)

        if not position_returns:
            return {"total_value": total_value, "note": "No cost basis available for returns calculation"}

        # Use MLX for calculation
        try:
            import mlx.core as mx
            
            # Convert to MLX array on GPU/Unified Memory
            returns_mx = mx.array(position_returns)
            
            portfolio_return = float(mx.mean(returns_mx))
            
            # MLX std dev
            # Note: mx.std defaults to population std usually, numpy defaults to population too unless ddof specified
            portfolio_volatility = float(mx.std(returns_mx)) if len(position_returns) > 1 else 0.0

            # Sharpe Ratio (risk-free 2%)
            risk_free_rate = 0.02
            sharpe_ratio = (portfolio_return - risk_free_rate) / portfolio_volatility if portfolio_volatility > 0 else 0

            # Sortino Ratio
            # Boolean indexing in MLX: returns_mx[returns_mx < 0]
            neg_mask = returns_mx < 0
            # Check if any negative returns exist
            if mx.any(neg_mask).item():
                negative_returns = returns_mx[neg_mask]
                downside_deviation = float(mx.std(negative_returns))
            else:
                downside_deviation = 0.0
                
            sortino_ratio = (portfolio_return - risk_free_rate) / downside_deviation if downside_deviation > 0 else 0

            # Beta calculation (requires Covariance)
            beta = None
            if benchmark_returns and len(benchmark_returns) == len(position_returns):
                bench_mx = mx.array(benchmark_returns)
                
                # Covariance in MLX? mlx.core doesn't have direct cov/corr yet in 0.0.x sometimes
                # Manual Covariance: E[(X-meanX)(Y-meanY)]
                x_mean = mx.mean(returns_mx)
                y_mean = mx.mean(bench_mx)
                
                # Center data
                x_centered = returns_mx - x_mean
                y_centered = bench_mx - y_mean
                
                # Covariance = mean(x_centered * y_centered)
                covariance = float(mx.mean(x_centered * y_centered))
                
                # Variance of benchmark
                bench_var = float(mx.var(bench_mx))
                
                beta = covariance / bench_var if bench_var > 0 else 0
            
            # Fallback for benchmark length mismatch
            elif benchmark_returns:
                 # If lengths mismatch, we surely can't calculate per-asset correlation easily without time-series alignment
                 # Here we assume inputs are aligned vectors of returns.
                 pass

        except ImportError:
            return {"error": "MLX not available for portfolio metrics"}
        except Exception as e:
            return {"error": f"MLX Calculation error: {e}"}

        return {
            "total_value": total_value,
            "portfolio_return": portfolio_return,
            "portfolio_volatility": portfolio_volatility,
            "sharpe_ratio": sharpe_ratio,
            "sortino_ratio": sortino_ratio,
            "beta": beta,
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
                                      current_prices: Dict[str, float]) -> Dict[str, Any]:
        """
        Analyze if portfolio needs rebalancing based on target allocations.
        Input: current_allocation (dict of symbol: percentage as string), target_allocation (dict of symbol: percentage)
        """
        # Parse current allocation
        current_parsed = {}
        for symbol, pct_str in current_allocation.items():
            try:
                current_parsed[symbol] = float(pct_str.strip('%')) / 100
            except ValueError:
                current_parsed[symbol] = 0.0

        total_value = sum(current_parsed[symbol] * current_prices.get(symbol, 0) for symbol in current_parsed)

        if total_value == 0:
            return {"error": "Unable to calculate total portfolio value"}

        # Calculate deviations
        deviations = {}
        rebalance_actions = []

        for symbol, target_pct in target_allocation.items():
            current_pct = current_parsed.get(symbol, 0.0)
            deviation = target_pct - current_pct
            
            if abs(deviation) > 0.05:  # 5% threshold
                current_value = current_parsed[symbol] * total_value if symbol in current_parsed else 0
                target_value = target_pct * total_value
                value_change = target_value - current_value
                
                rebalance_actions.append({
                    "symbol": symbol,
                    "current_pct": current_pct,
                    "target_pct": target_pct,
                    "deviation": deviation,
                    "action": "buy" if value_change > 0 else "sell",
                    "value_change": abs(value_change)
                })

        return {
            "total_value": total_value,
            "deviations": deviations,
            "rebalance_actions": rebalance_actions,
            "needs_rebalancing": len(rebalance_actions) > 0
        }


# --- Utility Functions ---

def get_quant_engine():
    return QuantEngine()


if __name__ == '__main__':
    print("QuantEngine module loaded.")