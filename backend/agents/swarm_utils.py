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
        if self.new_data_event.is_set():
            self.new_data_event.clear()
            return True
        try:
            await asyncio.wait_for(self.new_data_event.wait(), timeout=timeout)
            self.new_data_event.clear()
            return True
        except asyncio.TimeoutError:
            return False

    def __len__(self):
        return len(self.results)

def summarize_specialist_data(result: AgentResult) -> AgentResult:
    """
    Summarizes large data values inside an AgentResult to keep prompt context size small
    and preserve prefix caching.
    """
    import re
    summarized_data = {}
    
    for k, v in result.data.items():
        if isinstance(v, str):
            # If string is large (more than 150 words or 600 characters)
            word_count = len(v.split())
            if word_count > 150 or len(v) > 600:
                # Extract first 2 sentences, or fallback to simple truncation
                sentences = re.split(r'(?<=[.!?])\s+', v)
                summary = " ".join(sentences[:2])
                if len(summary) < len(v) and len(summary) > 20:
                    summarized_data[k] = f"{summary} [Summarized; original length: {len(v)} chars]"
                else:
                    summarized_data[k] = v[:400] + "... [Truncated]"
            else:
                summarized_data[k] = v
        elif isinstance(v, list) and len(v) > 5:
            # For lists (e.g. news articles or tickers), take top 5 and add a summary count
            summarized_data[k] = v[:5] + [f"... and {len(v) - 5} more items"]
        elif isinstance(v, dict):
            # Recursively check sub-dicts or keep it simple
            summarized_data[k] = v
        else:
            summarized_data[k] = v
            
    return AgentResult(
        source=result.source,
        data=summarized_data,
        conviction=result.conviction,
        timestamp=result.timestamp,
        correlation_id=result.correlation_id
    )
