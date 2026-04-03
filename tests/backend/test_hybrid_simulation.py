import pytest
import numpy as np
import time
from decimal import Decimal
from quant_engine import QuantEngine, SimulationEngine

def test_monte_carlo_path_generation():
    engine = SimulationEngine()
    spot, vol, drift = 100.0, 0.2, 0.05
    steps, paths = 30, 1000
    
    start_time = time.time()
    paths_data = engine.run_monte_carlo(spot, vol, drift, steps, paths)
    end_time = time.time()
    
    assert paths_data.shape == (paths, steps)
    # Average final price should be around spot * exp(drift * T)
    # For T = 30/252 = 0.119
    final_prices = paths_data[:, -1]
    avg_final = np.mean(final_prices)
    assert 90.0 < avg_final < 110.0
    print(f"Simulation took {end_time - start_time:.4f} seconds")

def test_tail_loss_overlay():
    engine = SimulationEngine()
    # Create fake paths with some negative outcomes
    paths = np.random.normal(100, 5, (1000, 30))
    metrics = engine.predict_tail_loss_overlay(paths)
    
    assert "var_95" in metrics
    assert "cvar_95" in metrics
    assert "ml_adjusted_cvar" in metrics
    assert isinstance(metrics["ml_adjusted_cvar"], Decimal)

def test_quant_engine_stress_test():
    engine = QuantEngine()
    metrics = engine.simulate_stress_test(100.0, 0.2, 0.05, steps=10, paths=1000)
    
    assert "cvar_95" in metrics
    assert metrics["cvar_95"] is not None
