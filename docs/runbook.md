# Growin Platform Operations Runbook

This runbook documents day-to-day operational procedures, troubleshooting steps, diagnostic commands, and recovery workflows for the Growin App. It is strictly optimized for macOS (bash/zsh) and Apple Silicon environments.

---

## 🚀 Quick Command Sheet

### System Status & Git Tracking
```bash
# Current repository status
git status

# Recent commits (last 10)
git log --oneline -10

# Current active branch
git branch --show-current
```

### Server Process Management
```bash
# Start all backend services (FastAPI, Redis)
./start.sh

# Stop all background services cleanly
./stop.sh

# View active system log streams
tail -f backend/data/growin_server.log
```

---

## 🧠 MLX Inference Operations

The **MLX Inference Engine** (`mlx_engine.py`) manages local model loading, inference, and adapter hot-swapping using `mlx_lm` and `mlx_vlm` directly on Apple Silicon GPU.

### 1. Model Loading & Status
The `MLXInferenceEngine` is a lazy-initialized singleton — models load on first inference request.
```bash
# Check engine status (model loaded, memory usage)
PYTHONPATH=backend uv run python -c "from mlx_engine import get_mlx_engine; print(get_mlx_engine().get_status())"
```

### 2. Adapter Hot-Swap
Regime-specific QLoRA adapters can be swapped in-memory (10-50ms):
```bash
# Verify adapter weights exist
ls adapters/high_vol_bull/adapters.safetensors
```

### 3. Memory Pressure Monitoring
Always monitor Apple Silicon unified memory to ensure local execution remains within the **60% memory budget rule** (≤ 28GB weights + KV cache footprint):
```bash
# Monitor GPU memory and system compaction events in real time
sudo powermetrics --samplers gpu_power,cpu_power -i 1000
```

---

## 🔗 Connection Pooling Diagnostics

Growin aggregates outbound connections through the centralized `AgentHttpClient` to prevent TCP socket exhaustion.

### 1. Checking TCP Socket States
If the swarm encounters connection drops, verify active connection states:
```bash
# Count active sockets on backend ports (look for TIME_WAIT or ESTABLISHED spikes)
netstat -an | grep 8002 | awk '{print $6}' | sort | uniq -c
```

### 2. Circuit Breaker Testing
You can manually query client health metrics to see if connection breakers have tripped:
```bash
# Query agent client pool state
uv run python -c "from backend.app_context import get_http_client; print(get_http_client().get_circuit_status())"
```

---

## ♿ Accessibility Verification

Before pushing frontend updates, verify that UI layouts conform to native macOS accessibility and VoiceOver specs.

### 1. Verification Checklist
- Ensure interactive elements (e.g. `SovereignSlideToConfirm`, `SovereignSidebar` tabs) expose descriptive `.accessibilityLabel()` and `.accessibilityHint()` values.
- Verify elements use `.accessibilityAddTraits(.isButton)` or similar trait flags.
- Verify `.buttonStyle(.plain)` buttons include accessibility hints.

### 2. Launching Accessibility Diagnostics
Use the macOS native Xcode Accessibility Inspector to audit the layout:
```bash
# Launch native macOS Accessibility Inspector
open /Applications/Xcode.app/Contents/Applications/Accessibility\ Inspector.app
```

---

## 🧪 Testing Procedures

All tests run via `uv` to maintain strict dependency constraints.

### 1. Execution Roster
```bash
# Run all unit and integration tests
PYTHONPATH=backend uv run pytest tests/backend/

# Run quantitative math indicator tests
PYTHONPATH=backend uv run pytest tests/backend/test_quant_engine.py -v

# Run performance benchmarks
PYTHONPATH=backend uv run pytest tests/backend/test_performance_benchmarks.py -v
```

### 2. Database Analytical Auditing
Check DuckDB local data integrity, ticker normalizations, and agent alpha predictions:
```bash
# Query the live analyst database metrics
PYTHONPATH=backend uv run python -c "from analytics_db import get_analytics_db; print(get_analytics_db().get_agent_alpha_metrics())"
```

---

## 🛡️ Sandbox & Code Execution

The `MathGeneratorAgent` executes generated code blocks via the `SafePythonExecutor` sandbox.

### 1. Verifying Sandbox Security
Ensure the executor is properly isolated and not accessing external resources:
```bash
# Run sandbox isolation tests
PYTHONPATH=backend uv run pytest tests/backend/ -k "sandbox" -v
```

---

## 🩹 Debugging Protocols (The 3-Strike Rule)

If a bug or test failure persists after three consecutive debug attempts:

1.  **Stop**: Immediately suspend manual code changes.
2.  **Document**: Create an entry detailing:
    - Current error traceback
    - Details of the three attempts made
    - Active working hypothesis
    - Planned next approach
3.  **Handoff**: Commit staged changes and start a fresh context session to prevent prompt pollution.
