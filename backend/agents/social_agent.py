"""
Social Sentiment Agent - Analyzes social media and community sentiment
Integration with Tavily (searching Reddit, Twitter/X, etc.)
"""

from .base_agent import BaseAgent, AgentConfig, AgentResponse
from market_context import SocialData
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

    async def execute(self, context: Dict[str, Any]) -> AgentResponse:
        """Override execute to handle logic"""
        return await self.analyze(context)

    async def analyze(self, context: Dict[str, Any]) -> AgentResponse:
        """
        Fetch social discussions and analyze sentiment.
        """
        ticker = context.get("ticker", "MARKET")
        company_name = context.get("company_name", ticker)
        
        if not self.tavily_key:
            return self._neutral_response(ticker, error="Tavily API key missing")
            
        try:
            from tavily import TavilyClient
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
            from resilience import retry_with_backoff
            
            tavily = TavilyClient(api_key=self.tavily_key)
            sentiment_analyzer = SentimentIntensityAnalyzer()
            
            # --- Query Strategy with Fallback ---
            # 1. Ticker Query
            # 2. Company Query (if no results for ticker)
            
            results = []
            
            @retry_with_backoff(max_retries=2, base_delay=1.0)
            async def fetch_with_query(q):
                # Tavily client is sync, so offload to thread here
                return await asyncio.to_thread(
                    tavily.search,
                    query=q,
                    search_depth="advanced",
                    include_domains=["reddit.com", "x.com", "twitter.com", "stocktwits.com"],
                    max_results=7
                )

            # Strategy 1: Ticker Search
            if ticker == "MARKET":
                query = "retail investor sentiment reddit stockmarket wallstreetbets"
            else:
                query = f"${ticker} stock discussion reddit twitter"
            
            response = await fetch_with_query(query)
            results = response.get('results', [])
            
            # Strategy 2: Company Name Fallback (if specific ticker yields nothing)
            if not results and ticker != "MARKET" and company_name and company_name != ticker:
                logger.info(f"SocialAgent: No results for ${ticker}. Retrying with '{company_name}'...")
                query = f"{company_name} stock sentiment discussion"
                response = await fetch_with_query(query)
                results = response.get('results', [])

            if not results:
                # FAIL SOFT: Return success with neutral data
                return self._neutral_response(ticker, error=None, success=True, msg="No social discussions found.")
                
            sentiments = []
            discussions = []
            platforms = set()
            
            for res in results:
                content = res.get('content', '')
                title = res.get('title', '')
                url = res.get('url', '')
                
                # Identify platform
                if "reddit" in url:
                    platforms.add("Reddit")
                elif "twitter" in url or "x.com" in url:
                    platforms.add("X (Twitter)")
                elif "stocktwits" in url:
                    platforms.add("StockTwits")
                
                # Sentiment
                text = f"{title}. {content}"
                scores = sentiment_analyzer.polarity_scores(text)
                sentiments.append(scores['compound'])
                
                discussions.append(title)
            
            avg_sentiment = sum(sentiments) / len(sentiments) if sentiments else 0.0
            
            if avg_sentiment >= 0.15:
                label = "BULLISH"
            elif avg_sentiment <= -0.15:
                label = "BEARISH"
            else:
                label = "NEUTRAL"
            
            # Infer volume/hype (heuristic based on result count/relevance)
            # This is a weak proxy but better than nothing
            volume = "MEDIUM" if len(results) >= 5 else "LOW"

            social_data = SocialData(
                ticker=ticker,
                sentiment_score=avg_sentiment,
                sentiment_label=label,
                mention_volume=volume,
                top_discussions=discussions[:5],
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
