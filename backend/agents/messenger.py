"""
Agent Messenger - Decoupled communication for Multi-Agent Systems.
Implements a simple Actor-inspired message bus for 2026 SOTA MAS.
"""

import asyncio
import logging
import uuid
from typing import Dict, Any, Optional, Callable, Awaitable
from pydantic import BaseModel, Field
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

class AgentMessage(BaseModel):
    """Standard message for inter-agent communication."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    sender: str
    recipient: str
    subject: str
    payload: Dict[str, Any]
    correlation_id: Optional[str] = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

class AgentMessenger:
    """
    Decoupled message bus for agents.
    Allows agents to subscribe to topics and receive messages asynchronously.
    """
    def __init__(self):
        self._subscribers: Dict[str, Callable[[AgentMessage], Awaitable[None]]] = {}
        self._message_history: list[AgentMessage] = []
        self._lock = asyncio.Lock()

    def register_agent(self, agent_name: str, handler: Callable[[AgentMessage], Awaitable[None]]):
        """Register an agent and its message handler."""
        self._subscribers[agent_name] = handler
        logger.info(f"Messenger: Registered agent '{agent_name}'")

    async def send_message(self, message: AgentMessage):
        """Route a message to its recipient."""
        async with self._lock:
            self._message_history.append(message)
            # Limit history
            if len(self._message_history) > 1000:
                self._message_history.pop(0)

        recipient = message.recipient
        if recipient in self._subscribers:
            # Execute handler in background to avoid blocking sender
            asyncio.create_task(self._subscribers[recipient](message))
        elif recipient == "broadcast":
            for name, handler in self._subscribers.items():
                if name != message.sender:
                    asyncio.create_task(handler(message))
        else:
            logger.warning(f"Messenger: Recipient '{recipient}' not found for message from '{message.sender}'")

    def get_history(self, correlation_id: str) -> list[AgentMessage]:
        """Retrieve message history for a specific trace."""
        return [m for m in self._message_history if m.correlation_id == correlation_id]

# Singleton instance
_messenger = None

def get_messenger() -> AgentMessenger:
    global _messenger
    if _messenger is None:
        _messenger = AgentMessenger()
    return _messenger
