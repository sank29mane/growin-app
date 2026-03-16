import numpy as np
import logging
from typing import Dict, List, Any, Optional, Tuple
from decimal import Decimal
from datetime import datetime, time
from utils.financial_math import create_decimal

logger = logging.getLogger(__name__)

class ORBDetector:
    """
    SOTA 2026 Phase 30: Opening Range Breakout (ORB) Detector.
    Specifically tuned for TQQQ/SQQQ high-velocity intraday trading.
    
    Logic:
    1. Define Opening Range (OR) from first 30 minutes (9:30 - 10:00 EST).
    2. Monitor for breakout of OR High (Bullish) or OR Low (Bearish).
    3. Requires Volume Confirmation (> 1.5x 20-period SMA).
    4. Integrates NeuralJMCE Covariance Velocity for early shift detection.
    """
    
    def __init__(self, range_minutes: int = 30):
        self.range_minutes = range_minutes
        
    def detect_breakout(
        self, 
        ohlcv_5m: List[Dict[str, Any]], 
        covariance_velocity: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        Analyzes 5-minute bars to detect ORB signals.
        
        Args:
            ohlcv_5m: List of 5-min OHLCV bars.
            covariance_velocity: Optional shift metric from NeuralJMCE.
            
        Returns:
            Dict containing signal details.
        """
        if len(ohlcv_5m) < (self.range_minutes // 5):
            return {"signal": "WAIT", "reason": "Insufficient data for Opening Range"}
            
        # SOTA: Resilient key resolution for different data providers (Alpaca vs yfinance vs T212)
        def get_val(bar, *keys):
            for k in keys:
                if k in bar: return bar[k]
            raise KeyError(f"None of {keys} found in bar {bar.keys()}")

        # 1. Extract Opening Range (First 6 bars for 30 min)
        bars_in_range = self.range_minutes // 5
        or_bars = ohlcv_5m[:bars_in_range]
        
        or_high = max(float(get_val(b, 'h', 'high', 'High')) for b in or_bars)
        or_low = min(float(get_val(b, 'l', 'low', 'Low')) for b in or_bars)
        
        # 2. Analyze Current State (Latest bar)
        current_bar = ohlcv_5m[-1]
        current_close = float(get_val(current_bar, 'c', 'close', 'Close'))
        current_volume = float(get_val(current_bar, 'v', 'volume', 'Volume'))
        
        # 3. Volume Confirmation
        # Calculate 20-period volume SMA
        volumes = [float(get_val(b, 'v', 'volume', 'Volume')) for b in ohlcv_5m]
        vol_sma = np.mean(volumes[-20:]) if len(volumes) >= 20 else np.mean(volumes)
        volume_confirmed = current_volume > (vol_sma * 1.5)
        
        # 4. Signal Generation
        signal = "WAIT"
        confidence = 0.5
        
        if current_close > or_high:
            signal = "BULLISH_BREAKOUT"
            confidence = 0.7 if volume_confirmed else 0.6
        elif current_close < or_low:
            signal = "BEARISH_BREAKOUT"
            confidence = 0.7 if volume_confirmed else 0.6
            
        # 5. Neural JMCE Overlay (Phase 30 SOTA)
        # If covariance velocity is high, it indicates a volatility regime shift
        if covariance_velocity and abs(covariance_velocity) > 0.8:
            confidence += 0.15
            logger.info(f"ORB: High Covariance Velocity detected ({covariance_velocity:.2f}). Boosting confidence.")

        return {
            "signal": signal,
            "confidence": min(confidence, 1.0),
            "or_high": create_decimal(or_high),
            "or_low": create_decimal(or_low),
            "current_price": create_decimal(current_close),
            "volume_confirmed": volume_confirmed,
            "timestamp": datetime.now().isoformat()
        }
