"""
Base Agent Interface for Hybrid Trading System
All specialist agents inherit from this for consistency and scalability.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional
from pydantic import BaseModel, Field
from datetime import datetime, timezone
import logging

logger = logging.getLogger(__name__)


class AgentConfig(BaseModel):
    """Configuration for agent initialization"""
    name: str
    enabled: bool = True
    timeout: float = 10.0  # seconds
    cache_ttl: Optional[int] = 300  # 5 minutes


class TelemetryData(BaseModel):
    """Structured telemetry for observability"""
    agent_name: str
    model_version: Optional[str] = None
    latency_ms: float
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    correlation_id: Optional[str] = None
    cached: bool = False
    tokens_used: Optional[int] = None

    @property
    def decision_id(self) -> Optional[str]:
        return self.correlation_id


class AgentResponse(BaseModel):
    """Standardized response from any agent"""
    agent_name: str
    success: bool
    data: Dict[str, Any]
    error: Optional[str] = None
    latency_ms: float
    cached: bool = False
    telemetry: Optional[TelemetryData] = None


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
        self.model_version = "v1.0.0" # Default, override in specialists
        
        # Register with Messenger
        from .messenger import get_messenger
        get_messenger().register_agent(self.config.name, self.handle_message)
    
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

    async def handle_message(self, message: Any):
        """Standard message handler for decentralized communication."""
        self.logger.debug(f"Received message: {message.subject} from {message.sender}")
        # Default behavior: if subject is 'request_analysis', run execute
        if message.subject == "request_analysis":
            result = await self.execute(message.payload)
            await self.publish_result(result, message.correlation_id)

    async def publish_result(self, result: AgentResponse, correlation_id: Optional[str] = None):
        """Publish analysis results to the agent bus."""
        from .messenger import AgentMessage
        from .governance import get_governance
        
        message = AgentMessage(
            sender=self.config.name,
            recipient="broadcast",
            subject="analysis_result",
            payload=result.model_dump(),
            correlation_id=correlation_id
        )
        await get_governance().secure_dispatch(message)
    
    async def execute(self, context: Dict[str, Any]) -> AgentResponse:
        """
        Wrapper that handles errors, caching, and timing.
        """
        import time
        from app_logging import correlation_id_ctx
        
        if not self.config.enabled:
            return AgentResponse(
                agent_name=self.config.name,
                success=False,
                data={},
                error="Agent disabled in config",
                latency_ms=0
            )
        
        start = time.time()
        c_id = correlation_id_ctx.get() if correlation_id_ctx else None
        
        from .messenger import AgentMessage, get_messenger
        messenger = get_messenger()

        # Emit Start Event
        start_msg = AgentMessage(
            sender=self.config.name,
            recipient="broadcast",
            subject="agent_started",
            payload={"agent": self.config.name, "ticker": context.get("ticker")},
            correlation_id=c_id
        )
        await messenger.send_message(start_msg)

        try:
            # Check cache
            cache_key = self._get_cache_key(context)
            if cache_key:
                cached_data = self.cache.get(cache_key)
                if cached_data:
                    latency = (time.time() - start) * 1000
                    self.logger.debug(f"{self.config.name}: Cache hit ({latency:.1f}ms)")
                    
                    response = AgentResponse(
                        agent_name=self.config.name,
                        success=True,
                        data=cached_data,
                        latency_ms=latency,
                        cached=True
                    )
                    
                    response.telemetry = TelemetryData(
                        agent_name=self.config.name,
                        model_version=self.model_version,
                        latency_ms=latency,
                        correlation_id=c_id,
                        cached=True
                    )
                    
                    from telemetry_store import record_trace
                    record_trace(response.telemetry)
                    
                    # Emit Cache Hit Event
                    await messenger.send_message(AgentMessage(
                        sender=self.config.name,
                        recipient="broadcast",
                        subject="agent_complete",
                        payload={"agent": self.config.name, "success": True, "cached": True, "latency_ms": latency},
                        correlation_id=c_id
                    ))
                    
                    return response
            
            # Execute actual analysis
            response = await self.analyze(context)
            
            # Cache result if successful
            if response.success and cache_key:
                self.cache.set(cache_key, response.data, ttl=self.config.cache_ttl or 300)
            
            latency = (time.time() - start) * 1000
            response.latency_ms = latency
            
            # Populate Telemetry
            response.telemetry = TelemetryData(
                agent_name=self.config.name,
                model_version=getattr(self, "model_name", self.model_version),
                latency_ms=latency,
                correlation_id=c_id,
                cached=False
            )
            
            self.logger.info(
                f"{self.config.name}: {'Success' if response.success else 'Failed'} "
                f"({latency:.1f}ms)"
            )
            
            from telemetry_store import record_trace
            record_trace(response.telemetry)
            
            # Emit Success Event
            await messenger.send_message(AgentMessage(
                sender=self.config.name,
                recipient="broadcast",
                subject="agent_complete",
                payload={
                    "agent": self.config.name, 
                    "success": response.success, 
                    "cached": False, 
                    "latency_ms": latency,
                    "error": response.error if not response.success else None
                },
                correlation_id=c_id
            ))
            
            return response
            
        except Exception as e:
            latency = (time.time() - start) * 1000
            self.logger.error(f"{self.config.name}: Exception - {e}")
            
            response = AgentResponse(
                agent_name=self.config.name,
                success=False,
                data={},
                error=str(e),
                latency_ms=latency
            )
            
            response.telemetry = TelemetryData(
                agent_name=self.config.name,
                model_version=self.model_version,
                latency_ms=latency,
                correlation_id=c_id,
                cached=False
            )
            
            from telemetry_store import record_trace
            record_trace(response.telemetry)
            
            # Emit Failure Event
            await messenger.send_message(AgentMessage(
                sender=self.config.name,
                recipient="broadcast",
                subject="agent_complete",
                payload={"agent": self.config.name, "success": False, "error": str(e), "latency_ms": latency},
                correlation_id=c_id
            ))
            
            return response
    
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
