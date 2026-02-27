"""
Time-Series Forecasting Module

Provides statistical trend-based forecasting for stock prices.
Integrating IBM Granite TTM-R2 model for SOTA forecasting with statistical fallback.
"""

import numpy as np
import logging
import asyncio
import platform
from typing import List, Dict, Any
from sklearn.preprocessing import StandardScaler
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
        import sys
        # Use absolute paths for reliability in subprocess
        base_dir = os.path.dirname(os.path.abspath(__file__))
        # Use current python executable instead of missing venv
        venv_python = sys.executable
        bridge_script = os.path.join(base_dir, "forecast_bridge.py")
        
        # Check if tsfm_public is importable in current env or bridge script exists
        if os.path.exists(bridge_script):
            self.ttm_available = True
            self.venv_python = venv_python
            self.bridge_script = bridge_script
            logger.info(f"✅ TTM-R2 Forecasting Bridge detected and ready (using {sys.executable})")
        else:
            logger.warning(f"TTM-R2 Bridge script not found at {bridge_script}. Using Statistical Fallback.")
            self.ttm_available = False

    async def forecast(self, ohlcv_data: List[Dict[str, Any]], prediction_steps: int = 96, timeframe: str = "1Day") -> Dict[str, Any]:
        """
        Generate forecast using TTM-R2 with auxiliary ML comparisons.
        Priority:
        1. TTM-R2 (Primary)
        2. XGBoost (Fallback for TTM or Intraday)
        3. Holt-Winters (Last Resort)
        """
        if not ohlcv_data or len(ohlcv_data) < 50:
            return {
                "error": "Insufficient historical data (need at least 50 points)",
                "forecast": [],
                "confidence": 0.0
            }

        auxiliary_results = []
        xgb_result = None
        
        # --- 1. SOTA: XGBoost (Peer & Fallback Candidate) ---
        try:
            from fallbacks.ml_forecaster import MLIntradayForecaster
            xgb_forecaster = MLIntradayForecaster(model_type="xgboost")
            
            # Check minimum data requirements (XGB needs ~50 bars)
            if len(ohlcv_data) >= 50:
                xgb_preds = xgb_forecaster.train_and_predict(ohlcv_data, prediction_steps)
                
                if xgb_preds:
                    last_val = float(xgb_preds[-1])
                    start_val = ohlcv_data[-1]['c']
                    change_pct = ((last_val - start_val) / start_val) * 100
                    
                    # Store full result structure for possible fallback use
                    xgb_result = {
                        "forecast": [{"close": p} for p in xgb_preds], # Simplified structure
                        "note": "XGBoost ML (Intraday Optimized)",
                        "confidence": 0.7,
                        "model_used": "xgboost_ml",
                        "algorithm": "XGBoost (Intraday Optimized)",
                        "is_fallback": True
                    }
                    
                    auxiliary_results.append({
                        "model": "XGBoost (ML)",
                        "prediction_pct": change_pct,
                        "direction": "UP" if change_pct > 0 else "DOWN",
                        "confidence": "MEDIUM"
                    })
        except Exception as e:
            logger.warning(f"XGBoost execution failed: {e}")

        # --- 2. Baseline: Holt-Winters (Peer) ---
        try:
            loop = asyncio.get_running_loop()
            hw_raw = await loop.run_in_executor(
                None,
                self._statistical_forecast,
                ohlcv_data,
                prediction_steps,
                timeframe,
                None # Not a fallback, just a peer
            )
            
            if hw_raw.get("forecast"):
                last_hw = hw_raw["forecast"][-1]['close']
                start_p = ohlcv_data[-1]['c']
                pct_chg_hw = ((last_hw - start_p) / start_p) * 100
                auxiliary_results.append({
                    "model": "Holt-Winters (Statistical)",
                    "prediction_pct": pct_chg_hw,
                    "direction": "UP" if pct_chg_hw > 0 else "DOWN",
                    "confidence": "LOW"
                })
        except Exception as e:
            logger.warning(f"Holt-Winters execution failed: {e}")

        try:
            result = {}
            
            # --- PRIMARY: TTM-R2 ---
            ttm_success = False
            if self.ttm_available and len(ohlcv_data) >= 512:
                result = await self._ttm_forecast(ohlcv_data, prediction_steps, timeframe)
                if result and not result.get("is_fallback", False) and result.get("forecast"):
                    ttm_success = True
                
            if not ttm_success:
                # --- FALLBACK PATH ---
                reason = "Insufficient Data (Need 512)" if len(ohlcv_data) < 512 else "TTM Not Available"
                if self.ttm_available:
                     # Check if it was a sanity failure in _ttm_forecast
                     if result and "Sanity Check" in result.get("note", ""):
                         reason = "TTM Sanity Check Failed"
                     logger.info(f"TTM Skipped/Failed: {reason}. Using SOTA XGBoost Fallback.")
                
                if xgb_result:
                    # USE XGBOOST (Preferred Fallback)
                    result = xgb_result
                    result["note"] = f"Using XGBoost Fallback ({reason})"
                    
                    # Rehydrate timestamps (simple extrapolation) for the chart
                    last_ts = ohlcv_data[-1]['t']
                    preds = xgb_result["forecast"] 
                    full_forecast = []
                    
                    interval_ms = 3600000 # Default 1h
                    if "Day" in timeframe: interval_ms = 86400000
                    elif "Min" in timeframe: interval_ms = 60000
                    elif "Week" in timeframe: interval_ms = 604800000
                    
                    curr = last_ts
                    for p in preds:
                        curr += interval_ms
                        val = p['close']
                        full_forecast.append({
                            "timestamp": curr,
                            "close": val,
                            "high": val, 
                            "low": val,
                            "open": val,
                            "volume": 0
                        })
                    result["forecast"] = full_forecast
                    
                else:
                    # LAST RESORT: Holt-Winters (if XGBoost failed too)
                    loop = asyncio.get_running_loop()
                    result = await loop.run_in_executor(
                        None,
                        self._statistical_forecast,
                        ohlcv_data,
                        prediction_steps,
                        timeframe,
                        f"{reason} + ML Failed"
                    )

            # Attach auxiliary data
            result["auxiliary_forecasts"] = auxiliary_results
            return result
            
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
        
        loop = asyncio.get_running_loop()

        try:
            # Prepare input
            # DIAGNOSTIC: Log input data characteristics
            if ohlcv_data:
                prices = [d['c'] for d in ohlcv_data]
                logger.info(f"TTM Input Stats: Count={len(prices)}, Start={prices[0]}, End={prices[-1]}, Min={min(prices)}, Max={max(prices)}")
                logger.info(f"TTM Timeframe: {timeframe}, Steps: {prediction_steps}")

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
                return await loop.run_in_executor(None, self._statistical_forecast, ohlcv_data, prediction_steps, timeframe, f"Bridge Error: {error_msg[:50]}...")

            # Parse response
            result = json.loads(stdout.decode())
            
            if not result.get("success"):
                logger.error(f"Bridge returned error: {result.get('error')}")
                return await loop.run_in_executor(None, self._statistical_forecast, ohlcv_data, prediction_steps, timeframe, f"Bridge Error: {result.get('error')}")

            # --- Sanity Check ---
            forecast = result.get("forecast", [])
            if not forecast:
                return await loop.run_in_executor(None, self._statistical_forecast, ohlcv_data, prediction_steps, timeframe, "Empty TTM Forecast")

            last_price = float(ohlcv_data[-1]['c'])
            last_forecast_price = float(forecast[-1]['close'])
            
            # If prediction is > 50% jump/drop, it's likely an "outrageous" number due to scaling issues
            pct_change = abs(last_forecast_price - last_price) / last_price
            if pct_change > 0.5:
                debug_info = result.get("debug_scaling", {})
                logger.warning(f"⚠️ TTM result failed sanity check ({pct_change*100:.1f}% change). Fallback. Debug: {debug_info}")
                return await loop.run_in_executor(None, self._statistical_forecast, ohlcv_data, prediction_steps, timeframe, f"Sanity Check Failed: Forecast {last_forecast_price:.2f} vs Last {last_price:.2f} (>50% variance)")
                
            # Add algorithm metadata
            result["algorithm"] = "IBM Granite TTM-R2.1"
            result["is_fallback"] = False
            return result
            
        except Exception as e:
            logger.error(f"TTM Bridge execution failed: {e}. Falling back.")
            return await loop.run_in_executor(None, self._statistical_forecast, ohlcv_data, prediction_steps, timeframe, f"Bridge Exe Error: {str(e)}")

    def _statistical_forecast(self, ohlcv_data: List[Dict[str, Any]], prediction_steps: int, timeframe: str = "1Day", reason: str = None) -> Dict[str, Any]:
        """
        State-of-the-Art (SOTA) Fallback Engine:
        1. For Intraday (1Min-1Hour): Uses Enhanced XGBoost with technical indicators.
        2. For Trend/Global: Uses Holt-Winters (Double Exponential Smoothing).
        """
        # --- 1. SOTA ML Fallback for Intraday & TTM Recovery ---
        intraday_timeframes = ["1Min", "5Min", "15Min", "1Hour"]
        should_use_sota = (
            timeframe in intraday_timeframes or 
            (reason and ("TTM" in reason or "Sanity Check" in reason))
        )
        
        if should_use_sota: 
            try:
                from fallbacks.ml_forecaster import MLIntradayForecaster
                ml_f = MLIntradayForecaster()
                ml_preds = ml_f.train_and_predict(ohlcv_data, prediction_steps)
                
                if ml_preds:
                    # Construct forecast response
                    forecast = []
                    last_ts = ohlcv_data[-1].get('t')
                    curr_dt = datetime.datetime.fromtimestamp(last_ts / 1000.0) if last_ts else datetime.datetime.now()
                    
                    # Time delta mapping
                    delta_map = {"1Min": 1, "5Min": 5, "15Min": 15, "1Hour": 60}
                    delta_mins = delta_map.get(timeframe, 60)
                    
                    for p in ml_preds:
                        curr_dt += datetime.timedelta(minutes=delta_mins)
                        forecast.append({
                            "open": float(p), "high": float(p), "low": float(p), "close": float(p),
                            "volume": float(ohlcv_data[-1]['v']),
                            "timestamp": int(curr_dt.timestamp() * 1000)
                        })
                    
                    return {
                        "forecast": forecast,
                        "prediction_steps": prediction_steps,
                        "confidence": 0.7, # Higher confidence for ML fallback
                        "model_used": "sota_xgboost_intraday",
                        "algorithm": "XGBoost (Intraday Optimized)",
                        "is_fallback": True,
                        "note": f"SOTA XGBoost Fallback (RSI/ATR Optimized) [Reason: {reason or 'Intraday Mode'}]"
                    }
            except Exception as e:
                logger.warning(f"SOTA ML Fallback failed, reverting to baseline: {e}")

        # --- 2. Baseline Fallback (Holt-Winters) ---
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
            
        note = f"Holt-Winters Deterministic Trend (TF: {timeframe})"
        if reason:
            note += f" [Fallback: {reason}]"
            
        return {
            "forecast": forecast,
            "prediction_steps": prediction_steps,
            "confidence": 0.5,
            "model_used": "statistical_trend_holt",
            "algorithm": "Holt-Winters Statistical",
            "is_fallback": True,
            "note": note
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
