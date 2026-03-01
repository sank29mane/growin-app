"""
NewsData.io Client - SOTA 2026 External News Provider
Encapsulates high-performance news fetching for the Growin MAS.
"""

import os
import asyncio
import logging
import httpx
from typing import List, Dict, Any, Optional
from utils.error_resilience import circuit_breaker, CircuitBreaker

logger = logging.getLogger(__name__)

# Dedicated Circuit Breaker for NewsData.io
newsdata_circuit = CircuitBreaker(failure_threshold=3, recovery_timeout=30)

class NewsDataIOClient:
    """
    Asynchronous client for fetching news from NewsData.io.
    Provides verified regulatory news and business sentiment data.
    """
    
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("NEWSDATA_API_KEY")
        self.base_url = "https://newsdata.io/api/1/latest"
        
    @circuit_breaker(newsdata_circuit)
    async def fetch_latest_news(self, query: str, country: str = "gb", category: str = "business") -> List[Dict[str, Any]]:
        """
        Fetch the latest news articles for a given query.
        """
        if not self.api_key:
            logger.warning("NewsDataIOClient: API Key missing. Skipping fetch.")
            return []
            
        params = {
            "apikey": self.api_key,
            "q": query,
            "country": country,
            "category": category
        }
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(self.base_url, params=params)
                if resp.status_code == 200:
                    data = resp.json()
                    results = data.get('results', [])
                    
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
                else:
                    logger.error(f"NewsDataIO fetch failed with status {resp.status_code}: {resp.text}")
                    return []
        except Exception as e:
            logger.error(f"NewsDataIOClient error: {e}")
            return []

    async def fetch_regulatory_news(self, ticker: str) -> List[Dict[str, Any]]:
        """
        Specialized fetch for regulatory news (e.g. LSE RNS).
        """
        # Logic specific to RNS or SEC announcements
        query = f"{ticker} RNS" if ticker.endswith(".L") else f"{ticker} SEC filing"
        return await self.fetch_latest_news(query)
