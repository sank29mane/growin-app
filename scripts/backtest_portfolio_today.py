import os
import asyncio
import logging
import numpy as np
import json
from datetime import datetime, timezone
from typing import List, Dict, Any

# Root relative imports
from backend.mcp_client import Trading212MCPClient
from backend.utils.orb_detector import ORBDetector
from backend.utils.jmce_model import get_jmce_model, TimeResolution

# Setup minimal logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("PortfolioScan")

async def run_portfolio_scan():
    logger.info("🚀 Initializing SOTA 2026 Portfolio Intraday Scan")
    
    # 1. Initialize Clients
    mcp = Trading212MCPClient()
    detector = ORBDetector(range_minutes=30)
    
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
        # 2. Get Portfolio Positions
        logger.info("📊 Retrieving live portfolio positions (Invest + ISA)...")
        portfolio_result = await mcp.call_tool("analyze_portfolio", {"account_type": "all"})
        
        if not portfolio_result.content or not portfolio_result.content[0].text:
            logger.error("❌ Failed to retrieve portfolio.")
            return
            
        portfolio_data = json.loads(portfolio_result.content[0].text)
        positions = portfolio_data.get("positions", [])
        
        if not positions:
            logger.warning("⚠️ Portfolio is empty.")
            return
            
        # Group by account
        report = []
        unique_tickers = list(set([p['ticker'] for p in positions if p.get('ticker')]))
        logger.info(f"✅ Found {len(unique_tickers)} unique tickers across accounts.")
        
        # Create a mapping of ticker to account(s)
        ticker_accounts = {}
        for p in positions:
            t = p['ticker']
            acc = p.get('account_type', 'unknown').upper()
            if t not in ticker_accounts: ticker_accounts[t] = []
            if acc not in ticker_accounts[t]: ticker_accounts[t].append(acc)

        # 3. Scan Each Ticker
        for ticker in unique_tickers:
            try:
                # SOTA 2026: Normalize ticker before fetching history
                from backend.utils.ticker_utils import normalize_ticker
                norm_ticker = normalize_ticker(ticker)
                
                # Fetch history
                history_result = await mcp.call_tool("get_price_history", {
                    "ticker": norm_ticker,
                    "interval": "5m",
                    "period": "1d"
                })
                
                if not history_result.content or not history_result.content[0].text:
                    continue
                    
                history_data = json.loads(history_result.content[0].text)
                bars = history_data.get("price_data", [])
                
                if not bars:
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
                except Exception:
                    pass

                # ORB Signal
                signal_data = detector.detect_breakout(bars, covariance_velocity=shift_metric)
                
                report.append({
                    "ticker": norm_ticker,
                    "original": ticker,
                    "accounts": ", ".join(ticker_accounts.get(ticker, [])),
                    "price": signal_data['current_price'],
                    "signal": signal_data['signal'],
                    "shift": shift_metric,
                    "status": "WAIT" if signal_data['signal'] == "WAIT" else "ACTION"
                })
                
            except Exception as e:
                logger.warning(f"⚠️ Failed to scan {ticker}: {e}")

        # 4. Present Consolidated Report
        print("\n" + "="*80)
        print(f"📈 MULTI-ACCOUNT INTRADAY SCAN - {datetime.now().strftime('%H:%M:%S')}")
        print("="*80)
        print(f"{'TICKER':<10} | {'ACCOUNT':<12} | {'PRICE':<10} | {'SHIFT':<8} | {'SIGNAL':<18}")
        print("-"*80)
        
        for item in sorted(report, key=lambda x: x['shift'], reverse=True):
            price_str = f"£{float(item['price']):.2f}"
            shift_str = f"{item['shift']:.2f}"
            signal_str = item['signal']
            acc_str = item['accounts']
            
            # Highlight high shift or breakouts
            if item['shift'] > 1.5 or "BREAKOUT" in signal_str:
                print(f"🔥 {item['ticker']:<8} | {acc_str:<12} | {price_str:<10} | {shift_str:<8} | {signal_str:<18}")
            else:
                print(f"  {item['ticker']:<8} | {acc_str:<12} | {price_str:<10} | {shift_str:<8} | {signal_str:<18}")
        
        print("="*80)
        print("🔥 = High Volatility or Breakout detected.")

if __name__ == "__main__":
    asyncio.run(run_portfolio_scan())
