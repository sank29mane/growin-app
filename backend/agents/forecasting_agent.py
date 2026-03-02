"""
Forecasting Agent - Wrapper around TTM for standardized interface
"""

from typing import Dict, Any
import logging

from .base_agent import BaseAgent, AgentConfig, AgentResponse
from market_context import ForecastData
from forecaster import get_forecaster
from error_resilience import CircuitBreaker

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

        self.circuit_breaker = CircuitBreaker("forecaster", failure_threshold=3, recovery_timeout=60)

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
        # TTM-R2 has a logical max prediction length of 96
        MAX_STEPS = 96

        if "Min" in timeframe:
            # Estimate minutes per bar
            mins = 1
            if "5" in timeframe:
                mins = 5
            if "15" in timeframe:
                mins = 15
            if "30" in timeframe:
                mins = 30
            
            total_minutes = days * 24 * 60
            steps = min(int(total_minutes / mins), MAX_STEPS)
            
        elif "Hour" in timeframe:
            steps = min(days * 24, MAX_STEPS)
            
        else:
            # Day, Week, Month
            steps = min(days, MAX_STEPS)

        # --- DATA SANITIZATION (Robust Whole-Series Unit Fix) ---
        # Fixes GBP/GBX inconsistencies by normalizing outliers to the series median.
        if len(ohlcv_data) > 10:
            try:
                import numpy as np
                # Extract closes to find median
                closes = [float(d.get('c', 0)) for d in ohlcv_data if d.get('c')]
                if closes:
                    median_price = np.median(closes)
                    
                    # Heuristic: If median is < 5 (likely GBP) and we see points > 50 (likely GBX), or vice versa.
                    # Or simpler: anything deviation > 50x from median is a unit error.
                    
                    corrections = 0
                    for bar in ohlcv_data:
                        c = float(bar.get('c', 0))
                        if c <= 0:
                            continue
                        
                        ratio = c / median_price
                        factor = 1.0
                        
                        # Case 1: Point is 100x larger than median (GBX in GBP series)
                        if ratio > 50.0: 
                             factor = 0.01 # Divide by 100
                        
                        # Case 2: Point is 100x smaller than median (GBP in GBX series)
                        elif ratio < 0.02:
                             factor = 100.0 # Multiply by 100
                             
                        if factor != 1.0:
                            corrections += 1
                            # Apply to all fields
                            for f in ['o', 'h', 'l', 'c']:
                                if f in bar and bar[f] is not None:
                                    bar[f] = float(bar[f]) * factor
                                    
                    if corrections > 0:
                        logger.warning(f"ForecastingAgent: Sanitized {corrections} bars with unit mismatches (Median: {median_price:.2f}).")
                        
            except Exception as ex:
                logger.warning(f"ForecastingAgent sanitization error: {ex}")
        # ---------------------------------------------
            
        if not self.circuit_breaker.can_proceed():
            logger.error(f"Forecast skipped: circuit breaker {self.circuit_breaker.name} is OPEN")
            return AgentResponse(
                agent_name=self.config.name,
                success=False,
                data={},
                error="Forecasting failed or circuit breaker is OPEN",
                latency_ms=0
            )

        try:
            # Generate forecast using TTM
            result = await self.forecaster.forecast(
                ohlcv_data,
                prediction_steps=steps,
                timeframe=timeframe
            )

            self.circuit_breaker.record_success()

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
                confidence=result.get("confidence", 0.5) > 0.7 and "HIGH" or "MEDIUM",
                trend=trend,
                algorithm=result.get("algorithm") or result.get("model_used") or "IBM Granite TTM-R2.1",
                is_fallback=result.get("is_fallback", False) or result.get("model_used") == "statistical_trend_holt",
                note=result.get("note"),
                raw_series=forecast_bars, # Already matches TimeSeriesItem structure
                auxiliary_forecasts=result.get("auxiliary_forecasts")
            )

            from status_manager import status_manager
            status_manager.set_status("forecasting_agent", "ready", f"Trend: {forecast_data.trend}")
            return AgentResponse(
                agent_name=self.config.name,
                success=True,
                data=forecast_data.model_dump(),
                latency_ms=0
            )

        except Exception as e:
            self.circuit_breaker.record_failure()
            logger.error(f"Forecast failed: {e}")
            return AgentResponse(
                agent_name=self.config.name,
                success=False,
                data={},
                error=str(e),
                latency_ms=0
            )
