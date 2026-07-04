import mlx.core
import mlx.nn
import gc
import os
import sys
import time
import numpy as np
import pytest

# Ensure root and backend are in sys.path
root_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if root_path not in sys.path:
    sys.path.insert(0, root_path)
backend_path = os.path.join(root_path, "backend")
if backend_path not in sys.path:
    sys.path.append(backend_path)

from utils.mlx_loader import mx, nn, HAS_MLX

pytestmark = pytest.mark.skipif(not HAS_MLX, reason="MLX is not installed or available")

from backend.mlx.adapter_manager import MLXAdapterManager, REGIMES

class MockMLXModel(nn.Module):
    """Mock MLX model for testing in-memory parameter updates."""
    def __init__(self):
        super().__init__()
        self.w1 = mx.array([10.0, 20.0])
        self.w2 = mx.array([30.0, 40.0])

    def update(self, params):
        if 'w1' in params:
            self.w1 = params['w1']
        if 'w2' in params:
            self.w2 = params['w2']

def test_adapter_manager_preload_and_swap():
    model = MockMLXModel()
    
    # Initialize adapter manager with the model
    # It will auto-generate dummy weights since no paths are specified
    manager = MLXAdapterManager(model=model)
    
    # Verify weights were preloaded for all 3 GMM regimes
    assert len(manager.preloaded_weights) == 3
    for r_id in REGIMES:
        assert r_id in manager.preloaded_weights
        weights = manager.preloaded_weights[r_id]
        # In mock model, the keys are w1 and w2
        assert 'w1' in weights
        assert 'w2' in weights
        
        # Verify the dummy value logic (regime_id + 1.0)
        expected_val = float(r_id) + 1.0
        assert np.allclose(np.array(weights['w1']), expected_val)
        assert np.allclose(np.array(weights['w2']), expected_val)

    # Test swap to regime 0 (Quiet/Trend)
    success = manager.swap_adapter(0)
    assert success is True
    assert np.allclose(np.array(model.w1), 1.0)
    assert np.allclose(np.array(model.w2), 1.0)

    # Test swap to regime 1 (Choppy/Mean-Reverting)
    success = manager.swap_adapter(1)
    assert success is True
    assert np.allclose(np.array(model.w1), 2.0)
    assert np.allclose(np.array(model.w2), 2.0)

    # Test swap to regime 2 (Crisis/Liquidity)
    success = manager.swap_adapter(2)
    assert success is True
    assert np.allclose(np.array(model.w1), 3.0)
    assert np.allclose(np.array(model.w2), 3.0)

def test_adapter_manager_latency_benchmark():
    model = MockMLXModel()
    manager = MLXAdapterManager(model=model)
    
    # Run a warmup
    manager.swap_adapter(0)
    
    # Measure 100 swaps to check latency
    latencies = []
    for i in range(100):
        # Swap back and forth between regime 1 and 2
        r_id = 1 if i % 2 == 0 else 2
        
        t0 = time.perf_counter()
        success = manager.swap_adapter(r_id)
        t1 = time.perf_counter()
        
        assert success is True
        latencies.append((t1 - t0) * 1000.0) # in ms
        
    avg_latency = np.mean(latencies)
    max_latency = np.max(latencies)
    p95_latency = np.percentile(latencies, 95)
    
    print(f"\n⚡ Hot-Swap Latency (100 iterations):")
    print(f"  - Average: {avg_latency:.4f} ms")
    print(f"  - 95th Pct: {p95_latency:.4f} ms")
    print(f"  - Max: {max_latency:.4f} ms")
    
    # Strictly under 2.0 milliseconds
    assert avg_latency < 2.0, f"Average swap latency {avg_latency:.2f}ms exceeds the 2ms budget"
    assert p95_latency < 2.0, f"95th percentile swap latency {p95_latency:.2f}ms exceeds the 2ms budget"

def test_adapter_manager_no_gc_interference():
    model = MockMLXModel()
    manager = MLXAdapterManager(model=model)
    
    # Temporarily force GC to enable, verify it is handled properly
    gc.enable()
    
    # Run swap and verify GC is not triggered/affected
    was_enabled_before = gc.isenabled()
    success = manager.swap_adapter(0)
    was_enabled_after = gc.isenabled()
    
    assert success is True
    assert was_enabled_before == was_enabled_after
    assert was_enabled_after is True
