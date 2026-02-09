
import random
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any

def generate_synthetic_chart_data(ticker: str, timeframe: str = "1Day", limit: int = 100) -> List[Dict[str, Any]]:
    """
    Generates synthetic realistic-looking chart data for development/offline mode.
    Uses Random Walk with Drift.
    """
    
    # Base parameters based on ticker hash to be deterministic-ish
    seed_val = sum(ord(c) for c in ticker)
    random.seed(seed_val)
    
    base_price = 100.0
    if ticker in ["AAPL", "GOOGL", "AMZN"]: base_price = 150.0
    elif ticker in ["TSLA", "NVDA"]: base_price = 200.0
    elif ticker in ["MSFT"]: base_price = 300.0
    elif "BTC" in ticker: base_price = 40000.0
    
    volatility = 0.02 # 2% daily volatility
    drift = 0.0005 # Slight upward drift
    
    # Determine timeframe delta
    delta_map = {
        "1Min": timedelta(minutes=1),
        "5Min": timedelta(minutes=5),
        "15Min": timedelta(minutes=15),
        "1Hour": timedelta(hours=1),
        "1Day": timedelta(days=1),
        "1Week": timedelta(weeks=1),
        "1Month": timedelta(days=30),
    }
    dt = delta_map.get(timeframe, timedelta(days=1))
    
    # Start time
    end_time = datetime.now(timezone.utc)
    current_time = end_time - (dt * limit)
    
    data = []
    current_price = base_price
    
    for i in range(limit):
        # Random walk
        change_pct = random.gauss(drift, volatility)
        current_price = current_price * (1 + change_pct)
        
        # OHLV generation
        open_price = current_price * (1 - random.gauss(0, 0.005))
        close_price = current_price
        high_price = max(open_price, close_price) * (1 + abs(random.gauss(0, 0.01)))
        low_price = min(open_price, close_price) * (1 - abs(random.gauss(0, 0.01)))
        volume = int(random.lognormvariate(10, 1) * 1000)
        
        # Ensure timestamp format matches what the frontend expects (ISO string)
        timestamp_str = current_time.isoformat()
        if not timestamp_str.endswith("Z") and "+" not in timestamp_str:
            timestamp_str += "Z"
            
        data.append({
            "timestamp": timestamp_str,
            "open": round(open_price, 2),
            "high": round(high_price, 2),
            "low": round(low_price, 2),
            "close": round(close_price, 2),
            "volume": volume,
            # Add short keys for compatibility
            "o": round(open_price, 2),
            "h": round(high_price, 2),
            "l": round(low_price, 2),
            "c": round(close_price, 2),
            "v": volume
        })
        
        current_time += dt
        
    return data
