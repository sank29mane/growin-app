# GSD ROADMAP - SOTA Profit Extraction Edition

This document outlines the high-level phases for the Growin App, specifically optimized for M4 Pro (48GB RAM) and Local Profit extraction via LSE Leveraged ETFs.

---

## 🏛 ARCHIVE: Completed Milestones
- **Milestone v1.0: Foundation & Precision Alpha** (Completed 2026-02-23)
- **Milestone v2.0: SOTA Intelligence & Financial Autonomy** (Completed 2026-03-04)
- **Milestone v3.0: Autonomous Experience & Production Scaling** (Completed 2026-03-20)
- **Milestone v4.0: Sovereign UI & Visual DNA** (Completed 2026-04-03)
    - **Phase 40**: Sovereign Alpha Command Center (0px Primitives)
    - **Phase 41**: Sovereign UI Stitch Generation (Full Component Suite)
- **Milestone v5.0: MLX Intelligence & macOS 2026 UX Overhaul** (Completed 2026-06-20)
    - **Phase 42**: Model Performance Comparison
    - **Phase 43**: Local Serving Core Engine
    - **Phase 44**: Swarm Optimization
    - **Phase 45**: Sovereign UX & macOS 2026 Redesign
    - **Phase 46**: Adaptive Learning & QLoRA Fine-tuning
    - **Phase 47**: Multimodal Intelligence

---

## 🚀 CURRENT MILESTONE: v6.0: Hardened System Exploitation & Alpha Accuracy
**Goal**: Drastically improve overall trading accuracy and maximize system throughput by fully utilizing the M4 Pro (48GB RAM) compute blocks (ANE, GPU, and CPU) and establishing an off-market learning loop.

**Phases Checklist:**
- [x] **Phase 48:** High-Fidelity Feature Engineering & DuckDB Ingestion
- [x] **Phase 49:** Apple Neural Engine (ANE) Integration for Numeric Forecasting
- [ ] **Phase 50:** Vol-Spread GMM Clustering for Regime Classification
- [ ] **Phase 51:** Simulation-in-the-Loop Research & Swarm Gate
- [ ] **Phase 52:** Adaptive Limit Orders with Dynamic Re-Quoting
- [ ] **Phase 53:** Autonomic Off-Market QLoRA Fine-Tuning

### Must-Haves
- [ ] Vectorized feature engineering in DuckDB (PERF-04)
- [x] Sub-millisecond forecasting on ANE/NPU (PERF-05)
- [ ] Vol-Spread GMM Clustering for Regime Classification (ACC-01, ACC-02)
- [ ] Simulation-in-the-Loop Research & Swarm Gate (ACC-03)
- [ ] Adaptive Limit Orders with Dynamic Re-Quoting (EXEC-01, EXEC-02)
- [ ] Autonomic Off-Market QLoRA Fine-Tuning (LEARN-04)

### Phase 48: High-Fidelity Feature Engineering & DuckDB Ingestion
- **Goal**: Optimize DuckDB query logic and create high-throughput feature ingestion tables for rolling spread, volatility, and volume indicators.
- **Success Criteria**:
    - Vectorized feature rollups execute within 10ms in DuckDB.
    - Automated logging of feature parameters to telemetry database.
- **Requirements**: PERF-04
- **Status**: ✅ Complete (2026-07-01)

### Phase 49: Apple Neural Engine (ANE) Integration for Numeric Forecasting
- **Goal**: Calibrate and serve time-series models on ANE, achieving sub-millisecond numeric predictions.
- **Success Criteria**:
    - Neural JMCE/TTM-R2 running on ANE with sub-millisecond latency.
    - Zero crash or memory fragmentation when running alongside MLX.
- **Requirements**: PERF-05
- **Status**: ✅ Complete (2026-07-01)

### Phase 50: Vol-Spread GMM Clustering for Regime Classification
- **Goal**: Build a GMM clustering classifier to trigger 2ms hot-swaps of local QLoRA adapters.
- **Success Criteria**:
    - GMM model correctly classifies at least 3 historical regimes.
    - MLX adapter hot-swap latency stays under 2ms.
- **Requirements**: ACC-01, ACC-02
- **Status**: ⬜ Not Started

### Phase 51: Simulation-in-the-Loop Research & Swarm Gate
- **Goal**: Prototype pre-flight trade backtesting and evaluate re-optimization vs. capital scaling policies.
- **Success Criteria**:
    - In-memory simulation checks execute within 50ms.
    - Configurable fail-safe pathways defined and empirically tested.
- **Requirements**: ACC-03
- **Status**: ⬜ Not Started

### Phase 52: Adaptive Limit Orders with Dynamic Re-Quoting
- **Goal**: Implement volatility-adjusted limit orders and a dynamic re-quoting engine with emergency fallbacks.
- **Success Criteria**:
    - Volatility-adjusted price boundaries computed in real-time.
    - Re-quoting engine actively updates order parameters every N seconds.
- **Requirements**: EXEC-01, EXEC-02
- **Status**: ⬜ Not Started

### Phase 53: Autonomic Off-Market QLoRA Fine-Tuning
- **Goal**: Set up background validation auditing and off-market training pipelines for adapter updates.
- **Success Criteria**:
    - System detects performance drift below thresholds and triggers auto-training.
    - Automated cron successfully completes QLoRA fine-tuning within VRAM constraints (28GB override limit).
- **Requirements**: LEARN-04
- **Status**: ⬜ Not Started

---

## 📊 PROGRESS SUMMARY

| Milestone | Status | Completed Date |
|-----------|--------|----------------|
| v1.0 | ✅ | 2026-02-23 |
| v2.0 | ✅ | 2026-03-04 |
| v3.0 | ✅ | 2026-03-20 |
| v4.0 | ✅ | 2026-04-03 |
| v5.0 | ✅ | 2026-06-20 |
| v6.0 | 🏗 ACTIVE | |

---

## 📋 BACKLOG (FUTURE REQUIREMENTS)
- [ ] AUTH-01: Multi-User Supabase Migration.
- [ ] OPTS-01: Options Greeks Agent.
- [ ] DATA-01: Real-time Order Book Heatmaps.
- [ ] XR-01: VisionOS Pro Integration.
