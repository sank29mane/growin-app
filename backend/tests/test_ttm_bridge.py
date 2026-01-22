import asyncio
import logging
import json
import time
from forecaster import TTMForecaster

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("TestTTM")

async def test_ttm_scaling():
    print("\n--- Testing TTM with Large Scale Data (UK Stocks) ---")
    forecaster = TTMForecaster()
    
    # 1. Generate large scale data (7000 range like SGLN.L)
    now = int(time.time() * 1000)
    uk_data = []
    base_price = 7000.0
    for i in range(600):
        uk_data.append({
            "t": now - (600 - i) * 86400 * 1000, # 1 day steps
            "o": base_price + i * 2,
            "h": base_price + i * 2 + 5,
            "l": base_price + i * 2 - 5,
            "c": base_price + i * 2,
            "v": 100000
        })
    
    last_price = uk_data[-1]['c']
    print(f"Last input price: {last_price}")
    
    print("🚀 Running TTM-R2 forecast (Timeframe: 1Day)...")
    start_time = time.time()
    result = await forecaster.forecast(uk_data, prediction_steps=10, timeframe="1Day")
    end_time = time.time()
    
    print(f"Elapsed time: {end_time - start_time:.2f}s")
    print(f"Model used: {result.get('model_used')}")
    print(f"Note: {result.get('note')}")
    
    forecast = result.get('forecast', [])
    if forecast:
        first_f = forecast[0]['close']
        last_f = forecast[-1]['close']
        print(f"First forecast price: {first_f:.2f}")
        print(f"Last forecast price: {last_f:.2f}")
        
        # Verify scaling (should be near last price, not 0-1)
        if 6000 < first_f < 9000:
            print("✅ Scaling Verification Successful (Unscaled price returned)")
        else:
            print(f"❌ Scaling Verification Failed! (Price {first_f} is out of expected range)")

        # Verify deterministic nature (run again)
        print("🔄 Running second pass for determinism...")
        result2 = await forecaster.forecast(uk_data, prediction_steps=10, timeframe="1Day")
        if result2.get('forecast', [])[-1]['close'] == last_f:
            print("✅ Determinism Verification Successful!")
        else:
            print("❌ Determinism Verification Failed! (Forecast changed on refresh)")
    else:
        print("❌ No forecast generated")

if __name__ == "__main__":
    asyncio.run(test_ttm_scaling())
