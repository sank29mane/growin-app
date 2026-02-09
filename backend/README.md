# Growin Backend: AI Financial Intelligence Engine

This is the Python-based heart of the Growin App. It orchestrates a multi-agent system to provide real-time financial analysis, leveraging local LLMs and Rust-accelerated quantitative engines.

## 🚀 SOTA Tech Stack (2026)
- **Runtime**: Python 3.12+ optimized with `uv` for ultra-fast dependency management.
- **API Framework**: FastAPI with full `asyncio` support for non-blocking agent orchestration.
- **Local Inference**: **MLX** integration for native Apple Silicon GPU acceleration.
- **Performance Core**: **Rust** (`growin_core`) for high-throughput technical indicators.
- **Precision**: `decimal.Decimal` based financial math layer.

## 📁 Directory Structure
- `/agents`: Specialist agents (Quant, Portfolio, Forecast, Research).
- `/routes`: FastAPI endpoints for Chat, Market, and System Status.
- `/utils`: Shared utilities including the `SafePythonExecutor` (Sandbox) and `FinancialMath`.
- `/growin_core_src`: Rust source code for the performance-critical math engine.

## 🛠️ Quick Start

### 1. Install `uv` (Recommended)
```bash
brew install uv
```

### 2. Environment Setup
```bash
uv venv
source .venv/bin/activate
uv pip install -r requirements.txt
```

### 3. Run Development Server
```bash
./start_backend.sh
```
The server will be available at `http://127.0.0.1:8002`.

## 🧪 Testing & Quality
Run the full test suite with coverage:
```bash
uv run pytest tests/ -v --cov=.
```

## 🛡️ Security & Sandboxing
The backend implements a **SafePythonExecutor** for model-generated code.
- **Security Goal**: Future migration to MicroVM/WASM based isolation for 2026 SOTA agent safety.
- **Masking**: All logs are scrubbed for API keys via the `SecretMasker` middleware.

## 📈 Agentic Workflow
The backend uses a **Coordinator-Specialist** pattern:
1. **Coordinator** (Granite-Tiny): Classifies intent and routes to agents.
2. **Specialists**: Execute parallel domain-specific tasks (e.g., fetching T212 data).
3. **Decision Agent**: Synthesizes results into a human-readable advisory response.
