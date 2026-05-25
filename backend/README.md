# Growin Backend: AI Financial Intelligence Engine

This is the Python-based heart of the Growin App. It orchestrates a multi-agent system to provide real-time financial analysis, leveraging local LLMs, a dedicated serving layer, and Rust-accelerated quantitative engines.

---

## 🚀 SOTA Tech Stack (v5.0 — May 2026)

*   **Runtime**: Python 3.12+ optimized with `uv` for ultra-fast dependency resolution and virtual environment isolation.
*   **API Framework**: FastAPI with full `asyncio` support for non-blocking agent orchestration.
*   **Local Inference**: **vMLX** (jjang-ai) integration serving local models on Apple Silicon GPU utilizing PagedAttention.
*   **Primary Models**: **Gemma 4 26B A4B MoE** (primary reasoning hub) paired with **Nemotron-Cascade-2 30B** (executive synthesis and code generation).
*   **Background Processing**: **arq** for async Redis-backed background workers and long-running optimization tasks.
*   **Serialization**: **orjson** for high-throughput, ultra-fast JSON serialization.
*   **Streaming**: **sse-starlette** for high-efficiency Server-Sent Events (SSE) reasoning trace delivery.
*   **Performance Core**: **Rust** (`growin_core`) for high-throughput quantitative math and technical indicator calculation.
*   **Precision**: `decimal.Decimal` based financial math layer to guarantee arithmetic accuracy.

---

## 📁 Directory Structure

```
backend/
├── agents/             # Specialist agents and coordinators
│   └── social_swarm/   # Reddit & Twitter micro-agents
├── routes/             # FastAPI endpoint routers (chat, status, market)
├── models/             # Neural ODE and local MLX model architectures
├── workers/            # arq background task processors (e.g. portfolio optimization)
├── backtest_lab/       # Historical backtesting simulation environments
├── benchmarks/         # Performance profiling and latency benchmarks
├── fallbacks/          # Graceful degradation handlers for API and serving layers
├── utils/              # Shared utilities (SafePythonExecutor sandbox, TickerResolver)
└── growin_core_src/    # Rust source code for the quantitative math engine
```

---

## 🔗 Connection Architecture

Growin employs a robust connection layer designed to survive high-concurrency swarm queries without operating system resource leakage.

*   **AgentHttpClient**: A single, centralized global HTTP client utilizing `httpx.AsyncClient` with connection pooling. Replaces per-agent individual client instantiations.
*   **Circuit Breakers**: Endpoint-specific circuit breakers to prevent cascade failures. If an external service (e.g., Alpaca or Trading 212) exhibits failures, the circuit trips immediately to route requests to local fallback estimators.
*   **Rate Limiting**: Enforces strict Token Bucket rate limits per destination to comply with broker API limits.

---

## 🛠️ Quick Start

### 1. Install `uv` (Mandatory Package Manager)
```bash
brew install uv
```

### 2. Environment Setup
```bash
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

### 3. Start System Services
Use the main project scripts at the root directory to manage FastAPI, Redis, and vMLX serving layers:
```bash
# Start all backend services
./start.sh

# Stop all backend services cleanly
./stop.sh
```

---

## 🧪 Testing & Quality Assurance

Run the comprehensive test suite including vision, LLM evaluation, and benchmark suites:
```bash
# Run unit and integration tests
uv run pytest

# Run performance benchmarks
uv run pytest tests/benchmarks/ -v

# Run agent visual processing evaluations
uv run pytest tests/vision/ -v
```
*   **DeepEval Integration**: Incorporates LLM-in-the-loop validation for agent outputs, verifying factual consistency, trading constraint adherence, and semantic accuracy.

---

## 🛡️ Security & Sandboxing

*   **GovernanceService**: Central security module enforcing role-based capabilities, query budget limits, and runtime token verification on the agentic swarm.
*   **SafePythonExecutor**: Generated mathematical scripts and Monte Carlo simulations run in secure, sandboxed Docker containers.
*   **Error Sanitization**: Route-level filters scrub raw database tracebacks and agent execution logs of private variables before transmitting client-facing errors.
*   **Secret Masking**: FastAPI middleware scrubs sensitive keys (API tokens, passwords) from system logs.

---

## 📈 Agentic Swarm Workflow

The backend uses a high-performance **Coordinator-Specialist** design:

1.  **Ingress & Governance**: The user request passes through the **GovernanceService** for validation and rate limiting.
2.  **SwarmOrchestrator**: Uses a 2-stage streaming pipeline to classify intent. It consults the **Strategy Suggestion Engine** for real-time ledger alpha, and routes to appropriate specialists.
3.  **Parallel Specialists**: Specialist agents execute concurrently:
    -   *QuantAgent*: Vectorized indicator math.
    -   *ForecasterAgent*: Prediction models using ANE-based Neural JMCE.
    -   *ResearchAgent & Social Swarm*: Tavily RAG searches and sentiment profiling on Reddit/Twitter.
    -   *WhaleAgent*: Flow and block-trade metrics.
    -   *VisionAgent*: Non-blocking chart analysis via `prepare_vlm_image_async()`.
    -   *DividendOptimizationAgent*: Yield tax-drag models.
    -   *GoalPlannerAgent*: Financial target milestones.
4.  **Trajectory Optimization**: The **R-Stitch Engine** dynamically stitches outputs from Small Language Models (SLMs) and Large Language Models (LLMs) to optimize speed.
5.  **Synthesis**: The final response is generated using local **vMLX** inference, streaming thoughts via Server-Sent Events (SSE).

---

## 🔭 Observability & Audit

*   **SSE Streaming**: Real-time token and agent telemetry delivery.
*   **Reasoning Trace**: Full visibility into the multi-agent decision chain via `GET /api/telemetry/trace/{id}`.
*   **Tamper-Evident Audit**: Cryptographically chained logs at `backend/data/audit.log` (SHA-256/RFC 8785) ensure institutional compliance.
