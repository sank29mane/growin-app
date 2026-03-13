# Growin App Multi-Agent System: Architecture Evolution (SOTA 2026)

## 1. Executive Summary
As of Phase 31, the Growin App has transitioned from a purely assistive "Copilot" model to a high-conviction **Autonomous "Autopilot"** system. This evolution leverages the massive parallel processing power of the M4 generation Apple Silicon, implementing **Hardware-Aware Partitioning**, **Neural JMCE Regime Detection**, and **Autonomous Execution Bypasses**.

## 2. Evolution: From Assistive to Autonomous

### Previous Architecture (Phases 16-20)
- **User** -> **Orchestrator Agent**: Unified entry point for intent classification.
- **Orchestrator** -> **Specialist Swarm**: Parallel execution of Quant, Research, Portfolio.
- **Status**: Assisted decision-making with mandatory UI trade approval.
*Latency: Optimized (<500ms routing overhead)*

### Current SOTA 2026 Architecture (Phases 31-32)
1. **User** -> **Coordinator Agent**: Sub-100ms routing and classification pinned to ANE.
2. **Coordinator** -> **Parallel Swarm + MathGenerator**: Specialists perform analysis while MathGenerator creates optimized MLX scripts for the M4 GPU.
3. **Decision Agent (The Synthesis Brain)**:
    - Integrates specialist data with **Neural JMCE** covariance velocity.
    - **High Conviction Bypass**: If conviction is absolute (`LEVEL: 10`), it bypasses the HITL (Human-in-the-Loop) gate.
4. **Autonomous Execution**: Direct trade placement on Trading 212 with full audit logging.
*Autonomy: Level 4 (High Conviction Self-Execution)*

## 3. Core SOTA Components (Phases 31+)

### A. Neural JMCE (Joint Mean-Covariance Estimator)
A sophisticated mathematical engine that runs on the **Apple Neural Engine (ANE)**.
- **Regime Shift Detection**: Detects volatility spikes and correlation breakdowns in <5ms.
- **Shift Metric**: A real-time "Velocity" indicator that boosts confidence in intraday breakouts.

### B. M4 Hardware-Aware Partitioning
Growin intelligently splits its brain across the M4 SoC:
- **CPU (AMX)**: Handles the high-frequency Python/REST orchestration and T212 API communication.
- **GPU (MLX/Metal)**: Executes large-model reasoning and performs **Daily Calibration (Weight Adapters)** to tune models to current market regimes.
- **NPU (ANE)**: Dedicated to Neural JMCE inference and real-time technical indicators.

### C. Autonomous Decision Loop & Audit
- **Conviction Logic**: Uses `DecisionAgent` to evaluate the alignment of Quant, Forecast, and Risk signals.
- **Security Sandbox**: Model-generated code is executed in a secure local sandbox using `safe_python.py`.
- **Audit Logging**: Every autonomous action is recorded in a tamper-proof local log with the full reasoning trace preserved.

### D. Ticker & Currency Normalization Engine
- **Unified Resolver**: Consolidates 12+ conflicting ticker formats into a single market standard.
- **Fidelity Guarantee**: Ensures 100% data parity between the app and the broker (Trading 212), including automated GBX to GBP conversion.

---
*Status: IMPLEMENTED & VERIFIED (March 2026)*
