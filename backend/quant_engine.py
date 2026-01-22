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
        Calculate technical indicators from OHLCV data.
        Input: List of dicts with keys 't', 'o', 'h', 'l', 'c', 'v'
        Output: Dict with indicators and signals
        """
        if not PANDAS_TA_AVAILABLE:
            return {"error": "pandas-ta not available - technical analysis disabled"}

        if not ohlcv_data:
            return {"error": "No OHLCV data provided"}

        # Convert to pandas DataFrame
        df = pd.DataFrame(ohlcv_data)
        df['timestamp'] = pd.to_datetime(df['t'], unit='ms')
        df.set_index('timestamp', inplace=True)
        df.rename(columns={'o': 'open', 'h': 'high', 'l': 'low', 'c': 'close', 'v': 'volume'}, inplace=True)

        # Ensure data is sorted by time
        df.sort_index(inplace=True)

        # Calculate indicators
        indicators = {}

        # RSI (Relative Strength Index)
        indicators['rsi'] = ta.rsi(df['close'], length=14).iloc[-1] if len(df) >= 14 else None

        # MACD (Moving Average Convergence Divergence)
        macd = ta.macd(df['close'], fast=12, slow=26, signal=9)
        if macd is not None:
            indicators['macd'] = macd['MACD_12_26_9'].iloc[-1]
            indicators['macd_signal'] = macd['MACDs_12_26_9'].iloc[-1]
            indicators['macd_hist'] = macd['MACDh_12_26_9'].iloc[-1]

        # Bollinger Bands
        try:
            bbands = ta.bbands(df['close'], length=20, std=2)
            if bbands is not None and not bbands.empty:
                # Use the actual column names from pandas-ta
                bb_cols = bbands.columns.tolist()
                if len(bb_cols) >= 3:
                    indicators['bb_upper'] = bbands[bb_cols[0]].iloc[-1]
                    indicators['bb_middle'] = bbands[bb_cols[1]].iloc[-1]
                    indicators['bb_lower'] = bbands[bb_cols[2]].iloc[-1]
        except Exception as e:
            print(f"Bollinger Bands calculation error: {e}")

        # EMA (Exponential Moving Averages)
        indicators['ema_50'] = ta.ema(df['close'], length=50).iloc[-1] if len(df) >= 50 else None
        indicators['ema_200'] = ta.ema(df['close'], length=200).iloc[-1] if len(df) >= 200 else None

        # Volume indicators
        indicators['volume_sma'] = ta.sma(df['volume'], length=20).iloc[-1] if len(df) >= 20 else None

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
        Calculate portfolio-level metrics like Sharpe Ratio, Sortino Ratio, etc.
        Input: List of position dicts with 'symbol', 'qty', 'current_price', 'avg_cost' (optional)
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

        portfolio_return = np.mean(position_returns)
        portfolio_volatility = np.std(position_returns) if len(position_returns) > 1 else 0

        # Sharpe Ratio (assuming risk-free rate of 0.02 or 2%)
        risk_free_rate = 0.02
        sharpe_ratio = (portfolio_return - risk_free_rate) / portfolio_volatility if portfolio_volatility > 0 else 0

        # Sortino Ratio (downside deviation)
        negative_returns = [r for r in position_returns if r < 0]
        downside_deviation = np.std(negative_returns) if negative_returns else 0
        sortino_ratio = (portfolio_return - risk_free_rate) / downside_deviation if downside_deviation > 0 else 0

        # Beta calculation (if benchmark provided)
        beta = None
        if benchmark_returns:
            covariance = np.cov(position_returns, benchmark_returns)[0][1]
            benchmark_variance = np.var(benchmark_returns)
            beta = covariance / benchmark_variance if benchmark_variance > 0 else 0

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