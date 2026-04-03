# Phase 33 Context: High-Fidelity Forecasting & M4 Pro Optimization

This document codifies the strategic, hardware-specific, and research-backed decisions for Phase 33. It serves as the definitive guide for downstream agents.

---

## 1. RESOURCE ALLOCATION: THE "THREE-BRAIN" ARCHITECTURE
To maximize the 48GB Unified Memory and Apple Silicon M4 Pro architecture, workloads are strictly partitioned:

*   **CPU (Logic & Precision)**:
    *   Python environment (3.13) orchestration and Coordinator Agent reasoning.
    *   **High-Precision Math**: Final pricing and ledger logic (using `Accelerate.framework` / AMX).
*   **GPU (Intelligence & Throughput)**:
    *   **Primary Brain**: IBM Granite TTM-R2 (Frozen Foundation Model) for zero-shot base forecasting.
    *   **Corrective Brain**: Neural JMCE (Online $\delta$-Adapter) implemented via **MLX**.
    *   **Hardware Fusion**: Leverage MLX lazy evaluation for **Zero-Copy memory sharing** between models.
*   **NPU (Efficiency & Monte Carlo)**:
    *   **Simulation Engine**: High-speed, lower-precision (FP16) Monte Carlo path generation.

## 2. FORECASTING: MULTIVARIATE ADAPTIVE SCALING
*   **Channels**: TTM-R2 utilizes `[Close, RSI, ATR, Volume]` + exogenous `[Log-VIX 20D Z-Score]`.
*   **Robust Scaling**: Move to **Median/IQR (RobustScaler)** to neutralize LSE flash-spike noise.
*   **Adaptive Regime**: Scaling windows shorten (to 64 bars) during high-volatility regimes to increase reactivity.

## 3. NEURAL JMCE FEEDBACK LOOP (SOTA 2026)
*   **Correction Logic**: JMCE acts as an **ELF-style Residual Adapter**. It inputs TTM Error Vectors (Actual - Predict) and outputs bounded mathematical shifts.
*   **Fourier Domain**: Correct phase lag in long-range predictions using Fourier-domain residual analysis.
*   **Risk Brain**: Output a **Quantile-Calibrated Uncertainty Envelope**.
    *   **Gating**: Apply an **EP Loss (Enhanced Peak Loss)** penalty to confidence if residuals exceed the 95th percentile.
*   **Iterative Feedback**: Previous correction errors are fed back into the next inference cycle for online adaptation.

## 4. LSE COMPLIANCE & FRICTION
*   **Alpha Hurdle**: 75bp minimum predicted move (after 0.15% FX modeling).
*   **Temporal Jitter**: Randomized execution delays (500ms-2000ms) to avoid "velocity clustering" surveillance flags.

---

## REUSABLE ASSETS
*   `backend/utils/worker_service.py`: GPU model residency management.
*   `backend/utils/indicator_engine.py`: MLX/AMX-accelerated technical analysis.
*   `backend/forecast_bridge.py`: Primary multivariate implementation.

---

## NEXT STEPS
1.  **Researcher**: Investigate MLX-native Fourier Transform implementations for residual correction.
2.  **Planner**: Breakdown task: "Integrate Residual Feedback recursive loop into forecast_bridge.py".
3.  **Executor**: Implement RobustScaler and VIX Z-score injection.
