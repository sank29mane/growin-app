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
2. Liquidity (Institutional SOTA 2026):
   - Flag orders > 1% of 30-day Average Daily Volume (ADV).
   - High Market Impact leads to "FLAGGED" status.
3. Institutional Macro Triggers (REFINED Phase 26):
   - High VIX (>30) or Inverted Yield Spread (<0) should NOT block short-term volatility plays (like shorting or inverse ETFs). 
   - These environments are high-ROI for active traders but dangerous for long-only investors. 
   - Distinction: "BLOCKED" for long-only medium/long horizon; "APPROVED with Warning" for short-term setups.
4. Trade Horizon Calibration:
   - Adjust exposure checks based on horizon: 
     - Short (Intraday): Higher tolerance for volatility, smaller size relative to ADV.
     - Medium (Swing): 1:3 RR mandate.
     - Long (Invest): Focus on fundamental stability.
5. Compliance: Ensure no prohibited instruments or wash-sale risks.
6. Multi-Asset Risk Considerations (Phase 29):
   - CRYPTO: Extremely high volatility. Adjust position sizing drastically down. Flag any exposure > 2% of portfolio.
   - OPTIONS: Time decay (Theta) and leveraged exposure. Ensure the trade horizon matches the option expiry. Block naked options.
   - FX: High leverage. Verify that stop-losses are explicitly defined to prevent catastrophic margin calls.
7. Wash Sale Protection (SOTA 2026):
   - Block 'BUY' orders for tickers sold for a loss in the last 30 days.
   - Applies specifically to 'Invest' (taxable) accounts.
8. Contrarian Analysis: 

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

        # SOTA 2026: Institutional Risk Logic
        risk_governance = market_context.risk_governance
        vix = risk_governance.vix_level if (risk_governance and risk_governance.vix_level is not None) else Decimal("20")
        spread = risk_governance.yield_spread_10y2y if (risk_governance and risk_governance.yield_spread_10y2y is not None) else Decimal("1.0")
        adv = risk_governance.adv_30d if (risk_governance and risk_governance.adv_30d is not None) else Decimal("0")
        
        # Heuristic to extract order size from suggestion (e.g. "Buy 100 shares")
        order_size = Decimal("0")
        size_match = re.search(r'(BUY|SELL|ORDER)\s+(\d+)', suggestion, re.IGNORECASE)
        if size_match:
            order_size = create_decimal(size_match.group(2))
            
        liq_impact = Decimal("0")
        if adv > 0 and order_size > 0:
            liq_impact = (order_size / adv) * 100 # Percentage of ADV
            if risk_governance:
                risk_governance.liquidity_impact = liq_impact

        # SOTA 2026: Wash Sale Detection logic
        wash_sale_alert = False
        if any(word in suggestion.upper() for word in ["BUY", "LONG"]):
            # Check for recent loss sales in telemetry or context
            # In a real system, we'd query the MCP for 'get_historical_orders'
            recent_trades = market_context.user_context.get("recent_trades", [])
            for trade in recent_trades:
                if trade.get("ticker") == market_context.ticker and trade.get("side") == "SELL" and trade.get("pnl", 0) < 0:
                    # Potential wash sale detected
                    wash_sale_alert = True
                    break

        prompt = f"""
        [CONTEXT]
        Ticker: {market_context.ticker}
        Asset Type: {market_context.price.asset_type.value if market_context.price and hasattr(market_context.price, 'asset_type') and hasattr(market_context.price.asset_type, 'value') else "EQUITY"}
        Portfolio Value: £{market_context.portfolio.total_value if market_context.portfolio else "Unknown"}
        Trade Horizon: {market_context.trade_horizon} (SOTA 2026 Phase 26 Calibration)
        
        [MACRO RISK]
        VIX: {vix}
        Yield Spread (10Y-3M): {spread}
        ADV (30d): {adv}
        Est. Liquidity Impact: {liq_impact:.4f}% of ADV
        Est. Slippage: {risk_governance.slippage_bps if risk_governance else 0} bps
        Liquidity Status: {risk_governance.liquidity_status if risk_governance else "UNKNOWN"}
        Systemic Risk Level: {risk_governance.systemic_risk_level if risk_governance else "NORMAL"}
        
        [PROPOSED STRATEGY]
        {suggestion}
        
        Audit this strategy against our risk protocols. If VIX > 30 or Spread < 0, you MUST allow short-term volatility plays but be extremely skeptical of long-only medium/long term entries. Block trades where estimated slippage > 1% (100 bps).
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
