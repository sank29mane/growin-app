# Strategic Suggestions

## Detailed Action Plan

| Priority | Area | Suggestion | Rationale |
| :--- | :--- | :--- | :--- |
| High | Architecture | **Centralized Currency Service** | Currently, currency conversion logic (e.g., pence to pounds for LSE) is scattered across `market_routes.py` and potentially other files, using ad-hoc heuristics (e.g., `value > 500`). This is fragile and error-prone for high-value stocks or non-standard tickers. A dedicated `CurrencyService` or explicit `currency` field in data models should handle normalization centrally. |
| Medium | Architecture | **Async Agent Initialization** | `QuantAgent` initializes `talib` and `CoreML` in `__init__`, which might block startup. Moving heavy initialization to an async `start()` method or using lazy loading could improve startup time. |
