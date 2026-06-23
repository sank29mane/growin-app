"""
Whale Alert Agent - Monitors large block trades and institutional flow
"""

import logging
import asyncio
from typing import Dict, Any, List, Optional
from .base_agent import BaseAgent, AgentConfig, AgentResponse
from market_context import WhaleData
from data_engine import get_alpaca_client
from resilience import get_circuit_breaker, CircuitBreakerOpenError
from utils.http_client import agent_http_client

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
                timeout=20.0,  # Increased for fallback logic
                cache_ttl=60,  # whale data is very time-sensitive
            )
        super().__init__(config)
        self.alpaca = get_alpaca_client()
        self.whale_threshold_usd = 50000.0 # Lowered from $250k for paper/IEX data density
        self._tavily_cb = get_circuit_breaker("tavily_whale", failure_threshold=3, recovery_timeout=30.0)

    async def analyze(self, context: Dict[str, Any]) -> AgentResponse:
        """
        Analyze recent trades for a ticker to find large block orders.
        """
        ticker = context.get("ticker", "MARKET")

        # 1. Handle "MARKET" Ticker with Bellwether Aggregation
        if ticker == "MARKET":
            bellwethers = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN"]
            logger.info("WhaleAgent: Performing Bellwether Aggregation for broad market...")
            
            # Optimization: Fetch bars for all bellwethers in one batch to prevent N+1 queries during fallback.
            # Only do this if they end up needing volume anomaly (e.g., if trades fetch fails).
            batch_bars = await self.alpaca.get_batch_bars(bellwethers, timeframe="1Day", limit=25)

            # Use parallel execution for speed
            # Optimization: Skip institutional holdings for bellwethers as it's not used in aggregation
            tasks = [self.analyze({
                "ticker": b,
                "skip_holdings": True,
                "pre_fetched_bars": batch_bars.get(b)
            }) for b in bellwethers]
            results = await asyncio.gather(*tasks)

            valid_results = [
                r for r in results if r.success and r.data.get("sentiment_impact")
            ]
            if not valid_results:
                return AgentResponse(
                    agent_name=self.config.name,
                    success=True,
                    data=WhaleData(
                        ticker="MARKET",
                        summary="Institutional flow data unavailable for market bellwethers.",
                    ).model_dump(),
                    latency_ms=0,
                )

            # Aggregate sentiment
            bullish_count = sum(
                1 for r in valid_results if r.data["sentiment_impact"] == "BULLISH"
            )
            bearish_count = sum(
                1 for r in valid_results if r.data["sentiment_impact"] == "BEARISH"
            )

            market_impact = "NEUTRAL"
            if bullish_count > bearish_count:
                market_impact = "BULLISH"
            elif bearish_count > bullish_count:
                market_impact = "BEARISH"

            summary = f"Broad Market Whale Index: {market_impact}. (Aggregated from {len(valid_results)} bellwethers: {bullish_count} Bullish, {bearish_count} Bearish)."

            return AgentResponse(
                agent_name=self.config.name,
                success=True,
                data=WhaleData(
                    ticker="MARKET", sentiment_impact=market_impact, summary=summary
                ).model_dump(),
                latency_ms=0,
            )

        try:
            # SOTA 2026: Institutional Alpha (13F Filings)
            institutional_holdings = await self._fetch_institutional_holdings(ticker)

            from utils.financial_math import create_decimal

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
                from utils.volume_anomaly import analyze_via_volume_anomaly
                return await analyze_via_volume_anomaly(ticker, self.config.name, self.alpaca, context.get("pre_fetched_bars"))
            from utils.currency_utils import DataSourceNormalizer

            ticker_currency = DataSourceNormalizer.get_currency_for_ticker(ticker)
            currency_symbol = "£" if ticker_currency == "GBP" else "$"

            # Pre-parse trades to avoid repetitive create_decimal calls and dictionary overhead
            # Using tuples for memory and iteration efficiency
            decimal_trades = [
                (create_decimal(t['p']), create_decimal(t['s']))
                for t in trades
            ]

            # 3. Identify Large Trades
            large_trades = []
            large_decimal_prices = []
            total_whale_volume = create_decimal(0)
            whale_threshold = create_decimal(self.whale_threshold_usd)
            
            for i, (p, s) in enumerate(decimal_trades):
                value = p * s
                if value >= whale_threshold:
                    t = trades[i]
                    large_trades.append({
                        "price": float(p),
                        "size": float(s),
                        "value_usd": float(value), # Frontend property is valueUsd but mapping handles it
                        "timestamp": str(t['t']),
                        "currency": ticker_currency,
                        "is_whale": True,
                        "_dec_price": p # Keep decimal precision for average calc
                    })
                    large_decimal_prices.append(p)
                    total_whale_volume += value

            # 4. Analyze Unusual Volume
            # (In a real app, we would compare to 20-day avg volume)
            # For now, we flag if we see more than 3 whales in 500 trades
            unusual_activity = len(large_trades) > 3

            # 5. Sentiment Impact
            # If price is at the High of recent trades and we see whales, it might be accumulation
            impact = "NEUTRAL"
            if len(large_trades) > 0 and len(decimal_trades) > 0 and len(large_decimal_prices) > 0:
                avg_price = sum(p for p, _ in decimal_trades) / len(decimal_trades)
                whale_avg_price = sum(large_decimal_prices) / len(large_decimal_prices)
                
                if whale_avg_price > avg_price * create_decimal(1.001):
                    impact = "BULLISH"
                elif whale_avg_price < avg_price * create_decimal(0.999):
                    impact = "BEARISH"

            # Clean up internal keys for frontend response
            for w in large_trades:
                w.pop('_dec_price', None)
            
            # 6. Build Summary
            if len(large_trades) > 0:
                summary = f"Detected {len(large_trades)} large block trades (Whales) totaling {currency_symbol}{float(total_whale_volume) / 1e6:.2f}M in the last hour. "
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
                summary=summary,
            )

            # SOTA 2026: Intelligent Signal Broadcast
            if impact in ["BULLISH", "BEARISH"] and len(large_trades) >= 2:
                from .messenger import AgentMessage, get_messenger
                from app_logging import correlation_id_ctx

                asyncio.create_task(
                    get_messenger().send_message(
                        AgentMessage(
                            sender=self.config.name,
                            recipient="broadcast",
                            subject="whale_signal",
                            payload={
                                "ticker": ticker,
                                "impact": impact,
                                "total_value": float(total_whale_volume),
                                "currency": ticker_currency,
                                "count": len(large_trades),
                            },
                            correlation_id=correlation_id_ctx.get(),
                        )
                    )
                )

            return AgentResponse(
                agent_name=self.config.name,
                success=True,
                data=whale_data.model_dump(),
                latency_ms=0,
            )

        except Exception as e:
            logger.error(f"WhaleAgent failed: {e}")
            return AgentResponse(
                agent_name=self.config.name,
                success=False,
                data={},
                error=str(e),
                latency_ms=0,
            )

    async def _fetch_institutional_holdings(self, ticker: str) -> List[Dict]:
        """Fetch institutional holdings (13F) data for a ticker using Search Plugin."""
        try:
            # We use Search Plugin here as a robust way to find recent 13F filings summarized on sites like Fintel or WhaleWisdom
            # This is more resilient than direct EDGAR scraping for a prototype
            from utils.search_provider import get_search_plugin
            search_plugin = get_search_plugin()

            if not search_plugin:
                return []
                
            query = f"top institutional holders and 13F filing summary for {ticker} 2025 2026"
            
            search_results = await search_plugin.search(
                query=query, search_depth="advanced", max_results=5
            )

            results = [{"title": r.title, "content": r.content, "snippet": r.content} for r in search_results]
            logger.info(f"WhaleAgent: Search returned {len(results)} results")

            # Simple heuristic to extract holder info from search snippets
            holders = []
            seen_holder_names = set()
            for r in results:
                title = r.get("title", "")
                body = r.get("content", "") or r.get("snippet", "")
                content = f"{title} {body}".lower()
                logger.debug(f"WhaleAgent: Analyzing content: {content[:100]}...")
                # Look for common institutional names (expanded for SOTA coverage)
                institutions = [
                    "vanguard",
                    "blackrock",
                    "state street",
                    "fidelity",
                    "geode",
                    "morgan stanley",
                    "jpmorgan",
                    "bank of america",
                    "goldman sachs",
                    "northern trust",
                    "norges bank",
                    "t. rowe price",
                ]
                for inst in institutions:
                    inst_name = inst.capitalize() if inst != "jpmorgan" else "JPMorgan"
                    if inst in content and inst_name not in seen_holder_names:
                        seen_holder_names.add(inst_name)
                        holders.append(
                            {
                                "name": inst_name,
                                "type": "Institutional",
                                "source": r.get("url"),
                            }
                        )

            if holders:
                logger.info(
                    f"WhaleAgent: Identified {len(holders)} major institutional holders for {ticker}"
                )

            return holders[:5]
        except Exception as e:
            logger.warning(f"13F fetch failed: {e}")
            return []
