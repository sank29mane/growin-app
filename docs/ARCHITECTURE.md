# Growin Architecture: Comprehensive AI-Powered Portfolio Intelligence Platform

## Executive Summary

**Growin** is a sophisticated financial intelligence platform that combines advanced artificial intelligence with real-time market data to provide intelligent portfolio analysis, automated trading insights, and conversational financial advice. It adheres to SOTA best practices for **Agentic Autonomy**, **Financial Precision**, and **Hardware-Aware Local Inference**.

### System Vision
To democratize sophisticated financial analysis by providing retail investors with institutional-grade portfolio intelligence through an intuitive, AI-powered macOS application optimized for Apple Silicon (M4 generation) hardware.

---

## 1. System Context & High-Level Architecture

### System Context Diagram
```mermaid
graph TB
    subgraph "External Environment"
        T212[Trading 212<br/>MCP Server]
        ALP[Alpaca Markets<br/>Real-time Data]
        YF[yFinance<br/>Universal Fallback]
        NEWS[Tavily Search API<br/>Market News & Socials]
    end

    subgraph "Growin Platform (macOS Native)"
        UI[macOS SwiftUI Frontend<br/>Sovereign UI & VoiceOver]
        API[FastAPI Backend<br/>uv Virtual Env]
        REDIS[Redis L2 Cache<br/>& arq Background Tasks]
        AUDIT[Audit Log<br/>Autonomous History]
    end

    subgraph "Local Inference (MLX Direct)"
        MLX[MLX Engine<br/>mlx_lm / mlx_vlm]
        GEMMA4[Gemma 4 26B MoE<br/>Primary Reasoning]
        ADAPT[QLoRA Adapter Router<br/>Regime-Aware Hot-Swap]
    end

    subgraph "AI Processing Layer (MAS)"
        GOV[GovernanceService<br/>Agent Policy Enforcement]
        ORCH[SwarmOrchestrator<br/>2-Stage Streaming Coordinator]
        STRAT[Strategy Suggestion Engine<br/>Proactive Trade Alpha]
        SWARM[Specialist Swarm<br/>Quant, Forecast, Research, Risk, Whale, Goal]
        RSTITCH[R-Stitch Engine<br/>SLM↔LLM Trajectory Stitching]
        ERR[Centralized Error Handler<br/>DatabaseError / handle_error]
    end

    subgraph "Data Layer"
        DUCK[DuckDB Analytics<br/>Raw & Clean Tables]
        FEAT[Feature Engineering<br/>Indicators & SLM Datasets]
    end

    UI -->|REST / SSE Traces| API
    API --> GOV
    GOV --> ORCH
    ORCH --> STRAT
    ORCH --> SWARM
    SWARM --> RSTITCH
    RSTITCH --> MLX
    
    MLX --> GEMMA4
    MLX --> ADAPT

    SWARM --> REDIS
    SWARM --> POOL[AgentHttpClient<br/>Connection Pool]
    POOL --> T212
    POOL --> ALP
    POOL --> YF
    POOL --> NEWS
    SWARM --> DUCK
    DUCK --> FEAT
    
    style UI fill:#e1f5fe
    style API fill:#f3e5f5
    style ORCH fill:#fff3e0
    style MLX fill:#e8f5e8
    style GOV fill:#ffebee
```

---

## 2. Agentic Swarm & Autonomous Execution

Growin implements a hybrid **Autonomous Agentic** model with radical **Sovereign UI** aesthetics and multi-model local inference.

