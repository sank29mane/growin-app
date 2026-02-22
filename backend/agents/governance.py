"""
Agent Governance Service - 2026 SOTA Security & Coordination.
Mediates all inter-agent interactions and enforces security protocols.
"""

import logging
from typing import Dict, Any, Optional, List
from .messenger import get_messenger, AgentMessage
from pydantic import BaseModel

logger = logging.getLogger(__name__)

class AgentPolicy(BaseModel):
    """Defines what an agent is allowed to do."""
    name: str
    can_read_portfolio: bool = False
    can_trade: bool = False
    allowed_recipients: List[str] = ["CoordinatorAgent", "DecisionAgent"]

class GovernanceService:
    """
    Enforces security and coordination policies for the agent swarm.
    """
    def __init__(self):
        self.messenger = get_messenger()
        self.policies: Dict[str, AgentPolicy] = {
            "CoordinatorAgent": AgentPolicy(name="CoordinatorAgent", can_read_portfolio=True, allowed_recipients=["broadcast"]),
            "PortfolioAgent": AgentPolicy(name="PortfolioAgent", can_read_portfolio=True),
            "QuantAgent": AgentPolicy(name="QuantAgent"),
            "ResearchAgent": AgentPolicy(name="ResearchAgent"),
            "DecisionAgent": AgentPolicy(name="DecisionAgent", can_read_portfolio=True, can_trade=True, allowed_recipients=["broadcast"])
        }

    def is_authorized(self, sender: str, action: str, recipient: Optional[str] = None) -> bool:
        """Verify if an agent is authorized to perform an action."""
        policy = self.policies.get(sender)
        if not policy:
            logger.warning(f"Governance: No policy found for agent '{sender}'")
            return False

        if action == "send_message" and recipient:
            if "broadcast" in policy.allowed_recipients:
                return True
            return recipient in policy.allowed_recipients

        if action == "read_portfolio":
            return policy.can_read_portfolio

        if action == "trade":
            return policy.can_trade

        return False

    async def secure_dispatch(self, message: AgentMessage):
        """Dispatches a message only if authorized by policy."""
        if self.is_authorized(message.sender, "send_message", message.recipient):
            await self.messenger.send_message(message)
        else:
            logger.error(f"Governance: BLOCKED unauthorized message from '{message.sender}' to '{message.recipient}'")

# Singleton instance
_governance = None

def get_governance() -> GovernanceService:
    global _governance
    if _governance is None:
        _governance = GovernanceService()
    return _governance
