# Strategic Suggestions

This document tracks architectural improvements and technical debt that require significant refactoring or user approval.

## 1. Unified Ticker Resolution Service

**Status:** In Progress (Tier 1 Native Rust Mitigation Applied)
**Priority:** High
**Context:**
The current ticker normalization logic (`backend/trading212_mcp_server.py:normalize_ticker`) relies on fragile regex heuristics and hardcoded exclusion lists to distinguish between US and UK stocks. This caused a bug where US tickers like `IBM`, `F`, `SMCI`, `RDDT`, and `ARM` were incorrectly suffixed with `.L`. A tactical fix expanded the exclusion list and refactored constants for performance, but the fundamental fragility remains.

**Problem:**
- Heuristics (e.g., "length <= 4 implies UK") are inaccurate for a global market.
- Maintaining hardcoded lists (`us_exclusions`) is a game of whack-a-mole.
- Inconsistent behavior across agents (some use the MCP server, others might use raw input).
- `AlpacaClient` and `FinnhubClient` have different expectations for ticker formats.

**Proposal:**
Implement a dedicated `TickerResolutionService` (or Agent) backed by a master symbol database (e.g., SQLite or a lightweight trie).
- **Capabilities (Tiered Approach):**
    - **Tier 1 (Cache/Rules):** Check against a curated local database of common tickers (US & UK) for ambiguous symbols like `VOD`.
    - **Tier 2 (API Verification):** Use `yfinance` or `Alpaca` info APIs to verify if a ticker exists on the assumed exchange before appending suffixes.
    - **Tier 3 (Search):** Use `search_instruments` only as a fallback.
    - Support for ISIN/CUSIP lookup if available.
    - Context-aware resolution (e.g., "Buy Vodafone" -> prefer user's home exchange).
- **Implementation:**
    - A standalone module in `backend/services/`.
    - Populate a local cache/db from Trading212 and Alpaca instrument lists on startup.
    - Expose a clean API: `resolve(symbol: str, context: UserContext) -> NormalizedTicker`.

**Impact:**
- Eliminates "wrong exchange" errors.
- Improves user trust.
- simplifies `CoordinatorAgent` and `DataEngine` logic.

## 2. Async Data Pipeline

**Status:** Proposed
**Priority:** Medium
**Context:**
Data fetching is currently mixed between `asyncio` calls and blocking libraries (like `yfinance` or `pandas` operations) wrapped in `run_in_executor`.

**Proposal:**
Migrate to a fully async data pipeline where possible, or isolate blocking compute (heavy pandas math) into a separate process/worker to avoid blocking the main event loop, especially as user load increases.

## 3. Improved Search Fallback
**Issue:**
In `market_routes.py`, the search fallback when MCP is unavailable is a hardcoded list of ~8 tech stocks (`common_tickers`). This provides a poor user experience if the connection to T212 is lost or in development mode.

**Proposal:**
Implement a real fallback using `yfinance` or `Alpaca` search.
- If MCP is offline, use `yfinance.Ticker(query)` or `alpaca.data_client` to find matches.
- This ensures users can still "search" for stocks even without a live broker connection.

## 4. Concurrency Stress Testing
**Issue:**
The `QuantAgent` and `PortfolioAgent` use `asyncio.gather` and thread pools (`run_in_executor`). While manual locks (`portfolio_lock`) are in place, there is no formalized stress testing for race conditions under high load.

**Proposal:**
Add a `tests/stress_test.py` suite using `locust` or `asyncio` to simulate 100+ concurrent agent requests.
- Verify `SafePythonExecutor` isolation under load.
- Verify `cache_manager` thread safety.

## 5. SafePythonExecutor Audit
**Status:** Mitigated (AST Validation Applied)
**Issue:**
The `SafePythonExecutor` relies on `ast` parsing and `exec`. While it blocks common dangerous imports, `exec` is inherently risky.

**Mitigation (Applied):**
Refactored `_validate_code` to use robust AST traversal instead of fragile string matching. Now blocks `ast.Call` nodes for dangerous builtins (`exec`, `eval`, `open`) and `ast.Attribute` access to dunder methods (`__subclasses__`). Verified with exploit tests.

**Proposal (Long-term):**
Consider migrating to a WASM-based runtime (e.g., `pyodide` or a separate sandboxed process) for `CoordinatorAgent` code execution to strictly isolate memory and CPU usage.

## 6. Cross-Platform Portfolio Metrics (MLX Fallback)
**Status:** Resolved (Refactored to Standard Python)
**Issue:**
The `QuantEngine.calculate_portfolio_metrics` method strictly depended on `mlx` (Apple Silicon optimized machine learning library). It was also calculating time-series metrics (Sharpe, Beta) using cross-sectional data, which was mathematically incorrect.

**Resolution:**
Refactored the method to calculate only snapshot metrics (Total Value, Cost, PnL, Weighted Return) using standard Python sum/math operations. Removed `mlx` dependency for this specific function as it was unnecessary overhead for simple arithmetic.

## 7. Unified Financial Math Library
**Status:** Proposed
**Priority:** Medium
**Context:**
Currently, `QuantEngine` (backend/quant_engine.py), `QuantAgent` (backend/agents/quant_agent.py), and `PortfolioAgent` (backend/agents/portfolio_agent.py) all contain overlapping logic for technical indicators (RSI, MACD, SMA) and portfolio math. Some use `pandas_ta`, some use `talib`, and others use custom `numpy`/`pandas` implementations.

**Proposal:**
Create a single `growin_math` or `backend/lib/financial_math.py` module that serves as the Source of Truth.
- Implement robust, vectorized versions of all core indicators.
- Agents should import from this library instead of re-implementing logic.
- Ensure consistent handling of edge cases (NaNs, insufficient data) across the app.

## 8. True Portfolio Analysis Module
**Status:** Proposed
**Priority:** High
**Context:**
The current `calculate_portfolio_metrics` in `QuantEngine` was previously attempting to calculate Sharpe Ratio, Beta, and Volatility using a single snapshot of positions. This is dimensionally incorrect (Cross-Sectional vs Time-Series).

**Proposal:**
Implement a proper `PortfolioAnalyzer` class that:
- Ingests **historical** portfolio value (time-series).
- Calculates true Daily Returns, Volatility (std dev of daily returns), Sharpe Ratio, Sortino Ratio, and Beta against a benchmark.
- Can leverage the "Backcast" history generation logic currently in `PortfolioAgent` to approximate history if real transaction data is missing.
