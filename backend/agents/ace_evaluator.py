"""
ACE Evaluator - Adversarial Confidence Estimation logic.
Formalizes the scoring of agentic debates using a weighted robustness model.
"""

import logging
import json
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

class ACEEvaluator:
    """
    Evaluates the quality and robustness of an adversarial debate.
    Produces an ACE score (0.0 to 1.0) based on how well the primary agent
    defended its thesis against the critic.
    """
    
    def __init__(self, base_score: float = 1.0):
        self.base_score = base_score
        
    def calculate_score(self, debate_trace: List[Dict[str, Any]], final_risk_status: str, risk_governance: Optional[Any] = None) -> float:
        """
        Calculate robustness score.
        
        Logic:
        - Starts at base_score (1.0).
        - Takes a penalty for each turn required to resolve a critique (-0.1 per turn).
        - Takes a massive penalty if the final status is still FLAGGED or BLOCKED.
        - High weight given to 'Addressing' a high-risk refutation.
        - SOTA 2026: Institutional Macro Multipliers (VIX, Spread).
        """
        score = self.base_score
        
        if not debate_trace:
            # If no debate happened, but status is not approved, start with lower score
            if final_risk_status != "APPROVED":
                score = 0.5
        else:
            # turn penalties
            num_rebuttals = len(debate_trace) - 1
            score -= (num_rebuttals * 0.1)
            
            # Outcome penalty
            if final_risk_status == "BLOCKED":
                score *= 0.2
            elif final_risk_status == "FLAGGED":
                score *= 0.6
                
            # Analysis of defense quality (simplified heuristic for now)
            import re
            for turn in debate_trace:
                refutation = turn.get("refutation") or ""
                refutation = refutation.lower()
                # Use word boundaries and check for negation
                if re.search(r'\b(addressed|resolved|fixed)\b', refutation):
                    if not re.search(r'\b(not|never|un|failed to)\b', refutation):
                        score += 0.05 # Bonus for resolution
            
        # SOTA 2026: Macro Penalties (Always applied)
        if risk_governance:
            # VIX Penalty
            vix = risk_governance.vix_level or 20.0
            if float(vix) > 30:
                score *= 0.8
            elif float(vix) > 22:
                score *= 0.9
                
            # Yield Spread Penalty (10y-3m inversion)
            spread = risk_governance.yield_spread_10y2y or 1.0
            if float(spread) < 0:
                score *= 0.8 # Recessionary risk
            elif float(spread) < 0.5:
                score *= 0.95
                
        return max(0.0, min(1.0, score))

    def get_robustness_label(self, score: float) -> str:
        if score >= 0.85: return "BATTLE_TESTED"
        if score >= 0.70: return "VERIFIED"
        if score >= 0.50: return "CAUTIONARY"
        return "HIGH_ENTROPY"
