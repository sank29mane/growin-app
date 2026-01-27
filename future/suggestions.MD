# Strategic Suggestions & Technical Debt

## 1. Robust Ticker Resolution Service
**Issue:**
Ticker normalization is currently fragmented between `CoordinatorAgent` (LLM-based + Python script) and `Trading212McpServer` (Regex + Heuristics). This leads to inconsistencies, specifically with short US tickers (e.g., "IBM", "F") being incorrectly normalized to UK tickers (e.g., "IBM.L") by the MCP server's aggressive heuristic.

**Proposal:**
Consolidate normalization logic into a dedicated **Ticker Resolution Service**.
- **Tier 1 (Cache/Rules):** Check against a curated local database of common tickers (US & UK).
- **Tier 2 (API Verification):** Use `yfinance` or `Alpaca` info APIs to verify if a ticker exists on the assumed exchange before appending suffixes.
- **Tier 3 (Search):** Use `search_instruments` only as a fallback.
- **Implementation:** Create a shared `TickerResolver` class used by both the Coordinator and the MCP Server.

## 2. Improved Search Fallback
**Issue:**
In `market_routes.py`, the search fallback when MCP is unavailable is a hardcoded list of ~8 tech stocks (`common_tickers`). This provides a poor user experience if the connection to T212 is lost or in development mode.

**Proposal:**
Implement a real fallback using `yfinance` or `Alpaca` search.
- If MCP is offline, use `yfinance.Ticker(query)` or `alpaca.data_client` to find matches.
- This ensures users can still "search" for stocks even without a live broker connection.

## 3. Concurrency Stress Testing
**Issue:**
The `QuantAgent` and `PortfolioAgent` use `asyncio.gather` and thread pools (`run_in_executor`). While manual locks (`portfolio_lock`) are in place, there is no formalized stress testing for race conditions under high load.

**Proposal:**
Add a `tests/stress_test.py` suite using `locust` or `asyncio` to simulate 100+ concurrent agent requests.
- Verify `SafePythonExecutor` isolation under load.
- Verify `cache_manager` thread safety.

## 4. SafePythonExecutor Audit
**Issue:**
The `SafePythonExecutor` relies on `ast` parsing and `exec`. While it blocks common dangerous imports, `exec` is inherently risky.

**Proposal:**
Consider migrating to a WASM-based runtime (e.g., `pyodide` or a separate sandboxed process) for `CoordinatorAgent` code execution to strictly isolate memory and CPU usage.
