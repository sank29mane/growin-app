# Milestone v5.0 Requirements: MLX Intelligence & macOS 2026 UX Overhaul

## Status: ACTIVE
**Goal:** Transform Growin into a SOTA macOS 2026 native experience by integrating fine-tuned Gemma 4 26B A4B MoE intelligence with a complete UI/UX overhaul and hardened RL-driven profit extraction.

---

## 🎯 Active Requirements

### Core Intelligence & Serving
- [ ] **INTEL-01**: Deploy Gemma 4 26B A4B MoE as the primary reasoning engine.
- [ ] **INTEL-02**: Implement "Thinking" Mode visibility in the chat interface.
- [ ] **INTEL-03**: Enable Gemma 4 Multimodal support for image/chart uploads and analysis.
- [ ] **PERF-01**: Serve Gemma 4 MoE locally via vllm-mlx with PagedAttention.
- [ ] **PERF-02**: Implement FP8 KV-cache quantization for M4 Pro VRAM optimization.
- [ ] **PERF-03**: Configure system-level memory limit overrides for concurrent serving/training.

### macOS 2026 UX Overhaul
- [ ] **UX-01**: Complete UI redesign using SwiftUI SDK 17+ (macOS Tahoe patterns).
- [ ] **UX-02**: Integrate Stage Manager 2.0 with saved window sets for trading workflows.
- [ ] **UX-03**: Implement Agentic Sidebar for proactive context management.
- [ ] **UX-04**: Build Dynamic Interactive Tiles for real-time asset monitoring and fast actions.
- [ ] **UX-05**: Achieve 120Hz ProMotion fluid rendering across all high-density views.

### Adaptive Learning (Unsloth)
- [ ] **LEARN-01**: Set up Unsloth/mlx-tune pipeline for local LoRA fine-tuning.
- [ ] **LEARN-02**: Implement automated alpha feature engineering from DuckDB data.
- [ ] **LEARN-03**: Dynamic LoRA adapter switching based on detected market regimes.

### Integrated Trading Features
- [ ] **TRADE-01**: Real-time price synchronization (<16ms) for 120Hz fidelity.
- [ ] **TRADE-02**: Deep linking from chat reasoning to Sovereign Ledger views.

---

## 📋 Future Requirements (Deferred)
- **AUTH-01**: Multi-User Supabase Migration.
- **OPTS-01**: Options Greeks Agent.
- **DATA-01**: Real-time Order Book Heatmaps.
- **XR-01**: VisionOS Pro Integration.

---

## 🚫 Out of Scope
- Cloud-based LLM fallback (Milestone focus is 100% Sovereign Local AI).
- Mobile/iOS support (Milestone focus is macOS 2026 Desktop patterns).

---

## 🔗 Traceability

| REQ-ID | Phase | Success Criteria | Status |
|--------|-------|------------------|--------|
| INTEL-01 | | | ⬜ |
| INTEL-02 | | | ⬜ |
| INTEL-03 | | | ⬜ |
| PERF-01 | | | ⬜ |
| PERF-02 | | | ⬜ |
| PERF-03 | | | ⬜ |
| UX-01 | | | ⬜ |
| UX-02 | | | ⬜ |
| UX-03 | | | ⬜ |
| UX-04 | | | ⬜ |
| UX-05 | | | ⬜ |
| LEARN-01 | | | ⬜ |
| LEARN-02 | | | ⬜ |
| LEARN-03 | | | ⬜ |
| TRADE-01 | | | ⬜ |
| TRADE-02 | | | ⬜ |
