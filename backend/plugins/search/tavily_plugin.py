import logging
from typing import List, Optional
from resilience import get_circuit_breaker
from utils.http_client import agent_http_client
from .base import SearchPlugin, SearchResult

logger = logging.getLogger(__name__)

class TavilySearchPlugin(SearchPlugin):
    def __init__(self, api_key: str):
        self.api_key = api_key
        self._circuit_breaker = get_circuit_breaker("tavily_search", failure_threshold=3, recovery_timeout=30.0)

    async def search(self, query: str, max_results: int = 5, include_domains: Optional[List[str]] = None, search_depth: str = "advanced") -> List[SearchResult]:
        if not self.api_key:
            return []

        url = "https://api.tavily.com/search"
        headers = {"Content-Type": "application/json"}
        payload = {
            "api_key": self.api_key,
            "query": query,
            "search_depth": search_depth,
            "max_results": max_results
        }

        if include_domains:
            payload["include_domains"] = include_domains

        try:
            response = await agent_http_client.execute_with_breaker(
                self._circuit_breaker, "POST", url, headers=headers, json=payload
            )

            return [
                SearchResult(
                    title=r.get("title", ""),
                    content=r.get("content", "") or r.get("snippet", ""),
                    url=r.get("url", ""),
                    source="Tavily"
                )
                for r in response.get("results", [])
            ]
        except Exception as e:
            logger.warning(f"TavilySearchPlugin failed: {e}")
            return []
