import pytest
import numpy as np

try:
    import mlx.core as mx
    HAS_MLX = True
except ImportError:
    mx = None
    HAS_MLX = False
    pytest.skip("MLX not available", allow_module_level=True)

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../backend')))

from backend.app_context import SplitBrainController
from backend.agents.rl_policy import create_policy

def test_split_brain_heavy_routing():
    """Verify reasoning tasks route to the GPU context engine"""
    controller = SplitBrainController()
    assert controller.route_task('REASONING') == 'VLLMMXEngine'
    assert controller.route_task('TRAINING') == 'VLLMMXEngine'
    assert controller.route_task('POLICY') == 'VLLMMXEngine'

def test_split_brain_light_routing():
    """Verify tool/eval tasks route to the Ollama CPU coordination engine"""
    controller = SplitBrainController()
    assert controller.route_task('COORDINATION') == 'Ollama'
    assert controller.route_task('TOOL_USE') == 'Ollama'
    assert controller.route_task('NEWS') == 'Ollama'

def test_unified_policy_scaling():
    """Verify output clipping and GBX scaling logic for RLPolicy."""
    policy = create_policy(n_assets=2)
    dummy_state = mx.random.normal((1, 64))
    
    # 1. Check valid output bounds
    weights, _ = policy(dummy_state)
    weights_np = np.array(weights)
    assert (weights_np >= 0.0).all() and (weights_np <= 1.0).all(), "Policy unclipped!"
    
    # 2. Check GBX vs GBP Scaling
    dummy_weights = mx.array([[0.5, 0.5]]) # 50/50 allocation
    capital_gbp = 1000.0 # £1000
    prices_gbx = mx.array([[5000.0, 2000.0]]) # 5000p = £50, 2000p = £20
    
    # Capital per asset = £500
    # Asset 1 = £500 / £50 = 10 shares
    # Asset 2 = £500 / £20 = 25 shares
    shares = policy.scale_for_gbx(weights=dummy_weights, capital_gbp=capital_gbp, ticker_prices_gbx=prices_gbx)
    shares_np = np.array(shares)
    
    assert np.allclose(shares_np, [[10.0, 25.0]]), f"Shares scaling resulted in {shares_np}"
