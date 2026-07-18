# Growin - Comprehensive AI-Powered Portfolio Intelligence Platform

**Growin** is a sophisticated, native macOS application that combines advanced AI capabilities with real-time financial data to provide intelligent portfolio analysis, automated trading insights, and conversational financial advice. Built specifically for **Apple Silicon (M4 optimized)**, it leverages direct MLX inference with in-memory QLoRA adapter hot-swapping, Neural JMCE regime detection, and an adaptive learning pipeline for privacy-focused, high-performance financial autonomy.

[![macOS](https://img.shields.io/badge/platform-macOS_15.0+_Sequoia-000000?style=flat-square&logo=apple)](https://developer.apple.com/macos/)
[![SwiftUI](https://img.shields.io/badge/UI-SwiftUI-blue?style=flat-square)](https://developer.apple.com/xcode/swiftui/)
[![Python](https://img.shields.io/badge/Backend-Python_3.11+-3776AB?style=flat-square&logo=python)](https://www.python.org/)
[![Rust](https://img.shields.io/badge/Language-Rust-ea4335?style=flat-square&logo=rust)](https://www.rust-lang.org/)
[![MLX](https://img.shields.io/badge/Engine-MLX-orange?style=flat-square)](https://github.com/ml-explore/mlx)
[![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?style=flat-square)](https://fastapi.tiangolo.com/)

---

## 🏗️ System Architecture

Growin implements a **Hardware-Aware Multi-Agent Architecture** optimized for Apple Silicon partitioning (CPU/GPU/NPU), paired with a **Sovereign Ledger (Brutal Editorial) UI** generated via SwiftUI.

```mermaid
graph TB
    subgraph "macOS Native Frontend"
        UI[SwiftUI Application]
        RV[Reasoning Trace View]
        ACC[Accessibility & VoiceOver]
    end

    subgraph "MLX Inference Layer"
        MLX[MLX Engine<br/>mlx_lm / mlx_vlm]
        GEMMA4[Gemma 4 26B A4B MoE<br/>Primary Reasoning Hub]
        ADAPT[QLoRA Adapter Router<br/>Regime-Aware Hot-Swap]
    end

    subgraph "Python Backend (uv virtualenv)"
        API[FastAPI Server]
        ORCH[SwarmOrchestrator<br/>2-Stage Streaming]
        GOV[GovernanceService<br/>Policy Enforcement]
        STRAT[Strategy Suggestion Engine<br/>Proactive Alpha]
        SWARM[Specialist Swarm<br/>Quant, Forecast, Risk, Social]
        RSTITCH[R-Stitch Engine<br/>SLM↔LLM Stitching]
        POOL[AgentHttpClient<br/>Connection Pooling]
        ERR[Centralized Error Handler<br/>DatabaseError / handle_error]
    end

    subgraph "Data Sources & Infrastructure"
        T212[Trading 212 MCP]
        ALP[Alpaca Primary]
        REDIS[Redis L2 Cache & arq]
        DUCK[DuckDB Analytics<br/>Raw & Clean Tables]
    end

    subgraph "M4 Hardware (Local)"
        AMX[CPU - Orchestration]
        METAL[GPU - MLX Acceleration]
        ANE[NPU - Neural JMCE]
    end

    UI -->|REST/SSE| API
    API --> GOV
    GOV --> ORCH
    ORCH --> STRAT
    ORCH --> SWARM
    SWARM --> RSTITCH
    RSTITCH --> POOL
    POOL --> T212
    POOL --> ALP
    
    MLX --> GEMMA4
    MLX --> ADAPT
    ORCH --> MLX
    
    MLX -.-> METAL
    SWARM -.-> AMX
    UI -.-> ACC
    SWARM -.-> DUCK
```

### Key Architectural Upgrades & SOTA Features
*   **🧠 Dual-Model Intelligence**: Combines **Gemma 4 26B A4B MoE** (primary reasoning hub) with **Nemotron-Cascade-2 30B** (executive synthesis model) for deep financial analysis, trading strategies, and code synthesis.
*   **⚡ Direct MLX Inference**: High-performance local inference via `mlx_lm` and `mlx_vlm` directly on Apple Silicon GPU. Supports VLM-capable models (e.g., Gemma 4) with lazy model loading, async generation, and Metal cache management.
*   **🔄 Phase 46 Adaptive Learning Pipeline**: End-to-end on-device fine-tuning — DuckDB data ingestion → feature engineering → QLoRA fine-tuning on Gemma 4 → CoreML NeuralJMCE export → in-memory adapter hot-swap (10-50ms swap latency, zero 2× RAM duplication).
*   **🌊 SwarmOrchestrator & Strategy Engine**: A two-stage streaming agent delegation pipeline. The Coordinator incorporates a proactive **Strategy Suggestion Engine** that continuously analyzes the ledger and yields real-time, high-conviction trade and alpha recommendations.
*   **🛡️ Governance & Authorization**: The **GovernanceService** enforces strict runtime agent authorization, input validation, and system boundaries, serving as a runtime policy engine for safe, autonomous trading.
*   **🔗 Global Connection Pooling**: Centralized **AgentHttpClient** with endpoint-specific circuit breakers and automated token-bucket rate limiting. Replaces per-agent HTTP client instantiation and eliminates socket exhaustion under heavy swarm loads.
*   **♻️ Centralized Error Handling**: Unified `DatabaseError` and `handle_error()` utilities in `utils/error_handler.py` replace fragmented `try/except` blocks across `chat_manager.py` and other modules.
*   **⚡ Lazy-Loaded VADER Sentiment**: Asynchronous `get_sentiment_analyzer_async()` offloads the heavy VADER lexicon initialization to a background thread, preventing event loop stalling during concurrent agent execution.
*   **♿ Comprehensive VoiceOver Accessibility**: Fully integrated accessibility modifiers (`.accessibilityLabel`, `.accessibilityHint`, `.accessibilityAddTraits`) across all SwiftUI view components.
*   **🎨 Sovereign UI Aesthetics**: Radical, high-density Trading 212-inspired ledger aesthetics with 0px corner radiuses, tonal layering, and authoritative typography.

---

## 🚀 Installation & Setup

### Hardware Requirements
- **macOS Version**: 15.0+ (Sequoia) - Apple Silicon required.
- **Processor**: Optimized for **M4 Pro/Max/Ultra** architectures.
- **Memory**: 32GB+ unified memory recommended (48GB+ preferred for dual-model concurrent serving).
- **Inference**: Direct MLX inference on Apple Silicon GPU.

### Quick Start
1. **Clone the repository**
   ```bash
   git clone https://github.com/sank29mane/Growin-app.git
   cd Growin-app
   ```

2. **Setup Environment**
   ```bash
   cp backend/.env.example backend/.env
   # Edit backend/.env with your API keys and credentials.
   ```

3. **Launch the System Services**
   The project is managed using `uv` for Python environments. Use the helper scripts to start/stop the backend stack:
   ```bash
   ./start.sh
   ```

4. **Launch Frontend**
   ```bash
   open Growin/Growin.xcodeproj
   # Press Cmd+R in Xcode to build and run.
   ```

---

## 🧪 Verification Suite

Verify the system architecture, connection pooling, and test suite:
```bash
# Run backend unit and integration test suite with the locked Python 3.11 environment.
# CI=true disables native MLX imports so tests do not depend on accelerator state.
CI=true PYTHONPATH=backend uv run --project backend pytest tests/backend/

# Run performance benchmarks
CI=true PYTHONPATH=backend uv run --project backend pytest tests/backend/test_performance_benchmarks.py
```

---

## 📜 Documentation Directory

### Architecture & Design
*   [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) - System context, component designs, and agent relationships.
*   [docs/architecture_evolution_SOTA.md](docs/architecture_evolution_SOTA.md) - Chronological roadmap of architectural milestones.
*   [docs/ARCHITECTURAL_LEARNINGS.md](docs/ARCHITECTURAL_LEARNINGS.md) - Extracted lessons on connection pooling, vectorization, and async runtime issues.
*   [docs/mac_native_architecture.md](docs/mac_native_architecture.md) - Apple Silicon memory budgets and SwiftUI component inventory.
*   [docs/MAS_Strategy.md](docs/MAS_Strategy.md) - Multi-Agent System (MAS) strategy, coordinator workflows, and agent execution policies.

### Operations & Guidelines
*   [docs/runbook.md](docs/runbook.md) - Setup, MLX management, connection diagnostics, and verification checklists.
*   [docs/model-selection-playbook.md](docs/model-selection-playbook.md) - Dual-model routing decisions and task-based model recommendations.
*   [docs/LIFECYCLE_MANDATES.md](docs/LIFECYCLE_MANDATES.md) - Tear-down guidelines for background services and memory limits.

---
*Growin - Transforming financial intelligence through the power of AI and Apple Silicon.* 🚀
