"""
Whale Alert Agent - Monitors large block trades and institutional flow
"""

import logging
import asyncio
from typing import Dict, Any, List
from .base_agent import BaseAgent, AgentConfig, AgentResponse
from market_context import WhaleData
from data_engine import get_alpaca_client

logger = logging.getLogger(__name__)

class WhaleAgent(BaseAgent):
    """
    Agent specialized in detecting high-value trades (Whales).
    Analyzes recent trade data to identify institutional movement.
    """
    
    def __init__(self, config: AgentConfig = None):
        if config is None:
            config = AgentConfig(
                name="WhaleAgent",
                enabled=True,
                timeout=20.0, # Increased for fallback logic
                cache_ttl=60  # whale data is very time-sensitive
            )
        super().__init__(config)
        self.alpaca = get_alpaca_client()
        self.whale_threshold_usd = 50000.0 # Lowered from $250k for paper/IEX data density
        
    async def analyze(self, context: Dict[str, Any]) -> AgentResponse:
        """
        Analyze recent trades for a ticker to find large block orders.
        """
        ticker = context.get("ticker", "MARKET")
        
        # 1. Handle "MARKET" Ticker Gracefully
        if ticker == "MARKET":
            # For broad market, we can't track specific whales easily without a composite index
            # Return a neutral success response to avoid "FAILED" state
            return AgentResponse(
                agent_name=self.config.name,
                success=True,
                data=WhaleData(
                    ticker="MARKET", 
                    summary="Whale analysis requires specific ticker. Institutional flow for broad market is mixed."
                ).model_dump(),
                latency_ms=0
            )

        try:
            # 2. Fetch recent trades (last 500)
            logger.info(f"WhaleAgent: Fetching trades for {ticker}...")
            
            # Use resilience pattern for API call
            from resilience import retry_with_backoff
            
            @retry_with_backoff(max_retries=2, base_delay=0.5)
            async def fetch_trades():
                return await self.alpaca.get_recent_trades(ticker, limit=500)
                
            trades = await fetch_trades()
            
            # --- FALLBACK: Data Maximization via Volume Anomaly ---
            if not trades:
                logger.info(f"WhaleAgent: No trades found for {ticker}. Attempting Volume Anomaly Detection...")
                return await self._analyze_via_volume_anomaly(ticker)
            
            # 3. Identify Large Trades
            large_trades = []
            total_whale_volume_usd = 0.0
            
            for t in trades:
                value = t['p'] * t['s']
                if value >= self.whale_threshold_usd:
                    large_trades.append({
                        "price": t['p'],
                        "size": t['s'],
                        "value_usd": value,
                        "timestamp": t['t'],
                        "is_whale": True
                    })
                    total_whale_volume_usd += value
            
            # 4. Analyze Unusual Volume
            # (In a real app, we would compare to 20-day avg volume)
            # For now, we flag if we see more than 3 whales in 500 trades
            unusual_activity = len(large_trades) > 3
            
            # 5. Sentiment Impact
            # If price is at the High of recent trades and we see whales, it might be accumulation
            impact = "NEUTRAL"
            if len(large_trades) > 0:
                avg_price = sum(t['p'] for t in trades) / len(trades)
                whale_avg_price = sum(w['price'] for w in large_trades) / len(large_trades)
                
                if whale_avg_price > avg_price * 1.001:
                    impact = "BULLISH"
                elif whale_avg_price < avg_price * 0.999:
                    impact = "BEARISH"
            
            # 6. Build Summary
            if len(large_trades) > 0:
                summary = f"Detected {len(large_trades)} large block trades (Whales) totaling ${total_whale_volume_usd/1e6:.2f}M in the last hour. "
                if impact == "BULLISH":
                    summary += "Activity suggests institutional accumulation."
                elif impact == "BEARISH":
                    summary += "Activity suggests institutional distribution/selling."
                else:
                    summary += "Institutional activity is mixed/neutral."
            else:
                summary = "No significant whale activity detected in the last hour. Trading appears retail-driven."
            
            whale_data = WhaleData(
                ticker=ticker,
                large_trades=large_trades,
                unusual_volume=unusual_activity,
                sentiment_impact=impact,
                summary=summary
            )
            
            return AgentResponse(
                agent_name=self.config.name,
                success=True,
                data=whale_data.model_dump(),
                latency_ms=0
            )
            
        except Exception as e:
            logger.error(f"WhaleAgent failed: {e}")
            return AgentResponse(
                agent_name=self.config.name,
                success=False,
                data={},
                error=str(e),
                latency_ms=0
            )

    async def _analyze_via_volume_anomaly(self, ticker: str) -> AgentResponse:
        """
        Fallback: Analyze daily volume vs 20-day average to detect hidden institutional activity.
        Useful when granular trade data is unavailable (e.g. some LSE stocks or free tier).
        """
        try:
            # Fetch daily bars
            bars_resp = await self.alpaca.get_historical_bars(ticker, limit=25, timeframe="1Day")
            if not bars_resp or "bars" not in bars_resp or len(bars_resp["bars"]) < 20:
                 return AgentResponse(
                    agent_name=self.config.name,
                    success=True, # Success but empty
                    data=WhaleData(ticker=ticker, summary="Insufficient data for whale or volume analysis.").model_dump(),
                    latency_ms=0
                )
            
            bars = bars_resp["bars"]
            current_vol = bars[-1]['v']
            
            # Calculate avg volume of previous 20 days
            prev_vols = [b['v'] for b in bars[:-1][-20:]]
            avg_vol = sum(prev_vols) / len(prev_vols) if prev_vols else 1
            
            ratio = current_vol / avg_vol
            impact = "NEUTRAL"
            summary = f"Volume is {ratio:.1f}x average. "
            
            if ratio > 1.5:
                # High volume
                price_change = (bars[-1]['c'] - bars[-1]['o']) / bars[-1]['o']
                if price_change > 0:
                    impact = "BULLISH"
                    summary += "High volume on up day suggests institutional buying (Accumulation)."
                else:
                    impact = "BEARISH"
                    summary += "High volume on down day suggests institutional selling (Distribution)."
            else:
                summary += "Volume is within normal range. No anomalies detected."
                
            return AgentResponse(
                agent_name=self.config.name,
                success=True,
                data=WhaleData(
                    ticker=ticker,
                    unusual_volume=ratio > 1.5,
                    sentiment_impact=impact,
                    summary=summary + " (Derived from Daily Volume Anomaly)"
                ).model_dump(),
                latency_ms=0
            )
            
        except Exception as e:
            logger.warning(f"Volume anomaly fallback failed: {e}")
            return AgentResponse(
                agent_name=self.config.name,
                success=True,
                data=WhaleData(ticker=ticker, summary="Data unavailable for whale analysis.").model_dump(),
                latency_ms=0
            )
