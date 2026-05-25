# Backend Architecture Evolution Plan

This document details the backend architectural roadmap, tracing our development from simple local modules into a robust, high-performance containerized multi-agent system.

---

## v5.0 Production Readiness & Scale (May 2026)

The modern iteration of the Growin backend focuses on serving efficiency, high-throughput local AI execution, and system-level resilience.

### 1. Dual-Model Local Inference & vMLX Serving
- **Evolution**: Migrated from standard single-model REST configurations to a local **dual-model serving architecture** pairing **Gemma 4 26B A4B MoE** and **Nemotron-Cascade-2 30B**.
- **Implementation**: Replaced unmanaged local model loads with a dedicated local server `vmlx_manager.py` using **PagedAttention** and continuous batching.
- **Benefit**: Enabled multi-agent concurrent serving without memory thrashing, keeping weights and KV caches within a strict 60% memory limit (≤ 28GB on 48GB M4 Pro configurations).

### 2. Centralized Connection Pooling & AgentHttpClient
- **Evolution**: Transitioned from individual per-agent client instantiations (`requests` or standard `httpx.AsyncClient` calls per request) to a centralized `AgentHttpClient` pool.
- **Implementation**: The pool maintains a single persistent `httpx.AsyncClient` state, enforces endpoint circuit breakers, and applies token-bucket rate limits.
- **Benefit**: Completely resolved TCP socket leaks and resource exhaustion issues, dropping outbound connection latency by ~40%.

### 3. Asynchronous Pipeline Overhauls
- **Vision Pipeline**: vision processing operations were moved from synchronous PIL calls to a thread-executor wrapper: `prepare_vlm_image_async()`.
- **Database Optimizations**: Avoided slow N+1 query patterns by executing vectorized batch OHLCV retrievals (`get_recent_ohlcv`) across the entire portfolio in a single pass in DuckDB.
- **DuckDB Temporal Compatibility**: Rewrote all temporal query functions to use native Python `datetime` arithmetic, replacing fragile database `INTERVAL` syntax.

---

## Future Architecture (Next Generations)

### 1. Containerization & MicroVM Sandboxing
- **Objective**: Standardize reproducible deployments and isolate agent sandbox tasks.
- **Dockerfile Strategy**: Multi-stage Python build keeping image footprint low:
  - Base Image: `python:3.12-slim`
  - Builder Stage: Installs build dependencies (gcc, cargo, etc.) and compiles requirements.
  - Runtime Stage: Copies virtualenv and minimal application code.
- **Docker Compose Setup**:
  - `backend`: The FastAPI application server.
  - `redis`: Shared cache and arq background task worker queue.
  - `growin-sandbox`: Local NPU compute image running generated mathematical modeling scripts safely.

### 2. Monitoring, Observability & Tracing
- **OpenTelemetry (OTEL)**: Instrument `GlobalTracer` to trace requests through FastAPI -> SwarmOrchestrator -> vMLX models -> tool execution.
- **Prometheus Metrics**:
  - `agent_execution_time`: Latency histograms per specialist agent.
  - `vmlx_cache_hit_rate`: Monitor KV-cache reuse.
  - `circuit_breaker_trips`: Track third-party broker API outages.
- **Grafana Dashboard**: Visualizing agent execution times, system memory pressures, and audit logging statistics.

### 3. CI/CD Pipelines
- **Quality Gates**: Automate Ruff checking, mypy type checking, and pytest verification suite runs.
- **DeepEval Pipelines**: Continuous integration tests utilizing LLM-in-the-loop evaluation to ensure model fine-tuning or prompt updates do not degrade advisory quality.
