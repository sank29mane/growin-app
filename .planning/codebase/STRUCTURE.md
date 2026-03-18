# Codebase Structure: Growin App

## Root Directory Overview
- `PROJECT_RULES.md`: Canonical GSD operational rules.
- `README.md`: High-level vision and architecture.
- `start.sh`, `stop.sh`, `run`: System lifecycle scripts.
- `docker-compose.yml`: (Optional) environment services.

## Core Directories
### 🧠 Backend (`backend/`)
FastAPI server and the multi-agent swarm.
- `agents/`: Specialist agents (Quant, Forecast, Decision, etc.).
- `mlx_engine.py`: Core MLX inference engine.
- `vllm_mlx_engine.py`: Native vLLM inference server.
- `data_engine.py`: Real-time and historical data ingestion.
- `routes/`: API endpoints for the SwiftUI app.
- `utils/`: Common utilities (ticker normalization, logic).

### 🖥️ Native App (`Growin/`)
SwiftUI application for macOS.
- `Views/`: UI components and screens.
- `ViewModels/`: Application state management.
- `Services/`: Networking and hardware integration.
- `GrowinApp.swift`: Main entry point.

### 🔬 Research & Analysis (`scripts/`)
Quant research and backtesting.
- `backtest_portfolio_today.py`: Current portfolio backtest.
- `fetch_leveraged_etf_data.py`: LSE ETF data harvesting.
- `npu_backtest_lab.py`: Massively parallel MLX simulations.

### 💾 Data & Models (`data/`, `models/`)
Storage for datasets and weights.
- `data/etfs/`: LSE ETP datasets.
- `models/mlx/`: Local MLX model weights.
- `growin_rag_db/`: ChromaDB for contextual research.

### 📋 Planning & Progress (`.planning/`)
GSD framework artifacts.
- `PROJECT.md`: Vision and requirements.
- `ROADMAP.md`: Multi-phase execution plan.
- `STATE.md`: Persistent session memory.
- `phases/`: Detailed implementation plans.
- `milestones/`: Archived completed work.
- `codebase/`: In-depth system documentation.
