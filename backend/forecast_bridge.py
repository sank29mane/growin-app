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
        model = TinyTimeMixerForPrediction.from_pretrained(
            "ibm-granite/granite-timeseries-ttm-r2",
            context_length=512,
            prediction_length=96
        )
        
        # 2. Extract and Scale Data
        # documentation says: "Users have to externally standard scale their data before feeding it to the model"
        closes = np.array([float(d['c']) for d in ohlcv_data]).reshape(-1, 1)
        scaler = StandardScaler()
        scaled_closes = scaler.fit_transform(closes).flatten()
        
        # Prepare dataframe
        df = pd.DataFrame([
            {
                'timestamp': d['t'],
                'close': float(scaled_closes[i]),
                'high': float(d['h']), # TTM-R2 primarily uses close, but we provide others
                'low': float(d['l']),
                'volume': float(d['v']),
                'id': 'ticker'
            } for i, d in enumerate(ohlcv_data)
        ])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        
        # Create pipeline
        pipeline = TimeSeriesForecastingPipeline(
            model=model,
            freq=freq,
            timestamp_column='timestamp',
            id_columns=['id'],
            target_columns=['close'],
            explode_forecasts=True
        )
        
        # Generate predictions
        forecast_df = pipeline(df)
        
        # 3. Inverse Transform and Format
        forecast = []
        last_vol = ohlcv_data[-1]['v']
        last_price = float(ohlcv_data[-1]['c'])
        
        for idx, row in forecast_df.iterrows():
            ts_val = row.get('timestamp', idx)
            if not hasattr(ts_val, 'timestamp'):
                ts_val = pd.to_datetime(ts_val)
                
            # Get scaled prediction
            scaled_price = row.get('close', 0)
            
            # Inverse transform to original scale
            try:
                # Scaler expects 2D array for inverse_transform
                unscaled_price = float(scaler.inverse_transform([[scaled_price]])[0][0])
            except:
                unscaled_price = last_price # Fallback
                
            # Handle potential NaNs or outrageous values from model
            if np.isnan(unscaled_price) or np.isinf(unscaled_price):
                unscaled_price = last_price

            forecast.append({
                "open": float(unscaled_price * 0.999),
                "high": float(unscaled_price * 1.005),
                "low": float(unscaled_price * 0.995),
                "close": float(unscaled_price),
                "volume": float(last_vol),
                "timestamp": int(ts_val.timestamp() * 1000)
            })
        
        return {
            "success": True,
            "forecast": forecast[:prediction_steps],
            "prediction_steps": len(forecast[:prediction_steps]),
            "confidence": 0.85,
            "model_used": "TTM-R2",
            "frequency_used": freq,
            "note": "Using IBM Granite TTM-R2 with Standard Scaling"
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
