# Phase 23 Research: Unified Financial Intelligence & Portfolio Analytics

## 1. Ticker Resolution Fragmentation
**Findings:**
- `normalize_ticker` is duplicated across `backend/utils/ticker_utils.py` (Python) and `backend/growin_core_src/src/lib.rs` (Rust).
- Multiple agents and routes (e.g., `CoordinatorAgent`, `DataEngine`, `market_routes.py`) perform their own local cleaning or import from different places.
- `CoordinatorAgent` has its own `extract_ticker_from_text` logic using regex.

**Strategy:**
- Move all logic into a single `TickerResolver` class in `backend/utils/ticker_utils.py`.
- The Python utility will remain the entry point, but it will prefer calling the Rust implementation if available for performance.
- Implement tiered resolution: Check Cache -> API Verify -> Search Fallback.

## 2. Financial Math & Technical Indicators
**Findings:**
- `QuantEngine` implements RSI, MACD, SMA, and BBands using 4 different paths: MLX, Rust, Pandas-TA, and Pure Pandas.
- `QuantAgent` and `PortfolioAgent` have overlapping logic for basic math.
- There is no central library for high-precision financial arithmetic (though `utils/financial_math.py` exists for `Decimal` creation).

**Strategy:**
- Consolidate all technical indicator logic into `backend/utils/financial_math.py`.
- `QuantEngine` should call this library instead of having inline implementations.
- Ensure 1:1 mathematical parity between MLX, Rust, and NumPy fallbacks.

## 3. Portfolio Analysis (Time-Series vs Cross-Sectional)
**Findings:**
- `calculate_portfolio_metrics` currently only provides a "snapshot" (Current Value vs Cost).
- Attempts to calculate Sharpe/Beta in previous versions were using snapshot data, which is mathematically invalid for time-series risk metrics.
- `PortfolioAgent` has some "backcasting" logic to generate history, but it's not standardized.

**Strategy:**
- Implement a `PortfolioAnalyzer` in `backend/utils/portfolio_analyzer.py`.
- Core requirements: Daily Returns calculation, Volatility (annualized), Sharpe Ratio, Sortino Ratio, and Beta against a benchmark (SPY).
- Use `Decimal` for precision but `NumPy`/`MLX` for vectorized performance.

## 4. Testing & Fallback Gaps
**Findings:**
- Tests currently assume a full environment. If `mlx` or `scipy` is missing, tests might fail or skip without verifying the fallback.

**Strategy:**
- Add a dedicated test suite `tests/backend/test_unified_intelligence.py` that mocks the absence of optimized libraries to verify pure-Python fallbacks.
