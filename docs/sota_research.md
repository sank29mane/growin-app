# SOTA Research Overview for Growin App

This living document outlines state-of-the-art (SOTA) approaches across key domains relevant to Growin App: time-series forecasting, on-device ML for macOS, technical indicators, AI-assisted analytics, and data engineering. It also notes macOS compatibility and practical tradeoffs for a Mac-native experience.

## Time-series Forecasting (Backend)
- TFT (Temporal Fusion Transformer)
  - Why: interpretable, multi-horizon forecasting with attention over inputs; strong empirical results on a variety of datasets.
  - Key source: Lim et al., TFT paper (arXiv:1912.09363).
  - macOS note: forecasting models can be CPU-bound; consider on-device offloads for lightweight variants.
  - Link: https://arxiv.org/abs/1912.09363
- Informer
  - Why: efficient Transformer for long sequences with ProbSparse attention; scalable for long horizons.
  - Key source: Informer: Beyond Efficient Transformer for Long Sequence Time-Series Forecasting (arXiv:2012.07436).
  - Link: https://arxiv.org/abs/2012.07436
- Autoformer
  - Why: decomposition transformer with auto-correlation; state-of-the-art on long-horizon benchmarks with efficient computation.
  - Key sources: Autoformer (NeurIPS 2021). OpenReview, arXiv.
  - Link: https://arxiv.org/abs/2106.13008
- N-BEATS / N-HiTS
  - Why: strong baseline for univariate/m multivariate forecasting with interpretable blocks.
  - Tradeoffs: simpler, faster in practice; good baseline to compare newer models.
- Granite-TSFM
  - Why: practical TS forecasting framework; good for enterprise pipelines.
- Practical guidance
  - Build a modular forecasting backend where models can be swapped via a plug-in interface and API contracts remain stable.

## On-device / Mac-native ML (Core ML, Apple Silicon)
- Core ML for on-device inference
  - Why: ultra-low latency, privacy-preserving, battery-friendly on macOS devices.
  - Source: Apple Core ML docs and WWDC sessions on on-device models (Core ML overview and on-device model deployment).
  - Key references: Core ML docs (Apple Developer), WWDC 2024 sessions on Core ML and on-device transformer optimization.
  - Link: https://developer.apple.com/machine-learning/core-ml/
- Model conversion & optimization
  - Tooling: Core ML Tools for converting PyTorch/TensorFlow models to Core ML; model stitching and quantization features for memory efficiency.
  - Guidance: target lightweight forecast and indicator models for on-device inference first; offload heavier tasks if needed.
- LLMs and on-device inference
  - Approach: consider running small LLMs on-device with Core ML (quantized/ distilled models) for user-facing natural language analytics; plan for hybrid workflows where larger models run on-device-accelerated components are used.
  - Source: CoE and WWDC materials on Core ML integration for transformer-style models.

## Technical Indicators & Analytics (TA-Lib Alternatives)
- TA-Lib alternatives
  - pandas_ta, ta, Tulipy for macOS; easier installation and pure-Python paths as fallback.
  - Tradeoffs: simpler API but possibly fewer indicators or performance constraints; validate against TA-Lib baseline.
- Robust fallbacks
  - Ensure None/NaN handling in RSI, MACD, Bollinger bands with explicit guards and unit tests.

## AI-assisted Analytics & Reasoning
- Local reasoning vs cloud
  - Prefer LLM-assisted reasoning with privacy-preserving local inferences; consider policy-based tool use to minimize API calls.
  - Research path: smaller, quantized models or local inference via Core ML for standard tasks; use cloud for heavier tasks only when needed.

## Data Engineering & Pipelines
- Data quality and streaming
  - Validate OHLCV input, enforce schemas (Pydantic), and implement streaming or chunked reads to manage memory on-device.
- Caching & state
  - In-memory TTL caches, with optional Redis for distributed setups; ensure proper cache invalidation.

## Mac-native Stack Architecture (High-Level)
- Frontend: SwiftUI macOS app for UI, charts, and offline storage.
- Backend: Python FastAPI with modular components for forecasting, indicators, and AI analysis.
- Bridge: Optional lightweight bridge (REST or gRPC) for SwiftUI to call Python backend.
- Data Flow: Ingest -> Validate -> Extract features -> Forecast/Indicators -> AI Analysis -> UI

## Validation & Metrics
- Functional correctness: end-to-end tests for endpoints and UI flows.
- Performance: target sub-second inference for on-device models; sub-1s AI summaries; battery/thermal profiling.
- Accuracy: RSI/MACD/BBANDS values vs trusted libraries; forecast error metrics (MAE/MAPE/RMSE).
- Robustness: handle missing data gracefully; network dropouts if applicable.

## Roadmap & Phases
- Phase A (2–4 weeks): literature-backed evaluation and quick proofs of concept for core components; build the Mac-native bridge skeleton and a minimal SwiftUI prototype.
- Phase B (4–8 weeks): implement a targeted SOTA model (e.g., Autoformer or TFT variant) behind a swap-friendly backend module; prototype on-device Core ML for lightweight models.
- Phase C (8–12 weeks): full Mac-native stack with end-to-end MVP, caching, observability, and a basic SwiftUI front-end; begin user testing.

## References
- TFT: Temporal Fusion Transformers for Interpretable Multi-horizon Time Series Forecasting — arXiv:1912.09363
- Informer: Informer: Beyond Efficient Transformer for Long Sequence Time-Series Forecasting — arXiv:2012.07436
- Autoformer: Autoformer: Decomposition Transformers with Auto-Correlation for Long-Term Series Forecasting — arXiv:2106.13008
- Core ML on-device inference — Apple Developer docs, WWDC24 sessions
- Core ML Tools and model conversion guidance on macOS
- Mac-native transformer optimization on Core ML / ML Compute / Metal

