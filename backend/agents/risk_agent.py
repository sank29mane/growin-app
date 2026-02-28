"""
Risk Agent - Governance and Compliance Critic for the Growin App MAS.
SOTA 2026: Implements the "Critic Pattern" to review and validate trade suggestions.
"""

import logging
import json
import re
from typing import Dict, Any, List, Optional
from decimal import Decimal

from .base_agent import BaseAgent, AgentResponse, AgentConfig
from market_context import MarketContext
from utils.financial_math import create_decimal

logger = logging.getLogger(__name__)

RISK_SYSTEM_PROMPT = """
You are the Risk Agent (The Critic). Your job is to audit trade recommendations for risk, compliance, and suitability.
In SOTA 2026 mode, you adopt "The Contrarian" persona: your primary goal is to find reasons why the proposed strategy is WRONG or DANGEROUS.

Review Criteria:
1. Exposure: Is the suggested trade too large for the account? (Max 5% per position).
2. Compliance: Ensure no prohibited instruments or wash-sale risks.
3. Contrarian Analysis: 
   - What tail-risk or geopolitical event (GPR) could break this thesis?
   - Is there a logic gap (e.g. ignoring a bearish EMA cross)?
   - Identify "Crowded Trade" scenarios where retail sentiment is dangerously high.

Output Format (JSON ONLY):
{
  "status": "APPROVED" | "FLAGGED" | "BLOCKED",
  "confidence_score": 0.0 to 1.0,
  "risk_assessment": "Short summary of risks",
  "compliance_notes": "Specific regulatory or rule-based notes",
  "recommendation_adjustment": "Suggested change if FLAGGED",
  "debate_refutation": "A sharp, adversarial argument challenging the core logic of the suggestion",
  "requires_hitl": true | false
}
"""

class RiskAgent(BaseAgent):
    """
    Critic Agent that reviews Orchestrator suggestions before they reach the user.
    Uses high-precision models (e.g. Claude or 8-bit AFFINE Granite) to ensure safety.
    """
    
    def __init__(self, model_name: str = "granite-tiny"):
        config = AgentConfig(name="RiskAgent", timeout=15.0)
        super().__init__(config)
        self.model_name = model_name
        self._llm = None

    async def _initialize(self):
        if self._llm:
            return
        from .llm_factory import LLMFactory
        self._llm = await LLMFactory.create_llm(self.model_name)

    async def analyze(self, context_dict: Dict[str, Any]) -> AgentResponse:
        """
        Main analysis method required by BaseAgent.
        context_dict should contain:
        - context: MarketContext
        - suggestion: str (the Orchestrator's response)
        """
        await self._initialize()
        
        # In this context, 'context' is the MarketContext object
        market_context: MarketContext = context_dict.get("context")
        suggestion: str = context_dict.get("suggestion", "")
        
        if not market_context:
            return AgentResponse(agent_name=self.config.name, success=False, data={}, error="Missing MarketContext", latency_ms=0)

        prompt = f"""
        [CONTEXT]
        Ticker: {market_context.ticker}
        Intent: {market_context.intent}
        Portfolio Value: £{market_context.portfolio.total_value if market_context.portfolio else "Unknown"}
        Cash: £{market_context.portfolio.cash_balance.get('total', 0) if market_context.portfolio else "Unknown"}
        
        [PROPOSED STRATEGY]
        {suggestion}
        
        Audit this strategy against our risk protocols.
        """
        
        try:
            from langchain_core.messages import SystemMessage, HumanMessage
            response = await self._llm.ainvoke([
                SystemMessage(content=RISK_SYSTEM_PROMPT),
                HumanMessage(content=prompt)
            ])
            
            content = response.content if hasattr(response, 'content') else str(response)
            
            # Extract JSON
            match = re.search(r'\{.*\}', content, re.DOTALL)
            if match:
                data = json.loads(match.group())
                # Enforce HITL for any trade-related suggestion
                if any(word in suggestion.upper() for word in ["BUY", "SELL", "ORDER", "TRADE"]):
                    data["requires_hitl"] = True
                
                return AgentResponse(
                    agent_name=self.config.name,
                    success=True,
                    data=data,
                    latency_ms=0 # Will be updated by BaseAgent.execute
                )
            else:
                return AgentResponse(agent_name=self.config.name, success=False, data={}, error="Failed to parse Risk Agent output", latency_ms=0)
                
        except Exception as e:
            logger.error(f"RiskAgent failed: {e}")
            return AgentResponse(agent_name=self.config.name, success=False, data={}, error=str(e), latency_ms=0)

    async def review(self, context: MarketContext, suggestion: str) -> Dict[str, Any]:
        """Convenience method for Orchestrator integration"""
        res = await self.execute({"context": context, "suggestion": suggestion})
        if res.success:
            return res.data
        return {
            "status": "FLAGGED",
            "confidence_score": 0.0,
            "risk_assessment": f"Risk Agent Error: {res.error}",
            "requires_hitl": True
        }
