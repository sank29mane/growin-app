import logging
from typing import Optional, Dict
from market_context import WhaleData
from agents.base_agent import AgentResponse

logger = logging.getLogger(__name__)

async def analyze_via_volume_anomaly(ticker: str, agent_name: str, alpaca_client, pre_fetched_bars: Optional[Dict] = None) -> AgentResponse:
    """
    Fallback: Analyze daily volume vs 20-day average to detect hidden institutional activity.
    Useful when granular trade data is unavailable (e.g. some LSE stocks or free tier).
    """
    try:
        from utils.financial_math import create_decimal
        # Fetch daily bars if not pre-fetched
        bars_resp = pre_fetched_bars
        if not bars_resp:
            bars_resp = await alpaca_client.get_historical_bars(ticker, limit=25, timeframe="1Day")
        if not bars_resp or "bars" not in bars_resp or len(bars_resp["bars"]) < 20:
            return AgentResponse(
                agent_name=agent_name,
                success=True,  # Success but empty
                data=WhaleData(
                    ticker=ticker,
                    summary="Insufficient data for whale or volume analysis.",
                ).model_dump(),
                latency_ms=0,
            )
        bars = bars_resp["bars"]

        # Only pre-parse the elements we actually need (last 20 + last 1)
        last_21_bars = bars[-21:] if len(bars) >= 21 else bars
        prev_vols = [create_decimal(b['v']) for b in last_21_bars[:-1]] if len(last_21_bars) > 1 else []
        last_bar = bars[-1]
        current_vol = create_decimal(last_bar['v'])

        # Calculate avg volume of previous 20 days
        avg_vol = sum(prev_vols) / len(prev_vols) if prev_vols else create_decimal(1)

        ratio = float(current_vol / avg_vol)
        impact = "NEUTRAL"
        summary = f"Volume is {ratio:.1f}x average. "

        if ratio > 1.5:
            # High volume
            c = create_decimal(last_bar['c'])
            o = create_decimal(last_bar['o'])
            price_change = float((c - o) / o) if o > create_decimal(0) else 0.0
            if price_change > 0:
                impact = "BULLISH"
                summary += "High volume on up day suggests institutional buying (Accumulation)."
            else:
                impact = "BEARISH"
                summary += "High volume on down day suggests institutional selling (Distribution)."
        else:
            summary += "Volume is within normal range. No anomalies detected."

        return AgentResponse(
            agent_name=agent_name,
            success=True,
            data=WhaleData(
                ticker=ticker,
                unusual_volume=ratio > 1.5,
                sentiment_impact=impact,
                summary=summary + " (Derived from Daily Volume Anomaly)",
            ).model_dump(),
            latency_ms=0,
        )

    except Exception as e:
        logger.warning(f"Volume anomaly fallback failed: {e}")
        return AgentResponse(
            agent_name=agent_name,
            success=True,
            data=WhaleData(
                ticker=ticker, summary="Data unavailable for whale analysis."
            ).model_dump(),
            latency_ms=0,
        )
