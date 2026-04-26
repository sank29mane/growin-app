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
**Goal**: Transform Growin into a SOTA macOS 2026 native experience by integrating fine-tuned intelligence with a complete UI/UX overhaul and hardened RL-driven profit extraction.

### Phase 42: Model Performance Comparison (COMPLETED)
- **Goal**: Compare vllm-mlx output performance for Gemma 2 27B vs Llama-3.1-Nemotron-70B.
- **Success Criteria**:
    - Benchmark report on latency, throughput, and reasoning quality.
    - Final model selection for the milestone.
- **Requirements**: INTEL-01 (Partial), PERF-01 (Partial), UX-01, UX-02, UX-05
- **Status**: COMPLETED

**Plans:** 2/4 plans complete
- [x] 42-01-PLAN.md — Benchmark Environment Setup
- [x] 42-02-PLAN.md — Model Quantization & Acquisition
- [x] 42-03-PLAN.md — Performance Analysis & Selection
- [x] 42-04-PLAN.md — SOTA AI Chat Interface Enhancements

### Phase 43: Local Intelligence & Serving (Core Engine) (COMPLETED)
- **Goal**: Transition from vllm-mlx to vMLX with hardware-aware memory optimizations for M4 Pro (48GB).
- **Success Criteria**:
    - vMLX server managed by backend with 28GB total memory limit.
    - "Thinking" Mode visibility enabled via real-time telemetry.
    - 12GB KV-cache configured via PagedAttention.
- **Requirements**: INTEL-01, INTEL-02, PERF-01, PERF-02, PERF-03
- **Status**: COMPLETED

**Plans:** 2/2 plans complete
- [x] 43-01-PLAN.md — Engine & Factory Transition
- [x] 43-02-PLAN.md — Telemetry, Thinking & Validation

### Phase 44: Multi-Agent Strategy & Swarm Optimization
- **Goal**: Optimize specialist agent swarm coordination to minimize latency and maximize reasoning coherence.
- **Success Criteria**:
    - "Progressive Synthesis" active (DecisionAgent reasons while specialists complete).
    - Swarm output summarization active to preserve prefix caching.
    - Concurrent MLX threshold calibration for M4 Pro.
- **Requirements**: INTEL-01, PERF-01, UX-05
- **Status**: ACTIVE

**Plans:** 1/3 plans complete
- [x] 44-01-PLAN.md — Core Engine Room (Hardware Guard & ContextBuffer)
- [ ] 44-02-PLAN.md — Reasoning Pivot (2-Stage Streaming Logic)
- [ ] 44-03-PLAN.md — Swarm Calibration (Latency Thresholds & Summarization)

### Phase 45: Sovereign UX & macOS 2026 Redesign
- **Goal**: Complete architectural UI redesign following macOS Tahoe patterns and 120Hz ProMotion standards.
- **Success Criteria**:
    - SwiftUI 17+ 0px Ledger UI implemented with Tahoe "Liquid Glass" materials.
    - Stage Manager 2.0 window sets integrated for trading workflows.
    - 120Hz ProMotion fluid rendering and <16ms price synchronization.
- **Requirements**: UX-01, UX-02, UX-03, UX-05, TRADE-01
- **Status**: PLANNED

### Phase 46: Adaptive Learning & Alpha Engineering (Unsloth)
- **Goal**: Deploy local LoRA fine-tuning pipeline for regime-specific alpha extraction.
- **Success Criteria**:
    - Unsloth/mlx-tune pipeline active for local LoRA fine-tuning on M4 Pro.
    - Automated alpha feature engineering from DuckDB historical data.
    - Dynamic LoRA adapter switching based on detected market regimes.
- **Requirements**: LEARN-01, LEARN-02, LEARN-03
- **Status**: PLANNED

### Phase 47: Multimodal Intelligence & Deep Integration
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
| 42: Model Comparison | ✅ | 4/4 | 100% |
| 43: Core Engine | ✅ | 2/2 | 100% |
| 44: Swarm Optimization | 🏗 | 1/3 | 33% |
| 45: Sovereign UX | ⬜ | 0/3 | 0% |
| 46: Adaptive Learning | ⬜ | 0/3 | 0% |
| 47: Multimodal | ⬜ | 0/3 | 0% |

---

## 📋 BACKLOG (FUTURE REQUIREMENTS)
- [ ] AUTH-01: Multi-User Supabase Migration.
- [ ] OPTS-01: Options Greeks Agent.
- [ ] DATA-01: Real-time Order Book Heatmaps.
- [ ] XR-01: VisionOS Pro Integration.
