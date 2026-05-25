# Changelog

All notable changes to the Growin project will be documented in this file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
