import os
import asyncio
import logging
import numpy as np
import json
import sys
from datetime import datetime, timezone
from typing import List, Dict, Any

# Root relative imports
from backend.mcp_client import Trading212MCPClient
from backend.utils.orb_detector import ORBDetector
from backend.utils.jmce_model import get_jmce_model, TimeResolution

# Setup minimal logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ISA-JMCE")

async def run_isa_jmce():
    logger.info("🚀 Initializing SOTA 2026 ISA-Specific Intraday Scan")
    
    mcp = Trading212MCPClient()
    detector = ORBDetector(range_minutes=30)
    
    # Configuration for T212 stdio server - Passing ALL keys for robust mapping
    server_config = {
        "name": "Trading 212 ISA",
        "type": "stdio",
        "command": sys.executable,
        "args": ["backend/trading212_mcp_server.py"],
        "env": {
            "TRADING212_API_KEY": os.getenv("TRADING212_API_KEY"),
            "TRADING212_API_KEY_INVEST": os.getenv("TRADING212_API_KEY_INVEST"),
            "TRADING212_API_KEY_ISA": os.getenv("TRADING212_API_KEY_ISA"),
            "PH_OPT_OUT": "1"
        }
    }
    
    async with mcp.connect_all([server_config]):
        # 1. Get ISA Positions
        logger.info("📊 Retrieving live ISA positions...")
        portfolio_result = await mcp.call_tool("analyze_portfolio", {"account_type": "isa"})
        
        if not portfolio_result.content or not portfolio_result.content[0].text:
            logger.error("❌ Failed to retrieve ISA portfolio.")
            return
            
        portfolio_data = json.loads(portfolio_result.content[0].text)
        positions = portfolio_data.get("positions", [])
        
        if not positions:
            logger.warning("⚠️ ISA Portfolio is empty.")
            return
            
        tickers = list(set([p['ticker'] for p in positions if p.get('ticker')]))
        logger.info(f"✅ Found {len(tickers)} ISA tickers: {tickers}")
        
        # 2. Scan Each ISA Ticker
        for ticker in tickers:
            logger.info(f"\n🔍 NeuralJMCE Analysis for {ticker} (ISA)...")
            try:
                # Fetch 5m history for today
                history_result = await mcp.call_tool("get_price_history", {
                    "ticker": ticker,
                    "interval": "5m",
                    "period": "1d"
                })
                
                if not history_result.content or not history_result.content[0].text:
                    continue
                    
                history_data = json.loads(history_result.content[0].text)
                bars = history_data.get("price_data", [])
                
                if not bars:
                    logger.warning(f"⚠️ No intraday bars for {ticker}")
                    continue
                
                # Neural JMCE Velocity (GPU)
                shift_metric = 0.0
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
                except Exception as e:
                    logger.warning(f"JMCE Head failed: {e}")

                # ORB Signal
                signal_data = detector.detect_breakout(bars, covariance_velocity=shift_metric)
                
                # Report
                print("\n" + "="*60)
                print(f"💎 ISA ASSET REPORT: {ticker}")
                print("="*60)
                
                if signal_data['signal'] != "WAIT":
                    print(f"Current Price:  £{float(signal_data['current_price']):.2f}")
                    print(f"Risk Regime:    {'🔥 HIGH VELOCITY' if shift_metric > 1.5 else 'NORMAL'}")
                    print(f"Shift Metric:   {shift_metric:.4f}")
                    print(f"ORB Signal:     {signal_data['signal']}")
                    print(f"Confidence:     {signal_data['confidence']:.2f}")
                    print(f"Range:          Low £{float(signal_data['or_low']):.2f} - High £{float(signal_data['or_high']):.2f}")
                else:
                    # SOTA: Basic info for WAIT status
                    print(f"ORB Signal:     {signal_data['signal']}")
                    print(f"Shift Metric:   {shift_metric:.4f}")
                    print(f"Status:         Establishing Opening Range ({len(bars)}/6 bars)")
                
                print("-"*60)
                if signal_data['signal'] != "WAIT" and "BREAKOUT" in signal_data['signal']:
                    print(f"✨ STRATEGIC VERDICT: {'ACCUMULATE' if 'BULLISH' in signal_data['signal'] else 'REDUCE'} - HIGH CONVICTION")
                else:
                    print("✨ STRATEGIC VERDICT: HOLD / MONITOR (Waiting for Range)")
                print("="*60)
                
            except Exception as e:
                logger.error(f"❌ Failed to scan ISA ticker {ticker}: {e}")

if __name__ == "__main__":
    asyncio.run(run_isa_jmce())
