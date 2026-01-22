"""
Time-Series Forecasting Module

Provides statistical trend-based forecasting for stock prices.
Integrating IBM Granite TTM-R2 model for SOTA forecasting with statistical fallback.
"""

import numpy as np
import logging
import asyncio
import platform
from typing import List, Dict, Any, Optional
from sklearn.preprocessing import StandardScaler
import pandas as pd
import datetime

logger = logging.getLogger(__name__)


class TTMForecaster:
    """
    Hybrid Forecaster:
    1. Tries to use IBM Granite TTM-R2 (TinyTimeMixer) if available (Python 3.9-3.12).
    2. Falls back to Statistical Trend Analysis (Linear Regression + Dampening) if not.
    """
    
    def __init__(self):
        self.scaler = StandardScaler()
        self.platform = self._detect_platform()
        self.ttm_model = None
        self.ttm_available = False
        self._init_ttm_model()
        logger.info(f"📊 Forecaster initialized (TTM-R2 Available: {self.ttm_available})")
    
    def _detect_platform(self) -> str:
        """Detect if running on Apple Silicon"""
        machine = platform.machine()
        system = platform.system()
        
        if machine in ['arm64', 'aarch64'] and system == 'Darwin':
            return 'apple_silicon'
        return 'other'

    def _init_ttm_model(self):
        """Initialize TTM-R2 bridge if available"""
        import os
        # Use absolute paths for reliability in subprocess
        base_dir = os.path.dirname(os.path.abspath(__file__))
        venv_python = os.path.join(base_dir, "venv_forecast/bin/python")
        bridge_script = os.path.join(base_dir, "forecast_bridge.py")
        
        if os.path.exists(venv_python) and os.path.exists(bridge_script):
            self.ttm_available = True
            self.venv_python = venv_python
            self.bridge_script = bridge_script
            logger.info("✅ TTM-R2 Forecasting Bridge detected and ready (via venv_forecast)")
        else:
            logger.warning(f"TTM-R2 Bridge components not found at {venv_python}. Using Statistical Fallback.")
            self.ttm_available = False

    async def forecast(self, ohlcv_data: List[Dict[str, Any]], prediction_steps: int = 96, timeframe: str = "1Day") -> Dict[str, Any]:
        """
        Generate forecast using TTM-R2 or fallback to statistical.
        """
        if not ohlcv_data or len(ohlcv_data) < 50:
             return {
                "error": "Insufficient historical data (need at least 50 points)",
                "forecast": [],
                "confidence": 0.0
            }
        
        try:
            # Need 512 points for TTM usually, but check availability first
            if self.ttm_available and len(ohlcv_data) >= 512:
                return await self._ttm_forecast(ohlcv_data, prediction_steps, timeframe)
            else:
                if self.ttm_available:
                     logger.info(f"TTM available but insufficient data ({len(ohlcv_data)} < 512). Using statistical.")
                return self._statistical_forecast(ohlcv_data, prediction_steps, timeframe)
        except Exception as e:
            logger.error(f"Forecasting error: {e}")
            return {
                "error": str(e),
                "forecast": [],
                "confidence": 0.0
            }

    async def _ttm_forecast(self, ohlcv_data: List[Dict[str, Any]], prediction_steps: int, timeframe: str) -> Dict[str, Any]:
        """Generate forecast using TTM-R2 model via bridge script with sanity checks"""
        import json
        import subprocess
        
        try:
            # Prepare input
            payload = {
                "ohlcv_data": ohlcv_data,
                "prediction_steps": prediction_steps,
                "timeframe": timeframe
            }
            
            logger.info(f"🚀 Invoking TTM-R2 Forecasting Bridge (TF: {timeframe})...")
            
            # Call bridge via subprocess
            process = await asyncio.create_subprocess_exec(
                self.venv_python, self.bridge_script,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            
            stdout, stderr = await process.communicate(input=json.dumps(payload).encode())
            
            if process.returncode != 0:
                error_msg = stderr.decode().strip()
                logger.error(f"Bridge failed (code {process.returncode}): {error_msg}")
                return self._statistical_forecast(ohlcv_data, prediction_steps, timeframe)
            
            # Parse response
            result = json.loads(stdout.decode())
            
            if not result.get("success"):
                logger.error(f"Bridge returned error: {result.get('error')}")
                return self._statistical_forecast(ohlcv_data, prediction_steps, timeframe)
            
            # --- Sanity Check ---
            forecast = result.get("forecast", [])
            if not forecast:
                return self._statistical_forecast(ohlcv_data, prediction_steps, timeframe)
            
            last_price = float(ohlcv_data[-1]['c'])
            last_forecast_price = float(forecast[-1]['close'])
            
            # If prediction is > 30% jump/drop, it's likely an "outrageous" number due to scaling issues
            pct_change = abs(last_forecast_price - last_price) / last_price
            if pct_change > 0.3:
                logger.warning(f"⚠️ TTM result failed sanity check ({pct_change*100:.1f}% change). Falling back to statistical.")
                return self._statistical_forecast(ohlcv_data, prediction_steps, timeframe)
                
            return result
            
        except Exception as e:
             logger.error(f"TTM Bridge execution failed: {e}. Falling back.")
             return self._statistical_forecast(ohlcv_data, prediction_steps, timeframe)

    def _statistical_forecast(self, ohlcv_data: List[Dict[str, Any]], prediction_steps: int, timeframe: str = "1Day") -> Dict[str, Any]:
        """
        Deterministic Holt-Winters (Double Exponential Smoothing) forecast.
        Captures both Level and Trend (Momentum) without random noise.
        """
        closes = np.array([d['c'] for d in ohlcv_data])
        last_price = closes[-1]
        
        # 1. Initialize Double Exponential Smoothing (Holt's Method)
        # alpha = smoothing for level, beta = smoothing for trend
        alpha = 0.3
        beta = 0.1
        
        level = closes[0]
        trend = closes[1] - closes[0]
        
        for i in range(len(closes)):
            last_level = level
            level = alpha * closes[i] + (1 - alpha) * (level + trend)
            trend = beta * (level - last_level) + (1 - beta) * trend
            
        # 2. Generate Forecast
        forecast = []
        last_ts = ohlcv_data[-1].get('t')
        if isinstance(last_ts, (int, float)):
             curr_dt = datetime.datetime.fromtimestamp(last_ts / 1000.0)
        else:
            curr_dt = datetime.datetime.now()
        
        if "Day" in timeframe: delta = datetime.timedelta(days=1)
        elif "Week" in timeframe: delta = datetime.timedelta(weeks=1)
        elif "Month" in timeframe: delta = datetime.timedelta(days=30)
        else: delta = datetime.timedelta(hours=1)

        # Dampening factor to ensure trend doesn't explode
        phi = 0.95 
        
        for i in range(1, prediction_steps + 1):
            # Holt's Forecast: F(t+h) = level + h*trend
            # damped: F(t+h) = level + (phi^1 + phi^2 + ... + phi^h) * trend
            damped_steps = sum([phi**j for j in range(1, i + 1)])
            new_price = level + damped_steps * trend
            
            # Constraints: Max +/- 20% move from last price
            new_price = max(new_price, last_price * 0.8)
            new_price = min(new_price, last_price * 1.2)
            
            curr_dt += delta
            
            forecast.append({
                "open": float(new_price * 0.999),
                "high": float(new_price * 1.001),
                "low": float(new_price * 0.999),
                "close": float(new_price),
                "volume": float(ohlcv_data[-1]['v']),
                "timestamp": int(curr_dt.timestamp() * 1000)
            })
            
        return {
            "forecast": forecast,
            "prediction_steps": prediction_steps,
            "confidence": 0.5,
            "model_used": "statistical_trend_holt",
            "note": f"Holt-Winters Deterministic Trend (TF: {timeframe})"
        }


# --- Utility Functions ---

_forecaster_instance = None

def get_forecaster():
    """Singleton pattern for TTMForecaster"""
    global _forecaster_instance
    if _forecaster_instance is None:
        _forecaster_instance = TTMForecaster()
    return _forecaster_instance

if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    f = get_forecaster()
    print(f"Forecaster ready. Model: {f.ttm_available and 'TTM-R2' or 'Statistical'}")
