"""
Forecasting Agent - Wrapper around TTM for standardized interface
"""

from typing import Dict, Any
import logging

from .base_agent import BaseAgent, AgentConfig, AgentResponse
from market_context import ForecastData
from forecaster import get_forecaster

logger = logging.getLogger(__name__)


class ForecastingAgent(BaseAgent):
    """
    TTM-powered price forecasting agent.

    Performance: ~200-500ms (depending on model loaded)
    Falls back to mock predictions if TTM unavailable
    """

    def __init__(self, config: AgentConfig = None):
        if config is None:
            config = AgentConfig(
                name="ForecastingAgent",
                enabled=True,
                timeout=30.0,  # TTM can take a while
                cache_ttl=300  # Cache forecasts for 5 minutes
            )
        super().__init__(config)
        self.forecaster = get_forecaster()  # Singleton

    async def analyze(self, context: Dict[str, Any]) -> AgentResponse:
        """
        Generate price forecast.

        Context should include:
        - ticker: Stock symbol
        - ohlcv_data: Historical bars
        - days: Forecast horizon (default 5)
        """
        ticker = context.get("ticker", "UNKNOWN")
        ohlcv_data = context.get("ohlcv_data", [])
        days = context.get("days", 5)
        timeframe = context.get("timeframe", "1Day")

        if not ohlcv_data or len(ohlcv_data) < 50:
            return AgentResponse(
                agent_name=self.config.name,
                success=False,
                data={},
                error="Insufficient historical data for forecasting (need 50+ bars)",
                latency_ms=0
            )

        # Calculate steps based on timeframe
        if "Hour" in timeframe:
            steps = days * 24
        else:
            steps = days
            
        try:
            # Generate forecast using TTM
            result = await self.forecaster.forecast(
                ohlcv_data,
                prediction_steps=steps,
                timeframe=timeframe
            )

            if "error" in result:
                return AgentResponse(
                    agent_name=self.config.name,
                    success=False,
                    data={},
                    error=result["error"],
                    latency_ms=0
                )

            # Extract predictions
            forecast_bars = result.get("forecast", [])

            if not forecast_bars:
                return AgentResponse(
                    agent_name=self.config.name,
                    success=False,
                    data={},
                    error="No forecast generated",
                    latency_ms=0
                )

            # Calculate key forecast points based on timeframe
            if "Hour" in timeframe:
                idx_24h = 23
                idx_48h = 47
            else:
                # If daily data, 24h = idx 0, 48h = idx 1
                idx_24h = 0
                idx_48h = 1
                
            forecast_24h = forecast_bars[min(idx_24h, len(forecast_bars)-1)]['close']
            forecast_48h = forecast_bars[min(idx_48h, len(forecast_bars)-1)]['close'] if len(forecast_bars) > idx_48h else None
            forecast_7d = forecast_bars[-1]['close'] if len(forecast_bars) > 0 else None

            # Determine trend
            current_price = ohlcv_data[-1]['c']
            trend = "BULLISH" if forecast_24h > current_price else "BEARISH" if forecast_24h < current_price else "NEUTRAL"

            # Build forecast data
            forecast_data = ForecastData(
                ticker=ticker,
                forecast_24h=forecast_24h,
                forecast_48h=forecast_48h,
                forecast_7d=forecast_7d,
                confidence=result.get(
                    "confidence", 0.5) > 0.7 and "HIGH" or "MEDIUM",
                trend=trend,
                raw_series=forecast_bars # Already matches TimeSeriesItem structure
            )

            return AgentResponse(
                agent_name=self.config.name,
                success=True,
                data=forecast_data.model_dump(),
                latency_ms=0
            )

        except Exception as e:
            logger.error(f"Forecast failed: {e}")
            return AgentResponse(
                agent_name=self.config.name,
                success=False,
                data={},
                error=str(e),
                latency_ms=0
            )
