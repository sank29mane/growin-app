import torch
import numpy as np
from typing import Dict, Any, List, Optional, Tuple
from datetime import datetime, timedelta
from backend.models.neural_ode import RecoveryODE

class DividendOptimizationAgent:
    """
    Core agent for dividend capture optimization.
    Enforces CB-APM consensus and handles dynamic order execution strategies.
    """
    def __init__(self, config: Dict[str, Any] = None):
        self.config = config or {}
        # Consensus-Bottleneck Adaptive Probability Modeling (CB-APM) weights
        self.weights = {
            'ttm_r2': 0.40,
            'xgboost': 0.35,
            'monte_carlo': 0.25
        }
        # Thresholds from Plan 21-02
        self.min_confidence = 40.0  # 40%
        self.drawdown_buffer = 0.05 # 5%
        
        # Neural ODE for recovery modeling
        # Input features might include technical indicators, sector volatility, etc.
        self.recovery_model = RecoveryODE(input_dim=16, hidden_dim=32)
        
    def calculate_consensus(self, model_outputs: Dict[str, float]) -> float:
        """
        Synthesizes signals from TTM-R2, XGBoost, and Monte Carlo.
        Returns a confidence score [0, 100].
        """
        weighted_sum = 0.0
        total_weight = 0.0
        
        for model, weight in self.weights.items():
            if model in model_outputs:
                weighted_sum += model_outputs[model] * weight
                total_weight += weight
        
        if total_weight == 0:
            return 0.0
            
        # Assuming model outputs are probabilities [0, 1]
        consensus_prob = weighted_sum / total_weight
        return consensus_prob * 100.0

    def evaluate_execution_risk(self, 
                                 consensus_score: float, 
                                 expected_drawdown: float, 
                                 dividend_amount: float, 
                                 stock_price: float) -> Tuple[bool, Optional[str]]:
        """
        Gauge & Abort logic: Return (should_abort, reason).
        Abort if:
        1. Confidence (consensus_score) < 40%
        2. Expected Drawdown > Dividend + 5% of stock price
        """
        div_percent = (dividend_amount / stock_price) if stock_price > 0 else 0
        abort_drawdown_threshold = div_percent + self.drawdown_buffer
        
        if consensus_score < self.min_confidence:
            return True, f"Confidence {consensus_score:.1f}% below threshold {self.min_confidence}%"
            
        if expected_drawdown > abort_drawdown_threshold:
            return True, f"Drawdown {expected_drawdown:.2%} exceeds dividend+buffer {abort_drawdown_threshold:.2%}"
            
        return False, None

    def determine_order_strategy(self, 
                                  current_time: datetime, 
                                  ex_div_date: datetime) -> str:
        """
        Hybrid Order Strategy Logic:
        - Phase A: Limit Orders (2-3 days prior)
        - Phase B: Market Orders (< 2 hours before cutoff)
        """
        time_to_exdiv = ex_div_date - current_time
        
        if time_to_exdiv > timedelta(days=3):
            return "WAIT"
        elif time_to_exdiv > timedelta(hours=2):
            return "LIMIT_ORDER_PHASE_A"
        elif time_to_exdiv > timedelta(seconds=0):
            return "MARKET_ORDER_PHASE_B"
        else:
            return "EX_DIVIDEND_PASSED"

    def predict_recovery_velocity(self, features: torch.Tensor) -> float:
        """
        Uses the Neural ODE model to predict post-dividend price recovery velocity.
        """
        with torch.no_grad():
            velocity = self.recovery_model(features)
        return float(velocity.item())

    def process_opportunity(self, 
                            ticker: str,
                            model_outputs: Dict[str, float],
                            dividend_amount: float,
                            stock_price: float,
                            expected_drawdown: float,
                            ex_div_date: datetime,
                            features: torch.Tensor) -> Dict[str, Any]:
        """
        Main entry point for processing a dividend capture opportunity.
        """
        consensus_score = self.calculate_consensus(model_outputs)
        should_abort, reason = self.evaluate_execution_risk(
            consensus_score, expected_drawdown, dividend_amount, stock_price
        )
        
        if should_abort:
            return {
                "ticker": ticker,
                "status": "ABORTED",
                "reason": reason,
                "consensus_score": consensus_score
            }
            
        current_time = datetime.now()
        strategy = self.determine_order_strategy(current_time, ex_div_date)
        recovery_velocity = self.predict_recovery_velocity(features)
        
        return {
            "ticker": ticker,
            "status": "APPROVED",
            "consensus_score": consensus_score,
            "order_strategy": strategy,
            "predicted_recovery_velocity": recovery_velocity,
            "timestamp": current_time.isoformat()
        }

if __name__ == "__main__":
    # Quick test for Task 2/3 verification
    agent = DividendOptimizationAgent()
    
    # Mock signals
    signals = {
        'ttm_r2': 0.8,
        'xgboost': 0.7,
        'monte_carlo': 0.6
    }
    
    score = agent.calculate_consensus(signals)
    print(f"Consensus Score: {score:.2f}")
    assert score > 70
    
    # Mock Risk check (High drawdown)
    abort, reason = agent.evaluate_execution_risk(score, 0.15, 0.05, 1.0)
    print(f"Abort high drawdown: {abort}, Reason: {reason}")
    assert abort is True
    
    # Mock Strategy check
    now = datetime.now()
    exdiv = now + timedelta(hours=1)
    strategy = agent.determine_order_strategy(now, exdiv)
    print(f"Strategy 1h before: {strategy}")
    assert strategy == "MARKET_ORDER_PHASE_B"
    
    print("Task 2 & 3 verification PASSED")
