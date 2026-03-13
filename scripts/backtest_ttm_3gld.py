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

logging.basicConfig(level=logging.INFO, format='%(levelname)s:%(name)s:%(message)s')
logger = logging.getLogger("TTM-Backtest")

async def run_backtest():
    logger.info("🚀 Starting IBM TTM-R2 Blind Backtest for 3GLD.L")
    
    mcp = Trading212MCPClient()
    server_config = {
        "name": "T212-Data-Fetcher",
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
        # 1. Fetch history at 1H resolution to ensure enough context (TTM likes 512 points)
        logger.info("📊 Fetching historical data for 3GLD.L...")
        history_res = await mcp.call_tool("get_price_history", {
            "ticker": "3GLD.L",
            "interval": "1h",
            "period": "1y"
        })
        
        if not history_res.content or not history_res.content[0].text:
            logger.error(f"❌ Failed to fetch history. Result: {history_res}")
            return
            
        logger.info(f"Raw response length: {len(history_res.content[0].text)}")
        try:
            history_data = json.loads(history_res.content[0].text)
        except Exception as e:
            logger.error(f"❌ JSON Decode Error: {e}")
            logger.error(f"First 100 chars: {history_res.content[0].text[:100]}")
            return
        all_bars = history_data.get("price_data", [])
        
        if not all_bars:
            logger.error("❌ No price data returned")
            return

        # 2. Slice data: Everything BEFORE today (2026-03-12)
        # We assume today's date is March 12, 2026 based on session context
        today_start = datetime(2026, 3, 12, 0, 0, 0, tzinfo=timezone.utc)
        
        # Convert bars to objects with datetime
        df = pd.DataFrame(all_bars)
        df['Date'] = pd.to_datetime(df['Date'], utc=True)
        
        past_data_df = df[df['Date'] < today_start]
        actual_today_df = df[df['Date'] >= today_start]
        
        if past_data_df.empty:
            logger.error("❌ No historical data found before today")
            return
            
        if actual_today_df.empty:
            logger.warning("⚠️ No actual data found for today yet. Verification will be limited to trajectory shape.")
        
        logger.info(f"✅ Isolated {len(past_data_df)} bars of context data (ending {past_data_df.iloc[-1]['Date']})")
        logger.info(f"✅ Isolated {len(actual_today_df)} bars of actual today's data for verification.")

        # 3. Format context for TTM (it expects 'c', 'h', 'l', 'o', 'v', 't')
        context_data = []
        for _, row in past_data_df.iterrows():
            context_data.append({
                "c": float(row["Close"]),
                "h": float(row["High"]),
                "l": float(row["Low"]),
                "o": float(row["Open"]),
                "v": float(row["Volume"]),
                "t": int(row["Date"].timestamp() * 1000)
            })

        # 4. Invoke TTM Forecaster
        forecaster = get_forecaster()
        logger.info("🔮 Invoking IBM Granite TTM-R2 for blind prediction...")
        
        # Predict 78 steps (approx one LSE trading day at 5m resolution)
        prediction = await forecaster.forecast(context_data, prediction_steps=78, timeframe="5Min")
        
        if "error" in prediction:
            logger.error(f"❌ Forecast failed: {prediction['error']}")
            return

        forecast_bars = prediction.get("forecast", [])
        model_used = prediction.get("model_used", "Unknown")
        
        # 5. Correlation & Accuracy Analysis
        print("\n" + "="*90)
        print(f"📊 BLIND BACKTEST REPORT: 3GLD.L | MODEL: {model_used}")
        print("="*90)
        
        last_price = float(past_data_df.iloc[-1]["Close"])
        print(f"Yesterday's Close:  £{last_price:.2f}")
        
        if forecast_bars:
            pred_start = forecast_bars[0]["close"]
            pred_end = forecast_bars[-1]["close"]
            pred_change = ((pred_end - last_price) / last_price) * 100
            print(f"Predicted Trajectory: £{pred_start:.2f} -> £{pred_end:.2f} ({pred_change:+.2f}%)")
            
            if not actual_today_df.empty:
                act_start = float(actual_today_df.iloc[0]["Close"])
                act_end = float(actual_today_df.iloc[-1]["Close"])
                act_change = ((act_end - last_price) / last_price) * 100
                
                error = abs(pred_end - act_end)
                accuracy_pct = 100 - (error / act_end * 100)
                
                print(f"Actual Trajectory:    £{act_start:.2f} -> £{act_end:.2f} ({act_change:+.2f}%)")
                print("-" * 90)
                print(f"🎯 DIRECTIONAL MATCH: {'✅ YES' if np.sign(pred_change) == np.sign(act_change) else '❌ NO'}")
                print(f"📈 PRICE ACCURACY:    {accuracy_pct:.2f}%")
                print(f"📉 ABSOLUTE ERROR:    £{error:.4f}")
            else:
                print("-" * 90)
                print("⚠️  Actual data for today is unavailable for comparison.")
        else:
            print("❌ Model returned no forecast bars.")
            
        print("="*90)

if __name__ == "__main__":
    sys.path.append(os.path.join(os.getcwd(), "backend"))
    asyncio.run(run_backtest())
