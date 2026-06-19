# Growin App Multi-Agent System: Architecture Evolution

## 1. Executive Summary

The Growin App architecture has evolved from a simple interactive advisor into a state-of-the-art, high-conviction **Autonomous "Autopilot"** portfolio platform. This evolution leverages the massive parallel processing capabilities of Apple Silicon (M4 generation), implementing **Hardware-Aware Partitioning**, **Direct MLX inference with adaptive QLoRA fine-tuning**, **Systematic Governance**, and **Robust Connection Pooling**.

---

## 2. Chronological Architecture Evolution

### First-Generation Foundation
- **Design Intent**: Assistive Copilot for portfolio monitoring.
- **Workflow**: Simple user interaction triggering a linear chain of agent checks (Coordinator → Specialists).
- **Execution Model**: All final transaction ideas presented to the user as simple suggestions; execution required manual browser or UI interactions.
- **Latency**: Variable, bound by per-agent setup and sequential API queries.

### Second-Generation Autonomy
- **Design Intent**: High-conviction autonomous execution and hardware partitioning.
- **Workflow**: Transitioned to parallel specialist evaluation (Quant, Forecast, Research, Risk) feeding a centralized synthesis agent.
- **Execution Model**: **High Conviction Bypass** introduced. If the synthesis agent computes a conviction level of `10/10`, the platform autonomously routes transactions directly to Trading 212 via sandbox APIs, keeping a local tamper-proof audit log.
- **Hardware Integration**: First mapping of tasks across Apple Silicon: CPU (orchestration), GPU (local model weight adapters), and NPU/ANE (Neural JMCE regime detection).
- **Data Parity**: Implemented a comprehensive `TickerResolver` mapping brokerage formats to global market standards, alongside automatic GBX-to-GBP normalization.

### Third-Generation SOTA Architecture (v5.0 — May 2026)
- **Design Intent**: Multi-agent concurrent scaling, high-throughput local serving, and unified system reliability.
- **Dual-Model Reasoning**: Transitioned from a single model footprint to a dedicated **dual-model architecture** pairing **Gemma 4 26B A4B MoE** (primary reasoning hub) with **Nemotron-Cascade-2 30B** (executive synthesis and code generation) for deep financial tasks.
- **SwarmOrchestrator & Strategy Engine**: Upgraded agent coordination to a 2-stage streaming pipeline. The Coordinator now embeds a proactive **Strategy Suggestion Engine** that streams high-conviction trade and alpha recommendations directly into the system ledger.
- **Centralized Connection Pooling**: Replaced individual per-agent client instances with the global `AgentHttpClient`. This pool maintains a persistent `httpx.AsyncClient` state, enforces endpoint circuit breakers, and applies token-bucket rate limits, eliminating TCP socket exhaustion under heavy swarm loads.
- **Performance Optimizations**: 
  - **Vectorized Math**: Optimization of portfolio processing by avoiding slow iterative pandas `assign` calls and using batch OHLCV retrievals (`get_recent_ohlcv`) to avoid database N+1 loops.
  - **DuckDB Compatibility**: Rewrote all temporal query functions to use native Python `datetime` arithmetic, replacing fragile database `INTERVAL` syntax.

### Fourth-Generation Adaptive Architecture (v5.1 — June 2026)
- **Design Intent**: On-device continuous learning, architectural simplification, and production hardening.
- **vMLX Deprecation**: Removed the `vmlx_manager.py` PagedAttention serving layer, `vllm_engine.py`, `mlx_vlm_engine.py`, and associated test suites. Inference now uses **direct MLX** via `mlx_lm` and `mlx_vlm` libraries, simplifying the serving stack and eliminating the overhead of a managed inference server.
- **Phase 46 Adaptive Learning Pipeline**: End-to-end on-device fine-tuning system:
  1. Raw leveraged ETF data → DuckDB `raw_market_data` table (10-min intervals).
  2. Outlier thresholding and frequency gap-filling via `clean_etf_data.py`.
  3. Feature engineering (volatility, RSI, ATR, CVD) via `prepare_training_data.py`.
  4. QLoRA 4-bit fine-tuning on Gemma 4 via `mlx_vlm` with regime-specific adapters.
  5. CoreML NeuralJMCE export to `.mlpackage` for ANE acceleration.
  6. In-memory adapter hot-swap via `MLXInferenceEngine.switch_adapter()` — 10-50ms latency, zero base model duplication.
