"""
Quant Agent - Technical Analysis using QuantEngine
Ultra-fast algorithmic technical indicator calculations.
"""

from .base_agent import BaseAgent, AgentConfig, AgentResponse
from typing import Dict, Any
import logging
from utils.financial_math import create_decimal

logger = logging.getLogger(__name__)


class QuantAgent(BaseAgent):
    """
    Fast algorithmic technical analysis using QuantEngine (MLX/Rust/Pandas-TA).

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

        # Use centralized QuantEngine
        from quant_engine import get_quant_engine
        self.engine = get_quant_engine()
        logger.info("QuantAgent initialized using QuantEngine")

    async def analyze(self, context: Dict[str, Any]) -> AgentResponse:
        """
        Calculate technical indicators and generate trading signal.

        Context should include:
        - ohlcv_data: List of bars with o, h, l, c, v keys
        - ticker: Stock symbol
        - intent: (SOTA 30) High-velocity intent to trigger ORB
        """
        ticker = context.get("ticker", "UNKNOWN")
        ohlcv_data = context.get("ohlcv_data", [])
        intent = context.get("intent", "analytical")

        if not ohlcv_data or len(ohlcv_data) < 20: # SOTA: Relax limit to 20 for early ORB
            return AgentResponse(
                agent_name=self.config.name,
                success=False,
                data={},
                error="Insufficient data for technical analysis (need 20+ bars)",
                latency_ms=0
            )

        try:
            # 1. Calculate indicators via QuantEngine (Centralized & Optimized)
            # This handles MLX (NPU), Rust Core, and Pandas fallbacks automatically
            result = self.engine.calculate_technical_indicators(ohlcv_data)
            
            if "error" in result:
                return AgentResponse(
                    agent_name=self.config.name,
                    success=False,
                    data={},
                    error=result["error"],
                    latency_ms=0
                )

            indicators = result["indicators"]
            signals = result["signals"]

            # 2. Calculate Support/Resistance
            sr_levels = self.engine.calculate_pivot_levels(ohlcv_data)

            # 3. SOTA 2026 Phase 30: Opening Range Breakout (ORB) Detection
            orb_signal = None
            if intent == "intraday_trade" or len(ohlcv_data) < 100: # Heuristic for intraday
                try:
                    from utils.orb_detector import ORBDetector
                    detector = ORBDetector(range_minutes=30)
                    
                    # SOTA 2026: Covariance Shift from NeuralJMCE (NPU Accelerated)
                    cov_velocity = None
                    if len(ohlcv_data) >= 5: # Need at least a few bars for velocity
                         try:
                             from utils.portfolio_analyzer import PortfolioAnalyzer, TimeResolution
                             analyzer = PortfolioAnalyzer(n_assets=1, resolution=TimeResolution.INTRADAY_5MIN)
                             
                             # Calculate log returns for JMCE
                             closes = np.array([float(b['c']) for b in ohlcv_data], dtype=np.float32)
                             # Prevent zero division
                             closes[closes == 0] = 1.0
                             rets = np.diff(np.log(closes))
                             rets = rets[:, np.newaxis] # Shape (seq_len, 1)
                             
                             cov_velocity = await analyzer.get_covariance_velocity(rets)
                             if cov_velocity:
                                 logger.info(f"QuantAgent: NPU Covariance Velocity extracted: {cov_velocity:.4f}")
                         except Exception as jmce_e:
                             logger.warning(f"QuantAgent: JMCE Velocity extraction failed: {jmce_e}")
                    
                    orb_signal = detector.detect_breakout(ohlcv_data, covariance_velocity=cov_velocity)
                    logger.info(f"QuantAgent: ORB detection complete for {ticker}: {orb_signal['signal']}")
                except Exception as orb_e:
                    logger.warning(f"QuantAgent: ORB detection failed: {orb_e}")

            # 4. Map to QuantData model
            from market_context import QuantData, Signal
            
            # Map string overall_signal to Signal enum
            signal_map = {
                "buy": Signal.BUY,
                "sell": Signal.SELL,
                "hold": Signal.NEUTRAL,
                "neutral": Signal.NEUTRAL
            }
            overall_signal = signals.get("overall_signal", "neutral")
            
            # ORB Overlay: If ORB is BULLISH/BEARISH, it can override or boost technical signals
            if orb_signal and "BREAKOUT" in orb_signal["signal"]:
                if "BULLISH" in orb_signal["signal"]: overall_signal = "buy"
                elif "BEARISH" in orb_signal["signal"]: overall_signal = "sell"

            signal = signal_map.get(overall_signal, Signal.NEUTRAL)

            quant_data = QuantData(
                ticker=ticker,
                rsi=create_decimal(indicators.get("rsi", 50.0)),
                macd={
                    "value": create_decimal(indicators.get("macd", 0.0)),
                    "signal": create_decimal(indicators.get("macd_signal", 0.0)),
                    "histogram": create_decimal(indicators.get("macd_hist", 0.0))
                },
                bollinger_bands={
                    "upper": create_decimal(indicators.get("bb_upper", 0.0)),
                    "middle": create_decimal(indicators.get("bb_middle", 0.0)),
                    "lower": create_decimal(indicators.get("bb_lower", 0.0))
                },
                signal=signal,
                support_level=create_decimal(sr_levels["support"]),
                resistance_level=create_decimal(sr_levels["resistance"]),
                orb_signal=orb_signal
            )

            from status_manager import status_manager
            status_manager.set_status("quant_agent", "ready", f"Signal: {quant_data.signal} (ORB: {orb_signal['signal'] if orb_signal else 'N/A'})")

            return AgentResponse(
                agent_name=self.config.name,
                success=True,
                data=quant_data.model_dump(),
                latency_ms=0
            )

        except Exception as e:
            logger.error(f"QuantAgent analysis failed: {e}")
            return AgentResponse(
                agent_name=self.config.name,
                success=False,
                data={},
                error=str(e),
                latency_ms=0
            )
