import pytest
import numpy as np
from decimal import Decimal
from backend.utils.portfolio_analyzer import PortfolioAnalyzer
from backend.utils.jmce_model import NeuralJMCE

@pytest.mark.asyncio
async def test_optimize_weights_caps():
    """Verify that no weight exceeds the 10% hard cap."""
    n_assets = 20 # Need at least 10 to satisfy 10% cap with sum=1.0
    seq_len = 180
    
    # Generate random returns
    # Make one asset have very high returns to test capping
    returns = np.random.normal(0.0001, 0.01, (seq_len, n_assets))
    returns[:, 0] = returns[:, 0] + 0.1 # Asset 0 is super bullish
    
    analyzer = PortfolioAnalyzer(n_assets=n_assets)
    
    # Mock macro signals
    signals = {"vix": 18.0, "tnx": 4.0}
    
    weights = await analyzer.optimize_weights(returns, signals, persona='aggressive')
    
    # Assertions
    assert len(weights) == n_assets
    
    # Sum of weights should be approx 1.0
    total_w = sum(float(w) for w in weights)
    assert total_w == pytest.approx(1.0, abs=1e-3)
    
    # No weight should exceed 0.10
    for i, w in enumerate(weights):
        assert float(w) <= 0.10001, f"Weight for asset {i} ({w}) exceeds 10% cap"

@pytest.mark.asyncio
async def test_optimize_weights_defensive():
    """Verify defensive persona minimizes volatility."""
    n_assets = 15
    seq_len = 180
    
    # Asset 0 is very volatile, Asset 1 is stable
    returns = np.random.normal(0.0001, 0.01, (seq_len, n_assets))
    returns[:, 0] = returns[:, 0] * 10 # 10x volatility
    returns[:, 1] = returns[:, 1] * 0.1 # 0.1x volatility
    
    analyzer = PortfolioAnalyzer(n_assets=n_assets)
    signals = {"vix": 18.0, "tnx": 4.0}
    
    # Defensive mode (Min Vol)
    weights = await analyzer.optimize_weights(returns, signals, persona='defensive')
    
    # Check that it returns valid weights
    assert len(weights) == n_assets
    assert sum(float(w) for w in weights) == pytest.approx(1.0, abs=1e-3)
    
    # Still must obey caps
    for w in weights:
        assert float(w) <= 0.10001
