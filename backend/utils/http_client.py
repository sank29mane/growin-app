import asyncio
import logging
from typing import Optional, Any
import httpx
from resilience import CircuitBreaker, execute_with_breaker as resilience_execute_with_breaker

logger = logging.getLogger(__name__)

class AgentHttpClient:
    """
    Centralized HTTP client for agents to avoid socket exhaustion and reduce boilerplate.
    Provides a persistent, lazy-initialized httpx.AsyncClient and integrates with resilience circuit breakers.
    """
    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None

    @property
    def client(self) -> httpx.AsyncClient:
        """Lazy-initialized persistent HTTP client."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=10.0)
            logger.info("AgentHttpClient initialized new httpx.AsyncClient")
        return self._client

    async def execute_with_breaker(self, breaker: CircuitBreaker, method: str, url: str, **kwargs) -> Any:
        """
        Executes a request using the persistent client, protected by the given circuit breaker.
        Delegates to the existing resilience.execute_with_breaker logic.
        """
        return await resilience_execute_with_breaker(
            breaker, method, url, client=self.client, **kwargs
        )

    async def close(self):
        """Close the underlying client if it exists."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            logger.info("AgentHttpClient closed httpx.AsyncClient")
            self._client = None

# Global instance for use across agents
agent_http_client = AgentHttpClient()
