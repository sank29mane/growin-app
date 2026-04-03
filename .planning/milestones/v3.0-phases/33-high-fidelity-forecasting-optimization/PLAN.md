# Phase 33: High-Fidelity Forecasting & M4 Pro Hardware Optimization Plan

## Goal
Achieve institutional-grade forecasting accuracy and operational efficiency by tightly integrating Neural JMCE with Multivariate TTM-R2, specifically optimized to exploit the M4 Pro chip and 48GB of Unified Memory.

## Project Type
BACKEND / QUANT / ML / HARDWARE-EXPLOITATION (macOS Native)

## Success Criteria
- [x] **Context Locked**: CONTEXT.md finalized with smart hybrid braking and Alpha-RL strategy.
- [ ] **Instant-Start Inference**: Zero model-loading overhead (< 5ms) via Model Residency.
- [ ] **Operational Speed**: Full portfolio forecast (40+ tickers) completed in < 8 seconds.
- [ ] **Precision Benchmark**: Directional accuracy > 92% utilizing Float32 NPU pipelines.
- [ ] **Adaptive Braking**: Verified system correctly halts entry when Shift Metric > 2.5.

---

## Track 1: M4 Pro Resource Exploitation (Hardware Logic)
- [ ] **Model Residency Service**: Implement `backend/utils/worker_service.py` to keep TTM-R2, JMCE, and XGBoost weights pinned in Unified Memory (08:00–21:00 UTC).
- [ ] **AMX-Accelerated Indicators**: Create `indicator_engine.py` using Apple's `Accelerate` framework for high-speed portfolio-wide RSI/ATR/OBV calculation.
- [ ] **Memory Guard**: Integrate `sysctl` monitoring to automatically unload models if memory pressure exceeds 60%.
- [ ] **Concurrency**: Tuning `asyncio.Semaphore` to 12 parallel workers to saturate M4 Pro cores.

## Track 2: Multivariate & Recency-Aware Scaling (Accuracy)
- [ ] **Multivariate Bridge**: Update `forecast_bridge.py` to support "Decoder Channel-Mixing" with Price, RSI, ATR, and OBV.
- [ ] **Exogenous Infusion**: Implement static bias injection for Sentiment and GPR metrics.
- [ ] **EWMA Normalizer**: Replace standard Z-scaling with recency-weighted EWMA (Span: 32) to prioritize immediate price action.
- [ ] **Extended Memory**: Implement 1000-bar warm-start for indicators to eliminate edge lag.

## Track 3: Neural JMCE "Regime Brake" (Smart Hybrid)
- [ ] **Tiered Logic**: Implement `RegimeBrake` controller in `backend/utils/risk_engine.py`:
    - Tier 1 (> 1.5): 50% Size Reduction + Horizon shortening.
    - Tier 2 (> 2.5): Autonomous Kill-Switch + HITL Escalation.
- [ ] **Smart Stops**: Implement auto-adjustment of open trade Stop Losses to break-even -0.5% when Tier 2 is triggered.
- [ ] **Recovery Protocol**: Implement 3-bar "Safe Level" (< 1.0) confirmation before resuming autonomy.

## Track 4: Alpha-RL XGBoost Integration
- [ ] **Model Upgrade**: Refactor `fallbacks/ml_forecaster.py` to use `XGBRegressor` with monotonic constraints.
- [ ] **RL Reward Function**: Implement a Sortino-based reward function to optimize for LETF volatility drag (TQQQ/SQQQ focus).
- [ ] **Critic Integration**: Wire XGBoost as a mandatory "Secondary Critic" in the `DecisionAgent` reasoning loop.

---

## Phase X: Verification
- [ ] **M4 Pro Performance Audit**: Monitor thermal and NPU utilization during peak load.
- [ ] **A/B Backtest**: Run `scripts/backtest_ttm_3gld.py` to measure accuracy delta.
- [ ] **Stability Stress Test**: Verify 48GB memory stability under 100 consecutive scans.

## Done When
- [ ] High-fidelity forecasting is verified with a measurable improvement in price accuracy and operational efficiency.