### Multi-Agent Orchestration & Core Framework
*   **SwarmOrchestrator**: Manages a 2-stage streaming delegation pipeline, eliminating the latency of classic step-by-step reasoning.
*   **GovernanceService**: The central security gateway of the swarm. It enforces runtime policy checks, sanitizes inputs, handles agent execution limits, and verifies token authorizations.
*   **Strategy Suggestion Engine**: Embedded directly in the Coordinator flow, this engine proactively generates high-conviction trade and portfolio alpha recommendations based on real-time ledger metrics.
*   **R-Stitch Engine**: Optimizes model response time via Dynamic Trajectory Stitching (SLM↔LLM). Fast, non-critical sub-tasks use optimized Small Language Models, whereas heavy synthesis hops are dynamically stitched into Large Language Model inference runs.
*   **AgentHttpClient**: A centralized connection pooling client utilizing persistent `httpx.AsyncClient` connections, endpoint circuit breakers, and token-bucket rate limiting. Replaces per-agent individual HTTP clients to eliminate TCP socket exhaustion under multi-agent swarm loads.
*   **Centralized Error Handler**: Unified `DatabaseError` and `handle_error()` utilities in `utils/error_handler.py` replace fragmented `try/except` blocks across backend modules, improving traceability and failure isolation.

### Specialist Agent Roster
1.  **QuantAgent**: High-frequency indicators, statistical arbitrage, and vectorized portfolio metrics.
2.  **ForecasterAgent**: ML-driven price prediction utilizing joint return volatility forecasting.
3.  **ResearchAgent**: RAG-enhanced market news analysis and semantic query synthesis.
4.  **RiskAgent**: Exposure auditing, leverage thresholds, and volatility regime detection.
5.  **WhaleAgent**: Analyzes large-block transaction footprints, institutional filings, and flow patterns. Optimized with pre-parsed `Decimal` arithmetic for high-precision financial calculations.
6.  **GoalPlannerAgent**: Translates high-level user financial goals into actionable portfolio targets and trading milestones.

### Social Swarm (Reddit & Twitter micro-agents)
- Dedicated micro-agents in `backend/agents/social_swarm/` utilize Tavily Search APIs (via the pluggable `SearchPlugin` abstraction) to perform sentiment profiling, social signal scoring, and retail momentum detection on Reddit and Twitter.
- Sentiment analysis uses the **lazy-loaded VADER analyzer** (`get_sentiment_analyzer_async()`) to avoid event loop blocking on first invocation.

### Neural JMCE (Joint Mean-Covariance Estimator)
- **Regime-Aware Math**: Predicts returns and covariance shifts simultaneously.
- **Covariance Velocity**: Detects early-stage regime shifts (e.g., market panic) to boost ORB signal confidence.
- **Hardware Integration**: Runs on the Apple Neural Engine (ANE) via CoreML for <10ms inference.
- **NPU Degradation**: Robust CPU/NumPy statistical fallback (mean/covariance estimation) in `PortfolioAnalyzer` when MLX or CoreML are unavailable.

### Phase 46: Adaptive Learning Pipeline
The on-device adaptive learning system continuously refines the NeuralJMCE model using recent market data:
1.  **Data Ingestion**: 10-minute interval leveraged ETF data → DuckDB `raw_market_data` table.
2.  **Data Cleaning**: Outlier thresholding, 10-minute frequency gap-filling via `clean_etf_data.py`.
3.  **Feature Engineering**: Technical indicators (volatility, RSI, ATR, CVD) and SLM tuning datasets via `prepare_training_data.py`.
4.  **QLoRA Fine-Tuning**: 4-bit QLoRA fine-tuning on Gemma 4 via `mlx_vlm` with regime-specific adapters (e.g., `high_vol_bull`).
5.  **CoreML Export**: NeuralJMCE model exported to `.mlpackage` for ANE acceleration.
6.  **In-Memory Hot-Swap**: Adapter weights patched in-memory via `MLXInferenceEngine.switch_adapter()` — 10-50ms swap latency, zero 2× base model RAM duplication.

---

## 3. Hardware-Aware Partitioning (M4 Optimized)

Growin maximizes the M4 generation Apple Silicon architectures by routing workloads dynamically to CPU, GPU, and NPU.

