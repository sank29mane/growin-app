# Phase 51: Simulation-in-the-Loop Research & Swarm Gate - Research

## Objective
Answer: **"What do I need to know to PLAN this phase well?"**

To properly plan Phase 51, you need to understand the boundaries, the required technical approach, and the specific metrics and failure modes defined by the domain. Phase 51 requires building an ultra-fast, in-memory pre-flight simulator that intercepts orders and scales or blocks them based on simulated execution and active market risk regimes.

## 1. Key Requirements to Satisfy (ACC-03)
* **Goal:** Prototype pre-flight trade backtesting and evaluate re-optimization vs. capital scaling policies. Trigger re-optimization on simulator drawdowns.
* **Latency SLA:** The absolute maximum time for simulation + gating is **< 50ms**. (Budget: <20ms for fill simulation, <5ms for Swarm Gate).
* **Target Markets:** High-volatility LSE Leveraged ETFs (e.g., 3LSE, 3SQQ).
* **Guarantees:** Thread-safe, 100% stateless (pure functional), and asynchronous.

## 2. Technical Stack & Constraints
* **Framework:** Custom event-driven engine using **NumPy (>=1.24)** and **Pandas (>=2.0)**. 
* **Strictly Out of Scope:** Object-oriented backtesters like Backtrader, Zipline, or vectorbt PRO (due to latency and overhead).
* **Concurrency:** The simulation must run on a thread pool using the async event loop's `run_in_executor` to prevent GIL blocks on the main asynchronous trading loop.
* **State Management:** Hold a circular buffer of the most recent 10,000 tick updates as raw NumPy arrays (`float64`) in memory for constant-time, zero-copy slicing.

## 3. Core Components to Plan

### A. PreFlightSimulator (The Engine)
* Uses a pre-loaded memory view of sliding tick history buffers.
* Slices array up to the current tick without look-ahead bias.
* Evaluates expected fill price given the proposed trade size and current order book depth.

### B. MarketImpactModel (The Slippage Estimator)
* Calculates non-linear slippage and spread expansion based on L2 depth data.
* Integrates logic from Phase 50's `online_vol.py` and `online_spread.py`.

### C. RiskSwarmGate (The Capital Scaler)
* Queries dynamic scaling policies from SQLite/DuckDB.
* Uses the GMM regime classification (from Phase 50) to determine risk.
* For tail-risk regimes, it must hard scale the order size to 0.0 (block buy).
* If the spread exceeds 5.0% of the mid-price, it immediately blocks the order execution.

### D. Integration Point (`backend/trading_loop.py`)
* The simulator needs to intercept order generation in the main trading loop.
* Uses Pydantic for strict output schema (e.g., `PreFlightDecision` with `approved`, `simulated_fill_price`, `scaled_size`, `regime_id`, `latency_ms`).

## 4. Evaluation & Testing Strategy
* **Testing Tooling:** Standard `pytest` and `pytest-asyncio`.
* **Test Dataset:** 50 historical sequences of LSE Leveraged ETF market states containing:
  * 15 flash-crash scenarios
  * 20 high-spread regime transitions
  * 15 regular trading periods
* **Labels:** Ground truth based on actual broker execution fills (timestamp, price, slippage, volume).
* **Goal Metrics:** 
  * Mean Squared Error (MSE) of simulated vs. actual slippage < 0.05% of trade value.
  * 100% of high-risk regimes properly scale size to 0.

## 5. Production Monitoring Requirements
* Trace telemetry via local SQLite database.
* Key metrics to capture on every cycle: `simulation_latency_ms`, `slippage_error_bps`, `gate_blocks_count`, `active_regime_distribution`.
* Set up a smart sampling strategy to persist the full historical tick buffer when actual execution price deviates from simulated fill price by > 5 basis points, or when the Swarm Gate reduces order size by > 50%.

## Conclusion for Planning
When generating the PLAN, focus on breaking down the implementation of the three main abstraction classes (`PreFlightSimulator`, `RiskSwarmGate`, `MarketImpactModel`), the async data wiring in the trading loop, the SQLite monitoring telemetry, and finally the pytest-based evaluation suite covering the 50 historical sequences. Make sure every computational step relies on NumPy operations to ensure the <50ms SLA is met.
