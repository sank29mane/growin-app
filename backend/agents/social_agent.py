"""
Social Sentiment Agent - Analyzes social media and community sentiment
Integration with Tavily (searching Reddit, Twitter/X, etc.)
"""

from .base_agent import BaseAgent, AgentConfig, AgentResponse
from market_context import SocialData
from .social_swarm import RedditMicroAgent, TwitterMicroAgent
from utils.financial_math import create_decimal
from decimal import Decimal
from typing import Dict, Any
import logging
import os
import asyncio

logger = logging.getLogger(__name__)


class SocialAgent(BaseAgent):
    """
    Social Sentiment analyzer.
    Uses Tavily to search specifically for social media discussions ($TICKER on reddit, twitter).
    """

    def __init__(self, config: AgentConfig = None):
        if config is None:
            config = AgentConfig(
                name="SocialAgent",
                enabled=True,
                timeout=15.0,
                cache_ttl=600
            )
        super().__init__(config)
        
        self.tavily_key = os.getenv("TAVILY_API_KEY")
        
        if not self.tavily_key:
            logger.warning("TAVILY_API_KEY not found. SocialAgent will be disabled.")
            
        self.swarm = [
            RedditMicroAgent(tavily_key=self.tavily_key),
            TwitterMicroAgent(tavily_key=self.tavily_key)
        ]

    async def execute(self, context: Dict[str, Any]) -> AgentResponse:
        """Override execute to handle logic"""
        return await self.analyze(context)

    async def analyze(self, context: Dict[str, Any]) -> AgentResponse:
        """
        Orchestrate the social micro-agent swarm to fetch discussions and analyze sentiment.
        """
        ticker = context.get("ticker", "MARKET")
        company_name = context.get("company_name", ticker)
        
        if not self.tavily_key:
            return self._neutral_response(ticker, error="Tavily API key missing")
            
        try:
            # Sub-second polling architecture: Execute swarm concurrently
            fetch_tasks = [
                agent.fetch_data(ticker, company_name)
                for agent in self.swarm
            ]
            
            swarm_results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
            
            valid_results = []
            platforms = set()
            total_sentiment = create_decimal("0.0")
            total_volume = 0
            all_discussions = []
            
            for res in swarm_results:
                if isinstance(res, Exception):
                    logger.error(f"Swarm agent failed with exception: {res}")
                    continue
                if not res.success:
                    logger.warning(f"Swarm agent {res.source} failed: {res.error}")
                    continue
                
                valid_results.append(res)
                platforms.add(res.source)
                if res.mention_volume > 0:
                    # Weight by volume if applicable or simply average
                    total_sentiment += res.sentiment_score * create_decimal(str(res.mention_volume))
                    total_volume += res.mention_volume
                all_discussions.extend(res.top_discussions)

            if not valid_results or total_volume == 0:
                # FAIL SOFT: Return success with neutral data
                return self._neutral_response(ticker, error=None, success=True, msg="No social discussions found.")
                
            avg_sentiment = total_sentiment / create_decimal(str(total_volume))
            
            if avg_sentiment >= create_decimal("0.15"):
                label = "BULLISH"
            elif avg_sentiment <= create_decimal("-0.15"):
                label = "BEARISH"
            else:
                label = "NEUTRAL"
            
            # Infer volume/hype (heuristic based on result count/relevance)
            if total_volume >= 10:
                volume_label = "HIGH"
            elif total_volume >= 5:
                volume_label = "MEDIUM"
            else:
                volume_label = "LOW"

            social_data = SocialData(
                ticker=ticker,
                sentiment_score=avg_sentiment,
                sentiment_label=label,
                mention_volume=volume_label,
                top_discussions=all_discussions[:5],
                platforms=list(platforms)
            )
            
            return AgentResponse(
                agent_name=self.config.name,
                success=True,
                data=social_data.model_dump(),
                latency_ms=0
            )

        except Exception as e:
            logger.error(f"Social analysis failed: {e}")
            # Fail soft on generic errors
            return self._neutral_response(ticker, error=str(e), success=True)

    def _neutral_response(self, ticker: str, error: str = None, success: bool = False, msg: str = None) -> AgentResponse:
        final_msg = msg or (f"Social data unavailable: {error}" if error else "No discussions found")
        data = SocialData(
            ticker=ticker,
            top_discussions=[final_msg]
        )
        return AgentResponse(
            agent_name=self.config.name,
            success=success, 
            data=data.model_dump(),
            latency_ms=0,
            error=error
        )
