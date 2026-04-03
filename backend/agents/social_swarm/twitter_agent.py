import asyncio
import logging
from decimal import Decimal
from typing import Optional
from .base_micro import BaseMicroAgent, MicroAgentResponse
from utils.financial_math import create_decimal
from resilience import get_circuit_breaker, CircuitBreakerOpenError

logger = logging.getLogger(__name__)

tavily_cb = get_circuit_breaker("tavily_twitter", failure_threshold=3, recovery_timeout=30.0)

class TwitterMicroAgent(BaseMicroAgent):
    """
    Micro-agent for monitoring Twitter/X sentiment.
    Uses Tavily (or mocked logic) to gather sub-second data without blocking.
    """

    def __init__(self, tavily_key: Optional[str] = None):
        super().__init__("TwitterAgent")
        self.tavily_key = tavily_key

    async def fetch_data(self, ticker: str, company_name: str) -> MicroAgentResponse:
        """Fetch Twitter discussions asynchronously."""
        if not self.tavily_key:
            return MicroAgentResponse(
                source="Twitter/X",
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
            query = f"${ticker} stock discussion twitter x.com" if ticker != "MARKET" else "retail investor sentiment twitter x.com stockmarket"
            
            url = "https://api.tavily.com/search"
            headers = {"Content-Type": "application/json"}
            payload = {
                "api_key": self.tavily_key,
                "query": query,
                "search_depth": "advanced",
                "include_domains": ["x.com", "twitter.com", "stocktwits.com"],
                "max_results": 5
            }

            async def _fetch_twitter_tavily():
                async with httpx.AsyncClient() as client:
                    res = await client.post(url, headers=headers, json=payload)
                    res.raise_for_status()
                    response = res.json()

                results = response.get('results', [])

                if not results and ticker != "MARKET" and company_name and company_name != ticker:
                    query = f"{company_name} stock sentiment discussion twitter"
                    payload["query"] = query
                    async with httpx.AsyncClient() as client:
                        res = await client.post(url, headers=headers, json=payload)
                        res.raise_for_status()
                        response = res.json()
                    results = response.get('results', [])
                return results

            results = await tavily_cb.call(_fetch_twitter_tavily)

            if not results:
                return MicroAgentResponse(
                    source="Twitter/X",
                    sentiment_score=create_decimal("0.0"),
                    mention_volume=0,
                    top_discussions=["No recent Twitter discussions found."],
                    success=True
                )

            sentiments = []
            discussions = []
            
            # Sub-second sentiment analysis logic
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
                source="Twitter/X",
                sentiment_score=avg_sentiment,
                mention_volume=len(results),
                top_discussions=discussions[:3],
                success=True
            )

        except CircuitBreakerOpenError:
            self.logger.warning("Twitter/X analysis skipped: circuit breaker is OPEN")
            return MicroAgentResponse(
                source="Twitter/X",
                sentiment_score=create_decimal("0.0"),
                mention_volume=0,
                top_discussions=["Service temporarily unavailable."],
                success=True
            )
        except Exception as e:
            self.logger.error(f"Twitter/X analysis failed: {e}")
            return MicroAgentResponse(
                source="Twitter/X",
                sentiment_score=create_decimal("0.0"),
                mention_volume=0,
                top_discussions=[],
                success=False,
                error=str(e)
            )
