import pytest
import numpy as np
from decimal import Decimal
from utils.risk_engine import RiskEngine

def test_calculate_cvar_95_simple():
    # Simple returns with a fat tail at the end
    # Total 20 returns: one is -0.10 (5th percentile is this value)
    returns = [0.01] * 19 + [-0.10]
    
    cvar = RiskEngine.calculate_cvar_95(returns)
    
    # In this case, -0.10 is the only value in the 5% tail
    # absolute value should be 0.10
    assert float(cvar) == pytest.approx(0.10)

def test_calculate_cvar_95_multiple_tail_values():
    # Total 40 returns: 5% is 2 values
    # Worst 2 values are -0.12 and -0.08
    returns = [0.01] * 38 + [-0.12, -0.08]
    
    cvar = RiskEngine.calculate_cvar_95(returns)
    
    # CVaR is mean of tail losses: (-0.12 + -0.08) / 2 = -0.10
    # Abs value is 0.10
    assert float(cvar) == pytest.approx(0.10)

def test_calculate_cvar_95_empty():
    assert float(RiskEngine.calculate_cvar_95([])) == 0.0

def test_calculate_volatility():
    # Std dev of [0.01, 0.01, 0.01] is 0
    returns = [0.01, 0.01, 0.01]
    vol = RiskEngine.calculate_volatility(returns, annualized=False)
    assert float(vol) == 0.0
    
    # Annualized vol of [0.01, -0.01]
    returns = [0.01, -0.01]
    vol = RiskEngine.calculate_volatility(returns, annualized=True)
    # std([0.01, -0.01]) = 0.01
    # 0.01 * sqrt(252) approx 0.1587
    assert float(vol) == pytest.approx(0.1587, abs=1e-3)

def test_market_shock_reproduction():
    """Verify CVaR captures fat-tail average loss correctly using 2020-like data."""
    # Simulating a quiet period (90 days) followed by a crash (10 days)
    quiet_days = list(np.random.normal(0.0005, 0.01, 90)) # 1% daily vol
    crash_days = [-0.03, -0.05, -0.12, -0.08, -0.02, 0.01, -0.04, -0.06, -0.03, -0.01] # Heavy losses
    
    total_returns = quiet_days + crash_days # 100 days
    
    # 5% of 100 days = 5 worst days
    # Worst 5 are likely from the crash: -0.12, -0.08, -0.06, -0.05, -0.04
    # Mean of these = (-0.12 - 0.08 - 0.06 - 0.05 - 0.04) / 5 = -0.07
    
    cvar = RiskEngine.calculate_cvar_95(total_returns)
    
    # It should be significantly higher than simple volatility
    # std is approx 0.03
    vol = RiskEngine.calculate_volatility(total_returns, annualized=False)
    
    print(f"Daily Vol: {vol}, CVaR: {cvar}")
    assert float(cvar) > float(vol)
    assert float(cvar) >= 0.05 # CVaR should capture the severity
