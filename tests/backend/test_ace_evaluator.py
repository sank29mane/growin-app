import pytest
from backend.agents.ace_evaluator import ACEEvaluator

def test_ace_scoring_logic():
    evaluator = ACEEvaluator()
    
    # Case 1: Instant Approval
    trace = [{"turn": 0, "status": "APPROVED", "refutation": ""}]
    score = evaluator.calculate_score(trace, "APPROVED")
    assert score == 1.0
    assert evaluator.get_robustness_label(score) == "BATTLE_TESTED"
    
    # Case 2: One Rebuttal then Approval
    trace = [
        {"turn": 0, "status": "FLAGGED", "refutation": "Too risky"},
        {"turn": 1, "status": "APPROVED", "refutation": "Resolved"}
    ]
    score = evaluator.calculate_score(trace, "APPROVED")
    # 1.0 - (1*0.1) + 0.05 bonus = 0.95
    assert abs(score - 0.95) < 0.001
    
    # Case 3: Failed Rebuttal (Blocked)
    trace = [
        {"turn": 0, "status": "FLAGGED", "refutation": "Too risky"},
        {"turn": 1, "status": "BLOCKED", "refutation": "Not addressed"}
    ]
    score = evaluator.calculate_score(trace, "BLOCKED")
    # (1.0 - 0.1) * 0.2 = 0.18
    print(f"DEBUG: Case 3 actual score: {score}")
    assert abs(score - 0.18) < 0.01
    assert evaluator.get_robustness_label(score) == "HIGH_ENTROPY"

if __name__ == "__main__":
    test_ace_scoring_logic()
    print("ACE Evaluator unit tests passed.")
