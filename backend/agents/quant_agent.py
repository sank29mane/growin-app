"""Quant Agent - Technical Analysis using TA-Lib
Ultra-fast algorithmic technical indicator calculations.
"""

from .base_agent import BaseAgent, AgentConfig, AgentResponse
from market_context import QuantData, Signal
from typing import Dict, Any
import logging
import numpy as np

logger = logging.getLogger(__name__)

# Bolt Optimization: Import optional dependencies at module level to avoid repeated ImportErrors (PR #48)
try:
    from scipy.signal import argrelextrema
    SCIPY_AVAILABLE = True
except ImportError:
    argrelextrema = None
    SCIPY_AVAILABLE = False


class QuantAgent(BaseAgent):
    """
    Fast algorithmic technical analysis using TA-Lib.

    Performance: ~5-10ms for all indicators
    No LLM needed - pure algorithmic calculations
    """

    def __init__(self, config: AgentConfig = None):
        if config is None:
            config = AgentConfig(
                name="QuantAgent",
                enabled=True,
                timeout=5.0,
                cache_ttl=60  # Cache for 1 minute
            )
        super().__init__(config)

        # Try to import TA-Lib
        try:
            import talib
            self.talib = talib
            self.available = True
            logger.info("TA-Lib loaded successfully")
        except ImportError:
            self.talib = None
            self.available = False
            # Use error_resilience for one-time warning (prevents log spam)
            from error_resilience import provider_manager
            provider_manager.check_talib_available()

        # Try to load Core ML for on-device inference
        from coreml_inference import CoreMLRunner
        self.coreml_runner = CoreMLRunner()
        if self.coreml_runner.load("models/coreml/forecast_small.mlmodel"):
            logger.info("Core ML model loaded for on-device inference")
        else:
            logger.info("Core ML not available, using CPU fallbacks")

    async def analyze(self, context: Dict[str, Any]) -> AgentResponse:
        """
        Calculate technical indicators and generate trading signal.

        Context should include:
        - ohlcv_data: List of bars with o, h, l, c, v keys
        - ticker: Stock symbol
        """
        ticker = context.get("ticker", "UNKNOWN")
        ohlcv_data = context.get("ohlcv_data", [])

        if not ohlcv_data or len(ohlcv_data) < 50:
            return AgentResponse(
                agent_name=self.config.name,
                success=False,
                data={},
                error="Insufficient data for technical analysis (need 50+ bars)",
                latency_ms=0
            )

        # Use centralized analysis method with fallbacks
        return await self._perform_analysis(ticker, ohlcv_data)

    async def _perform_analysis(self, ticker: str, bars: list) -> AgentResponse:
        """Technical analysis using TA-Lib (5-10ms) or NumPy fallbacks"""
        import numpy as np

        # Extract price arrays (ensure float64 for TA-Lib)
        closes = np.array([b['c'] for b in bars], dtype=np.float64)
        highs = np.array([b['h'] for b in bars], dtype=np.float64)
        lows = np.array([b['l'] for b in bars], dtype=np.float64)


        # Calculate indicators (use TA-Lib if available, otherwise pure Python fallbacks)
        if self.talib is not None:
            rsi = self.talib.RSI(closes, timeperiod=14)
            macd, macd_signal, macd_hist = self.talib.MACD(closes)
            bb_upper, bb_middle, bb_lower = self.talib.BBANDS(closes)
            sma_20 = self.talib.SMA(closes, timeperiod=20)
            sma_50 = self.talib.SMA(closes, timeperiod=50)
        else:
            # Pure Python/Numpy fallbacks
            rsi = self._calculate_rsi(closes, 14)
            macd, macd_signal, macd_hist = self._calculate_macd(closes)
            bb_upper, bb_middle, bb_lower = self._calculate_bbands(closes)
            sma_20 = self._calculate_sma(closes, 20)
            sma_50 = self._calculate_sma(closes, 50)

        # Get latest values
        current_price = closes[-1]
        latest_rsi = float(rsi[-1]) if not np.isnan(rsi[-1]) else 50.0
        latest_macd = float(macd[-1]) if not np.isnan(macd[-1]) else 0.0
        latest_macd_signal = float(macd_signal[-1]) if not np.isnan(macd_signal[-1]) else 0.0

        # Calculate support/resistance (simple approach)
        # Calculate structural support/resistance using pivot point clustering
        sr_levels = self._calculate_pivot_levels(highs, lows, closes)
        support = sr_levels["support"]
        resistance = sr_levels["resistance"]

        # Generate signal (algorithmic rules)
        signal = self._generate_signal(
            latest_rsi,
            latest_macd,
            latest_macd_signal,
            current_price,
            float(sma_20[-1]),
            float(sma_50[-1])
        )

        # Build response
        quant_data = QuantData(
            ticker=ticker,
            rsi=latest_rsi,
            macd={
                "value": latest_macd,
                "signal": latest_macd_signal,
                "histogram": float(macd_hist[-1]) if not np.isnan(macd_hist[-1]) else 0.0
            },
            bollinger_bands={
                "upper": float(bb_upper[-1]),
                "middle": float(bb_middle[-1]),
                "lower": float(bb_lower[-1])
            },
            signal=signal,
            support_level=support,
            resistance_level=resistance
        )

        return AgentResponse(
            agent_name=self.config.name,
            success=True,
            data=quant_data.model_dump(),
            latency_ms=0  # Will be set by execute()
        )

    def _generate_signal(self, rsi: float, macd: float, macd_signal: float,
                        price: float, sma20: float, sma50: float) -> Signal:
        """
        Algorithmic signal generation based on multiple indicators.

        Rules:
        - Strong BUY: RSI < 30 AND price < SMA20 AND MACD bullish cross
        - BUY: RSI < 40 AND price near SMA20
        - SELL: RSI > 60 AND price > SMA20
        - Strong SELL: RSI > 70 AND MACD bearish cross
        """
        buy_signals = 0
        sell_signals = 0

        # RSI signals
        if rsi < 30:
            buy_signals += 2
        elif rsi < 40:
            buy_signals += 1
        elif rsi > 70:
            sell_signals += 2
        elif rsi > 60:
            sell_signals += 1

        # MACD signals
        if macd > macd_signal and macd > 0:
            buy_signals += 1
        elif macd < macd_signal and macd < 0:
            sell_signals += 1

        # Trend signals (SMA)
        if price < sma20:
            buy_signals += 1
        elif price > sma50:
            sell_signals += 1

        # Decision
        if buy_signals >= 3:
            return Signal.BUY
        elif sell_signals >= 3:
            return Signal.SELL
        else:
            return Signal.NEUTRAL

    def _calculate_rsi(self, closes: np.ndarray, period: int = 14) -> np.ndarray:
        """Pure Python RSI calculation using Wilder's Smoothing (matches TA-Lib)"""
        if len(closes) < period + 1:
            return np.full(len(closes), 50.0)

        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)

        try:
            import pandas as pd
            # Optimized Wilder's Smoothing using Vectorized Pandas

            def wilders_smoothing(data, p):
                # First value is SMA
                first_val = data[:p].mean()
                # Construct data for EWM
                rest_of_data = data[p:]
                concat_data = np.concatenate(([first_val], rest_of_data))
                # Apply EWM with alpha=1/period (Wilder's definition)
                return pd.Series(concat_data).ewm(alpha=1/p, adjust=False).mean().values

            avg_gains = wilders_smoothing(gains, period)
            avg_losses = wilders_smoothing(losses, period)

            # Reconstruct RSI array
            with np.errstate(divide='ignore', invalid='ignore'):
                rs = avg_gains / avg_losses
                rsi_vals = 100.0 - (100.0 / (1.0 + rs))

            # Handle 0 loss case (RSI = 100) and 0/0 case (RSI = 50)
            rsi_vals[avg_losses == 0] = 100.0
            rsi_vals[(avg_gains == 0) & (avg_losses == 0)] = 50.0

            # Pad with neutral values for early data points
            result = np.full(len(closes), 50.0)
            result[period:] = rsi_vals

            return result

        except ImportError:
            # Fallback to loop if pandas missing
            avg_gains = np.zeros(len(gains))
            avg_losses = np.zeros(len(losses))

            avg_gains[period-1] = np.mean(gains[:period])
            avg_losses[period-1] = np.mean(losses[:period])

            for i in range(period, len(gains)):
                avg_gains[i] = ((avg_gains[i-1] * (period - 1)) + gains[i]) / period
                avg_losses[i] = ((avg_losses[i-1] * (period - 1)) + losses[i]) / period

            rs = np.zeros_like(avg_gains)
            with np.errstate(divide='ignore', invalid='ignore'):
                rs = avg_gains / avg_losses

            rsi = np.full(len(closes), 50.0)
            for i in range(period, len(closes)):
                idx = i - 1
                if avg_losses[idx] == 0:
                    rsi[i] = 100.0 if avg_gains[idx] != 0 else 50.0
                else:
                    rsi[i] = 100.0 - (100.0 / (1.0 + rs[idx]))
            return rsi

    def _calculate_macd(self, closes: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Pure Python MACD calculation"""
        if len(closes) < 26:
            return np.zeros(len(closes)), np.zeros(len(closes)), np.zeros(len(closes))

        ema12 = self._calculate_ema(closes, 12)
        ema26 = self._calculate_ema(closes, 26)
        macd = ema12 - ema26
        macd_signal = self._calculate_ema(macd, 9)
        macd_hist = macd - macd_signal
        return macd, macd_signal, macd_hist

    def _calculate_ema(self, data: np.ndarray, period: int) -> np.ndarray:
        """Exponential Moving Average (Bolt Optimized)"""
        if len(data) < period:
            return np.full(len(data), data.mean() if len(data) > 0 else 0)

        try:
            import pandas as pd
            # Vectorized Pandas implementation (~10x faster)

            # Calculate SMA for the initialization point
            initial_sma = data[:period].mean()

            # Construct data for EWM: [initial_sma, data[period], data[period+1], ...]
            # We skip the first 'period' elements of original data for EWM calculation
            rest_of_data = data[period:]
            concat_data = np.concatenate(([initial_sma], rest_of_data))

            # Apply EWM
            # span=period corresponds to alpha = 2/(period+1)
            # adjust=False ensures recursive calculation: y_t = (1-alpha)*y_{t-1} + alpha*x_t
            ewm_values = pd.Series(concat_data).ewm(span=period, adjust=False).mean().values

            # Reconstruct the full array
            result = np.zeros(len(data))
            # The 'ewm_values' array corresponds to indices [period-1, period, period+1, ...]
            result[period-1:] = ewm_values

            return result

        except ImportError:
            # Fallback to original loop implementation if pandas is missing
            ema = np.zeros(len(data))
            ema[period-1] = data[:period].mean()
            multiplier = 2 / (period + 1)
            for i in range(period, len(data)):
                ema[i] = (data[i] - ema[i-1]) * multiplier + ema[i-1]
            return ema

    def _calculate_bbands(self, closes: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Pure Python Bollinger Bands"""
        if len(closes) < 20:
            mean = closes.mean() if len(closes) > 0 else 0
            return np.full(len(closes), mean + 2), np.full(len(closes), mean), np.full(len(closes), mean - 2)

        sma = self._calculate_sma(closes, 20)

        try:
            import pandas as pd
            # Optimized standard deviation using pandas rolling window
            # ddof=0 to match np.std behavior (population std dev)
            std = pd.Series(closes).rolling(window=20).std(ddof=0).to_numpy(copy=True)
            # Replace the initial NaNs (due to rolling window) with 0.0 to match original behavior
            std[:19] = 0.0
        except ImportError:
            # Fallback if pandas is not available
            std = np.zeros(len(closes))
            for i in range(19, len(closes)):
                std[i] = np.std(closes[i-19:i+1])

        bb_upper = sma + (std * 2)
        bb_lower = sma - (std * 2)
        return bb_upper, sma, bb_lower

    def _calculate_sma(self, closes: np.ndarray, period: int) -> np.ndarray:
        """Simple Moving Average"""
        if len(closes) < period:
            return np.full(len(closes), closes.mean() if len(closes) > 0 else 0)

        weights = np.ones(period) / period
        sma = np.convolve(closes, weights, mode='valid')
        pad = np.full(period - 1, closes[:period].mean())
        return np.concatenate([pad, sma])

    def _calculate_pivot_levels(self, highs: np.ndarray, lows: np.ndarray, closes: np.ndarray) -> Dict[str, float]:
        """
        Calculates structural Support and Resistance using local extremes (Vectorized).
        Uses scipy.signal.argrelextrema for efficiency.
        """
        current_price = closes[-1]
        order = 5 # Window size (lookback/lookahead)

        if SCIPY_AVAILABLE:
            # Find local maxima (peaks) and minima (troughs)
            # order=5 means it must be the max/min within 5 points on EITHER side
            peak_idx = argrelextrema(highs, np.greater, order=order)[0]
            trough_idx = argrelextrema(lows, np.less, order=order)[0]

            peaks = highs[peak_idx]
            troughs = lows[trough_idx]
        else:
            # Fallback to window loop logic if scipy missing
            peaks = []
            troughs = []
            window = order
            for i in range(window, len(closes) - window):
                if all(highs[i] > highs[i-j] for j in range(1, window+1)) and \
                   all(highs[i] > highs[i+j] for j in range(1, window+1)):
                    peaks.append(highs[i])
                if all(lows[i] < lows[i-j] for j in range(1, window+1)) and \
                   all(lows[i] < lows[i+j] for j in range(1, window+1)):
                    troughs.append(lows[i])

        # If no structural points found, fallback to 50-day extremes
        if len(peaks) == 0: peaks = np.array([np.max(highs[-50:])])
        if len(troughs) == 0: troughs = np.array([np.min(lows[-50:])])

        # Find closest structural levels to current price
        # Resistance: Lowest peak above price, or highest peak if none above
        above_mask = peaks > current_price
        if np.any(above_mask):
            res = np.min(peaks[above_mask])
        else:
            res = np.max(peaks)

        # Support: Highest trough below price, or lowest trough if none below
        below_mask = troughs < current_price
        if np.any(below_mask):
            sup = np.max(troughs[below_mask])
        else:
            sup = np.min(troughs)

        return {
            "support": float(sup),
            "resistance": float(res)
        }
