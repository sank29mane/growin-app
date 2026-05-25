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
# Start all backend services (FastAPI, Redis, vMLX)
./start.sh

# Stop all background services cleanly
./stop.sh

# View active system log streams
tail -f backend/data/growin_server.log
```

---

## 🧠 vMLX Operations (Local Serving Layer)

The **vMLX Serving Layer** hosts our local quantized dual-model layout (**Gemma 4 26B A4B MoE** and **Nemotron-Cascade-2 30B**).

### 1. Starting/Stopping the Serving Stack
The `vmlx_manager.py` manages model loading and hardware slot routing.
```bash
# Start the vMLX server manually (if not using start.sh)
uv run python backend/vmlx_manager.py --host 127.0.0.1 --port 8006 --quantize 4bit

# Clean teardown of local serving ports
kill -9 $(lsof -t -i:8006)
```

### 2. Live Heartbeat Verification
Verify the server is responsive and models are loaded:
```bash
# Fetch serving layer health status
curl http://127.0.0.1:8006/health

# Test prompt completion completion trace on the primary reasoner
curl http://127.0.0.1:8006/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gemma4-26b-moe",
    "messages": [{"role": "user", "content": "Test heartbeat"}]
  }'
```

### 3. Memory Pressure Monitoring
Always monitor Apple Silicon unified memory to ensure local execution remains within the **60% memory budget rule** (≤ 28GB weights + KV cache footprint):
```bash
# Monitor GPU memory and system compaction events in real time
sudo powermetrics --samplers gpu_power,cpu_power -i 1000

# Retrieve currently allocated active cache sizes
uv run python -c "from backend.vmlx_manager import get_active_cache; print(get_active_cache().get_summary())"
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
uv run pytest

# Run quantitative math indicator tests
uv run pytest tests/backend/test_quant_engine.py -v

# Run non-blocking vision chart tests
uv run pytest tests/vision/ -v
```

### 2. Database Analytical Auditing
Check DuckDB local data integrity, ticker normalizations, and agent alpha predictions:
```bash
# Query the live analyst database metrics
uv run python -c "from backend.analytics_db import get_analytics_db; print(get_analytics_db().get_agent_alpha_metrics())"
```

---

## 🛡️ Sandbox & Container Management

The `MathGeneratorAgent` executes generated code blocks inside an isolated Docker sandbox.

### 1. Check Sandbox Container Readiness
```bash
docker ps | grep growin-npu
```

### 2. Recovering from Runaway Sandbox Processes
If a container fails to terminate within 5 seconds of task completion:
```bash
# Force-remove all loose growin sandbox containers
docker rm -f $(docker ps -a -q --filter "ancestor=growin-npu-compute")
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
