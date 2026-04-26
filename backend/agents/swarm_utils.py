import asyncio
import logging
import datetime
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

class AgentResult(BaseModel):
    """
    Structured output from a specialist agent.
    """
    source: str = Field(..., description="Name of the specialist agent (e.g., QuantAgent)")
    data: Dict[str, Any] = Field(..., description="The structured findings")
    conviction: int = Field(default=5, ge=1, le=10, description="Confidence in this data (1-10)")
    timestamp: datetime.datetime = Field(default_factory=datetime.datetime.now)
    correlation_id: Optional[str] = None

class ContextBuffer:
    """
    Async buffer for accumulating specialist agent results.
    Enables 'Progressive Synthesis' by allowing the DecisionAgent 
    to subscribe to arriving data.
    """
    
    def __init__(self):
        self.results: List[AgentResult] = []
        self.new_data_event = asyncio.Event()
        self._lock = asyncio.Lock()
        
    async def push(self, result: AgentResult):
        """
        Add a new result to the buffer and notify subscribers.
        """
        async with self._lock:
            self.results.append(result)
            self.new_data_event.set()
            # Immediately clear so it can be re-triggered for the next data
            self.new_data_event.clear()
            logger.debug(f"📥 ContextBuffer received results from {result.source}")

    async def get_all(self) -> List[AgentResult]:
        """
        Retrieve all current results in the buffer.
        """
        async with self._lock:
            return list(self.results)

    async def wait_for_new(self, timeout: Optional[float] = None) -> bool:
        """
        Wait until new data arrives in the buffer.
        """
        try:
            await asyncio.wait_for(self.new_data_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    def __len__(self):
        return len(self.results)
