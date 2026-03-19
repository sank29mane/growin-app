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

### Phase 38: SwiftUI Profit Command Center & RL Training Integration
- **Goal**: 120Hz dashboard for real-time Alpha tracking and hardened RL training loop.
- **Plans**: 3 plans
- **Status**: PLANNED
- **Wave 1: Control & Data Backbone**
    - [ ] **38-01-PLAN.md**: WebSocket Alpha-Stream, Smart Money DST, Split-Brain.
- **Wave 2: Command Center Implementation**
    - [ ] **38-02-PLAN.md**: 120Hz Canvas Dashboard, Regime Indicator, Fast-HITL.
- **Wave 3: RL Training & Hardening**
    - [ ] **38-03-PLAN.md**: PPO Training Loop, GAE, Financial Reward.

### Phase 39: Code Purity & Jules Maintenance
- **Goal**: Unified imports and cleanup legacy debt.
- **Status**: IN_PROGRESS (Delegated to Jules - Session 17444431987523978288)

---

## 📋 BACKLOG (POST-WEEKEND)
- [ ] Multi-User Supabase Migration.
- [ ] Options Greeks Agent.
- [ ] Real-time Order Book Heatmaps.
