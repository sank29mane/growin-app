"""
Social Sentiment Agent - Analyzes social media and community sentiment
Integration with Tavily (searching Reddit, Twitter/X, etc.)
"""

from base_agent import BaseAgent, AgentConfig, AgentResponse
from typing import Dict, Any, List, Optional
from pydantic import BaseModel
import logging
import os
import asyncio

logger = logging.getLogger(__name__)

class SocialData(BaseModel):
    """Social sentiment data structure"""
    ticker: str
    sentiment_score: float = 0.0  # -1 to 1
    sentiment_label: str = "NEUTRAL"
    mention_volume: str = "LOW" # LOW, MEDIUM, HIGH (inferred)
    top_discussions: List[str] = []
    platforms: List[str] = []

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
            
            tavily = TavilyClient(api_key=self.tavily_key)
            sentiment_analyzer = SentimentIntensityAnalyzer()
            
            def fetch_social():
                # Search for cashtags and discussions
                if ticker == "MARKET":
                    query = "retail investor sentiment reddit stockmarket wallstreetbets"
                else:
                    query = f"${ticker} stock discussion reddit twitter"
                
                return tavily.search(
                    query=query,
                    search_depth="advanced", # Deeper search for social 
                    include_domains=["reddit.com", "x.com", "twitter.com", "stocktwits.com"],
                    max_results=7
                )
            
            response = await asyncio.to_thread(fetch_social)
            results = response.get('results', [])
            
            if not results:
                return self._neutral_response(ticker, error="No social discussions found")
                
            sentiments = []
            discussions = []
            platforms = set()
            
            for res in results:
                content = res.get('content', '')
                title = res.get('title', '')
                url = res.get('url', '')
                
                # Identify platform
                if "reddit" in url: platforms.add("Reddit")
                elif "twitter" in url or "x.com" in url: platforms.add("X (Twitter)")
                elif "stocktwits" in url: platforms.add("StockTwits")
                
                # Sentiment
                text = f"{title}. {content}"
                scores = sentiment_analyzer.polarity_scores(text)
                sentiments.append(scores['compound'])
                
                discussions.append(title)
            
            avg_sentiment = sum(sentiments) / len(sentiments) if sentiments else 0.0
            
            if avg_sentiment >= 0.15: label = "BULLISH"
            elif avg_sentiment <= -0.15: label = "BEARISH"
            else: label = "NEUTRAL"
            
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
            return self._neutral_response(ticker, error=str(e))

    def _neutral_response(self, ticker: str, error: str = None) -> AgentResponse:
        data = SocialData(
            ticker=ticker,
            top_discussions=[f"Social data unavailable: {error}" if error else "No discussions found"]
        )
        return AgentResponse(
            agent_name=self.config.name,
            success=False, # Return success false so Decision Agent knows data is missing
            data=data.model_dump(),
            latency_ms=0,
            error=error
        )
