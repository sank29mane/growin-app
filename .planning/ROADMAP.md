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

---

## 🚀 CURRENT MILESTONE: v5.0 MLX Intelligence & macOS 2026 UX Overhaul
**Goal**: Transform Growin into a SOTA macOS 2026 native experience by integrating fine-tuned Gemma 4 26B A4B MoE intelligence with a complete UI/UX overhaul and hardened RL-driven profit extraction.

### Phase 42: Model Performance Comparison
- **Goal**: Compare vllm-mlx output performance for Gemma 4 26B A4B MoE vs NVIDIA Nemotron 3 Nano 4-bit MLX.
- **Success Criteria**:
    - Benchmark report on latency, throughput, and reasoning quality.
    - Final model selection for the milestone.
- **Requirements**: INTEL-01 (Partial), PERF-01 (Partial)
- **Status**: ✅ COMPLETED (2026-04-10)

### Phase 43: Local Intelligence & Serving (Core Engine)
- **Goal**: Establish stable local inference for the selected model with hardware-aware memory optimizations.
- **Success Criteria**:
    - Selected model served locally via `vllm-mlx` with PagedAttention.
    - "Thinking" Mode visibility enabled in chat interface.
    - System-level memory overrides configured (FP8 KV-cache) for M4 Pro (48GB).
- **Requirements**: INTEL-01, INTEL-02, PERF-01, PERF-02, PERF-03
- **Status**: ACTIVE

### Phase 44: Sovereign UX & macOS 2026 Redesign
- **Goal**: Complete architectural UI redesign following macOS Tahoe patterns and 120Hz ProMotion standards.
- **Success Criteria**:
    - SwiftUI 17+ 0px Ledger UI implemented with Tahoe "Liquid Glass" materials.
    - Stage Manager 2.0 window sets integrated for trading workflows.
    - 120Hz ProMotion fluid rendering and <16ms price synchronization.
- **Requirements**: UX-01, UX-02, UX-03, UX-05, TRADE-01
- **Status**: PLANNED

### Phase 45: Adaptive Learning & Alpha Engineering (Unsloth)
- **Goal**: Deploy local LoRA fine-tuning pipeline for regime-specific alpha extraction.
- **Success Criteria**:
    - Unsloth/mlx-tune pipeline active for local LoRA fine-tuning on M4 Pro.
    - Automated alpha feature engineering from DuckDB historical data.
    - Dynamic LoRA adapter switching based on detected market regimes.
- **Requirements**: LEARN-01, LEARN-02, LEARN-03
- **Status**: PLANNED

### Phase 46: Multimodal Intelligence & Deep Integration
- **Goal**: Infuse vision intelligence into trading workflows and asset-level fast actions.
- **Success Criteria**:
    - Gemma 4 multimodal support active for image/chart upload analysis.
    - Dynamic Interactive Tiles built for real-time asset monitoring.
    - Deep linking from chat reasoning results to Sovereign Ledger views.
- **Requirements**: INTEL-03, UX-04, TRADE-02
- **Status**: PLANNED

---

## 📊 PROGRESS SUMMARY

| Phase | Status | Plans | Complete |
|-------|--------|-------|----------|
| 42: Model Comparison | ✅ | 1/1 | 100% |
| 43: Core Engine | 🏗 | 0/3 | 0% |
| 44: Sovereign UX | ⬜ | 0/3 | 0% |
| 45: Adaptive Learning | ⬜ | 0/3 | 0% |
| 46: Multimodal | ⬜ | 0/3 | 0% |

---

## 📋 BACKLOG (FUTURE REQUIREMENTS)
- [ ] AUTH-01: Multi-User Supabase Migration.
- [ ] OPTS-01: Options Greeks Agent.
- [ ] DATA-01: Real-time Order Book Heatmaps.
- [ ] XR-01: VisionOS Pro Integration.
