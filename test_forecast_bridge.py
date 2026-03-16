import json
import sys

from backend.forecast_bridge import run_forecast

# Generate dummy data
ohlcv = []
base_price = 100
for i in range(1000):
    ohlcv.append({
        "t": 1600000000000 + i * 3600000,
        "o": base_price,
        "h": base_price + 1,
        "l": base_price - 1,
        "c": base_price,
        "v": 1000
    })
    base_price += 0.1 # trending up

result = run_forecast(ohlcv, 96, timeframe="1Hour")
print(result.keys())
