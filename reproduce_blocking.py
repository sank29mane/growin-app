import sys
import types
import asyncio
import time
import random
import logging
import os

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Mock growin_core
growin_core = types.ModuleType("growin_core")
def mock_calculate_rsi(prices, period):
    return [50.0] * len(prices)
growin_core.calculate_rsi = mock_calculate_rsi
sys.modules["growin_core"] = growin_core

# Add backend to path
sys.path.append(os.path.join(os.getcwd(), "backend"))

from forecaster import TTMForecaster

async def heartbeat(interval=0.1):
    """Monitors event loop blocking."""
    logger.info("Starting heartbeat monitor...")
    max_delay = 0
    try:
        while True:
            start = time.perf_counter()
            await asyncio.sleep(interval)
            elapsed = time.perf_counter() - start
            delay = elapsed - interval
            if delay > max_delay:
                max_delay = delay
            if delay > 0.05: # Report significantly delayed heartbeats
                logger.warning(f"⚠️ Event loop blocked! Heartbeat delayed by {delay:.4f}s")
    except asyncio.CancelledError:
        logger.info(f"Heartbeat stopped. Max delay observed: {max_delay:.4f}s")
        return max_delay

async def run_forecast_benchmark():
    forecaster = TTMForecaster()
    
    # Generate dummy OHLCV data
    # Enough to trigger XGBoost (> 50 bars)
    n_bars = 200
    base_price = 100.0
    ohlcv_data = []
    current_time = int(time.time() * 1000) - (n_bars * 60 * 1000)
    
    for i in range(n_bars):
        price_change = random.uniform(-1, 1)
        base_price += price_change
        ohlcv_data.append({
            "t": current_time + (i * 60 * 1000),
            "o": base_price,
            "h": base_price + abs(price_change),
            "l": base_price - abs(price_change),
            "c": base_price + (price_change * 0.5),
            "v": 1000 + random.randint(0, 500)
        })
    
    logger.info("Starting forecast...")
    start_time = time.perf_counter()
    
    # Run forecast
    # We expect this to block the event loop because of the synchronous XGBoost training
    result = await forecaster.forecast(ohlcv_data, prediction_steps=10, timeframe="1Min")
    
    end_time = time.perf_counter()
    duration = end_time - start_time
    logger.info(f"Forecast completed in {duration:.4f}s")
    return result

async def main():
    monitor_task = asyncio.create_task(heartbeat())
    
    # Give the monitor a moment to start
    await asyncio.sleep(0.1)
    
    await run_forecast_benchmark()
    
    monitor_task.cancel()
    try:
        max_delay = await monitor_task
        print(f"MAX_DELAY_OBSERVED={max_delay:.4f}")
    except asyncio.CancelledError:
        pass

if __name__ == "__main__":
    asyncio.run(main())
