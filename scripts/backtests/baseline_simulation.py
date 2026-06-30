import asyncio
import os
import json
import logging
import sys
import pandas as pd
import numpy as np
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any

# Root relative imports
from backend.mcp_client import Trading212MCPClient
from backend.forecaster import get_forecaster
from backend.utils.orb_detector import ORBDetector

logging.basicConfig(level=logging.INFO, format='%(levelname)s:%(name)s:%(message)s')
logger = logging.getLogger("Baseline-Sim")

async def run_baseline():
    logger.info("🚀 Starting Phase 32 Baseline Simulation (TQQQ/SQQQ)")
    
    mcp = Trading212MCPClient()
    server_config = {
        "name": "Sim-Data-Fetcher",
        "type": "stdio",
        "command": "uv",
        "args": ["run", "backend/trading212_mcp_server.py"],
        "env": {
            "TRADING212_API_KEY": os.getenv("TRADING212_API_KEY"),
            "TRADING212_API_KEY_ISA": os.getenv("TRADING212_API_KEY_ISA"),
            "TRADING212_USE_DEMO": "false",
            "PYTHONPATH": "backend"
        }
    }

    async with mcp.connect_all([server_config]):
        # Using LSE-based LETFs to avoid 0.15% FX fee (Commission-free for UK acc)
        # LQQ3: WisdomTree Nasdaq 100 3x Daily Leveraged
        # 3QQQ: Leverage Shares 3x Long Tech 100
        # LQQS: WisdomTree Nasdaq 100 3x Daily Short
        tickers = ["LQQ3.L", "3QQQ.L", "LQQS.L"]
        results = []

        for ticker in tickers:
            logger.info(f"📊 Simulating {ticker}...")
            
            # Fetch 1mo of 1H data (SOTA baseline context)
            res = await mcp.call_tool("get_price_history", {
                "ticker": ticker,
                "interval": "1h",
                "period": "1y" # Request 1y to ensure we get a solid history
            })
            
            if not res.content or not res.content[0].text:
                logger.error(f"❌ Failed to fetch data for {ticker}")
                continue
                
            data = json.loads(res.content[0].text)
            bars = data.get("price_data", [])
            
            if len(bars) < 512:
                logger.error(f"❌ Insufficient data for {ticker} (Got {len(bars)}, need 512 for TTM)")
                continue

            # Run TTM-R2 Forecast
            forecaster = get_forecaster()
            # Use last 512 bars for context
            context = []
            for b in bars[-512:]:
                context.append({
                    "c": float(b["Close"]),
                    "h": float(b["High"]),
                    "l": float(b["Low"]),
                    "o": float(b["Open"]),
                    "v": float(b["Volume"]),
                    "t": int(pd.to_datetime(b["Date"]).timestamp() * 1000)
                })
            
            forecast = await forecaster.forecast(context, prediction_steps=78, timeframe="5Min")
            
            # Run ORB Detection on live (last day)
            detector = ORBDetector(range_minutes=30)
            # Find bars from today (last day in bars)
            last_date = pd.to_datetime(bars[-1]["Date"]).date()
            today_bars = [b for b in bars if pd.to_datetime(b["Date"]).date() == last_date]
            
            orb_signal = detector.detect_breakout(today_bars)
            
            results.append({
                "ticker": ticker,
                "last_price": float(bars[-1]["Close"]),
                "forecast_trend": forecast.get("algorithm", "Error"),
                "confidence": forecast.get("confidence", 0.0),
                "orb_signal": orb_signal.get("signal", "WAIT")
            })

        # Final Report
        print("\n" + "="*80)
        print("📈 PHASE 32 BASELINE PERFORMANCE SCORE")
        print("="*80)
        print(f"{'TICKER':<10} | {'PRICE':<10} | {'MODEL':<20} | {'CONF':<8} | {'ORB'}")
        print("-" * 80)
        for r in results:
            print(f"{r['ticker']:<10} | £{r['last_price']:<9.2f} | {r['forecast_trend']:<20} | {r['confidence']:<8.2f} | {r['orb_signal']}")
        print("="*80)

if __name__ == "__main__":
    sys.path.append(os.path.join(os.getcwd(), "backend"))
    asyncio.run(run_baseline())
