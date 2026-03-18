# GSD ROADMAP - SOTA Profit Extraction Edition

This document outlines the high-level phases for the Growin App, specifically optimized for M4 Pro (48GB RAM) and Local Profit extraction via LSE Leveraged ETFs.

---

## 🏛 ARCHIVE: Milestone: Foundation & Precision Alpha (Completed 2026-02-23)
## 🏛 ARCHIVE: Milestone: SOTA Intelligence & Financial Autonomy (Completed 2026-03-04)

---

## 🚀 MILESTONE: Local Profit Extraction & RL Hardening (M4 Pro)
**Goal**: Transform into a self-correcting trading weapon for LSE Leveraged ETFs.

### Phase 37: RL Regime & Backtest Lab
- **Goal**: Implement RL-driven rebalancing using JMCE + TTM-R2 fused state.
- **Inference Engine**: Native `vllm-mlx` with PagedAttention (replacing LM Studio API).
- **Status**: PLANNED
- **Wave 1: vllm-mlx & Data Fusion**
    - [ ] **Engine Setup**: Deploy `vllm-mlx` native server for Nemotron-3-Nano (30B MoE).
    - [ ] **RLStateFabricator**: Fuse JMCE Mu/Sigma + TTM-R2 trends + 2PM GMT Window.
    - [ ] **ETF Scraper**: Harvest active LSE Leveraged ETPs (NVD3, 3QQQ, 5LUK) in GBX.
- **Wave 2: Regime Agent (Market Character)**
    - [ ] **Volatility Clustering**: Implement `RegimeAgent` using JMCE Eigenvalues to detect regime shifts.
    - [ ] **Continuous Calibration**: Nightly "Prompt Calibration" based on day's Alpha vs. Predictions.
- **Wave 3: PPO Action Head & NPU Backtest**
    - [ ] **Policy Implementation**: Build a lightweight PPO head in MLX for rebalance actions.
    - [ ] **Mass-Parallel Backtest**: 1000x simulations on ANE/GPU to optimize the "Volatility Tax" threshold.

### Phase 38: SwiftUI Profit Command Center (High-Fidelity)
- **Goal**: 120Hz dashboard for real-time Alpha tracking and Fast-HITL.
- **Tasks**:
    - [ ] **Alpha Chart**: Real-time line chart showing Growin Portfolio vs. FTSE 100.
    - [ ] **Regime Indicator**: Visual display of detected market state.
    - [ ] **Fast-HITL Notifications**: Local macOS toasts for >0.95 conviction rebalances.

### Phase 39: Code Purity & Jules Maintenance
- **Goal**: Unified imports and cleanup legacy debt.
- **Status**: IN_PROGRESS (Delegated to Jules - Session 17444431987523978288)

---

## 📋 BACKLOG (POST-WEEKEND)
- [ ] Multi-User Supabase Migration.
- [ ] Options Greeks Agent.
- [ ] Real-time Order Book Heatmaps.
