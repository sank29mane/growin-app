# Phase 28 CONTEXT: Institutional Liquidity Deep-Dive (Gap Closure)

## Goal
Resolve blocker Swift build errors in `Models.swift` and ensure the liquidity/slippage data from Phase 28 implementation is correctly surfaced in the UI and enforced by the Risk Agent.

## Gap Analysis (from UAT.md)
1. **Swift Build Errors**: Blocker in `Growin/Models.swift`.
   - Extraneous braces or structural mismatch.
   - `AnyCodable` needs to be robust for `ToolFunction`.
   - `WhaleTrade` id/decoding issues.
   - `ChatMessageModel` role/agentName consistency.
2. **UI Integration**: Reasoning trace and strategy summary should mention "Est. Slippage" and "Liquidity Status".
3. **Risk Enforcement**: Verify `RiskAgent` identifies slippage > 100 bps as a risk.

## Tech Stack
- Backend: Python (FastAPI), Pydantic
- Frontend: SwiftUI, Apple Silicon optimized (M4)
- AI: Local NPU acceleration (MLX/CoreML)

## Requirements
- [RISK-LIQ-01] Models.swift must compile without errors.
- [RISK-LIQ-02] Slippage estimates must be visible in the reasoning trace for large trades.
- [RISK-LIQ-03] RiskAgent must trigger a warning/block if slippage > 1% (100 bps).
