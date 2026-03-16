<<<<<<< HEAD
# Future Architectural Suggestions

## Detailed Action Plan

| Suggestion | Rationale | Status |
| :--- | :--- | :--- |
| **Consolidate Circuit Breakers (`backend/utils/error_resilience.py` vs `backend/resilience.py`)** | The codebase has two separate, overlapping implementations of the `CircuitBreaker` pattern. The implementation in `resilience.py` is more robust (utilizes Enums for states, provides decorator patterns, better logging). We should deprecate `utils.error_resilience.py`, migrate all agents (`decision_agent.py`, `portfolio_agent.py`, `forecasting_agent.py`, etc.) to use `resilience.py`, and eliminate the redundancy. | Proposed |
| **Replace threadpool external API calls with native Asyncio HTTP clients** | Several modules (like `research_agent.py`, `social_agent.py`, and `forecaster.py`) use `asyncio.to_thread` or `loop.run_in_executor` to wrap synchronous libraries (like `newsapi`, `tavily`, `yfinance`). While functional, this can bottleneck the threadpool under high concurrency. We should replace these with native `httpx.AsyncClient` calls (as done in `t212_handlers.py`) to maximize scalability and reduce context-switching overhead. | Proposed |
=======
# Strategic Suggestions and Action Plan

This document tracks visionary or architectural suggestions identified during code exploration as part of the Jules-Loop protocol. Items listed here represent high-value improvements or refactoring efforts that should be considered for future implementation.

## Detailed Action Plan

| ID | Title | Description | Reason/Critique | Status |
|---|---|---|---|---|
| 001 | Centralize Async Timeout Logic | Create a centralized helper (e.g., `utils.async_utils.run_with_timeout`) to manage `asyncio.timeout` / `asyncio.wait_for` across the codebase. | Currently, the logic for checking `hasattr(asyncio, 'timeout')` is duplicated across multiple agents (`CoordinatorAgent`, `DecisionAgent`, `PortfolioAgent`). Centralizing this will prevent future Python < 3.11 compatibility bugs and reduce boilerplate code. | Pending |
>>>>>>> origin/jules-loop/coordinator-timeout-fallback-7141826787811951533
