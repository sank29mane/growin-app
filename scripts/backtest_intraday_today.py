import os
import asyncio
import logging
import numpy as np
import json
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from typing import List, Dict, Any

# Root relative imports
from backend.mcp_client import Trading212MCPClient
from backend.utils.orb_detector import ORBDetector
from backend.utils.jmce_model import get_jmce_model, TimeResolution

# Setup minimal logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("IntradayBacktest")

async def run_backtest():
    logger.info("🚀 Starting SOTA 2026 Intraday Backtest for Today (March 12, 2026)")
    
    # 1. Initialize T212 MCP Client
    mcp = Trading212MCPClient()
    
    # Configuration for T212 stdio server
    import sys
    server_config = {
        "name": "Trading 212",
        "type": "stdio",
        "command": sys.executable,
        "args": ["backend/trading212_mcp_server.py"],
        "env": {
            "TRADING212_API_KEY": os.getenv("TRADING212_API_KEY_INVEST"),
            "PH_OPT_OUT": "1"
        }
    }
    
    async with mcp.connect_all([server_config]):
        detector = ORBDetector(range_minutes=30)
        
        # 2. Define Tickers
        tickers = ["MAG5.L", "MAG7.L", "3GLD.L"]
        logger.info(f"📊 Fetching 5-minute bars via T212 MCP for {tickers}...")
        
        for ticker in tickers:
            logger.info(f"\n🔍 Analyzing {ticker}...")
            
            try:
                # Fetch history via MCP tool
                # Interval 5m, period 1d for today
                result = await mcp.call_tool("get_price_history", {
                    "ticker": ticker,
                    "interval": "5m",
                    "period": "1d"
                })
                
                # Parse result
                # T212 MCP returns TextContent, text is a JSON string
                if not result.content or not result.content[0].text:
                    logger.warning(f"⚠️ No data returned for {ticker}")
                    continue
                    
                history_data = json.loads(result.content[0].text)
                bars = history_data.get("price_data", [])
                
                if not bars:
                    logger.warning(f"⚠️ No price_data bars for {ticker}")
                    continue
                    
                logger.info(f"📈 Processing {len(bars)} 5-minute bars...")
                
                # 4. Neural JMCE Covariance Shift (GPU/NPU Path)
                try:
                    model = get_jmce_model(n_assets=1, use_ane=False, resolution=TimeResolution.INTRADAY_5MIN)
                    closes = np.array([float(b['Close']) for b in bars], dtype=np.float32)
                    rets = np.diff(np.log(closes + 1e-9))[:, np.newaxis]
                    
                    if len(rets) > 0:
                        import mlx.core as mx
                        x = mx.array(rets[np.newaxis, ...])
                        mu, L, V = model(x, return_velocity=True)
                        v_np = np.array(V[0])
                        shift_metric = float(np.linalg.norm(v_np))
                        logger.info(f"🧠 JMCE Covariance Shift: {shift_metric:.6f}")
                    else:
                        shift_metric = 0.0
                except Exception as e:
                    logger.warning(f"⚠️ JMCE analysis skipped: {e}")
                    shift_metric = 0.0

                # 5. ORB Signal Detection
                # Map T212 MCP keys (Open, High, Low, Close, Volume) to what detector expects
                signal_data = detector.detect_breakout(bars, covariance_velocity=shift_metric)
                
                logger.info(f"🚩 ORB Signal: {signal_data['signal']}")
                
                if signal_data['signal'] != "WAIT":
                    logger.info(f"💎 Signal Confidence: {signal_data['confidence']:.2f}")
                    logger.info(f"📊 Range: High {signal_data['or_high']} | Low {signal_data['or_low']}")
                    logger.info(f"🕒 Current Price: {signal_data['current_price']}")
                    
                    if "BREAKOUT" in signal_data['signal']:
                        action = "BUY (Aggressive)" if "BULLISH" in signal_data['signal'] else "SELL (Aggressive)"
                        logger.info(f"✨ VERDICT: {action} triggered by High-Velocity breakout.")
                    else:
                        logger.info("💤 VERDICT: Price within range. Monitoring.")
                else:
                    logger.info(f"💤 VERDICT: {signal_data.get('reason', 'Waiting for more data...')}")
                    
            except Exception as e:
                logger.error(f"❌ Failed to process {ticker}: {e}")

if __name__ == "__main__":
    asyncio.run(run_backtest())

if __name__ == "__main__":
    asyncio.run(run_backtest())
