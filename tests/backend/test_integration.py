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

from backend.mlx.adapter_manager import MLXAdapterManager
from backend.coreml.gmm_loader import load_gmm_params
from backend.trading_loop import LiveTradingLoop
from utils.mlx_loader import mx, nn, HAS_MLX

pytestmark = pytest.mark.skipif(not HAS_MLX, reason="MLX is not installed or available")

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

def test_live_trading_loop_integration():
    """
    Simulates 10k iterations of live market ticks, running them through:
    tick -> Online Features -> Welford standardization -> Numba GMM -> MLX Swap + Risk Scale
    Asserts end-to-end processing latency stays strictly under 2.5ms.
    """
    # 1. Load the real GMM parameters
    gmm_params = load_gmm_params()
    
    # 2. Setup Mock Model and Adapter Manager
    model = MockMLXModel()
    manager = MLXAdapterManager(model=model)
    
    # 3. Setup Live Trading Loop
    loop = LiveTradingLoop(model_manager=manager, gmm_params=gmm_params, alpha=0.05)
    
    # 4. Generate 10k simulated market ticks
    # We simulate a transition from quiet to choppy to crisis to match real GMM cluster means:
    # - Quiet Regime: vol ≈ 0.04, spread ≈ 0.06
    # - Choppy Regime: vol ≈ 0.10, spread ≈ 0.13
    # - Crisis Regime: vol ≈ 0.55, spread ≈ 0.50
    np.random.seed(42)
    n_ticks = 10000
    
    mid_prices = np.zeros(n_ticks)
    asks = np.zeros(n_ticks)
    bids = np.zeros(n_ticks)
    
    current_price = 100.0
    for i in range(n_ticks):
        # Determine regime group for simulation
        if i < 3000:
            # Quiet Regime
            vol = 0.04
            spread_pct = 0.06
        elif i < 7000:
            # Choppy Regime
            vol = 0.10
            spread_pct = 0.13
        else:
            # Crisis Regime
            vol = 0.55
            spread_pct = 0.50
            
        ret = np.random.normal(0, vol)
        current_price *= (1.0 + ret)
        spread = current_price * spread_pct
        
        mid_prices[i] = current_price
        asks[i] = current_price + spread / 2.0
        bids[i] = current_price - spread / 2.0
        
    # 5. Warm up JIT compilers
    # Run a few dummy ticks to compile Numba functions and warm up MLX adapters
    for i in range(50):
        loop.process_tick(mid_prices[i], asks[i], bids[i])
        
    # 6. Execute the benchmark loop over 10k ticks
    latencies = []
    regime_swaps = 0
    results = []
    
    # Record start time for the benchmark period (excluding warmup)
    start_time = time.perf_counter()
    for i in range(50, n_ticks):
        t0 = time.perf_counter()
        res = loop.process_tick(mid_prices[i], asks[i], bids[i])
        t1 = time.perf_counter()
        
        latencies.append((t1 - t0) * 1000.0) # Convert to ms
        results.append(res)
        if res["regime_changed"]:
            regime_swaps += 1
            
    end_time = time.perf_counter()
    
    total_time_ms = (end_time - start_time) * 1000.0
    avg_latency = np.mean(latencies)
    p95_latency = np.percentile(latencies, 95)
    max_latency = np.max(latencies)
    
    print(f"\n⚡ Integration Benchmark Details:")
    print(f"  - Total benchmark ticks: {n_ticks - 50}")
    print(f"  - Total time: {total_time_ms:.4f} ms")
    print(f"  - Avg latency per tick: {avg_latency:.4f} ms")
    print(f"  - 95th percentile latency: {p95_latency:.4f} ms")
    print(f"  - Max latency: {max_latency:.4f} ms")
    print(f"  - Total regime swaps: {regime_swaps}")
    
    # 8. Assertions
    # Latency constraint: strictly under 2.5ms
    assert avg_latency < 2.5, f"Average end-to-end latency {avg_latency:.2f}ms exceeds the 2.5ms budget"
    assert p95_latency < 2.5, f"95th percentile end-to-end latency {p95_latency:.2f}ms exceeds the 2.5ms budget"
    
    # Assert we observed regime switches during the simulation
    assert regime_swaps > 0, "No regime swaps were triggered during the simulation"
    
    # Assert dynamic risk scaling: risk leverage coefficient is scaled
    risk_coefficients = [r["risk_leverage_coefficient"] for r in results]
    assert max(risk_coefficients) > min(risk_coefficients), "Risk leverage did not dynamically scale"
    
    # Verify the bounds of leverage coefficients are respected
    for coef in risk_coefficients:
        assert 0.05 <= coef <= 1.0, f"Leverage coefficient {coef} out of bounds"
        
    print("✅ All integration assertions passed successfully!")