| Component | Hardware | Role | Memory & Execution Budget |
|-----------|----------|------|---------------------------|
| **CPU (AMX)** | Apple Silicon CPU | Vectorized math, API routing, and system orchestration. | Dynamic Core Allocations |
| **GPU (Metal/MLX)** | Apple Silicon GPU | Direct MLX inference via `mlx_lm` / `mlx_vlm` with QLoRA adapter routing. Model loading, generation, and streaming run natively on Metal. | **60% Memory Rule**: Weights + KV-cache ≤ 28GB (M4 Pro 48GB) |
| **NPU (ANE)** | Apple Neural Engine | Real-time Neural JMCE inference and indicator forecasting via CoreML `.mlpackage`. | CoreML Static ANE Allocations (<10ms) |

### Memory Management & Resource Guarding
- **MLX Engine**: Direct model loading via `mlx_lm.load()` and `mlx_vlm.load()` with lazy initialization. Supports VLM-capable models (Gemma 4). Model warmup uses `mx.async_eval()` for non-blocking graph compilation. Unload clears Metal cache via `mx.metal.clear_cache()`.
- **Adapter Hot-Swap**: In-memory weight patching via `model.update(tree_unflatten(...))` followed by `mx.eval()` — avoids full model reload for regime-switching adapters.
- **ResourceGuard**: Employs dynamic concurrency limiters and memory heartbeats to suspend low-priority agent queries if GPU/unified memory utilization exceeds 85% of the allocated budget.

---

## 4. Data Fidelity & Normalization

*   **MarketDataFrayer**: A multi-suffix recovery aggregator that reconciles mismatched ticker nomenclature across Alpaca, Trading 212, and Yahoo Finance.
*   **TickerResolver**: Centralized engine in `utils/ticker_utils.py` that maps Trading 212 internal IDs (e.g., `VODl_EQ`) to market standards (`VOD.L`).
*   **Currency Normalization**: Automatic GBX (pence) to GBP (£) conversion for LSE assets to ensure calculation accuracy.
*   **DuckDB Analytics Layer**: The analytics database uses native Python `datetime` arithmetic instead of database-specific `INTERVAL` strings. Raw and clean market data tables support the adaptive learning pipeline with bulk insert/upsert operations.
*   **Batch Operations**: Avoids N+1 query patterns by executing vectorized batch OHLCV retrievals (`get_recent_ohlcv`) across the entire portfolio in a single pass.

---

## 5. Security & Autonomy

*   **GovernanceService Authorization**: Enforces role-based capabilities on the agent swarm. Agents are barred from creating execution parameters outside their specified domain boundary.
*   **FastAPI Middleware**: Implements rigid CORS security controls and `SecurityHeaders` middleware.
*   **Decision Sandbox**: The execution layers run generated mathematical scripts in secure, sandboxed environments via `SafePythonExecutor`.
*   **Audit Trail**: Every autonomous execution is logged with full reasoning context in the system audit logs.
*   **Error Sanitization**: Route-level filters scrub raw database tracebacks and agent execution logs of private variables before transmitting client-facing errors.

---

## 6. Application Structure (SwiftUI Frontend)

The frontend is a lightweight, high-performance visual console that exposes the backend swarm's reasoning.

### SwiftUI Component Inventory
- **SovereignSidebar**: Monolithic, tonal navigation sidebar mapping the master views with accessible key commands.
- **MasterLedgerView**: High-density Trading 212-style portfolio ledger displaying cash balances, assets, and active allocations.
- **ExecutionPanelView**: Interface for viewing autonomous trade actions and manual portfolio overrides.
- **WatchlistView**: Market watcher component exposing real-time tickers and agent forecasts.
- **AIChatPanelView**: Deep reasoning chat console utilizing local model stream traces.
- **AccountOverviewBanner**: Top-level summary component aggregating overall ledger gains, ANE volatility scores, and active agent statuses.
- **SovereignSlideToConfirm**: Authoritative tactile gesture element for executing high-conviction order overrides.

### VoiceOver & Accessibility
- Complete SwiftUI accessibility mapping (`.accessibilityLabel()`, `.accessibilityHint()`, `.accessibilityAddTraits()`) is implemented across all interactive elements, including custom `.buttonStyle(.plain)` buttons, making Growin a premium, fully accessible macOS application.
