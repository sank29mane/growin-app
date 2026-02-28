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
        
        # 1. Handle "MARKET" Ticker with Bellwether Aggregation
        if ticker == "MARKET":
            bellwethers = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN"]
            logger.info(f"WhaleAgent: Performing Bellwether Aggregation for broad market...")
            
            # Use parallel execution for speed
            tasks = [self.analyze({"ticker": b}) for b in bellwethers]
            results = await asyncio.gather(*tasks)
            
            valid_results = [r for r in results if r.success and r.data.get("sentiment_impact")]
            if not valid_results:
                return AgentResponse(
                    agent_name=self.config.name,
                    success=True,
                    data=WhaleData(ticker="MARKET", summary="Institutional flow data unavailable for market bellwethers.").model_dump(),
                    latency_ms=0
                )
            
            # Aggregate sentiment
            bullish_count = sum(1 for r in valid_results if r.data["sentiment_impact"] == "BULLISH")
            bearish_count = sum(1 for r in valid_results if r.data["sentiment_impact"] == "BEARISH")
            
            market_impact = "NEUTRAL"
            if bullish_count > bearish_count: market_impact = "BULLISH"
            elif bearish_count > bullish_count: market_impact = "BEARISH"
            
            summary = f"Broad Market Whale Index: {market_impact}. (Aggregated from {len(valid_results)} bellwethers: {bullish_count} Bullish, {bearish_count} Bearish)."
            
            return AgentResponse(
                agent_name=self.config.name,
                success=True,
                data=WhaleData(
                    ticker="MARKET", 
                    sentiment_impact=market_impact,
                    summary=summary
                ).model_dump(),
                latency_ms=0
            )

        try:
            # SOTA 2026: Institutional Alpha (13F Filings)
            institutional_holdings = await self._fetch_institutional_holdings(ticker)
            
            from utils.financial_math import create_decimal, safe_div
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
            total_whale_volume_usd = create_decimal(0)
            
            for t in trades:
                p = create_decimal(t['p'])
                s = create_decimal(t['s'])
                value = p * s
                if value >= create_decimal(self.whale_threshold_usd):
                    large_trades.append({
                        "price": float(p),
                        "size": float(s),
                        "value_usd": float(value),
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
                avg_price = sum(create_decimal(t['p']) for t in trades) / len(trades)
                whale_avg_price = sum(create_decimal(w['price']) for w in large_trades) / len(large_trades)
                
                if whale_avg_price > avg_price * create_decimal(1.001):
                    impact = "BULLISH"
                elif whale_avg_price < avg_price * create_decimal(0.999):
                    impact = "BEARISH"
            
            # 6. Build Summary
            if len(large_trades) > 0:
                summary = f"Detected {len(large_trades)} large block trades (Whales) totaling ${float(total_whale_volume_usd)/1e6:.2f}M in the last hour. "
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
                institutional_holdings=institutional_holdings,
                unusual_volume=unusual_activity,
                sentiment_impact=impact,
                summary=summary
            )
            
            # SOTA 2026: Intelligent Signal Broadcast
            if impact in ["BULLISH", "BEARISH"] and len(large_trades) >= 2:
                from .messenger import AgentMessage, get_messenger
                from app_logging import correlation_id_ctx
                
                asyncio.create_task(get_messenger().send_message(AgentMessage(
                    sender=self.config.name,
                    recipient="broadcast",
                    subject="whale_signal",
                    payload={
                        "ticker": ticker,
                        "impact": impact,
                        "total_value_usd": float(total_whale_volume_usd),
                        "count": len(large_trades)
                    },
                    correlation_id=correlation_id_ctx.get()
                )))
            
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

    async def _fetch_institutional_holdings(self, ticker: str) -> List[Dict]:
        """Fetch institutional holdings (13F) data for a ticker using Tavily/Search."""
        try:
            # We use Tavily here as a robust way to find recent 13F filings summarized on sites like Fintel or WhaleWisdom
            # This is more resilient than direct EDGAR scraping for a prototype
            from tavily import TavilyClient
            import os
            
            tavily_key = os.getenv("TAVILY_API_KEY")
            if not tavily_key:
                return []
                
            tavily = TavilyClient(api_key=tavily_key)
            query = f"top institutional holders and 13F filing summary for {ticker} 2025 2026"
            
            def fetch():
                return tavily.search(query=query, search_depth="advanced", max_results=5)
            
            response = await asyncio.to_thread(fetch)
            results = response.get('results', [])
            logger.info(f"WhaleAgent: Search returned {len(results)} results")
            
            # Simple heuristic to extract holder info from search snippets
            holders = []
            for r in results:
                content = (r.get('title', '') + " " + (r.get('content', '') or r.get('snippet', ''))).lower()
                logger.debug(f"WhaleAgent: Analyzing content: {content[:100]}...")
                # Look for common institutional names (expanded for SOTA coverage)
                institutions = [
                    "vanguard", "blackrock", "state street", "fidelity", "geode", 
                    "morgan stanley", "jpmorgan", "bank of america", "goldman sachs", 
                    "northern trust", "norges bank", "t. rowe price"
                ]
                for inst in institutions:
                    if inst in content and inst.capitalize() not in [h['name'] for h in holders]:
                        holders.append({
                            "name": inst.capitalize() if inst != "jpmorgan" else "JPMorgan",
                            "type": "Institutional",
                            "source": r.get('url')
                        })
            
            if holders:
                logger.info(f"WhaleAgent: Identified {len(holders)} major institutional holders for {ticker}")
            
            return holders[:5]
        except Exception as e:
            logger.warning(f"13F fetch failed: {e}")
            return []

    async def _analyze_via_volume_anomaly(self, ticker: str) -> AgentResponse:
        """
        Fallback: Analyze daily volume vs 20-day average to detect hidden institutional activity.
        Useful when granular trade data is unavailable (e.g. some LSE stocks or free tier).
        """
        try:
            from utils.financial_math import create_decimal
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
            current_vol = create_decimal(bars[-1]['v'])
            
            # Calculate avg volume of previous 20 days
            prev_vols = [create_decimal(b['v']) for b in bars[:-1][-20:]]
            avg_vol = sum(prev_vols) / len(prev_vols) if prev_vols else create_decimal(1)
            
            ratio = float(current_vol / avg_vol)
            impact = "NEUTRAL"
            summary = f"Volume is {ratio:.1f}x average. "
            
            if ratio > 1.5:
                # High volume
                c = create_decimal(bars[-1]['c'])
                o = create_decimal(bars[-1]['o'])
                price_change = float((c - o) / o) if o > 0 else 0.0
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
