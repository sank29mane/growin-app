# T212 High-Velocity Profit Optimization Plan

## Goal
Research and design a 'High-Velocity Alpha Engine' for Trading 212 to maximize daily profits using LETFs (e.g., TQQQ/SQQQ) and intraday rebalancing, optimized for Apple Silicon NPU.

## Project Type
BACKEND / QUANT (Python/MLX)

## Success Criteria
- [ ] Comprehensive audit of current T212 API implementation (Rate limits, Execution logic).
- [ ] Comparison report of optimal LETF rebalancing strategies vs current benchmarks.
- [ ] Defined 'High-Velocity Alpha Engine' parameters (Tick intervals, Slippage tolerance).
- [ ] Schema update plan for DataFabricator to support high-frequency rebalancing telemetry.

## Track 1: Internal Research (Growin Audit)
- [ ] Audit `backend/t212_handlers.py` for latency bottlenecks in order execution. → Verify: Identify >2 areas of improvement.
- [ ] Review `docs/sota_research.md` for existing time-series forecasting models (TFT/Informer) applicable to intraday. → Verify: List models.
- [ ] Map `backend/quant_engine.py` simulation capabilities to T212 asset constraints. → Verify: Compatibility check.

## Track 2: External Research (T212 & LETF Strategy)
- [ ] Research T212 API rate limits for high-frequency polling/trading. → Verify: Document limit (e.g., requests per min).
- [ ] Google Search for "Trading 212 API best practices for HFT/Intraday". → Verify: Find 3 actionable tips.
- [ ] Research optimal LETF pair trading (TQQQ/SQQQ, UPRO/SPXU) for daily volatility harvesting. → Verify: Document entry/exit triggers.

## Track 3: Synthesis & Strategy (The Alpha Engine)
- [ ] Define the 'High-Velocity Alpha Engine' (HVAE) core logic (e.g., EMA cross + RSI volatility filter). → Verify: Logic pseudocode.
- [ ] Compare "Standard Portfolio Optimization" vs "Intraday Alpha Engine" for daily profit delta. → Verify: Theoretical comparison.
- [ ] Define slippage mitigation strategy (Limit orders vs Market orders with tight variance guards). → Verify: Guard parameters.

## Track 4: Integration Prep (Phase 30 Roadmap)
- [ ] Design `DataFabricator` updates to ingest T212 real-time quotes at 1-min intervals. → Verify: Schema draft.
- [ ] Plan `QuantEngine` MLX-accelerated intraday stress testing for LETF positions. → Verify: Simulation parameters.

## Phase X: Verification
- [ ] Run `.agent/scripts/security_scan.py` on API key handling in handlers.
- [ ] Execute `.agent/scripts/ux_audit.py` to ensure rebalance triggers are clearly reported in telemetry.
- [ ] Final plan review against "Institutional Portfolio Optimization" (Phase 29) standards.

## Done When
- [ ] Research phase documented and synthesis completed for Phase 30.