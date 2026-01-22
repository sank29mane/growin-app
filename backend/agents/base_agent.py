"""
Base Agent Interface for Hybrid Trading System
All specialist agents inherit from this for consistency and scalability.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
from pydantic import BaseModel
import logging

logger = logging.getLogger(__name__)


class AgentConfig(BaseModel):
    """Configuration for agent initialization"""
    name: str
    enabled: bool = True
    timeout: float = 10.0  # seconds
    cache_ttl: Optional[int] = 300  # 5 minutes


class AgentResponse(BaseModel):
    """Standardized response from any agent"""
    agent_name: str
    success: bool
    data: Dict[str, Any]
    error: Optional[str] = None
    latency_ms: float
    cached: bool = False


class BaseAgent(ABC):
    """
    Abstract base class for all specialist agents.
    
    Design Principles:
    - Each agent is independent and can fail without crashing the system
    - All agents return standardized AgentResponse objects
    - Agents are async by default
    - Easy to add new agents by extending this class
    """
    
    def __init__(self, config: AgentConfig):
        self.config = config
        self.logger = logging.getLogger(f"agents.{config.name}")
        from cache_manager import cache
        self.cache = cache
    
    @abstractmethod
    async def analyze(self, context: Dict[str, Any]) -> AgentResponse:
        """
        Main analysis method - must be implemented by each agent.
        
        Args:
            context: Input data (ticker, timeframe, user query, etc.)
        
        Returns:
            AgentResponse with results or error
        """
        pass
    
    async def execute(self, context: Dict[str, Any]) -> AgentResponse:
        """
        Wrapper that handles errors, caching, and timing.
        """
        import time
        
        if not self.config.enabled:
            return AgentResponse(
                agent_name=self.config.name,
                success=False,
                data={},
                error="Agent disabled in config",
                latency_ms=0
            )
        
        start = time.time()
        
        try:
            # Check cache
            cache_key = self._get_cache_key(context)
            if cache_key:
                cached_data = self.cache.get(cache_key)
                if cached_data:
                    latency = (time.time() - start) * 1000
                    self.logger.debug(f"{self.config.name}: Cache hit ({latency:.1f}ms)")
                    return AgentResponse(
                        agent_name=self.config.name,
                        success=True,
                        data=cached_data,
                        latency_ms=latency,
                        cached=True
                    )
            
            # Execute actual analysis
            response = await self.analyze(context)
            
            # Cache result if successful
            if response.success and cache_key:
                self.cache.set(cache_key, response.data, ttl=self.config.cache_ttl or 300)
            
            latency = (time.time() - start) * 1000
            response.latency_ms = latency
            
            self.logger.info(
                f"{self.config.name}: {'Success' if response.success else 'Failed'} "
                f"({latency:.1f}ms)"
            )
            
            return response
            
        except Exception as e:
            latency = (time.time() - start) * 1000
            self.logger.error(f"{self.config.name}: Exception - {e}")
            return AgentResponse(
                agent_name=self.config.name,
                success=False,
                data={},
                error=str(e),
                latency_ms=latency
            )
    
    def _get_cache_key(self, context: Dict[str, Any]) -> Optional[str]:
        """Generate cache key from context (override if needed)"""
        ticker = context.get("ticker")
        if ticker:
            return f"{self.config.name}:{ticker}"
        return None
    
    def clear_cache(self):
        """Clear all cached data (delegated to global manager)"""
        self.cache.clear()
        self.logger.info(f"{self.config.name}: Cache cleared")
