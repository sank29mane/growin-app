"""
Quant Agent - Technical Analysis using TA-Lib
Ultra-fast algorithmic technical indicator calculations.
"""

from base_agent import BaseAgent, AgentConfig, AgentResponse
from market_context import QuantData, Signal
from typing import Dict, Any
import logging
import numpy as np

logger = logging.getLogger(__name__)


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
        
        if not self.available:
            # Fallback to simple calculations
            return await self._simple_analysis(ticker, ohlcv_data)
        
        # Use TA-Lib for fast calculations
        return await self._talib_analysis(ticker, ohlcv_data)
    
    async def _talib_analysis(self, ticker: str, bars: list) -> AgentResponse:
        """TA-Lib powered analysis (5-10ms)"""
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
            # Pure Python fallbacks
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
        recent_lows = lows[-20:]
        recent_highs = highs[-20:]
        support = float(np.min(recent_lows))
        resistance = float(np.max(recent_highs))
        
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
    
    async def _simple_analysis(self, ticker: str, bars: list) -> AgentResponse:
        """Fallback without TA-Lib (slower but functional)"""
        closes = [b['c'] for b in bars]
        
        # Simple RSI calculation
        rsi = self._calculate_simple_rsi(closes)
        
        # Simple moving averages
        sma_20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else closes[-1]
        sma_50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else closes[-1]
        
        # Simple signal
        signal = Signal.BUY if closes[-1] < sma_20 else Signal.SELL if closes[-1] > sma_50 else Signal.NEUTRAL
        
        quant_data = QuantData(
            ticker=ticker,
            rsi=rsi,
            signal=signal,
            support_level=0,
            resistance_level=0
        )
        
        return AgentResponse(
            agent_name=self.config.name,
            success=True,
            data=quant_data.model_dump(),
            latency_ms=0,
            error="Using simple calculations (install TA-Lib for better performance)"
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
        """Pure Python RSI calculation using numpy"""
        if len(closes) < period + 1:
            return np.full(len(closes), 50.0)

        deltas = np.diff(closes)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)

        # Simple moving averages for gains and losses
        avg_gains = np.convolve(gains, np.ones(period)/period, mode='valid')
        avg_losses = np.convolve(losses, np.ones(period)/period, mode='valid')

        rs = np.divide(avg_gains, avg_losses, out=np.ones_like(avg_gains), where=avg_losses != 0)
        rsi = 100 - (100 / (1 + rs))

        # Pad with neutral values for early data points
        pad = np.full(len(closes) - len(rsi), 50.0)
        return np.concatenate([pad, rsi])

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
        """Exponential Moving Average"""
        if len(data) < period:
            return np.full(len(data), data.mean() if len(data) > 0 else 0)
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

    def _calculate_simple_rsi(self, closes: list, period: int = 14) -> float:
        """Simple RSI calculation without TA-Lib"""
        if len(closes) < period + 1:
            return 50.0
        
        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains = [d if d > 0 else 0 for d in deltas[-period:]]
        losses = [-d if d < 0 else 0 for d in deltas[-period:]]
        
        avg_gain = sum(gains) / period
        avg_loss = sum(losses) / period
        
        if avg_loss == 0:
            return 100.0
        
        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        
        return rsi
