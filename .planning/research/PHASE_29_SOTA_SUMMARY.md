# Phase 29 Research: Institutional Portfolio Optimization (SOTA 2026)

## 1. Optimization Strategy: Hybrid & Tree-Based Models
- **SOTA**: Transition from static MVO to **Regime-Aware Risk Parity** and **Goal-Oriented ML** (P-Trees/AP-Trees).
- **AP-Trees (Asset Pricing Trees)**: Explicitly optimize for Mean-Variance Efficiency by capturing non-linear asset interactions that linear models miss.
- **Deep Reinforcement Learning (DRL)**: Model-free agents directly maximizing Sharpe ratios while minimizing drawdowns.
- **Hybrid Model**: 
    - **Min Vol**: Core defensive ballast for inflationary/volatile regimes.
    - **Risk-Budgeting**: Maximize log-weighted carry subject to strict volatility targets (Risk Parity).

## 2. Covariance Matrix & Lookback
- **Best Practice**: **JMCE (Joint Mean-Covariance Estimators)** using neural architectures to learn conditional mean and **sliding-window covariance** simultaneously.
- **Lookback**: Treat lookback as a **dynamically tuned hyperparameter**.
    - **180-Day Window**: Standard for daily stock return forecasting (TFT models).
    - **100-Day Window**: Standard for macroeconomic carry strategies.
- **Stability**: Use **Shrinkage** and **JMCE** to handle distribution shifts and non-stationarity in multivariate time series.

## 3. Rebalancing Triggers & Thresholds
- **Trigger Strategy**: **Fixed Periodic (Monthly)** rebalancing is the institutional baseline to control overhead.
- **Drift/Efficiency Gate**: Tighter bands (3-5%) or **Monthly** resets.
- **Transaction Costs**: Apply rigorous **75 bps friction scaling** to ensure rebalancing yields genuine risk-adjusted alpha.
- **Turnover Management**: Balance the high-sensitivity of neural predictors (which can cause ~60% monthly turnover) against execution costs.

## 4. Constraints & Implementation
- **Position Limits**: Hard constraints ($w_i \le 10\%$) to mitigate idiosyncratic risk.
- **Sector Limits**: Soft constraints ($\pm 5\%$ relative to benchmark) to control systematic exposure.
- **Turnover Control**: Implement turnover as a **cost term** in the objective function rather than a hard limit, allowing for more flexible execution.
- **Math Library**: Use `scipy.optimize.minimize` with Sequential Least Squares Programming (SLSQP) for constrained non-linear optimization.

## 5. Integration Points for Growin App
- **PortfolioAnalyzer**: New module in `backend/utils/` to handle matrix math and optimization.
- **QuantEngine**: High-level interface `optimize_portfolio()` to be called by agents.
- **DataFabricator**: Parallel fetcher for historical series to build the covariance matrix.
