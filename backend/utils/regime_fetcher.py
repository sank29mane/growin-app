import yfinance as yf
import asyncio
from typing import Dict, Optional
from app_logging import setup_logging

logger = setup_logging("regime_fetcher")

class RegimeFetcher:
    """
    Fetches macro-regime signals (VIX, 10Y Yield) to inform portfolio optimization.
    Institutional-grade fetcher with error resilience.
    """
    
    def __init__(self):
        self.tickers = {
            "vix": "^VIX",
            "tnx": "^TNX",  # 10Y Treasury Yield
        }

    async def fetch_signals(self) -> Dict[str, Optional[float]]:
        """
        Fetches macro-regime signals (VIX, 10Y Yield).
        VIX is log-transformed and 20-day standardized for SOTA 2026 injection.
        """
        import numpy as np
        import pandas as pd
        signals = {}
        
        try:
            # 1. Fetch TNX (Latest only)
            tnx_ticker = self.tickers["tnx"]
            tnx_data = await asyncio.to_thread(lambda: yf.Ticker(tnx_ticker).history(period="1d"))
            if not tnx_data.empty:
                signals["tnx"] = float(tnx_data['Close'].iloc[-1])
            else:
                signals["tnx"] = 4.2 # 2026 Default

            # 2. Fetch VIX (30d for rolling Z-score)
            vix_ticker = self.tickers["vix"]
            vix_hist = await asyncio.to_thread(lambda: yf.Ticker(vix_ticker).history(period="1mo"))
            
            if not vix_hist.empty and len(vix_hist) >= 20:
                closes = vix_hist['Close'].astype(float)
                # SOTA 2026 Transform: log -> rolling z-score
                log_vix = np.log(closes)
                
                # Use last 20 days for Z-score
                window = log_vix.tail(20)
                mean = window.mean()
                std = window.std()
                
                current_log = log_vix.iloc[-1]
                z_score = (current_log - mean) / (std if std > 1e-6 else 1.0)
                
                signals["vix"] = float(closes.iloc[-1]) # Raw for display
                signals["vix_zscore"] = float(z_score) # For TTM injection
                logger.info(f"VIX Regime: Raw={signals['vix']:.2f}, Z-Score={z_score:.4f}")
            else:
                logger.warning("Insufficient VIX history, using baseline")
                signals["vix"] = 18.5
                signals["vix_zscore"] = 0.0 # Neutral regime
                    
        except Exception as e:
            logger.error(f"Error fetching macro signals: {str(e)}", exc_info=True)
            signals["tnx"] = signals.get("tnx", 4.2)
            signals["vix"] = signals.get("vix", 18.5)
            signals["vix_zscore"] = signals.get("vix_zscore", 0.0)
                    
        return signals
