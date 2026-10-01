# Phase 22: Leveraged Dividend Capture & Advanced Simulation

## Goal
Implement institutional-grade leveraged dividend capture, continuous-time price recovery modeling using Neural ODEs, and a hybrid simulation engine for portfolio stress testing (SOTA 2026).

## Requirements
- [LDC-01] Implement `LeveragedCaptureEngine` with 40bp execution gap solvers.
- [LDC-02] Add Delta-Neutral covered call overlays and Index Future Netting.
- [NODE-01] Integrate `RecoveryVelocityNODE` using `torchdiffeq` with Adjoint Sensitivity.
- [NODE-02] Model continuous-time price return trajectories post-dividend/shock.
- [SIM-01] Build `HybridSimEngine` (Monte Carlo + XGBoost/LightGBM).
- [SIM-02] Implement GPU-accelerated simulation agents (1000x speedup).
- [MARG-01] Implement `PortfolioMarginModel` (CME Prisma style).
- [MARG-02] Add SA-CCR and EPE modeling with MPoR segmentation.

## Wave 1: Core Engines & Continuous-Time Modeling
### 22-01: Leveraged Dividend Capture Engine
**Objective**: Implement institutional ex-day premium targeting with execution alpha and hedging.
- **Task 1**: Create `LeveragedCaptureEngine` in `backend/dividend_capture.py`. Implement logic to identify ex-day premium anomalies and apply 40bp execution gap solvers for routing.
- **Task 2**: Implement Hedging Overlays. Add Delta-Neutral covered call logic and Index Future Netting to neutralize systemic risk during the capture period.
- **Verification**: `pytest tests/backend/test_dividend_capture.py` verifying ex-day premium detection and hedging efficacy.

### 22-02: Neural ODE Recovery Modeling
**Objective**: Integrate continuous-time trajectory modeling for post-dividend price recovery.
- **Task 1**: Implement `RecoveryVelocityNODE` in `backend/models/neural_ode.py` using `torchdiffeq`. Use the Adjoint Sensitivity Method for $O(1)$ memory cost.
- **Task 2**: Integrate with `QuantEngine`. Model the price return trajectory to equilibrium post-shock/dividend using tick-level fidelity data.
- **Verification**: `pytest tests/backend/test_neural_ode_recovery.py` validating $dz(t)/dt$ accuracy and memory efficiency.

## Wave 2: Hybrid Simulation Framework
### 22-03: Hybrid MC-ML Simulation Framework
**Objective**: Large-scale portfolio stress testing with ML-enhanced tail-loss forecasting.
- **Task 1**: Build `HybridSimEngine` in `backend/quant_engine.py`. Combine Monte Carlo path generation with XGBoost/LightGBM overlays for tail-risk (VaR/CVaR) prediction.
- **Task 2**: Agentic GPU Acceleration. Implement GPU-optimized inference for simulation agents to achieve 1000x speedup over standard CPU MC.
- **Verification**: `pytest tests/backend/test_hybrid_simulation.py` comparing CPU vs GPU throughput and VaR/CVaR accuracy.

## Wave 3: Portfolio Margining & Risk Controls
### 22-04: Portfolio Margining & Risk Controls
**Objective**: Retail Portfolio Margin model (CME Prisma style) with real-time risk buffers.
- **Task 1**: Implement `PortfolioMarginModel`. Consolidate Equities, Futures, and Treasuries into a single cross-product risk framework for margin savings.
- **Task 2**: Risk Governance. Implement SA-CCR (Standardized Approach for Counterparty Credit Risk) and EPE (Expected Positive Exposure) modeling with MPoR segmentation.
- **Verification**: `pytest tests/backend/test_portfolio_margin.py` verifying margin reduction percentages and SA-CCR compliance.

## Success Criteria
- [ ] 40bp execution gap achieved in backtests.
- [ ] Neural ODE models recovery velocity with < 5% error vs historical benchmarks.
- [ ] Hybrid simulation generates 100k+ paths per second on GPU.
- [ ] Margin requirements reduced by 60%+ for correlated portfolios.
