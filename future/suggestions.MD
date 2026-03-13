# Future Architectural Suggestions

## Detailed Action Plan

| Suggestion | Rationale | Status |
| :--- | :--- | :--- |
| **Consolidate Circuit Breakers (`backend/utils/error_resilience.py` vs `backend/resilience.py`)** | The codebase has two separate, overlapping implementations of the `CircuitBreaker` pattern. The implementation in `resilience.py` is more robust (utilizes Enums for states, provides decorator patterns, better logging). We should deprecate `utils.error_resilience.py`, migrate all agents (`decision_agent.py`, `portfolio_agent.py`, `forecasting_agent.py`, etc.) to use `resilience.py`, and eliminate the redundancy. | Proposed |
| **Replace threadpool external API calls with native Asyncio HTTP clients** | Several modules (like `research_agent.py`, `social_agent.py`, and `forecaster.py`) use `asyncio.to_thread` or `loop.run_in_executor` to wrap synchronous libraries (like `newsapi`, `tavily`, `yfinance`). While functional, this can bottleneck the threadpool under high concurrency. We should replace these with native `httpx.AsyncClient` calls (as done in `t212_handlers.py`) to maximize scalability and reduce context-switching overhead. | Proposed |
