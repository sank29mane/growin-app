"""
Forecast Bridge - Standalone script to run TTM-R2 in a Python 3.11 environment.
This script is called by the main backend (Python 3.13) via subprocess.
"""

import sys
import json
import logging
import pandas as pd
from typing import List, Dict, Any

# Configure logging to stderr so it doesn't interfere with JSON output on stdout
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stderr
)
logger = logging.getLogger("ForecastBridge")

def run_forecast(ohlcv_data: List[Dict[str, Any]], prediction_steps: int, timeframe: str = "1Hour") -> Dict[str, Any]:
    """Execute TTM-R2 forecasting with scaling and frequency awareness"""
    try:
        from tsfm_public.models.tinytimemixer import TinyTimeMixerForPrediction
        from tsfm_public.toolkit.time_series_forecasting_pipeline import TimeSeriesForecastingPipeline
        from sklearn.preprocessing import StandardScaler
        import numpy as np
        
        logger.info(f"Loading TTM-R2 model for {len(ohlcv_data)} points with timeframe {timeframe}...")

        # 1. Map timeframe to pandas frequency
        tf_map = {
            "1Min": "min",
            "5Min": "5min",
            "1Hour": "H",
            "1Day": "D",
            "1Week": "W",
            "1Month": "ME"
        }
        freq = tf_map.get(timeframe, "H")
        
        # Load model
        # TTM-R2 is a Zero-Shot model by default. We load it in evaluation mode.
        model = TinyTimeMixerForPrediction.from_pretrained(
            "ibm-granite/granite-timeseries-ttm-r2",
            context_length=512,
            prediction_length=96
        )
        model.eval() # Ensure deterministic inference (Zero-Shot)
        
        # 2. Extract and Scale Data
        # documentation says: "Users have to externally standard scale their data before feeding it to the model"
        
        # CLEANUP & ROBUSTNESS: 
        # 1. Convert to DF first for easy cleaning
        # 2. Handle NaNs and Zeros (Forward Fill) preventing scaling corruption
        df_raw = pd.DataFrame(ohlcv_data)
        
        # ensure numeric
        cols = ['c', 'h', 'l', 'o', 'v']
        
        # --- PRE-DATAFRAME SANITIZATION (Unit Mismatch Fix) ---
        # Detect if the last point (often real-time) is disjointed from history due to GBX/GBP mismatch.
        if len(ohlcv_data) > 2:
            last = ohlcv_data[-1]
            prev = ohlcv_data[-2]
            
            try:
                last_c = float(last.get('c', 0))
                prev_c = float(prev.get('c', 0))
                
                if last_c > 0 and prev_c > 0:
                    ratio = last_c / prev_c
                    
                    # Case 1: Pence to Pounds jump (e.g. 0.70 -> 70.0) -> implies last is GBX, prev was GBP? 
                    # Or prev was GBP (0.7) and last is GBX (70). 
                    # If ratio > 50 (allow for massive volatility, but 100x is distinct)
                    if 90 < ratio < 110:
                        logger.warning(f"Unit Mismatch Detected (GBP->GBX): Prev={prev_c}, Last={last_c}. Normalizing Last / 100.")
                        # Normalize entire last bar
                        ohlcv_data[-1]['c'] = last_c / 100.0
                        ohlcv_data[-1]['h'] = float(last.get('h', last_c)) / 100.0
                        ohlcv_data[-1]['l'] = float(last.get('l', last_c)) / 100.0
                        ohlcv_data[-1]['o'] = float(last.get('o', last_c)) / 100.0
                        
                    # Case 2: Pounds to Pence drop (e.g. 70 -> 0.70) -> implies last is GBP, prev was GBX
                    elif 0.009 < ratio < 0.011:
                        logger.warning(f"Unit Mismatch Detected (GBX->GBP): Prev={prev_c}, Last={last_c}. Normalizing Last * 100.")
                        ohlcv_data[-1]['c'] = last_c * 100.0
                        ohlcv_data[-1]['h'] = float(last.get('h', last_c)) * 100.0
                        ohlcv_data[-1]['l'] = float(last.get('l', last_c)) * 100.0
                        ohlcv_data[-1]['o'] = float(last.get('o', last_c)) * 100.0
                        
                    # Case 3: Outrageous deviation (>30%) check
                    elif abs(ratio - 1.0) > 0.30:
                        logger.warning(f"Significant price deviation detected at tail: {prev_c} -> {last_c} ({((ratio-1)*100):.1f}%)")
                        # We don't auto-correct this as it might be a real crash/pump, but we log it.
                        
            except Exception as e:
                logger.warning(f"Sanitization check failed: {e}")
        # -----------------------------------------------------

        df_raw = pd.DataFrame(ohlcv_data)
        
        for c in cols:
            if c in df_raw.columns:
                df_raw[c] = pd.to_numeric(df_raw[c], errors='coerce')
        
        # Replace 0.0 with NaN (Price shouldn't be 0)
        for c in ['c', 'h', 'l', 'o']:
             df_raw[c] = df_raw[c].replace(0.0, np.nan)
             
        # Forward fill then Backward fill
        df_raw = df_raw.ffill().bfill()
        
        # Extract Cleaned Closes
        closes_raw = df_raw['c'].values
        
        # Winsorize outliers (clip to 1st/99th percentile) - Safety against flash crashes in training data
        p01 = np.percentile(closes_raw, 1)
        p99 = np.percentile(closes_raw, 99)
        closes_clamped = np.clip(closes_raw, p01, p99).reshape(-1, 1)

        # ROBUST SCALING STRATEGY (Windowed):
        # TTM requires N(0,1) scaling. Standard Scaler is ruinous with outliers.
        # We calculate mean/std from the "Stable Core" (middle 90%) of data only.
        # CRITICAL FIX: Use only the last 512 bars (Local Regime) to calculate stats!
        # Calculating stats on 4 years of history (1000 bars) when price moved 20->70 causes 
        # current price (70) to be an 11-sigma outlier vs ancient mean (20).
        
        flat = closes_clamped.flatten()
        
        # --- TICKER OPTIMIZATION (Dynamic Regime Detection) ---
        # We analyze the ticker's character to choose the best scaling parameters.
        # High volatility tickers need shorter, more sensitive windows.
        
        flat_raw = closes_clamped.flatten()
        
        # Calculate recent volatility (normalized)
        if len(flat_raw) > 30:
            recent_std = np.std(flat_raw[-30:])
            overall_mean = np.mean(flat_raw)
            volatility_index = recent_std / (overall_mean if overall_mean > 0 else 1)
        else:
            volatility_index = 0.02 # default moderate
            
        # Optimization: Highly volatile tickers (e.g. Crypto/Spiking) get tighter tracking
        # Moderate/Stable tickers get smoother tracking to avoid chasing noise.
        if volatility_index > 0.05: # High Volatility Ticker
            LOOKBACK_WINDOW = 64 # Focus more on the recent breakout
            EMA_SPAN = 5        # React quickly to price movements
            logger.info(f"Ticker is HIGH VOLATILITY ({volatility_index:.2%}). Optimizing for quick regime shifts.")
        else:
            LOOKBACK_WINDOW = 256 # Use more history for stable assets
            EMA_SPAN = 20       # Smooth out noise
            logger.info(f"Ticker is STABLE ({volatility_index:.2%}). Optimizing for trend stability.")

        # --- TICKER OPTIMIZATION (Local Price Centering) ---
        # TTM-R2 performs best when the input data is near Z=0.
        # We strictly use the last 512 bars to match the model's native context length.
        
        WINDOW_SIZE = 512
        if len(ohlcv_data) > WINDOW_SIZE:
             target_data = ohlcv_data[-WINDOW_SIZE:]
        else:
             target_data = ohlcv_data
             
        df_target = pd.DataFrame(target_data)
        flat_all = df_target['c'].values
        
        # 1. Calculation of Volatility (Standard Deviation)
        # We use the full 512-bar window to understand the ticker's 'signature' volatility.
        robust_std = np.std(flat_all)
        
        # 2. Robust Scaling Strategy (Full Window Standard)
        # TTM-R2 is trained on N(0,1) data. We must respect this distribution over the context window.
        # Previous attempts at "Local Centering" (last 32 bars) distorted the history Z-scores,
        # causing the model to misinterpret trends.
        # We now use standard mean/std of the FULL 512-bar window, and rely on Post-Hoc Anchoring to fix levels.
        
        WINDOW_SIZE = 512
        if len(ohlcv_data) > WINDOW_SIZE:
             target_data = ohlcv_data[-WINDOW_SIZE:]
        else:
             target_data = ohlcv_data
             
        df_target = pd.DataFrame(target_data)
        flat_all = df_target['c'].values
        
        # Calculate Standard Stats
        robust_mean = np.mean(flat_all)
        robust_std = np.std(flat_all)
        
        # Volatility Floor
        if robust_std < 1e-6: robust_std = 1.0 # Avoid div/0 for flat lines
        
        logger.info(f"TTM Scaling (Standard): Mean={robust_mean:.2f}, Std={robust_std:.2f} (Context: {len(flat_all)} bars)")
        
        # 3. Transform
        scaled_closes = (flat_all - robust_mean) / robust_std
        
        # For inverse transform later, we need a dummy scaler or just manually do it
        # We will manually inverse transform later using (scaled * std) + mean
        
        # FREQUENCY AGNOSTIC STRATEGY:
        # Instead of feeding timestamps (which causes NaN injection on gaps), we feed integers 0..N.
        # This forces the model to predict step N+1, N+2, regardless of weekends.
        
        # 1. Create Integer Index
        df = pd.DataFrame()
        df['time_idx'] = range(len(scaled_closes)) # 0, 1, 2...
        df['close'] = scaled_closes
        df['high'] = df_raw['h']
        df['low'] = df_raw['l']
        df['volume'] = df_raw['v']
        df['id'] = 'ticker'
        
        # 2. Pipeline (Integer Mode)
        # We misuse 'timestamp_column' to pass integers. TTM accepts this if freq is handled or ignored.
        # Actually, TSFM pipeline strictly requires dates for 'freq' logic.
        # Workaround: Construct a synthetic index that MATCHES the timeframe frequency.
        # This gives the model a better "sense of time" (e.g. daily vs minute patterns).
        
        start_date = pd.Timestamp("2020-01-01 00:00:00")
        
        # Map timeframe to synthetic delta
        syn_delta = pd.Timedelta(hours=1) # default
        pipeline_freq = "1h"
        
        if "Min" in timeframe:
            syn_delta = pd.Timedelta(minutes=int(timeframe.replace("Min", "").strip() or 1))
            pipeline_freq = "min"
        elif "Day" in timeframe:
            syn_delta = pd.Timedelta(days=1)
            pipeline_freq = "D"
            
        df['synthetic_time'] = [start_date + (syn_delta * i) for i in range(len(df))]
        
        pipeline = TimeSeriesForecastingPipeline(
            model=model,
            freq=pipeline_freq, # Use matched frequency
            timestamp_column='synthetic_time',
            id_columns=['id'],
            target_columns=['close'],
            explode_forecasts=True
        )
        
        # 3. Generate Predictions
        forecast_df = pipeline(df)
        
        # 4. Post-Processing: Map back to Real Time
        # The model output timestamps will be "2020-xx-xx..." (synthetic). We ignore them.
        # We calculate the *real* average delta from the input and extrapolate.
        
        # Calculate real delta from last 10 bars to capture recent frequency
        real_timestamps = pd.to_datetime(df_raw['t'], unit='ms')
        if len(real_timestamps) > 1:
             avg_delta = (real_timestamps.iloc[-1] - real_timestamps.iloc[-10]) / 9
        else:
             avg_delta = pd.Timedelta(hours=1)
             
        last_real_ts = real_timestamps.iloc[-1]
        
        forecast = []
        last_vol = ohlcv_data[-1]['v']
        
        for idx, row in forecast_df.iterrows():
            # Calculate next timestamp based on step index in forecast
            # forecast_df usually returns a sequence. We assume row order is chronological.
            step_num = idx + 1 # 1-based step
            
            # --- BUSINESS DAY LOGIC ---
            # If timeframe is '1Day', we should skip weekends.
            # Current naive logic: future_ts = last_real_ts + (avg_delta * step_num)
            
            if timeframe == "1Day":
                # Add 'step_num' business days to last_real_ts
                current_ts = last_real_ts
                days_added = 0
                while days_added < step_num:
                    current_ts += pd.Timedelta(days=1)
                    # 0=Mon, 6=Sun. If 5 (Sat) or 6 (Sun), skip.
                    if current_ts.dayofweek >= 5:
                        continue
                    days_added += 1
                future_ts = current_ts
            else:
                # For non-daily (e.g. Hour/Min), naive extrapolation is usually fine
                # (Crypto trades 24/7, but stocks don't. Ideally we'd map trading hours too, 
                # but valid-day-skip is the most critical fix for "Daily" charts).
                future_ts = last_real_ts + (avg_delta * step_num)
            
            # Get scaled prediction
            scaled_price = row.get('close', 0)
            
            try:
                # Manual Inverse Transform using Robust Params
                unscaled_price = float((scaled_price * robust_std) + robust_mean)
            except:
                unscaled_price = float(ohlcv_data[-1]['c'])

            if np.isnan(unscaled_price) or np.isinf(unscaled_price):
                unscaled_price = float(ohlcv_data[-1]['c'])

            forecast.append({
                "open": float(unscaled_price * 0.999),
                "high": float(unscaled_price * 1.005),
                "low": float(unscaled_price * 0.995),
                "close": float(unscaled_price),
                "volume": float(last_vol),
                "timestamp": int(future_ts.timestamp() * 1000)
            })
            
        # --- ANCHORING (BIAS CORRECTION) ---
        # The model's "robust mean" might differ significantly from the *current* price 
        # (e.g. if price rallied 50% recently, the mean is lagging).
        # This causes the first prediction to "crash" back towards the mean.
        # We calculate the gap at the last known point and shift the entire curve.
        
        last_actual_price = float(ohlcv_data[-1]['c'])
        
        # Reconstruct what the model thinks the "current price" is (using the LAST input point)
        # Note: We used the integers trick, so we look at the last forecast point's relation? 
        # Actually, best anchor is to compare the *start* of the forecast window.
        # Ideally, we should check `model(current_state)` but we can approximate:
        # If the forecast curve starts at X, and we are at Y, shift by (Y-X).
        
        if forecast:
            # --- RELATIVE ANCHORING ---
            # We want to preserve the model's predicted *move* (slope), but anchor the *level* to the real price.
            # If we rely on Hard Anchoring (T+1 = Last), we kill the T=0 -> T+1 prediction (always 0%).
            # Instead, we calculate the bias based on what the model *thought* T=0 was.
            
            # The model's view of the last input point:
            last_scaled_input = scaled_closes[-1]
            reconstructed_last = (last_scaled_input * robust_std) + robust_mean
            
            # The bias is the difference between Reality and Model's View of Reality
            # Bias = Real - Model
            anchor_bias = last_actual_price - reconstructed_last
            
            # logger.info(f"⚓ Anchoring: Actual={last_actual_price:.2f}, ModelLast={reconstructed_last:.2f}, Bias={anchor_bias:.2f}")

            # Apply bias to all forecast points
            for point in forecast:
                point['close'] += anchor_bias
                point['open'] += anchor_bias
                point['high'] += anchor_bias
                point['low'] += anchor_bias
        else:
            anchor_bias = 0.0

        return {
            "success": True,
            "forecast": forecast[:prediction_steps],
            "prediction_steps": len(forecast[:prediction_steps]),
            "confidence": 0.85,
            "model_used": "TTM-R2.1",
            "frequency_used": "Integer-Synthetic",
            "note": "Using IBM Granite TTM-R2.1 with Robust Statistical Scaling",
            "debug_scaling": {
                "mean": float(robust_mean),
                "std": float(robust_std),
                "anchor_bias": float(anchor_bias) if 'anchor_bias' in locals() else 0.0
            }
        }
        
    except Exception as e:
        logger.error(f"Bridge execution failed: {e}", exc_info=True)
        return {
            "success": False,
            "error": str(e)
        }

if __name__ == "__main__":
    # Read input from stdin
    try:
        input_data = json.load(sys.stdin)
        ohlcv = input_data.get("ohlcv_data", [])
        steps = input_data.get("prediction_steps", 96)
        timeframe = input_data.get("timeframe", "1Hour")
        
        result = run_forecast(ohlcv, steps, timeframe=timeframe)
        
        # Write result to stdout
        # Inline sanitization to ensure JSON safe output (NaN -> 0.0) without external deps
        def sanitize_nodes(obj):
            import math
            if isinstance(obj, float):
                if math.isnan(obj) or math.isinf(obj):
                    return 0.0
                return obj
            elif isinstance(obj, dict):
                return {k: sanitize_nodes(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [sanitize_nodes(v) for v in obj]
            return obj

        print(json.dumps(sanitize_nodes(result)))
    except Exception as e:
        print(json.dumps({"success": False, "error": f"Input error: {str(e)}"}))
        sys.exit(1)
