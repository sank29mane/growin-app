# Strategic Suggestions

This document tracks architectural improvements and technical debt that require significant refactoring or user approval.

## 1. Unified Ticker Resolution Service

**Status:** In Progress (Partial Mitigation Applied)
**Priority:** High
**Context:**
The current ticker normalization logic (`backend/trading212_mcp_server.py:normalize_ticker`) relies on fragile regex heuristics and hardcoded exclusion lists to distinguish between US and UK stocks. This caused a bug where US tickers like `IBM`, `F`, and `SMCI` were incorrectly suffixed with `.L`. A tactical fix expanded the exclusion list and refactored constants for performance, but the fundamental fragility remains.

**Problem:**
- Heuristics (e.g., "length <= 4 implies UK") are inaccurate for a global market.
- Maintaining hardcoded lists (`us_exclusions`) is a game of whack-a-mole.
- Inconsistent behavior across agents (some use the MCP server, others might use raw input).
- `AlpacaClient` and `FinnhubClient` have different expectations for ticker formats.

**Proposal:**
Implement a dedicated `TickerResolutionService` (or Agent) backed by a master symbol database (e.g., SQLite or a lightweight trie).
- **Capabilities:**
    - unambiguous resolution of `VOD` -> `VOD.L` (LSE) vs `VOD` (Nasdaq).
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
