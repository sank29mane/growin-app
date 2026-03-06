# Future Architectural Suggestions

## Detailed Action Plan

| Suggestion | Rationale | Status |
| :--- | :--- | :--- |
| **Centralize Input Sanitization & Defensive Typing** | Defensive type checking (e.g. against `None` or missing string representations) and normalization (e.g. `ticker.isalpha()` vs `ticker.isalnum()`) is scattered or ad-hoc. Moving this logic to dedicated utility functions or Pydantic validators would reduce cognitive load and prevent runtime crashes. | Proposed |
