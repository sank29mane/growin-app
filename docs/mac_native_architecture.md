# Mac-native Architecture Blueprint

Goal: A true Mac-native experience for Growin App, leveraging Apple Silicon (MLX) for on-device inference and a SwiftUI front-end with a lightweight Python backend bridge.

Core Components
- SwiftUI Frontend (macOS):
  - Local UI, charts, and user interactions.
  - Stores preferences and small state locally.
  - Communicates with the local backend bridge via localhost HTTP (REST) or gRPC.
- Python Backend (FastAPI):
  - Core data ingestion, forecasting, indicators, AI analysis, and agent orchestration.
  - Exposes a stable API surface for bridge and potential direct SwiftUI calls.
- Bridge Layer (Mac-native Prototype):
  - Lightweight REST bridge that forwards requests between SwiftUI frontend and Python backend (on localhost).
  - Allows experimentation with IPC strategies (REST, gRPC, or Unix sockets).
- On-device acceleration:
  - Core ML for lightweight models and potential transformer components.
  - Optional Torch -> Core ML conversion (via coremltools) for on-device inference when feasible.
- ANE-enabled on-device inference (default-off, auto-detect):
  - Leverages Apple Neural Engine on Apple Silicon for ultra-low latency ML tasks (forecasting, indicators).
  - Compute units: Prefer Neural Engine when available, fall back to CPU/GPU.
  - Path: SwiftUI -> IPC bridge -> Core ML runner -> results back through IPC.
  - Safety: Feature flag (USE_ANE env var or UI toggle) to disable; auto-enables on Apple Silicon by default.
  - Model constraints: Keep sizes small (1–20 MB) for on-device; target sub-500ms inference latency.

Data Flow
- Ingest OHLCV data -> Validate with Pydantic -> Feature extraction -> Forecasting (SOTA models) + Indicators (RSI, MACD, Bollinger) -> AI-generated summaries -> UI rendering.
- All sensitive computations (forecasting, AI analysis) keep within the device when possible; network calls are minimized.

Performance & Observability
- Profiling: Use Instruments / Activity Monitor to track CPU/GPU usage and energy impact.
- Caching: In-memory TTL or local SQLite to reduce redundant work.
- Logging: Structured logging with context IDs for tracing user sessions.

Security & Privacy
- On-device processing minimizes data leaving the device.
- If cloud offloads are introduced, ensure encryption and minimal data exposure.

Risks & Mitigations
- IPC complexity: Start with REST bridge and iterate to a more efficient IPC (gRPC or Unix sockets).
- Model size: Start with small, distilled models for on-device inference; keep larger models optional and offload when necessary.
- macOS version fragmentation: Build universal binaries and conditionals for Intel vs Apple Silicon.

Roadmap Snippet
- Phase 1: Prototyping bridge + a minimal SwiftUI view that calls the bridge and displays a forecast value.
- Phase 2: Add on-device Core ML models for lightweight forecasting tasks.
- Phase 3: Integrate a more sophisticated on-device AI module (LLM-like reasoning) via Core ML or ONNX with Core ML Tools.

References (SOTA & Core ML on-device)
- TFT: arXiv:1912.09363
- Informer: arXiv:2012.07436
- Autoformer: arXiv:2106.13008
- Core ML overview: Apple Developer Core ML docs
- WWDC24 sessions: on-device ML, Core ML Tools, transformer optimization

