import asyncio
import logging
from decimal import Decimal
from typing import Optional
from .base_micro import BaseMicroAgent, MicroAgentResponse
from utils.financial_math import create_decimal
from resilience import get_circuit_breaker, CircuitBreakerOpenError, execute_with_breaker

logger = logging.getLogger(__name__)

tavily_cb = get_circuit_breaker("tavily_reddit", failure_threshold=3, recovery_timeout=30.0)

class RedditMicroAgent(BaseMicroAgent):
    """
    Micro-agent for monitoring Reddit sentiment.
    Uses Tavily (or mocked logic) to gather sub-second data without blocking.
    """

    def __init__(self, tavily_key: Optional[str] = None):
        super().__init__("RedditAgent")
        self.tavily_key = tavily_key

    async def fetch_data(self, ticker: str, company_name: str) -> MicroAgentResponse:
        """Fetch Reddit discussions asynchronously."""
        if not self.tavily_key:
            return MicroAgentResponse(
                source="Reddit",
                sentiment_score=create_decimal("0.0"),
                mention_volume=0,
                top_discussions=[],
                success=False,
                error="Missing Tavily Key"
            )

        try:
            import httpx
            from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
            
            sentiment_analyzer = SentimentIntensityAnalyzer()
            
            # Non-blocking async execution
            query = f"${ticker} stock discussion reddit wallstreetbets" if ticker != "MARKET" else "retail investor sentiment reddit wallstreetbets"
            
            url = "https://api.tavily.com/search"
            headers = {"Content-Type": "application/json"}
            payload = {
                "api_key": self.tavily_key,
                "query": query,
                "search_depth": "advanced",
                "include_domains": ["reddit.com"],
                "max_results": 5
            }

            response = await execute_with_breaker(tavily_cb, "POST", url, headers=headers, json=payload)
            results = response.get('results', [])

            if not results and ticker != "MARKET" and company_name and company_name != ticker:
                query = f"{company_name} stock sentiment discussion reddit"
                payload["query"] = query
                response = await execute_with_breaker(tavily_cb, "POST", url, headers=headers, json=payload)
                results = response.get('results', [])

            if not results:
                return MicroAgentResponse(
                    source="Reddit",
                    sentiment_score=create_decimal("0.0"),
                    mention_volume=0,
                    top_discussions=["No recent Reddit discussions found."],
                    success=True
                )

            sentiments = []
            discussions = []
            
            # Sub-second sentiment analysis logic (blocking, but fast enough for small text, 
            # though wrapping in to_thread provides optimal safety against event loop blocks)
            def analyze_sentiment(res_list):
                sents = []
                discs = []
                for res in res_list:
                    title = res.get('title', '')
                    content = res.get('content', '')
                    text = f"{title}. {content}"
                    scores = sentiment_analyzer.polarity_scores(text)
                    sents.append(create_decimal(str(scores['compound'])))
                    discs.append(title)
                return sents, discs
                
            sentiments, discussions = await asyncio.to_thread(analyze_sentiment, results)

            avg_sentiment = sum(sentiments) / len(sentiments) if sentiments else create_decimal("0.0")

            return MicroAgentResponse(
                source="Reddit",
                sentiment_score=avg_sentiment,
                mention_volume=len(results),
                top_discussions=discussions[:3],
                success=True
            )

        except CircuitBreakerOpenError:
            self.logger.warning("Reddit analysis skipped: circuit breaker is OPEN")
            return MicroAgentResponse(
                source="Reddit",
                sentiment_score=create_decimal("0.0"),
                mention_volume=0,
                top_discussions=["Service temporarily unavailable."],
                success=True
            )
        except Exception as e:
            self.logger.error(f"Reddit analysis failed: {e}")
            return MicroAgentResponse(
                source="Reddit",
                sentiment_score=create_decimal("0.0"),
                mention_volume=0,
                top_discussions=[],
                success=False,
                error=str(e)
            )
