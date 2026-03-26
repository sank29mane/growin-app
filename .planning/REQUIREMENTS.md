# REQUIREMENTS

## V1 Requirements

### Sovereign UI (Sovereign Ledger Aesthetic)
- **SOV-UI-BASE**: Implement SovereignTheme and 0px SovereignPrimitives (Core Design DNA).
- **SOV-UI-01**: Portfolio Master Ledger - High-density portfolio screen with 0px corners and tonal layering.
- **SOV-UI-02**: Advanced Watchlist - Density-first watchlist with Swift Charts sparklines and Alpha-Stream.
- **SOV-UI-03**: Execution Panel & Strategy Overlay - Professional trading panel with Slide-to-Confirm.

### Local Profit Extraction (RL & JMCE)
- **RL-01**: RL-driven rebalancing using JMCE + TTM-R2 fused state.
- **RL-02**: JMCE Eigenvalue regime detection & LoRA calibration.
- **RL-03**: PPO Action Head & NPU Parallel Backtest.
- **RL-04**: WebSocket Alpha-Stream for real-time training/dashboard integration.

### Platform & Performance
- **PERF-01**: 120Hz smooth performance for all SwiftUI Canvas components.
- **PERF-02**: Native `vllm-mlx` inference server with PagedAttention.
- **PERF-03**: 100% absolute imports and DuckDB thread-safety.

---

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| SOV-UI-BASE | Phase 40 | ✅ Complete |
| SOV-UI-01 | Phase 41 | ⬜ Pending |
| SOV-UI-02 | Phase 41 | ⬜ Pending |
| SOV-UI-03 | Phase 41 | ⬜ Pending |
| RL-01 | Phase 37 | ⬜ Pending |
| RL-02 | Phase 37 | ⬜ Pending |
| RL-03 | Phase 37 | ⬜ Pending |
| RL-04 | Phase 38 | ⬜ Pending |
| PERF-01 | Phase 38 / 40 | ✅ Complete |
| PERF-02 | Phase 37 | ⬜ Pending |
| PERF-03 | Phase 39 | ✅ Complete |
