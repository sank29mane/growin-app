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

            # 3. Map to QuantData model
            from market_context import QuantData, Signal
            
            # Map string overall_signal to Signal enum
            signal_map = {
                "buy": Signal.BUY,
                "sell": Signal.SELL,
                "hold": Signal.NEUTRAL,
                "neutral": Signal.NEUTRAL
            }
            overall_signal = signals.get("overall_signal", "neutral")
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
                resistance_level=create_decimal(sr_levels["resistance"])
            )

            from status_manager import status_manager
            status_manager.set_status("quant_agent", "ready", f"Signal: {quant_data.signal}")

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
