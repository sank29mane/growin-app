# Growin App: Architectural Learnings & Optimization Log

## 📍 Architecture Resilience (Data Sourcing)
- **Mandate:** Absolute partitioning of data sources by region.
- **Resilient Strategy (Implemented 2026-02-23):**
    - **US Stocks:** Alpaca API (Primary) → yfinance (Fallback).
    - **UK/LSE Stocks:** Finnhub (Primary) → yfinance (Fallback).
    - **Global Fallback:** Yahoo Finance (yfinance) integrated as the universal mandatory backup.
- **Benefit:** Guarantees data provenance while ensuring the UI never shows "Empty" state if a specific provider is down.

## 🧠 Decision Model Evolution
- **Persona:** Elevated to "Lead Financial Trader" (Assertive, Executive, yet Friendly).
- **Consultation Flow:** Decision Model is now the client-facing primary. It explicitly consults the `CoordinatorAgent` for multi-specialist insights (Quant, Forecast, Research) before synthesizing a final recommendation.
- **Expertise:** Instructed to use deep general financial knowledge to answer "Abstract" questions (e.g., "Why is my portfolio flat?") by correlating broad market trends with specific Trading 212 holdings.

## ⚡ Blazing Fast Performance & Efficiency
- **Caching (L1/L2):** 
    - L1: In-memory `OrderedDict`.
    - L2: `Redis` integration added for persistent, shared cross-request caching.
    - **Learning:** On Apple Silicon (M4 Pro), keeping model weights and KV cache within 60% of physical RAM is critical to avoid SSD swap latency.
- **OLAP Speed:** `AnalyticsDB` (DuckDB) used for time-series aggregations. Vectorized `bulk_insert` is ~100x faster than traditional iterative SQL inserts.
- **Python Sandbox:** Integrated Secure Docker MCP tool (`docker_run_python`) allowing the agent to perform live Monte Carlo simulations and custom mathematical modeling without blocking the main event loop.
- **NPU Optimization:** Enhanced the Python Sandbox with an `engine: "npu"` option. This utilizes a specialized Docker image pre-configured with MLX and Core ML to offload heavy mathematical modeling to the Apple Neural Engine (ANE), ensuring "blazing fast" local compute for dynamic analysis.

## 🚀 LM Studio REST API & Concurrency (2026 Research)
- **Parallel Requests:** LM Studio 0.4.0+ introduced true parallel request support via **Continuous Batching** in the `llama.cpp` engine.
- **Max Concurrent Predictions:** Configurable setting to control how many simultaneous inference tasks a single model can handle (optimized for Apple Silicon memory bandwidth).
- **Unified KV Cache:** Enabled by default in 0.4.0+, this allows the model to share memory between concurrent requests efficiently, reducing overhead for varied prompt lengths.
- **Scalability Pattern:** Implement a **Load Balancing Wrapper** in Python to distribute requests across multiple loaded models if total physical RAM allows (>60GB), utilizing different "slots" or ports for extreme throughput.

## 🛡️ Security & Integrity
- **Error Sanitization:** All database and API errors are sanitized at the route level to prevent sensitive string leaks (DB strings, API keys).
- **Validation:** `PriceValidator` refactored to use region-locked providers, preventing cross-contamination during variance checks.

## 🧪 Future Optimization Notes (NotebookLM Research)
- **TTFT (Time to First Token):** For agentic workflows, TTFT is the bottleneck. Recommendation: Implement **Content-Based Prefix Caching** for shared agent system prompts to reduce TTFT by up to 5.8x.
- **Throughput:** Utilize `vllm-mlx` for continuous batching if concurrency exceeds 10+ simultaneous users.
