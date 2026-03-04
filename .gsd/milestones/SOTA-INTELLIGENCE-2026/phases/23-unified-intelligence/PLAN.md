# Phase 23: Unified Financial Intelligence & Portfolio Analytics

## Goal
Centralize and standardize financial intelligence (Ticker Resolution + Technical Indicators) and implement a high-precision Portfolio Analytics engine for time-series risk metrics (SOTA 2026).

## Requirements
- [INTEL-01] Implement `TickerResolver` with tiered resolution (Cache -> API -> Search).
- [INTEL-02] Centralize `TechnicalIndicators` library in `backend/utils/financial_math.py`.
- [INTEL-03] Ensure 1:1 mathematical parity between MLX, Rust, and NumPy fallbacks.
- [RISK-01] Implement `PortfolioAnalyzer` for time-series risk metrics (Sharpe, Beta, Sortino).
- [RISK-02] Build "Backcast" history generator for approximation of portfolio risk on new accounts.
- [REFACT-01] Refactor `QuantEngine`, `QuantAgent`, and `PortfolioAgent` to use centralized libraries.

## Wave 1: Intelligence Foundation (Ticker & Math)
### 23-01: Unified Ticker Resolution Service
**Objective**: Eliminate ticker discrepancies and extraction fragility.
- **Task 1**: Refactor `backend/utils/ticker_utils.py`. Implement `TickerResolver` class with NLP-based ticker extraction and tiered resolution (Disk Cache -> Provider Verify -> Alpaca Search Fallback).
- **Task 2**: Centralize all special mappings (e.g., T212 suffixes) and US/UK exclusion logic into this resolver.
- **Verification**: `pytest tests/backend/test_ticker_resolution.py` with 100+ ambiguous tickers (e.g. `VOD.L`, `AAPL_US_EQ`, `3GLD`).

### 23-02: Unified Financial Math Library
**Objective**: Consolidate technical indicators into a single source of truth.
- **Task 1**: Migrate RSI, MACD, SMA, EMA, and BBands from `QuantEngine` to `backend/utils/financial_math.py`.
- **Task 2**: Implement explicit "Multi-Path" strategy in this library: `get_rsi(prices, backend='mlx'|'rust'|'numpy')`.
- **Verification**: `pytest tests/backend/test_unified_math.py` ensuring results from all backends match within 1e-6 tolerance.

## Wave 2: Portfolio Risk & Analytics
### 23-03: Time-Series Portfolio Analyzer
**Objective**: Implement institutional-grade portfolio performance metrics.
- **Task 1**: Create `backend/utils/portfolio_analyzer.py`. Implement `PortfolioAnalyzer` class to calculate Daily Returns, Volatility, Sharpe Ratio, and Sortino Ratio.
- **Task 2**: Implement Time-Series Beta calculation against a dynamic benchmark (e.g. SPY).
- **Verification**: `pytest tests/backend/test_portfolio_analyzer.py` using known historical datasets.

### 23-04: Portfolio Backcast History Generator
**Objective**: Approximate historical performance for incomplete datasets.
- **Task 1**: Move history generation logic from `PortfolioAgent` into `PortfolioAnalyzer`.
- **Task 2**: Implement "Backcast" algorithm that generates portfolio value history using position dates and historical price data.
- **Verification**: Verify history generation matches actual Alpaca portfolio history for test accounts.

## Wave 3: Integration & Clean-Up
### 23-05: System-Wide Refactoring
**Objective**: Delete redundant logic and ensure consistency.
- **Task 1**: Update `QuantEngine` to call the unified math library.
- **Task 2**: Refactor `QuantAgent`, `PortfolioAgent`, and `CoordinatorAgent` to use `TickerResolver`.
- **Verification**: Run `validate-all.sh` to ensure no regressions in existing flows.

### 23-06: Fallback & Cross-Platform Verification
**Objective**: Ensure reliability on different hardware/environments.
- **Task 1**: Implement "Missing Dependency" test suite. Mock `mlx` and `growin_core` as missing and verify all pure-Python fallbacks work.
- **Verification**: CI pass on both Apple Silicon (local) and standard Linux (CI/Cloud).

## Success Criteria
- [ ] `TickerResolver` correctly handles 100% of tested ambiguous T212/Alpaca symbols.
- [ ] Technical indicators from all 3 backends (MLX, Rust, NumPy) return identical results.
- [ ] `PortfolioAnalyzer` calculates Sharpe and Beta with <1% variance vs industry benchmarks.
- [ ] Redundant math logic removed from at least 3 agent files.
