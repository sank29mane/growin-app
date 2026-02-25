# Strategic Suggestions

This document tracks architectural improvements and technical debt that require significant refactoring or user approval.

## 1. Unified Ticker Resolution Service

**Status:** In Progress (Tier 1 Native Rust Mitigation Applied, Tier 2/3 Search Fallbacks Enhanced)
**Priority:** High
**Context:**
The current ticker normalization logic (`backend/trading212_mcp_server.py:normalize_ticker`) relies on fragile regex heuristics and hardcoded exclusion lists to distinguish between US and UK stocks. `CoordinatorAgent` was missing fallback logic for malformed tickers (e.g., `VOD.L.`).

**Recent Improvements:**
- Added `_attempt_ticker_fix` in `CoordinatorAgent` to handle punctuation and cleaning.
- Enhanced `extract_ticker_from_text` to support alphanumeric tickers (e.g. `3GLD`) and stop words.

**Problem:**
- Heuristics (e.g., "length <= 4 implies UK") are inaccurate for a global market.
- `extract_ticker_from_text` is still regex/heuristic based.
- Inconsistent behavior across agents (some use the MCP server, others might use raw input).

**Proposal:**
Implement a dedicated `TickerResolutionService` (or Agent) backed by a master symbol database.
- **Capabilities:**
    - **NLP Extraction:** Replace regex with a small NER model (Spacy/LLM) to accurately identify tickers in complex queries ("Compare AAPL vs MSFT").
    - **Unified Normalization:** Centralize all cleaning logic (stripping dots, handling suffixes) in one module, removing duplicated logic in Agents.
    - **Tiered Resolution:** Cache -> API Verification -> Search Fallback.

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

## 9. Comprehensive Fallback Testing Strategy
**Status:** Proposed
**Priority:** Medium
**Context:**
A critical bug was found in the fallback logic of `QuantEngine.calculate_pivot_levels` which crashed when optional dependencies (`scipy`) were missing. This highlights a gap in testing where only the "happy path" (full environment) is tested.

**Proposal:**
Implement a test strategy that explicitly mocks the absence of optional dependencies (e.g. `sys.modules['scipy'] = None`) to verify fallback paths.
- Add CI matrix or parameterized tests to run test suites with and without optional packages (MLX, Scipy, Pandas-TA).
- Ensure all fallback implementations (pure Python/Pandas) are mathematically equivalent to the optimized versions.

## 10. Standardized Timezone Handling
**Status:** In Progress (Partial Fixes Applied)
**Priority:** Medium
**Context:**
`FinnhubClient` was using `datetime.now()` (local time) while `AlpacaClient` correctly uses UTC. This inconsistency can lead to data gaps or overlapping bars when requesting historical data across different providers.

**Proposal:**
Enforce `datetime.now(timezone.utc)` globally across all Data Engine clients.
- Create a `TimeUtils.now_utc()` helper to prevent regressions.
- Audit all `datetime` instantiations in `backend/data_engine.py` and `backend/utils/`.
