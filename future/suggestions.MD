# Detailed Action Plan
| Suggestion | Rationale |
|---|---|
| Centralize Ticker Normalization | `normalize_ticker` and fallback parsing logic is duplicated across multiple components (e.g., `trading212_mcp_server`, `coordinator_agent`, `data_engine`). Centralizing this into a single rust-core accessible module or dedicated microservice will reduce drift and bugs. |