- **Deprecated Components Removed**:
  - `VisionAgent` and associated utilities (`image_proc.py`, `mlx_injections.py`).
  - `DividendBridge` (`dividend_bridge.py`) and standalone `DividendAgent`.
  - Docker compose sandbox for NPU execution.
  - Legacy test suites for vMLX, vLLM, LMStudio fixes, and security headers.
- **Centralized Error Handling**: Unified `DatabaseError` and `handle_error()` in `utils/error_handler.py`, replacing fragmented try/except blocks across `chat_manager.py`, `data_engine.py`, and `lm_studio_client.py`.
- **Lazy VADER Sentiment**: `get_sentiment_analyzer_async()` offloads heavy lexicon initialization to a background thread, preventing event loop stalling.
- **High-Precision Financial Arithmetic**: WhaleAgent refactored to use pre-parsed `Decimal` arrays, eliminating redundant `create_decimal` calls in tight loops.
- **Accessibility Enhancements**: Added `.accessibilityHint()` and `.accessibilityAddTraits()` to all SwiftUI plain-style buttons, Settings, and Workspace controls.

---

## 3. Core SOTA Architecture Components

### A. Neural JMCE (Joint Mean-Covariance Estimator)
- **Mathematical Integrity**: Predicts expected asset returns and covariance shifts concurrently.
- **Apple Neural Engine (ANE)**: CoreML implementation executing volatility regime detection in under 10ms.
- **Vol-Regime Signal**: Feeds the RiskAgent real-time volatility velocity scores to scale down active positions ahead of market correction events.
- **NPU Degradation**: Robust CPU/NumPy statistical fallback (mean/covariance estimation) in `PortfolioAnalyzer` and graceful `None` fallback for covariance velocity when MLX/CoreML are missing.

### B. Direct MLX Inference & Adapter Hot-Swap
- **MLX Engine**: `MLXInferenceEngine` singleton in `mlx_engine.py` manages model lifecycle — lazy loading via `mlx_lm.load()` / `mlx_vlm.load()`, async generation, streaming via `stream_generate()`, and Metal cache cleanup.
- **Memory Rule**: Strict resource allocation allocating 60% of M4 unified memory for weights and KV caches (≤ 28GB on 48GB M4 Pro configurations).
- **Adapter Hot-Swap**: `switch_adapter()` patches QLoRA weights in-memory via `model.update(tree_unflatten(...))` and `mx.eval()`, avoiding full model reload. Regime-specific adapters (e.g., `high_vol_bull`) can be swapped in 10-50ms.
- **ResourceGuard**: Tracks active memory usage, suspending lower-priority background tasks if system resource pressure exceeds 85% capacity.

### C. Governance & Authorization Service
- **Access Control**: Prohibits agents from executing tasks outside their strict security scope.
- **Input Sanitization**: Automatically scrubs user input and third-party API payloads before parsing them to local agent prompt runtimes.
- **Secure Sandboxing**: Any code generated by synthesis engines executes via `SafePythonExecutor`.

---

*Status: IMPLEMENTED, PROFILE VERIFIED & AUDITED (June 2026)*

## 2026 SOTA Refactor Technical Additions
- Abstract `analyze` method explicitly added to `CoordinatorAgent` to prevent instantiation failures.
- `execute` acts as main router in `CoordinatorAgent`.
- Benchmark file for N+1 correctly placed and verified.
- Robust NPU degradation handling with CPU/NumPy statistical fallback (mean/covariance estimation) in `PortfolioAnalyzer` and graceful `None` fallback for covariance velocity when MLX/CoreML are missing.
- Verification scripts (`verify_mlx_degradation.py`, `verify_quant.py`) relocated to the `tests/` directory.
- Phase 46 adaptive learning pipeline implemented and verified end-to-end.
- Centralized error handling, lazy VADER sentiment, and high-precision financial arithmetic deployed.
