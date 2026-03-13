# Mac-native Architecture Blueprint (SOTA 2026)

Goal: A true Mac-native experience for Growin App, leveraging the full M4 generation Apple Silicon (AMX, Metal, ANE) for on-device inference and a SwiftUI front-end with a high-performance Python backend bridge.

## Core Components

### SwiftUI Frontend (macOS)
- **Fluid Interface**: Local UI, charts, and user interactions optimized for 120Hz ProMotion displays.
- **Hardware Acceleration**: Utilizes Apple's `Accelerate` framework for local, ultra-fast vector math and portfolio rebalancing.
- **Reasoning Visualization**: Real-time agentic "thought traces" visualization using `PhaseAnimator` and Metal-accelerated shaders.

### Python Backend (FastAPI / uv)
- **System Brain**: Managed via `uv` for lightning-fast dependency resolution and virtual environment isolation.
- **Hardware-Aware Routing**: Intelligently routes tasks to the optimal SoC component (AMX, GPU, or NPU).
- **Tool Handlers**: Modular handlers (`t212_handlers.py`) for secure communication with brokerage APIs via MCP.

## M4 Hardware Partitioning (The "Three-Brain" Model)

| Brain | Hardware | Primary Workload |
|-------|----------|------------------|
| **The Orchestrator** | **CPU (AMX)** | Python execution, API lifecycle, JSON parsing, and MCP server management. |
| **The Reasoner** | **GPU (Metal/MLX)** | Large Language Model (LLM) inference, daily model calibration, and **Weight Adapter** training. |
| **The Math Engine** | **NPU (ANE)** | **Neural JMCE** real-time inference, technical indicator calculations, and covariance shift detection. |

## Data Flow & Precision
- **Ingestion**: Tiered multi-source data (Alpaca Primary -> Finnhub Primary -> Yahoo Fallback).
- **Normalization**: Unified `TickerResolver` and `CurrencyNormalizer` ensure broker-data parity (GBX -> GBP handled automatically).
- **Execution**: High-conviction signals trigger **Autonomous Entry**, bypassing UI confirmation for verified SOTA trade setups.

## Security & Privacy
- **Local Sovereignty**: All sensitive AI reasoning and mathematical modeling stays on your Mac. No financial data leaves the device for processing.
- **Execution Guard**: Model-generated scripts are executed in a secure local sandbox (`safe_python.py`).
- **Audit Logs**: Comprehensive local audit trails record every autonomous decision, reasoning trace, and API call for full accountability.

## Roadmap & Status
- **Phase 1-2**: Prototyping and performance optimization.
- **Phase 24-29**: UX Polish and Institutional Portfolio Optimization.
- **Phase 30-31**: **High-Velocity Intraday Pivot** and **Autonomous Agentic Execution** (COMPLETED).
- **Phase 32**: **End-to-End Simulation** and production-readiness verification (IN PROGRESS).

---
*Verified for Apple Silicon M4 Pro/Max/Ultra (March 2026)*
