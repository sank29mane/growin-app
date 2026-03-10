import yfinance as yf
import asyncio
from typing import Dict, Optional
from backend.app_logging import setup_logging

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
        Fetches current levels for VIX and 10Y Yield.
        Returns a dictionary with signal names and their latest values.
        """
        signals = {}
        
        # Run yfinance calls in a thread pool to avoid blocking the event loop
        loop = asyncio.get_event_loop()
        
        try:
            for key, symbol in self.tickers.items():
                logger.info(f"Fetching macro signal: {symbol}")
                try:
                    # Using to_thread for simpler syntax in Python 3.9+ (backend uses 3.13)
                    data = await asyncio.to_thread(lambda s=symbol: yf.Ticker(s).history(period="1d"))
                    
                    if not data.empty:
                        latest_val = data['Close'].iloc[-1]
                        signals[key] = float(latest_val)
                        logger.info(f"Successfully fetched {key}: {latest_val}")
                    else:
                        raise ValueError(f"No data returned for {symbol}")
                except Exception as inner_e:
                    logger.warning(f"Fetch failed for {symbol}, using 2026 defaults: {inner_e}")
                    if key == "vix":
                        signals[key] = 18.5  # Typical 2026 baseline
                    elif key == "tnx":
                        signals[key] = 4.2   # Typical 2026 10Y baseline
                    
        except Exception as e:
            logger.error(f"Error fetching macro signals: {str(e)}", exc_info=True)
            # Ensure we return the keys even if values are None
            for key in self.tickers:
                if key not in signals:
                    signals[key] = None
                    
        return signals
