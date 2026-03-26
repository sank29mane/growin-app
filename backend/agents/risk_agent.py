"""
Risk Agent - Governance and Compliance Critic for the Growin App MAS.
SOTA 2026: Implements the "Critic Pattern" to review and validate trade suggestions.
"""

import logging
import json
import re
import asyncio
from typing import Dict, Any, List, Optional, Literal
from decimal import Decimal
from pydantic import BaseModel, Field
from magentic import prompt as mag_prompt

from .base_agent import BaseAgent, AgentResponse, AgentConfig
from market_context import MarketContext
from utils.financial_math import create_decimal

logger = logging.getLogger(__name__)

class RiskAssessment(BaseModel):
    """Structured risk audit output from the Critic."""
    status: Literal["APPROVED", "FLAGGED", "BLOCKED"] = Field(..., description="Overall risk status")
    confidence_score: float = Field(..., ge=0, le=1, description="Confidence in the risk assessment (0.0 to 1.0)")
    risk_assessment: str = Field(..., description="Short summary of identified risks")
    compliance_notes: str = Field(..., description="Specific regulatory or rule-based notes (e.g., Wash Sale)")
    recommendation_adjustment: str = Field(..., description="Suggested change if FLAGGED or BLOCKED")
    debate_refutation: str = Field(..., description="A sharp, adversarial argument challenging the core logic of the suggestion")
    requires_hitl: bool = Field(default=False, description="Whether human-in-the-loop approval is mandatory")

@mag_prompt(
    "Perform a professional financial risk audit as 'The Critic'.\n"
    "Market Context:\n"
    "- Ticker: {ticker}\n"
    "- Intent: {intent}\n"
    "- Portfolio Value: £{portfolio_value}\n"
    "- Wash Sale Risk Alert: {wash_sale_alert}\n\n"
    "Proposed Strategy/Action:\n"
    "{suggestion}\n\n"
    "Risk Protocols:\n"
    "{protocols}\n\n"
    "Audit this strategy and return a structured RiskAssessment."
)
def conduct_risk_audit(ticker: str, intent: str, portfolio_value: str, wash_sale_alert: bool, suggestion: str, protocols: str) -> RiskAssessment:
    ...

RISK_SYSTEM_PROMPT = """
You are the Risk Agent (The Critic). Your job is to audit trade recommendations for risk, compliance, and suitability.
In SOTA 2026 mode, you adopt "The Contrarian" persona: your primary goal is to find reasons why the proposed strategy is WRONG or DANGEROUS.

Review Criteria:
1. Exposure: Is the suggested trade too large for the account? (Max 5% per position).
2. Compliance: Ensure no prohibited instruments or wash-sale risks.
3. Wash Sale Protection (SOTA 2026):
   - Block 'BUY' orders for tickers sold for a loss in the last 30 days.
   - Applies specifically to 'Invest' (taxable) accounts.
4. Contrarian Analysis: 
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
    Uses high-precision models and magentic for structured Pydantic outputs.
    """
    
    def __init__(self, model_name: str = "granite-tiny"):
        config = AgentConfig(name="RiskAgent", timeout=15.0)
        super().__init__(config)
        self.model_name = model_name

    async def analyze(self, context_dict: Dict[str, Any]) -> AgentResponse:
        """
        Main analysis method required by BaseAgent.
        SOTA 2026: Agentic Risk Audit via Magentic.
        """
        # In this context, 'context' is the MarketContext object
        market_context: MarketContext = context_dict.get("context")
        suggestion: str = context_dict.get("suggestion", "")
        
        if not market_context:
            return AgentResponse(agent_name=self.config.name, success=False, data={}, error="Missing MarketContext", latency_ms=0)

        # SOTA 2026: Wash Sale Detection logic
        wash_sale_alert = False
        if any(word in suggestion.upper() for word in ["BUY", "LONG"]):
            recent_trades = market_context.user_context.get("recent_trades", [])
            for trade in recent_trades:
                if trade.get("ticker") == market_context.ticker and trade.get("side") == "SELL" and trade.get("pnl", 0) < 0:
                    wash_sale_alert = True
                    break

        try:
            # Execute structured audit via Magentic
            portfolio_val = market_context.portfolio.total_value if market_context.portfolio else "Unknown"
            
            audit_result = await asyncio.to_thread(
                conduct_risk_audit,
                market_context.ticker,
                market_context.intent,
                str(portfolio_val),
                wash_sale_alert,
                suggestion,
                RISK_SYSTEM_PROMPT
            )
            
            data = audit_result.model_dump()
                
            # Enforce HITL for any trade-related suggestion
            if any(word in suggestion.upper() for word in ["BUY", "SELL", "ORDER", "TRADE"]):
                data["requires_hitl"] = True
            else:
                # If not a trade, use the LLM's assessment of HITL need
                pass
                
            return AgentResponse(
                agent_name=self.config.name,
                success=True,
                data=data,
                latency_ms=0 # Managed by execution loop
            )
                
        except Exception as e:
            logger.error(f"RiskAgent (Magentic) failed: {e}")
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
