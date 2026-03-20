"""
Time-Series Forecasting Module

Provides statistical trend-based forecasting for stock prices.
Integrating IBM Granite TTM-R2 model for SOTA forecasting with statistical fallback.
"""

import numpy as np
import logging
import asyncio
import platform
import time
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
        """Initialize TTM-R2 bridge and Worker residency"""
        import os
        import sys
        from utils.worker_client import get_worker_client
        
        # Use absolute paths for reliability in subprocess
        base_dir = os.path.dirname(os.path.abspath(__file__))
        bridge_script = os.path.join(base_dir, "forecast_bridge.py")
        
        if os.path.exists(bridge_script):
            self.ttm_available = True
            self.bridge_script = bridge_script
            
            # SOTA 2026: Request residency in Worker Service
            async def setup_residency():
                try:
                    client = get_worker_client()
                    await client.load_ttm_model()
                    await client.load_jmce_model(resolution="daily")
                    logger.info("✅ High-Fidelity models pinned in Worker Service (GPU)")
                except Exception as e:
                    logger.warning(f"Residency request failed: {e}")
            
            # Fire and forget residency setup (asyncio.create_task requires a loop)
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(setup_residency())
                else:
                    # In sync main, we can't easily wait for it without blocking
                    pass
            except Exception:
                pass
                
            logger.info("✅ TTM-R2 Forecasting Bridge detected and ready")
        else:
            logger.warning(f"TTM-R2 Bridge script not found at {bridge_script}. Using Statistical Fallback.")
            self.ttm_available = False

    async def forecast(self, ohlcv_data: List[Dict[str, Any]], prediction_steps: int = 96, timeframe: str = "1Day", ticker: str = None) -> Dict[str, Any]:
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
                result = await self._ttm_forecast(ohlcv_data, prediction_steps, timeframe, ticker=ticker)
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

    async def _ttm_forecast(self, ohlcv_data: List[Dict[str, Any]], prediction_steps: int, timeframe: str, ticker: str = None) -> Dict[str, Any]:
        """Generate forecast using Fused TTM-JMCE via Worker Service"""
        import json
        from utils.worker_client import get_worker_client
        
        client = get_worker_client()

        try:
            # 1. Execute Fused Forecast via Worker (GPU/MLX)
            # This handles Multivariate TTM + Residual JMCE Correction
            logger.info(f"🚀 Dispatching Fused TTM-JMCE request (TF: {timeframe})...")
            start_time = time.time()
            result = await client.forecast_fused(
                ohlcv_data=ohlcv_data,
                prediction_steps=prediction_steps,
                timeframe=timeframe,
                ticker=ticker
            )
            duration_ms = (time.time() - start_time) * 1000
            logger.info(f"✅ Fused TTM-JMCE complete in {duration_ms:.2f}ms")
            
            if not result.get("success"):
                logger.error(f"Worker failed: {result.get('error')}")
                return await asyncio.get_event_loop().run_in_executor(None, self._statistical_forecast, ohlcv_data, prediction_steps, timeframe, f"Worker Error: {result.get('error')}")

            # --- SOTA 2026: Task 3.2 Alpha Hurdle ---
            forecast = result.get("forecast", [])
            if forecast:
                last_price = float(ohlcv_data[-1]['c'])
                max_pred = max(bar['close'] for bar in forecast)
                min_pred = min(bar['close'] for bar in forecast)
                
                # Check 75bp net-friction hurdle
                alpha_long = (max_pred - last_price) / last_price
                alpha_short = (last_price - min_pred) / last_price
                max_alpha = max(alpha_long, alpha_short)
                
                if max_alpha < 0.0075: # < 75bp
                    result["note"] += " | ⚠️ Sub-Hurdle Alpha (<75bp)"
                    result["clears_hurdle"] = False
                else:
                    result["clears_hurdle"] = True

            result["algorithm"] = "IBM Granite TTM-R2.1 + Neural JMCE"
            result["is_fallback"] = False
            return result
            
        except Exception as e:
            logger.error(f"Fused forecasting failed: {e}. Falling back.")
            return await asyncio.get_event_loop().run_in_executor(None, self._statistical_forecast, ohlcv_data, prediction_steps, timeframe, f"Fused Exe Error: {str(e)}")

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
        closes = np.array([float(d['c']) for d in ohlcv_data])
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
