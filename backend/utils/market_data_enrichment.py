import logging
from typing import Dict, List, Optional, Any
from utils.financial_math import create_decimal

logger = logging.getLogger(__name__)

class MarketDataEnrichmentService:
    @staticmethod
    def analyze_volume_anomaly(ticker: str, bars: List[Dict[str, Any]], window: int = 20, threshold: float = 1.5) -> Dict[str, Any]:
        """
        Analyze daily volume vs moving average to detect hidden institutional activity.
        Useful when granular trade data is unavailable.
        """
        if not bars or len(bars) < window:
            return {
                "ticker": ticker,
                "summary": "Insufficient data for volume analysis.",
                "status": "INSUFFICIENT_DATA"
            }

        last_n_bars = bars[-(window + 1):] if len(bars) > window else bars
        prev_vols = [create_decimal(b['v']) for b in last_n_bars[:-1]]
        last_bar = last_n_bars[-1]
        current_vol = create_decimal(last_bar['v'])

        avg_vol = sum(prev_vols) / len(prev_vols) if prev_vols else create_decimal(1)
        ratio = float(current_vol / avg_vol)
        impact = "NEUTRAL"
        summary = f"Volume is {ratio:.1f}x average. "

        if ratio > threshold:
            c = create_decimal(last_bar['c'])
            o = create_decimal(last_bar['o'])
            price_change = float((c - o) / o) if o > create_decimal(0) else 0.0

            if price_change > 0:
                impact = "BULLISH"
                summary += f"Strong buying pressure detected (+{price_change*100:.1f}%)."
            else:
                impact = "BEARISH"
                summary += f"Strong selling pressure detected ({price_change*100:.1f}%)."
        else:
            summary += "No significant anomalies."

        return {
            "ticker": ticker,
            "summary": summary,
            "impact": impact,
            "ratio": ratio,
            "status": "SUCCESS"
        }
