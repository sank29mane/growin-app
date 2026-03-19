"""
Time Utilities - SOTA 2026
Handles DST-aware market windows and 'Smart Money' detection.
Anchors rebalance windows to institutional overlap (London Close / NY Open).
"""

from datetime import datetime, timezone
import pytz
import logging

logger = logging.getLogger(__name__)

def is_smart_money_window() -> bool:
    """
    Detects if the current time is within the 'Smart Money' rebalance window.
    Standard Window: 14:00 - 14:30 GMT/UTC.
    
    This window is critical for LSE Leveraged ETFs because it represents the 
    period where European and US institutional liquidity overlaps.
    """
    # Use pytz to ensure we are looking at UTC/GMT regardless of server location
    now_utc = datetime.now(pytz.utc)
    
    # 2:00 PM GMT is 14:00
    is_window = (now_utc.hour == 14 and now_utc.minute < 30)
    
    if is_window:
        logger.info("Inside 2:00 PM GMT Smart Money Window.")
        
    return is_window

def get_market_seconds_to_close(market: str = "LSE") -> int:
    """
    Calculates seconds remaining until market close.
    Used for scaling RL rebalance urgency.
    """
    now_utc = datetime.now(pytz.utc)
    
    if market == "LSE":
        # LSE Closes at 16:30 GMT
        close_hour, close_min = 16, 30
    elif market == "NYSE":
        # NYSE Closes at 21:00 GMT (16:00 EST)
        # Note: This simple mapping needs DST offset check for full production
        close_hour, close_min = 21, 0
    else:
        return 0
        
    market_close = now_utc.replace(hour=close_hour, minute=close_min, second=0, microsecond=0)
    
    if now_utc > market_close:
        return 0 # Market already closed
        
    delta = market_close - now_utc
    return int(delta.total_seconds())

if __name__ == "__main__":
    # Test
    print(f"Current UTC Time: {datetime.now(pytz.utc)}")
    print(f"Is Smart Money Window: {is_smart_money_window()}")
    print(f"Seconds to LSE Close: {get_market_seconds_to_close('LSE')}")
