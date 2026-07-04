# Milestone v6.0 Requirements: Hardened System Exploitation & Alpha Accuracy

## Status: ACTIVE
**Goal:** Drastically improve overall trading accuracy and maximize system throughput by fully utilizing the M4 Pro (48GB RAM) compute blocks (ANE, GPU, and CPU) and establishing an off-market learning loop.

---

## 🎯 Active Requirements

### Core Optimization & Acceleration (System Exploitation)
- [x] **PERF-04**: Optimize vectorized SQL calculations and rollups in DuckDB for high-throughput feature ingestion.
- [x] **PERF-05**: Calibrate and optimize Apple Neural Engine (ANE/NPU) float16 pipelines for sub-millisecond numerical forecasts.

### Prediction & Classification Accuracy
- [ ] **ACC-01**: Implement unsupervised GMM (Gaussian Mixture Model) clustering on rolling price variance and volume profiles to classify regimes.
- [ ] **ACC-02**: Integrate 2ms hot-swapping router to switch local QLoRA adapters based on GMM regime classifications.
- [ ] **ACC-03**: Research and implement Simulation-in-the-Loop pre-flight checks with configurable failure modes (heuristic re-optimization vs. capital scaling).

### Hardened Order Execution
- [ ] **EXEC-01**: Build adaptive limit order execution utilizing short-term ANE volatility indicators.
- [ ] **EXEC-02**: Implement dynamic re-pricing and re-quoting engine with emergency market-order fallback.

### Continuous Learning Loop
- [ ] **LEARN-04**: Establish off-market QLoRA training runs triggered when validation performance drops below thresholds.

---

## 📋 Future Requirements (Deferred)
- **AUTH-01**: Multi-User Supabase Migration.
- **OPTS-01**: Options Greeks Agent.
- **DATA-01**: Real-time Order Book Heatmaps.
- **XR-01**: VisionOS Pro Integration.

---

## 🚫 Out of Scope
- Cloud-based LLM fallback (Milestone focus is 100% Sovereign Local AI).
- High-frequency tick updates (Targeting dynamic order book re-pricing on a sub-second scale but not HFT microsecond execution).

---

## 🔗 Traceability

| REQ-ID | Phase | Success Criteria | Status |
|--------|-------|------------------|--------|
| PERF-04 | 48 | Vectorized feature rollups execute within 10ms in DuckDB. | ✅ |
| PERF-05 | 49 | Neural JMCE/TTM-R2 running on ANE with sub-millisecond latency. | ✅ |
| ACC-01  | 50 | GMM clustering model correctly classifies at least 3 historical regimes. | ⬜ |
| ACC-02  | 50 | MLX adapter hot-swap latency stays under 2ms. | ⬜ |
| ACC-03  | 51 | Simulation-in-the-loop triggers re-optimization on simulator drawdowns. | ⬜ |
| EXEC-01 | 52 | Limit orders submitted with volatility-adjusted price bounds. | ⬜ |
| EXEC-02 | 52 | Re-quoting agent actively updates orders every N seconds. | ⬜ |
| LEARN-04| 53 | Automated off-market cron triggers QLoRA tuning when accuracy drifts. | ⬜ |
