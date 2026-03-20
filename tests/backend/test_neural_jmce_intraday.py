import pytest
import numpy as np
import sys
import os

# Ensure backend is in path and resolvable
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../backend')))

from utils.jmce_model import NeuralJMCE, TimeResolution

try:
    import mlx.core as mx
except ImportError:
    mx = None

pytestmark = pytest.mark.skipif(mx is None, reason="mlx not available")

def test_neural_jmce_intraday_shapes():
    """Verify NeuralJMCE works with intraday resolutions and correct output shapes."""
    n_assets = 2 # TQQQ, SQQQ
    seq_len = 390 # 1-min day
    model = NeuralJMCE(
        n_assets=n_assets, 
        seq_len=seq_len, 
        resolution=TimeResolution.INTRADAY_1MIN
    )
    
    # Batch of 1, sequence of 390, 2 assets
    x = mx.random.normal((1, seq_len, n_assets))
    
    # 1. Standard call
    mu, L, V = model(x, return_velocity=True)
    
    assert mu.shape == (1, n_assets)
    assert L.shape == (1, n_assets, n_assets)
    assert V.shape == (1, n_assets, n_assets)
    
    # Verify Covariance matrix
    sigma = model.get_covariance(L)
    assert sigma.shape == (1, n_assets, n_assets)
    
    # Check Positive Definiteness (Eigenvalues > 0)
    # Convert to numpy for check
    sigma_np = np.array(sigma[0])
    eigvals = np.linalg.eigvals(sigma_np)
    assert np.all(eigvals > 0)

def test_neural_jmce_resolution_padding():
    """Verify positional embeddings are correctly sized for resolution."""
    model_5m = NeuralJMCE(n_assets=2, resolution=TimeResolution.INTRADAY_5MIN)
    assert model_5m.target_seq_len >= 78
    
    model_1m = NeuralJMCE(n_assets=2, resolution=TimeResolution.INTRADAY_1MIN)
    assert model_1m.target_seq_len >= 390
