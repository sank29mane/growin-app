# Phase 26: Core Functionality Audit (Profit Maximization)

## Goal
Conduct a comprehensive system audit to ensure the platform fundamentally achieves its primary objective: maximizing intraday and intra-week trading profits. This phase shifts focus from infrastructure and UI to actionable strategy generation, ensuring the Multi-Agent System (MAS) actively seeks out, validates, and recommends high-ROI short-term trading opportunities.

## Requirements
- **PROFIT-01 (Intraday Data Velocity)**: Verify that `DataFabricator` and `QuantEngine` correctly pull and process minute/hourly resolution data (1Min, 5Min, 1Hour) required for intraday signals, not just daily closures.
- **PROFIT-02 (Strategy Prompt Calibration)**: Audit and optimize the `OrchestratorAgent`, `QuantAgent`, and `DecisionAgent` prompts to explicitly prioritize short-term profit maximization over generic market commentary. 
- **PROFIT-03 (Risk-Reward Balancing)**: Ensure the `RiskAgent` (from Phase 25) distinguishes between calculated high-probability intraday setups and reckless volatility, preventing it from incorrectly blocking legitimate short-term trades.
- **PROFIT-04 (Actionable Output Formatting)**: Force the assistant to output structured "Intraday/Intra-week Trading Plans" (Entry, Take Profit, Stop Loss, Position Size) rather than abstract analysis.
- **PROFIT-05 (Backtest Verification)**: Create an automated audit script to run historical intraday data through the current MAS pipeline to verify the accuracy and profitability of its generated signals.

## Focus Areas
1. **Timeframe Resolution**: Moving from `1Day` defaults to `1Min`/`5Min`/`1Hour` dynamically based on intent.
2. **Signal Aggressiveness**: Tuning the agents to act like day/swing traders.
3. **Execution Confidence**: Measuring the exact latency from query to actionable trade plan.
