# PHASE 21 CONTEXT: AI-Driven Dividend Optimization & Passive Income Tracking

This document codifies the functional and behavioral decisions for Phase 21, providing clear guidance for research, planning, and execution agents.

---

## 1. STRATEGIC FOCUS: INTRA-WEEK / INTRA-MONTH MANAGEMENT
*   **Goal**: Transition passive income into an active "yield-generation" engine through high-precision timing.
*   **Decision Frequency**: The system performs full analysis and rebalancing checks on an **Intra-week to Intra-month** basis.
*   **Intraday Timing**: While the strategy is multi-day/week, the final execution of "Dividend Capture" entries utilizes intraday precision (specifically the 2-hour window before market close).

## 2. DIVIDEND CAPTURE & ORDER EXECUTION
*   **Hybrid Order Strategy**: 
    *   **Phase A (Patience)**: Utilize **Limit Orders** to build positions over 2–3 days prior to the ex-dividend date to minimize slippage.
    *   **Phase B (Aggression)**: Automatically flip to **Market Orders** if the position is not filled within **2 hours of the Ex-Dividend cutoff** to ensure capture.
*   **Profit Taking**: Even if price recovery occurs faster than predicted (e.g., in 2 hours), the agent will **stick to the original intra-week plan** to avoid churn and ensure stability, unless the Orchestrator identifies a critical reason to exit early.

## 3. RISK MANAGEMENT: THE "GAUGE & ABORT" SYSTEM
*   **Multi-Model Consensus**: All "Capture" trades must be validated by a consensus of three models:
    1.  **TTM-R2 (IBM Granite)**: Primary trend and seasonality forecaster.
    2.  **XGBoost**: Secondary technical validation (RSI/ATR).
    3.  **Monte Carlo Simulation**: Payout probability and drawdown stress-testing.
*   **Abort Triggers**:
    *   **Max Drawdown**: Hard exit if price drops **>5% more than the dividend amount** (protecting capital over yield).
    *   **Confidence Drop**: Abort if the consensus confidence score falls below **40%**.
    *   **Orchestrator Mandate**: The Orchestrator MUST consult the **Risk Critic Agent** for a second opinion before executing a loss-making exit.
*   **HITL (Human-in-the-Loop)**: The system will send **push notifications** with intelligent recommendations (e.g., "Consensus shifted to Sell - High Confidence") for manual approval before "panic" exits.

## 4. UI/UX: THE COMMAND CENTER (STITCH REF: projects/3458842103377256179)
*   **Confidence Visualization**: Display a **'Consensus Range' probability cloud** (translucent shaded area) around the forecast line on the main dividend chart.
*   **Income Tracking**:
    *   **Solid Emerald Green**: Settled/Received cash.
    *   **Ghost/Hollow Outline (Soft Blue)**: Projected/AI-Predicted cash.
*   **Recommendation UI**: A prominent **'Quick-Action Card'** with a 'One-Tap Approve' button and a 'Reasoning Chip' summarizing the multi-model consensus.
*   **Portfolio List**: All stocks remain visible (no hiding growth stocks), but dividend-paying stocks receive a distinct badge icon.

---

## DEFERRED IDEAS (For Phase 22+)
*   Standalone Intraday/Intra-week Portfolio Management (not tied to dividends).
*   Leverage-based dividend capture (using margin for capture trades).
*   Full autonomy mode (Zero HITL for high-confidence trades).

---

## NEXT STEPS
1.  **Research**: Investigate Trading 212/Alpaca API nuances for "Ex-Dividend" data reliability.
2.  **Planning**: Detail the `backend/dividend_bridge.py` implementation for TTM-R2.
3.  **Execution**: Scaffold the `PassiveIncomeDashboard.swift` view.
