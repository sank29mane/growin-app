import pytest
from datetime import datetime, timedelta
import torch
from backend.agents.dividend_agent import DividendOptimizationAgent

def test_dividend_agent_consensus():
    agent = DividendOptimizationAgent()
    signals = {
        'ttm_r2': 0.8,
        'xgboost': 0.7,
        'monte_carlo': 0.6
    }
    # (0.8 * 0.40) + (0.7 * 0.35) + (0.6 * 0.25) = 0.32 + 0.245 + 0.15 = 0.715
    score = agent.calculate_consensus(signals)
    assert score == pytest.approx(71.5)

def test_dividend_agent_abort_low_confidence():
    agent = DividendOptimizationAgent()
    # (0.3 * 0.4) + (0.3 * 0.35) + (0.3 * 0.25) = 0.3 = 30%
    signals = {'ttm_r2': 0.3, 'xgboost': 0.3, 'monte_carlo': 0.3}
    score = agent.calculate_consensus(signals)
    abort, reason = agent.evaluate_execution_risk(score, 0.02, 0.05, 1.0)
    assert abort is True
    assert "Confidence" in reason

def test_dividend_agent_abort_high_drawdown():
    agent = DividendOptimizationAgent()
    # Consensus OK (80%)
    # Drawdown 12% > (Div 5% + Buffer 5%) = 10%
    abort, reason = agent.evaluate_execution_risk(80.0, 0.12, 0.05, 1.0)
    assert abort is True
    assert "Drawdown" in reason

def test_dividend_agent_order_strategy():
    agent = DividendOptimizationAgent()
    now = datetime.now()
    
    # Phase A: 2 days before
    strategy_a = agent.determine_order_strategy(now, now + timedelta(days=2))
    assert strategy_a == "LIMIT_ORDER_PHASE_A"
    
    # Phase B: 1 hour before
    strategy_b = agent.determine_order_strategy(now, now + timedelta(hours=1))
    assert strategy_b == "MARKET_ORDER_PHASE_B"
    
    # Wait: 4 days before
    strategy_wait = agent.determine_order_strategy(now, now + timedelta(days=4))
    assert strategy_wait == "WAIT"

def test_dividend_agent_processing():
    agent = DividendOptimizationAgent()
    now = datetime.now()
    ex_div = now + timedelta(hours=1)
    
    model_outputs = {'ttm_r2': 0.9, 'xgboost': 0.9, 'monte_carlo': 0.9}
    features = torch.randn(1, 16)
    
    result = agent.process_opportunity(
        ticker="AAPL",
        model_outputs=model_outputs,
        dividend_amount=0.25,
        stock_price=200.0,
        expected_drawdown=0.01,
        ex_div_date=ex_div,
        features=features
    )
    
    assert result["status"] == "APPROVED"
    assert result["order_strategy"] == "MARKET_ORDER_PHASE_B"
    assert "predicted_recovery_velocity" in result
