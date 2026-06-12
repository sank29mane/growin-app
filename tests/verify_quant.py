import asyncio
import sys
import os

# Ensure backend is in path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../backend')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from agents.quant_agent import QuantAgent
import logging

logging.basicConfig(level=logging.INFO)

async def run():
    agent = QuantAgent()
    
    # 25 rows
    input_data = {
        "ticker": "AAPL",
        "ohlcv_data": [
            {"t": 1704103200000, "c": 150.0, "h": 155.0, "l": 149.0, "v": 10000},
            {"t": 1704103500000, "c": 151.0, "h": 156.0, "l": 150.0, "v": 11000},
            {"t": 1704103800000, "c": 152.0, "h": 157.0, "l": 151.0, "v": 12000},
            {"t": 1704104100000, "c": 153.0, "h": 158.0, "l": 152.0, "v": 13000},
            {"t": 1704104400000, "c": 154.0, "h": 159.0, "l": 153.0, "v": 14000},
        ] * 5,
        "intent": "intraday_trade"
    }
    
    try:
        res = await agent.analyze(input_data)
        print("Result data:", res.data)
        print("Result error:", res.error)
    except Exception as e:
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(run())
