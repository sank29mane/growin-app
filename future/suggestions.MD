# Future Architectural Suggestions

## Detailed Action Plan

| Suggestion | Rationale | Status |
| :--- | :--- | :--- |
| Refactor Duplicate CircuitBreaker implementations | There are two overlapping `CircuitBreaker` classes in `backend/error_resilience.py` and `backend/utils/error_resilience.py`. They have different APIs (e.g. `can_proceed()` vs `.call()`). These should be unified to prevent bugs and API misuse. | Open |
| Add CircuitBreaker to DecisionAgent tool calls | In `backend/agents/decision_agent.py`, `mcp.call_tool` is executed without a `CircuitBreaker`. In `PortfolioAgent` and `ForecastingAgent`, it is protected by a module-level `CircuitBreaker` which is safer. Wrapping these calls in `DecisionAgent` will prevent external service failures from cascading. | Open |
| **Centralize Input Sanitization & Defensive Typing** | Defensive type checking (e.g. against `None` or missing string representations) and normalization (e.g. `ticker.isalpha()` vs `ticker.isalnum()`) is scattered or ad-hoc. Moving this logic to dedicated utility functions or Pydantic validators would reduce cognitive load and prevent runtime crashes. | Proposed |
