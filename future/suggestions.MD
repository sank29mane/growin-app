# Strategic Suggestions and Action Plan

This document tracks visionary or architectural suggestions identified during code exploration as part of the Jules-Loop protocol. Items listed here represent high-value improvements or refactoring efforts that should be considered for future implementation.

## Detailed Action Plan

| ID | Title | Description | Reason/Critique | Status |
|---|---|---|---|---|
| 001 | Centralize Async Timeout Logic | Create a centralized helper (e.g., `utils.async_utils.run_with_timeout`) to manage `asyncio.timeout` / `asyncio.wait_for` across the codebase. | Currently, the logic for checking `hasattr(asyncio, 'timeout')` is duplicated across multiple agents (`CoordinatorAgent`, `DecisionAgent`, `PortfolioAgent`). Centralizing this will prevent future Python < 3.11 compatibility bugs and reduce boilerplate code. | Completed |
| 002 | Consolidate Circuit Breakers | Merge `backend/utils/error_resilience.py` and `backend/resilience.py`. | Codebase redundancy. `resilience.py` is more robust (Enums, decorators, logging). Deprecate `utils.error_resilience.py` and migrate all agents to `resilience.py`. | Proposed |
| 003 | Native Asyncio HTTP Clients | Replace threadpool external API calls with native Asyncio HTTP clients. |Bottleneck risk under high concurrency in `research_agent.py`, `social_agent.py`, and `forecaster.py`. Replace `asyncio.to_thread` with `httpx.AsyncClient`. | Proposed |
| 004 | Strategy Suggestion Engine | Enhance the Coordinator Agent to provide proactive "Strategic Suggestions" (e.g., diversification tips, tax-loss harvesting alerts) during routine portfolio analysis. | Current analysis is reactive. Proactive suggestions increase the "Alpha" value of the agentic swarm. | Proposed |
| 005 | Quant Engine Standardization | `analyze_rebalancing_opportunity` assumes target allocations are given as valid fractions (e.g. 0.5) when users often provide percentages. | Standardize fractional representations inside `analyze_rebalancing_opportunity` to properly divide allocations > 1.0. | Proposed |
