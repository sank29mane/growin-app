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
                timeout=15.0,
                cache_ttl=60  # whale data is very time-sensitive
            )
        super().__init__(config)
        self.alpaca = get_alpaca_client()
        self.whale_threshold_usd = 50000.0 # Lowered from $250k for paper/IEX data density
        
    async def analyze(self, context: Dict[str, Any]) -> AgentResponse:
        """
        Analyze recent trades for a ticker to find large block orders.
        """
        ticker = context.get("ticker")
        if not ticker:
            return AgentResponse(
                agent_name=self.config.name,
                success=False,
                data={},
                error="No ticker provided for Whale analysis",
                latency_ms=0
            )
            
        try:
            # 1. Fetch recent trades (last 500)
            logger.info(f"WhaleAgent: Fetching trades for {ticker}...")
            try:
                trades = await self.alpaca.get_recent_trades(ticker, limit=500)
            except Exception as alpaca_err:
                logger.error(f"WhaleAgent: Alpaca fetch failed: {alpaca_err}")
                raise alpaca_err
            
            if not trades:
                return AgentResponse(
                    agent_name=self.config.name,
                    success=False, # Mark as failed when no data
                    data=WhaleData(ticker=ticker, summary="No recent trade data available to analyze whales.").model_dump(),
                    latency_ms=0
                )
            
            # 2. Identify Large Trades
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
            
            # 3. Analyze Unusual Volume
            # (In a real app, we would compare to 20-day avg volume)
            # For now, we flag if we see more than 3 whales in 500 trades
            unusual_activity = len(large_trades) > 3
            
            # 4. Sentiment Impact
            # If price is at the High of recent trades and we see whales, it might be accumulation
            impact = "NEUTRAL"
            if len(large_trades) > 0:
                avg_price = sum(t['p'] for t in trades) / len(trades)
                whale_avg_price = sum(w['price'] for w in large_trades) / len(large_trades)
                
                if whale_avg_price > avg_price * 1.001:
                    impact = "BULLISH"
                elif whale_avg_price < avg_price * 0.999:
                    impact = "BEARISH"
            
            # 5. Build Summary
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
