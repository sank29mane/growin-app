"""
Risk Agent - Governance and Compliance Critic for the Growin App MAS.
SOTA 2026: Implements the "Critic Pattern" to review and validate trade suggestions.
"""

import logging
import json
import re
import asyncio
from utils.error_handler import handle_error
from typing import Dict, Any, List, Optional, Literal
from decimal import Decimal
from pydantic import BaseModel, Field
from magentic import prompt as mag_prompt

from .critic_binding import clear_review, proposal_identity, _bind_successful_review
from .base_agent import BaseAgent, AgentResponse, AgentConfig
from market_context import MarketContext, RiskGovernanceData
from utils.financial_math import create_decimal
from model_registry import ROLE_RISK_CRITIC, ModelRegistryError, ProviderError, active_registry_or_none
from model_registry.provider import run_magentic

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
    "Liquidity and Risk Governance (structured figures from the market context):\n"
    "{risk_governance}\n\n"
    "Proposed Strategy/Action:\n"
    "{suggestion}\n\n"
    "Risk Protocols:\n"
    "{protocols}\n\n"
    "Audit this strategy and return a structured RiskAssessment."
)
def conduct_risk_audit(ticker: str, intent: str, portfolio_value: str, wash_sale_alert: bool, risk_governance: str, suggestion: str, protocols: str) -> RiskAssessment:
    ...


_UNAVAILABLE = "unavailable"


def _figure(value: Optional[Decimal]) -> str:
    """A figure as plain text; an unset one is 'unavailable', never zero."""
    if value is None:
        return _UNAVAILABLE
    try:
        return format(Decimal(value), "f")
    except Exception:
        return str(value)


def _category(risk_governance: RiskGovernanceData, name: str) -> str:
    """An enum-like field as text, or 'unavailable' when nobody ever set it.

    ``liquidity_status`` and ``systemic_risk_level`` default to STABLE and LOW on the
    model, so the value alone cannot tell "computed as STABLE" from "never computed".
    Pydantic's ``model_fields_set`` can: it holds exactly the fields that were passed in
    or assigned. A copy that re-feeds every field (``model_dump()`` without
    ``exclude_unset``) marks them all as set, so callers that rebuild the object must
    keep the set intact.
    """
    if name not in risk_governance.model_fields_set:
        return _UNAVAILABLE
    value = getattr(risk_governance, name)
    return str(value) if value else _UNAVAILABLE


def format_risk_governance(risk_governance: Optional[RiskGovernanceData]) -> str:
    """The five liquidity figures the critic is shown (P-17).

    Text only. The critic is advisory; the deterministic gate is the slippage check
    in ``risk_india.rules`` and admission, not this prompt. A figure that was never
    computed reads 'unavailable', for the enum fields as much as the numeric ones.
    """
    if risk_governance is None:
        return (
            f"- slippage_bps, liquidity_status, pov_participation, adv_30d, systemic_risk_level: {_UNAVAILABLE} "
            "(not computed for this request; an unavailable figure is not zero)"
        )
    return "\n".join(
        (
            f"- slippage_bps: {_figure(risk_governance.slippage_bps)}",
            f"- liquidity_status: {_category(risk_governance, 'liquidity_status')}",
            f"- pov_participation: {_figure(risk_governance.pov_participation)}",
            f"- adv_30d: {_figure(risk_governance.adv_30d)}",
            f"- systemic_risk_level: {_category(risk_governance, 'systemic_risk_level')}",
        )
    )

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
    
    def __init__(self):
        config = AgentConfig(name="RiskAgent", timeout=15.0)
        super().__init__(config)

    @property
    def model_name(self) -> Optional[str]:
        """The risk_critic role's model id for lineage display, or None."""
        registry = active_registry_or_none()
        if registry is None or not registry.has_role(ROLE_RISK_CRITIC):
            return None
        return registry.resolve(ROLE_RISK_CRITIC).model

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
            
            audit_result = await run_magentic(
                ROLE_RISK_CRITIC,
                conduct_risk_audit,
                market_context.ticker,
                market_context.intent,
                str(portfolio_val),
                wash_sale_alert,
                format_risk_governance(market_context.risk_governance),
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
                
        except (ModelRegistryError, ProviderError):
            raise
        except Exception as e:


            handle_error(e, "RiskAgent (Magentic) failed", logger, raise_error=False)
            return AgentResponse(agent_name=self.config.name, success=False, data={}, error=str(e), latency_ms=0)

    async def review(self, context: MarketContext, suggestion: str) -> Dict[str, Any]:
        """Convenience method for Orchestrator integration"""
        clear_review(context)
        proposal = context.user_context.get("deferred_proposal")
        if proposal:
            # Bind the server-stamped workspace too; registration must not change
            # the reviewed payload when the ledger supplies this field.
            from app_context import state
            if state._execution_ledger is not None and "workspace" not in proposal:
                proposal["workspace"] = state._execution_ledger.workspace.value
        identity = proposal_identity(proposal) if proposal else None
        res = await self.execute({"context": context, "suggestion": suggestion})
        if not res.success:
            raise ProviderError("CRITIC_OUTPUT_INVALID", ROLE_RISK_CRITIC)
        try:
            review = RiskAssessment.model_validate(res.data).model_dump()
        except (ValueError, TypeError):
            raise ProviderError("CRITIC_OUTPUT_INVALID", ROLE_RISK_CRITIC) from None
        if identity is not None:
            if context.user_context.get("deferred_proposal") is not proposal or proposal_identity(proposal) != identity:
                raise ModelRegistryError("RISK_REVIEW_REQUIRED", ROLE_RISK_CRITIC)
            _bind_successful_review(context, identity)
        return review
