# Phase 29 Context: Institutional Portfolio Optimization (Mean-Variance)

## Phase Objective
Implement an institutional-grade portfolio optimization engine using Modern Portfolio Theory (MPT), specifically optimized for local execution on Apple Silicon M4 NPU. This phase bridges the gap between simple data retrieval and active agentic portfolio management.

## 🏛 Institutional Guardrails (Locked)
1.  **10% Position Cap (Hard Limit)**: The optimizer is strictly forbidden from proposing an initial allocation that exceeds 10.0% for any single ticker.
2.  **1.5% Drift Buffer (Soft Trigger)**: Rebalance alerts are only triggered if a position grows organically beyond **11.5%** or if the overall portfolio Sharpe Ratio can be improved by **>0.1**.
3.  **Dynamic Alpha Hurdle**: Minimum Expected Gain must exceed **(75 bps + Real-Time Friction)**.
    *   **LSE stocks**: Include 0.50% Stamp Duty on buy leg.
    *   **US/Non-GBP stocks**: Include 0.15% FX conversion fee on both legs.
    *   **Spreads**: Inject real-time bid-ask spreads into the cost function.

## 🧠 Core Optimization Strategy: Neural JMCE
*   **Methodology**: Joint Mean-Covariance Estimator (JMCE).
*   **Hardware Target**: Apple Silicon M4 NPU via **Apple MLX Framework**.
*   **Why**: Superior to EWMA for capturing non-linear asset interactions and regime-aware correlation shifts. Utilizes the 38 TOPS Neural Engine and UMA (Unified Memory Architecture).
*   **Lookback**: Hyperparameter-tuned window (default 180-day rolling) with front-weighted decay to prioritize 2026 market dynamics.

## 🛠 Architectural Integration
1.  **`backend/utils/portfolio_analyzer.py`**: New math module implementing the JMCE architecture and SciPy-based non-linear optimization (SLSQP).
2.  **`backend/utils/risk_engine.py`**: Deterministic quantitative engine for **Conditional Value at Risk (CVaR)** at 95% confidence. This is the "Source of Truth" for risk metrics to prevent LLM hallucinations.
3.  **`RegimeFetcher` Tool**: Add to Research Agent to fetch Macro Signals (VIX, 10Y Yield, Inflation) to feed the Regime-Aware JMCE model.
4.  **Asynchronous Optimization Monitor**: A background service that runs intraday/intra-week to detect drift triggers or alpha opportunities without requiring a user prompt.

## 📊 User Experience & Communication
*   **Standardized Metric**: All "downside risk" warnings must be expressed as **CVaR (95%)**.
*   **Tool-Augmented Reasoning**: The LLM acts as a messenger for the `RiskEngine`. Responses must include uncertainty intervals (e.g., "95% probability risk is 12-15%").
*   **Strategy Profile**: User selects a base persona (Defensive vs. Aggressive) via the SwiftUI dashboard, which toggles the optimizer's objective function (Risk-Budgeting vs. Max Sharpe).

## 🧩 Code Context & Patterns
*   **Precision**: Use `Decimal` for all final weight and transaction cost calculations.
*   **Parallelism**: `DataFabricator` must fetch historical time-series for the entire portfolio in parallel to build the covariance matrix.
*   **Caching**: Covariance matrices should be cached locally and only invalidated upon major market regime shifts or price updates.

## 📝 Deferred Ideas (Next Phases)
*   Cross-asset bridge for Crypto and Commodities (Phase 30).
*   Tax-loss harvesting integration (Phase 31).
