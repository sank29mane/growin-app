# Phase 31: Autonomous Agentic Execution Plan

## Goal
Establish the autonomous execution loop and real-time model calibration to move from human-in-the-loop proposals to high-conviction autonomous trades.

## Project Type
BACKEND / AGENTIC / ML (macOS Native)

## Success Criteria
- [x] **Unified Ticker Engine**: Consolidate `normalize_ticker` logic into `utils.ticker_utils` for backend-wide consistency.
- [x] **Autonomous Loop**: `DecisionAgent` can bypass HITL gates for high-conviction signals (Conviction Level 10).
- [x] **MLX Calibration**: `apply_weight_adapter` implemented for on-the-fly model adjustments based on error feedback.
- [x] **Currency Resilience**: Verified GBX -> GBP normalization for LSE assets like 3GLD.L.

---

## Track 1: Ticker & Currency Normalization
- [x] **Consolidate Imports**: Audit `price_validation.py`, `market_routes.py`, etc., to use `utils.ticker_utils`.
- [x] **Robust Normalizer**: Enhance `TickerResolver` to handle case-insensitive suffixes and specific ISA ticker mappings (e.g., 3GLDl_EQ).
- [x] **Price Fix**: Implement automatic GBX -> GBP conversion in `handle_get_price_history` and `handle_get_current_price`.

## Track 2: Autonomous Decision Loop
- [x] **High Conviction Bypass**: Implement logic in `DecisionAgent` to detect `HIGH CONVICTION` and execute tools without manual approval.
- [x] **Proposal Enhancement**: Add `bypass_confirmation` flag to the internal `trade_proposal` model.
- [x] **Execution Audit**: Log autonomous bypass events for transparency.

## Track 3: MLX Model Adapters
- [x] **Weight Adapter Injection**: Create `apply_weight_adapter` snippet in `mlx_injections.py`.
- [x] **Agent Integration**: Update `MathGeneratorAgent` system prompt to make weight adapters available for script generation.
- [x] **Calibration Loop**: Enable daily calibration based on prediction error vs actual market movement.

---

## Phase X: Verification
- [x] **Real-time Backtest**: Verified JMCE on live ISA data for 3GLD.L.
- [x] **Price Accuracy**: Confirmed 3GLD.L displays £58.50 instead of £5850.00.
- [x] **Autonomous Test**: Verified bypass logic triggers correctly on high-conviction markers.

## Done When
- [x] Autonomous loop is functional and price normalization is verified across multi-account types.
