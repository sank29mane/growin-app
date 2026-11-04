"""
NewsData.io Client - SOTA 2026 External News Provider
Encapsulates high-performance news fetching for the Growin MAS.
"""

import os
import logging
from typing import List, Dict, Any, Optional
import httpx
from resilience import get_circuit_breaker
from utils.http_client import agent_http_client

logger = logging.getLogger(__name__)

# Dedicated Circuit Breaker for NewsData.io (SOTA 2026 Resilience API)
newsdata_circuit = get_circuit_breaker("newsdata", failure_threshold=3, recovery_timeout=30.0)

# Verified against https://newsdata.io/documentation/api-authentication on 2026-11-05:
# X-ACCESS-KEY is the recommended method, `?apikey=` the alternative.
# newsdata.io takes the key as a header. Never put it in the query string: httpx
# puts the full URL into HTTPStatusError text, and that text gets logged.
NEWSDATA_KEY_HEADER = "X-ACCESS-KEY"


def newsdata_headers(api_key: Optional[str]) -> Dict[str, str]:
    """Request headers carrying the newsdata.io key."""
    return {NEWSDATA_KEY_HEADER: api_key} if api_key else {}


NEWSAPI_KEY_HEADER = "X-Api-Key"  # newsapi.org accepts the key here instead of `apiKey=`


def newsapi_headers(api_key: Optional[str]) -> Dict[str, str]:
    """Request headers carrying the newsapi.org key."""
    return {NEWSAPI_KEY_HEADER: api_key} if api_key else {}


def provider_error_summary(exc: BaseException) -> str:
    """
    One short, log-safe line for a failed news-provider call: the status code for an
    HTTP error, otherwise the exception class. Never the message, the request, the
    headers or the body, none of which this function can prove are free of the key.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return type(exc).__name__


class NewsDataIOClient:
    """
    Asynchronous client for fetching news from NewsData.io.
    Provides verified regulatory news and business sentiment data.
    """
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("NEWSDATA_API_KEY")
        self.base_url = "https://newsdata.io/api/1/latest"
        
    @newsdata_circuit.protect
    async def fetch_latest_news(self, query: str, country: str = "gb", category: str = "business") -> List[Dict[str, Any]]:
        """
        Fetch the latest news articles for a given query.
        """
        if not self.api_key:
            logger.warning("NewsDataIOClient: API Key missing. Skipping fetch.")
            return []
            
        params = {
            "q": query,
            "country": country,
            "category": category
        }
        
        try:
            # SOTA: Transition to central http_client for connection pooling.
            # execute_with_breaker returns the decoded JSON body and raises on HTTP errors.
            data = await agent_http_client.execute_with_breaker(
                newsdata_circuit, "GET", self.base_url,
                params=params, headers=newsdata_headers(self.api_key), timeout=10.0,
            )
            results = (data or {}).get('results') or []

            # Normalize to standard article format
            normalized = []
            for art in results:
                normalized.append({
                    'title': art.get('title'),
                    'description': art.get('description'),
                    'source': {'name': 'NewsData.io'},
                    'url': art.get('link'),
                    'pub_date': art.get('pubDate')
                })
            return normalized
        except Exception as e:
            logger.error(f"NewsDataIOClient fetch failed: {provider_error_summary(e)}")
            return []

    async def fetch_regulatory_news(self, ticker: str) -> List[Dict[str, Any]]:
        """
        Specialized fetch for regulatory news (e.g. LSE RNS).
        """
        # Logic specific to RNS or SEC announcements
        query = f"{ticker} RNS" if ticker.endswith(".L") else f"{ticker} SEC filing"
        return await self.fetch_latest_news(query)
