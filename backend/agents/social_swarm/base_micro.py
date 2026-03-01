import abc
import logging
from decimal import Decimal
from typing import Dict, Any, Optional
from pydantic import BaseModel
from utils.financial_math import create_decimal

logger = logging.getLogger(__name__)

class MicroAgentResponse(BaseModel):
    """Standardized response from a micro-agent"""
    source: str
    sentiment_score: Decimal
    mention_volume: int
    top_discussions: list[str]
    success: bool
    error: Optional[str] = None

class BaseMicroAgent(abc.ABC):
    """
    Abstract base class for all social micro-agents.
    Designed for non-blocking, sub-second polling.
    """
    
    def __init__(self, name: str):
        self.name = name
        self.logger = logging.getLogger(f"agents.social_swarm.{name}")

    @abc.abstractmethod
    async def fetch_data(self, ticker: str, company_name: str) -> MicroAgentResponse:
        """
        Fetch and analyze data from the specific source.
        Must be implemented by subclasses.
        Returns a MicroAgentResponse.
        """
        pass
