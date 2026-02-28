# Growin - Comprehensive AI-Powered Portfolio Intelligence Platform

**Growin** is a sophisticated, native macOS application that combines advanced AI capabilities with real-time financial data to provide intelligent portfolio analysis, automated trading insights, and conversational financial advice. Built specifically for Apple Silicon, it leverages local LLMs and optimized algorithms for privacy-focused, high-performance financial intelligence.

[![macOS](https://img.shields.io/badge/platform-macOS-000000?style=flat-square&logo=apple)](https://developer.apple.com/macos/)
[![SwiftUI](https://img.shields.io/badge/UI-SwiftUI-blue?style=flat-square)](https://developer.apple.com/xcode/swiftui/)
[![Python](https://img.shields.io/badge/Backend-Python_3.11+-3776AB?style=flat-square&logo=python)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/API-FastAPI-009688?style=flat-square)](https://fastapi.tiangolo.com/)

## 🏗️ System Architecture (SOTA 2026)

Growin implements a **Flattened Multi-Agent Architecture** optimized for low-latency reasoning and strict financial governance.

### Key SOTA Features (Phase 16)
- **🚀 Unified Orchestration**: Replaces multi-hop hierarchies with a single-lifecycle `OrchestratorAgent` for sub-500ms routing and synthesis.
- **🛡️ The Critic Pattern**: Mandatory risk and compliance auditing via a dedicated `RiskAgent` (The Critic) before any trade recommendation reaches the user.
- **⚡ 8-bit AFFINE Optimization**: Hardware-level quantization for MLX models, specifically tuned for the M4 Pro NPU and Unified Memory.
- **📡 AG-UI Streaming Protocol**: Real-time transparency via `AgentMessenger`, streaming granular lifecycle events (e.g., `swarm_started`, `risk_review_started`) directly to SwiftUI.
- **💎 Financial Precision Layer**: String-initialized `Decimal` arithmetic for 100% accurate P&L and balance tracking.
- **🔒 HITL Trade Gates**: Backend enforcement of Human-in-the-Loop signatures (HMAC) for any automated trade execution via MCP.
- **🧠 Stateful AI Conversation**: Server-side context persistence with internal Chain of Thought (CoT) extraction for reasoning-optimized models.

### High-Level System Overview
```mermaid
graph TB
    subgraph "macOS Native Frontend"
        UI[SwiftUI Application]
        CV[Chat View]
        PV[Portfolio View]
        RV[Reasoning Trace View]
    end

    subgraph "Python Backend Services (SOTA 2026)"
        API[FastAPI Server<br/>Port 8002]
        ORCH[Orchestrator Agent<br/>Unified Lifecycle]
        SWARM[Specialist Swarm<br/>Parallel Swarm Bus]
        RISK[Risk Agent<br/>Governance Critic]
    end

    subgraph "Data Sources & APIs"
        T212[Trading 212<br/>Portfolio Data]
        ALP[Alpaca<br/>Market Data]
        YF[yFinance<br/>Historical Data]
    end

    subgraph "Local AI Engine"
        MLX[MLX 8-bit AFFINE<br/>Apple Silicon NPU/GPU]
        LMS[LM Studio<br/>Stateful Context]
    end

    UI -->|HTTP REST/SSE| API
    API --> ORCH
    ORCH --> SWARM
    SWARM --> ORCH
    ORCH --> RISK
    RISK --> ORCH
    ORCH --> UI

    SWARM --> T212
    SWARM --> ALP
    SWARM --> YF

    ORCH --> MLX
    ORCH --> LMS
```

## 🚀 Installation & Setup

### Hardware Requirements
- **macOS Version**: 14.0+ (Sonoma) - Apple Silicon required.
- **Processor**: Optimized for **M4 Pro/Max** with Unified Memory (24GB+ recommended).
- **Inference**: Native MLX support with 8-bit AFFINE quantization enabled.

### Quick Start
```bash
# 1. Clone the repository
git clone https://github.com/sanketmane/growin-app.git
cd growin-app

# 2. Setup Environment
cp backend/.env.example backend/.env
# Edit backend/.env with your T212, Alpaca, and LLM API keys.

# 3. Launch Backend (uv recommended)
./run

# 4. Launch Frontend
open Growin/Growin.xcodeproj
# Press Cmd+R in Xcode to build and run.
```

### 🧪 Verification Suite
Verify the Phase 16 Architecture:
```bash
# Run performance and critic pattern tests
PYTHONPATH=backend uv run pytest backend/tests/test_orchestrator_perf.py backend/tests/verify_phase_16.py
```

## 📜 Documentation
*   `docs/architecture_evolution_SOTA.md`: Deep dive into the flattened MAS and 2026 SOTA research.
*   `docs/ARCHITECTURE.md`: Technical system diagrams and precision specs.
*   `docs/MAS_Strategy.md`: Multi-Agent Strategy and Governance roadmap.

---
*Growin - Transforming financial intelligence through the power of AI and Apple Silicon.* 🚀
