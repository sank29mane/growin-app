# Future Suggestions & Architectural Evolution
## Detailed Action Plan

| Area | Critique | Proposal |
|---|---|---|
| Quant Engine | `analyze_rebalancing_opportunity` assumes target allocations are given as valid fractions (e.g. 0.5) when users often provide percentages. | Standardize fractional representations inside `analyze_rebalancing_opportunity` to properly divide allocations > 1.0. *(Note: Addressed in recent fix, consider global percentage standardization).* |
| API Error Resilience | Basic global circuit breaker/retries are not wired effectively through backend endpoints. | Implement specialized CircuitBreakers inside critical agent APIs (`PortfolioAgent`, `ForecastingAgent`). |


| PortfolioAgent | Needs proper error handling if the `mcp_client.call_tool` raises an exception (e.g., Timeout, ConnectionError) instead of returning a success payload with an "error" key. | Wrap the `call_tool` execution in a `try...except` block with a `circuit_breaker` from `error_resilience.py` to prevent repeated calls to a dead MCP server. |
