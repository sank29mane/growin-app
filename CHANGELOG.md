# Changelog

All notable changes to the Growin project will be documented in this file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [5.1.0] - 2026-06-17

### Added
- **Phase 46 Adaptive Learning Pipeline**: End-to-end on-device fine-tuning system — DuckDB raw/clean data ingestion (10-min intervals), feature engineering (volatility, RSI, ATR, CVD), QLoRA 4-bit fine-tuning on Gemma 4 via `mlx_vlm`, CoreML NeuralJMCE export, and in-memory adapter hot-swap (10-50ms, zero 2× RAM).
- **MLXInferenceEngine Adapter Hot-Swap**: `switch_adapter()` method patches QLoRA weights in-memory via `model.update(tree_unflatten(...))` — enables regime-aware adapter routing without full model reload.
- **Centralized Error Handler**: Unified `DatabaseError` and `handle_error()` in `utils/error_handler.py`, replacing fragmented try/except blocks across backend modules (#348).
- **Lazy-Loaded VADER Sentiment**: `get_sentiment_analyzer_async()` offloads heavy lexicon initialization to background thread, preventing async event loop stalling (#347).
- **High-Precision Financial Arithmetic**: WhaleAgent refactored to use pre-parsed `Decimal` arrays in tight loops, eliminating redundant `create_decimal` calls (#346).
- **Accessibility Enhancements**: Added `.accessibilityHint()` to all plain-style SwiftUI buttons, Settings, and Workspace controls (#345, #350).
- **HITL Trade Route Tests**: Fixed `AsyncClient` initialization using `httpx.ASGITransport(app=app)` for compatibility with newer httpx versions.
- **DuckDB Schema**: `raw_market_data` and `clean_market_data` tables with bulk insert/upsert operations for the adaptive learning pipeline.

### Removed
- **vMLX Serving Layer**: Deleted `vmlx_manager.py` (PagedAttention server), `vllm_engine.py`, and all associated tests/scripts. Inference now uses direct `mlx_lm`/`mlx_vlm`.
- **VisionAgent**: Deleted `agents/vision_agent.py`, `utils/image_proc.py`, `utils/mlx_injections.py`, and `mlx_vlm_engine.py`.
- **DividendBridge**: Deleted `dividend_bridge.py` and standalone `DividendAgent`. Dividend optimization is handled through the swarm coordinator.
- **Docker NPU Sandbox**: Removed Docker compose sandbox configuration.
- **Legacy Test Suites**: Removed `test_vmlx_engine.py`, `test_vllm_engine.py`, `test_mlx_injections.py`, `test_dividend_agent.py`, `test_dividend_bridge.py`, `test_dividend_capture.py`, `test_lmstudio_fix.py`, `test_nemotron_usage.py`, `test_security_headers.py`, `test_status_debug.py`.

### Changed
- **MLX Inference**: Now uses direct `MLXInferenceEngine` singleton in `mlx_engine.py` — lazy model loading, async generation, Metal cache management, and VLM detection for Gemma 4.
- **Import Paths**: `chat_manager.py` standardized to use relative imports (`utils.error_handler`) for `PYTHONPATH=backend` compatibility.
- **Search Abstraction**: Agents decoupled from hardcoded Tavily calls via pluggable `SearchPlugin` architecture.
- **Test Runner**: Standardized on `PYTHONPATH=backend uv run pytest tests/backend/`.
- **Start Script**: Removed vMLX process management from `start.sh`.

---

## [5.0.0] - 2026-05-23

### Added
- **Dual-Model Serving Layout**: Implemented local parallel serving pairing **Gemma 4 26B A4B MoE** (primary reasoning) with **Nemotron-Cascade-2 30B** (executive synthesis and code generation).
- **vMLX serving layer**: Introduced the `vmlx_manager.py` local PagedAttention server. Replaces direct unmanaged MLX instances, enabling concurrent multi-agent queries without memory fragmentation on Apple Silicon M4 Pro/Max GPUs.
- **SwarmOrchestrator**: Upgraded intent classification and task routing to a highly parallel 2-stage streaming pipeline, significantly decreasing user-perceived latencies.
- **Strategy Suggestion Engine**: Proactive trade and portfolio alpha suggestion system integrated directly inside the SwarmOrchestrator context.
- **Centralized Connection Pooling**: Created the global `AgentHttpClient` maintaining a unified, persistent `httpx.AsyncClient` socket pool with circuit breakers and rate limiters to resolve TCP socket leaks.
- **Sovereign UI frontend**: Full inventory of new SwiftUI components including `SovereignSidebar`, `MasterLedgerView`, `ExecutionPanelView`, `WatchlistView`, `AIChatPanelView`, `AccountOverviewBanner`, and `SovereignSlideToConfirm`.
- **Accessibility Integration**: Complete `.accessibilityLabel()`, `.accessibilityHint()`, and `.accessibilityAddTraits()` VoiceOver support across all SwiftUI frontend components.
- **Async vision pipelines**: Structured visual analysis via `prepare_vlm_image_async()` to offload CPU-bound PIL tasks onto background executor threads, keeping the event loop block-free.

### Changed
- **DuckDB Compatibility**: Rewrote all analytics time-series querying to use native Python `datetime` arithmetic instead of fragile database `INTERVAL` syntax.
- **Query Optimization**: Vectorized QuantEngine analytics and implemented batch `get_recent_ohlcv` queries to eliminate N+1 database loop overhead.
- **Robust Exception Handling**: Refactored all bare `except:` clauses to `except Exception:` to prevent catching system signals (`SystemExit`, `KeyboardInterrupt`) and ensure clean server shutdowns.
- **Requirements Update**: Standardized macOS Sequoia (15.0+) as the minimum OS version.

---

## [4.0.0] - 2026-03-15

### Added
- **High Conviction Autopilot Bypass**: Enabled autonomous trading on Trading 212 if the specialist swarm computes absolute conviction (`10/10`), accompanied by cryptographically chained log structures.
- **Ticker & Currency Normalization**: Centralized `TickerResolver` mapping brokerage assets to market standards with automatic GBX-to-GBP conversion.
- **Local Re-training Adapters**: Daily MLX calibration pipelines to adjust model weights on-the-fly based on recent market error feedback.

---

## [3.0.0] - 2026-02-15

### Added
- **M4 Hardware Partitioning**: Dynamic routing of Python server tasks to CPU, MLX inference to GPU, and Neural JMCE estimators to ANE NPU via CoreML.
- **L2 caching**: Integrated Redis cache middleware for shared cross-request caching, supplementing L1 OrderedDict systems.

---

## [2.0.0] - 2025-11-20

### Added
- **Specialist Agent Swarm**: Launched QuantAgent, ForecasterAgent, ResearchAgent, and RiskAgent roles.
- **Adversarial Debate**: Implemented the contrarian critic pattern using RiskAgent exposure validation before final advisory generation.

---

## [1.0.0] - 2025-06-01

### Added
- **Initial Release**: Basic macOS SwiftUI frontend and local FastAPI server with single agent portfolio monitoring.
