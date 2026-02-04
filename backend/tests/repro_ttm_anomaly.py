
import sys
import os
import json
import logging

# Add backend to path
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from forecast_bridge import run_forecast

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ReproTest")

def test_anomaly_fix():
    print("Testing TTM-R2 Anomaly Fix...")
    
    # 1. Create Synthetic Data (Stable at ~100.0)
    ohlcv = []
    base_price = 100.0
    for i in range(50):
        p = base_price + (i * 0.1) # Slow trend up
        ohlcv.append({
            't': 1600000000000 + (i * 3600000),
            'o': p, 'h': p+1, 'l': p-1, 'c': p, 'v': 1000
        })
        
    # 2. Inject Anomaly (100x Jump -> 10000.0) simulating GBP -> GBX switch
    anomaly_point = {
        't': 1600000000000 + (50 * 3600000),
        'o': 10000.0, 'h': 10100.0, 'l': 9900.0, 'c': 10000.0, 'v': 5000
    }
    ohlcv.append(anomaly_point)
    
    print(f"Data prepared: 50 points at ~{base_price}, last point at {anomaly_point['c']} (100x jump)")
    
    # 3. Run Forecast
    try:
        # We expect the bridge to detect and normalize the 10000 -> 100
        result = run_forecast(ohlcv, prediction_steps=10, timeframe="1Hour")
        
        if not result.get("success"):
            print(f"FAILURE: Forecast returned error: {result.get('error')}")
            return False
            
        forecast = result.get("forecast", [])
        if not forecast:
            print("FAILURE: No forecast returned")
            return False
            
        first_pred = forecast[0]['close']
        print(f"First Prediction: {first_pred}")
        
        # 4. Assert Logic
        # If fix worked, prediction should be around 100-105.
        # If fix failed, prediction would be skewed towards 10000 or crash.
        
        if 90 < first_pred < 120:
            print("SUCCESS: Prediction is within normal range (normalized).")
            return True
        else:
            print(f"FAILURE: Prediction {first_pred} implies normalization failed (Expected ~100).")
            return False
            
    except Exception as e:
        print(f"CRITICAL FAILURE: {e}")
        return False

if __name__ == "__main__":
    success = test_anomaly_fix()
    sys.exit(0 if success else 1)
