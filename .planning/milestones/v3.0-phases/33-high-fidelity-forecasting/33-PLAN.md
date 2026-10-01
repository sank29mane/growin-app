# Phase 33 Plan: High-Fidelity Forecasting & M4 Pro Optimization

This plan breaks down the strategic decisions from `33-CONTEXT.md` into executable tasks.

## 🎯 Goal
Implement adaptive scaling, VIX-integrated forecasting, and the Neural JMCE residual correction loop on the M4 Pro architecture.

## 🛠 Tasks

### Track 1: Adaptive Scaling & VIX Integration
- [x] **Task 1.1: Regime-Aware Z-Score**: Update `RegimeFetcher` to provide log-transformed and 20-day standardized VIX signals.
- [x] **Task 1.2: Robust Scaling Implementation**: Replace `StandardScaler` with a custom `RobustScaler` (Median/IQR) in `forecast_bridge.py`.
- [x] **Task 1.3: Dynamic Windowing**: Implement logic to shorten scaling windows to 64 bars during high-volatility regimes (ATR > 2.0 * mean_ATR).

### Track 2: Neural JMCE Residual Loop (GPU/MLX)
- [x] **Task 2.1: Error Vector Extraction**: Update `forecast_bridge.py` to calculate and store the latest TTM residual vector (Actuals vs Predictions).
- [x] **Task 2.2: JMCE Adapter Integration**: Refactor `NeuralJMCE` in `jmce_model.py` to accept the error vector as an optional input channel.
- [x] **Task 2.3: Zero-Copy Fusion**: Use MLX lazy evaluation to fuse TTM and JMCE operations, ensuring zero-copy memory sharing on the GPU via `WorkerService`.
- [x] **Task 2.4: Fourier Phase Correction**: Implement a lightweight Fourier-domain shift in the JMCE head to correct phase lag in long-range forecasts.

### Track 3: Gating & Compliance
- [x] **Task 3.1: Quantile Uncertainty Gate**: Implement the quantile-based blocking logic in `decision_agent.py` using JMCE variance outputs.
- [x] **Task 3.2: 75bp Alpha Hurdle**: Update `forecaster.py` to flag forecasts that don't clear the 75bp net-friction hurdle.
- [x] **Task 3.3: Execution Jitter**: Add randomized delay logic (500ms-2000ms) to the T212 order dispatch loop.

## 🧪 Verification
- [x] **Simulation Test**: Run `scripts/baseline_simulation.py` and verify "Multivariate" and "Corrected" output tags. (Verified: "IBM Granite TTM-R2.1 + Neural JMCE" active).
- [x] **Latency Benchmark**: Measure end-to-end inference time for the fused TTM-JMCE path. (Measured: ~300ms on warm cache. Target <100ms pending further kernel fusion).
- [x] **Residency Check**: Confirm models remain pinned in GPU memory via `worker_service.py` status. (Confirmed: Residency active in WorkerService).

## 📅 Status
- **Phase**: 33
- **Overall Progress**: 100%
- **Current Task**: Completed
