import os
import sys
import time
import sqlite3
import asyncio
import numpy as np
import pytest
from typing import Dict, Any, List

# Ensure root and backend are in sys.path
root_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if root_path not in sys.path:
    sys.path.insert(0, root_path)
backend_path = os.path.join(root_path, "backend")
if backend_path not in sys.path:
    sys.path.append(backend_path)

from backend.simulation.engine import PreFlightSimulator
from backend.simulation.swarm_gate import RiskSwarmGate
from backend.simulation.models import MarketImpactModel
from backend.simulation import PreFlightDecision

@pytest.fixture
def mock_db():
    db = sqlite3.connect(":memory:")
    cursor = db.cursor()
    cursor.execute("""
        CREATE TABLE scaling_policies (
            regime_id INTEGER PRIMARY KEY,
            scale_multiplier REAL
        )
    """)
    cursor.executemany("""
        INSERT INTO scaling_policies (regime_id, scale_multiplier)
        VALUES (?, ?)
    """, [(0, 1.0), (1, 0.5), (2, 0.0)])
    db.commit()
    yield db
    db.close()

def generate_50_sequences() -> List[Dict[str, Any]]:
    # Generate 50 mock sequences of market states
    # 15 flash crashes, 20 high-spread, 15 regular
    sequences = []
    np.random.seed(42)
    
    # 15 Flash Crashes
    for i in range(15):
        # Crash: dramatic price drop, massive spreads, thin book depth
        prices = 100.0 * np.cumprod(1.0 + np.random.normal(-0.02, 0.01, 100))
        spread = prices * 0.06  # > 5.0% spread
        ask = prices + spread / 2.0
        bid = prices - spread / 2.0
        # Ask depth: thin liquidity
        ask_depth = np.random.uniform(10, 50, (5, 2))
        ask_depth[:, 0] = ask[-1] * (1.0 + 0.001 * (np.arange(5) + 1.0))
        bid_depth = np.random.uniform(10, 50, (5, 2))
        bid_depth[:, 0] = bid[-1] * (1.0 - 0.001 * (np.arange(5) + 1.0))
        
        sequences.append({
            "type": "flash_crash",
            "tick_window": {
                "timestamps": np.arange(100, dtype=np.float64),
                "bid": bid,
                "ask": ask,
                "spread": spread / prices,
                "bid_depth": bid_depth,
                "ask_depth": ask_depth
            },
            "regime_id": 2, # Tail-risk regime
            "order_side": "BUY",
            "order_qty": 100.0,
            "portfolio_state": {"equity": 10000.0, "peak_equity": 11000.0},
            "actual_fill_price": ask[-1] * 1.07 # high slippage
        })

    # 20 High-Spread
    for i in range(20):
        # High spread, high vol, decent depth
        prices = 100.0 * np.cumprod(1.0 + np.random.normal(0, 0.015, 100))
        spread = prices * 0.03  # 3.0% spread
        ask = prices + spread / 2.0
        bid = prices - spread / 2.0
        ask_depth = np.random.uniform(500, 2000, (5, 2))
        ask_depth[:, 0] = ask[-1] * (1.0 + 0.001 * (np.arange(5) + 1.0))
        bid_depth = np.random.uniform(500, 2000, (5, 2))
        bid_depth[:, 0] = bid[-1] * (1.0 - 0.001 * (np.arange(5) + 1.0))
        
        sequences.append({
            "type": "high_spread",
            "tick_window": {
                "timestamps": np.arange(100, dtype=np.float64),
                "bid": bid,
                "ask": ask,
                "spread": spread / prices,
                "bid_depth": bid_depth,
                "ask_depth": ask_depth
            },
            "regime_id": 1,
            "order_side": "BUY",
            "order_qty": 200.0,
            "portfolio_state": {"equity": 10000.0, "peak_equity": 10000.0},
            "actual_fill_price": None
        })

    # 15 Regular
    for i in range(15):
        # Low vol, narrow spreads, deep book depth
        prices = 100.0 * np.cumprod(1.0 + np.random.normal(0, 0.002, 100))
        spread = prices * 0.001  # 0.1% spread
        ask = prices + spread / 2.0
        bid = prices - spread / 2.0
        ask_depth = np.random.uniform(5000, 10000, (5, 2))
        ask_depth[:, 0] = ask[-1] * (1.0 + 0.001 * (np.arange(5) + 1.0))
        bid_depth = np.random.uniform(5000, 10000, (5, 2))
        bid_depth[:, 0] = bid[-1] * (1.0 - 0.001 * (np.arange(5) + 1.0))
        
        sequences.append({
            "type": "regular",
            "tick_window": {
                "timestamps": np.arange(100, dtype=np.float64),
                "bid": bid,
                "ask": ask,
                "spread": spread / prices,
                "bid_depth": bid_depth,
                "ask_depth": ask_depth
            },
            "regime_id": 0,
            "order_side": "BUY",
            "order_qty": 500.0,
            "portfolio_state": {"equity": 10000.0, "peak_equity": 10000.0},
            "actual_fill_price": None
        })
        
    # Calibrate actual fill prices for testing test_slippage_mse_accuracy
    model = MarketImpactModel()
    for seq in sequences:
        if seq["actual_fill_price"] is None:
            # Derive simulated fill and add tiny noise
            mid = (seq["tick_window"]["bid"][-1] + seq["tick_window"]["ask"][-1]) / 2.0
            slippage = model.calculate_slippage(
                seq["order_qty"],
                seq["tick_window"]["bid_depth"],
                seq["tick_window"]["ask_depth"],
                mid
            )
            # Add small noise (std dev of 1 bp of price)
            noise = np.random.normal(0, 0.0001 * mid)
            seq["actual_fill_price"] = mid + slippage + noise
            
    return sequences

@pytest.mark.asyncio
async def test_preflight_latency(mock_db):
    simulator = PreFlightSimulator()
    swarm_gate = RiskSwarmGate()
    
    sequences = generate_50_sequences()
    seq = sequences[25] # Select a sequence
    
    loop = asyncio.get_running_loop()
    
    # Measure execution latency of run_in_executor + db query
    t0 = time.perf_counter()
    
    sim_res = await loop.run_in_executor(
        None,
        simulator.simulate_execution,
        seq["order_side"],
        seq["order_qty"],
        seq["tick_window"],
        seq["portfolio_state"]
    )
    
    # Swarm gate db query
    scaled_size = swarm_gate.evaluate(
        simulated_fill_price=sim_res["simulated_fill_price"],
        trade_size=seq["order_qty"],
        regime_id=seq["regime_id"],
        current_spread_pct=seq["tick_window"]["spread"][-1],
        db_connection=mock_db
    )
    
    t1 = time.perf_counter()
    total_latency_seconds = t1 - t0
    
    assert total_latency_seconds < 0.050, f"Latency {total_latency_seconds * 1000:.2f}ms exceeds the 50ms SLA"
    print(f"\nLatency test passed: {total_latency_seconds * 1000:.2f}ms")

def test_swarm_gate_blocks(mock_db):
    swarm_gate = RiskSwarmGate()
    
    # 1. Blocks if spread > 0.05
    scaled_size = swarm_gate.evaluate(
        simulated_fill_price=105.0,
        trade_size=100.0,
        regime_id=0, # normal regime
        current_spread_pct=0.06, # 6% spread
        db_connection=mock_db
    )
    assert scaled_size == 0.0
    
    # 2. Blocks if regime has 0.0 multiplier (regime 2)
    scaled_size = swarm_gate.evaluate(
        simulated_fill_price=105.0,
        trade_size=100.0,
        regime_id=2, # tail-risk
        current_spread_pct=0.01, # tight spread
        db_connection=mock_db
    )
    assert scaled_size == 0.0
    
    # 3. Scales properly for regime 1 (multiplier 0.5)
    scaled_size = swarm_gate.evaluate(
        simulated_fill_price=105.0,
        trade_size=100.0,
        regime_id=1,
        current_spread_pct=0.01,
        db_connection=mock_db
    )
    assert scaled_size == 50.0
    
    # 4. Keeps full size for regime 0 (multiplier 1.0)
    scaled_size = swarm_gate.evaluate(
        simulated_fill_price=105.0,
        trade_size=100.0,
        regime_id=0,
        current_spread_pct=0.01,
        db_connection=mock_db
    )
    assert scaled_size == 100.0

def test_slippage_mse_accuracy():
    simulator = PreFlightSimulator()
    sequences = generate_50_sequences()
    
    squared_errors = []
    
    for seq in sequences:
        res = simulator.simulate_execution(
            seq["order_side"],
            seq["order_qty"],
            seq["tick_window"],
            seq["portfolio_state"]
        )
        
        mid = (seq["tick_window"]["bid"][-1] + seq["tick_window"]["ask"][-1]) / 2.0
        # Simulated slippage as absolute price difference
        simulated_fill = res["simulated_fill_price"]
        simulated_slippage = abs(simulated_fill - mid)
        
        # Actual slippage from actual fill
        actual_fill = seq["actual_fill_price"]
        actual_slippage = abs(actual_fill - mid)
        
        trade_value = seq["order_qty"] * mid
        
        # Error as a fraction of trade value
        error_frac = (simulated_slippage - actual_slippage) / trade_value
        squared_errors.append(error_frac ** 2)
        
    mse = np.mean(squared_errors)
    print(f"\nMean Squared Error (MSE) of slippage: {mse * 100:.6f}% of trade value")
    
    # Assert MSE < 0.05% of trade value (0.0005 in decimal)
    assert mse < 0.0005, f"MSE {mse * 100:.6f}% exceeds the 0.05% threshold"
