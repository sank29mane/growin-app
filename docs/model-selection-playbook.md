# Model Selection & Routing Playbook

This playbook provides guidance for routing tasks across Growin's local and remote models. It details our dual-model local execution strategy, memory budgets, and task-specific model routing.

---

## 🧠 Local Dual-Model Architecture

Growin employs a dedicated **dual-model architecture** served locally on Apple Silicon GPUs using the **vMLX Serving Layer** (via PagedAttention and continuous batching). This dual-model approach balances extreme cognitive reasoning with fast execution.

### 1. Primary Reasoning Model: **Gemma 4 26B A4B MoE**
- **Role**: Primary reasoning, deep financial query processing, indicator analysis, and multi-agent debate coordination.
- **Why**: Extreme logical reasoning capabilities in a highly-optimized Mixture-of-Experts (MoE) footprint, ensuring high reasoning density.

### 2. Executive Synthesis Model: **Nemotron-Cascade-2 30B**
- **Role**: Code generation (for math sandboxes), executive summarization, structured output formulation, and final user advisory synthesis.
- **Why**: Superior code writing capabilities and structured JSON/spec generation.

---

## ⚡ vMLX Serving & Memory Budgets (M4 Pro 48GB)

To run dual-model inference locally without SSD swap latency, Growin enforces the **60% Memory Budget Rule** for Apple Silicon unified memory.

```
Total Physical RAM: 48GB (M4 Pro)
   ├── OS & SwiftUI Frontend: ~8GB
   ├── Backend Server & Redis: ~2GB
   └── vMLX Serving Budget (Max 60%): 28GB  <-- HARD CAP
          ├── Gemma 4 26B MoE weights + KV cache: ~12GB (4-bit quantized)
          └── Nemotron-Cascade-2 weights + KV cache: ~14GB (4-bit quantized)
```

### vMLX Memory Optimization Policies
- **PagedAttention**: Dynamically allocates KV cache in small pages, eliminating memory fragmentation and reducing the KV cache footprint by up to 40%.
- **ResourceGuard Concurrency Limiter**: Monitors system memory pressure. If unified memory usage exceeds 85%, lower-priority tasks (e.g. background news sentiment indexing) are paused to prioritize real-time user-facing SSE chat streams.
- **Continuous Batching**: Groups parallel agent inference calls into single GPU operations, maintaining throughput under swarm load.

---

## 🎯 Task-Specific Routing Matrix

Growin's `SwarmOrchestrator` routes incoming user intent dynamically to the optimal model configuration:

| Task Type | Core Model | Routing Logic |
|-----------|------------|---------------|
| **Intent Classification** | Optimized Small Language Model (SLM) | sub-50ms token routing pinned to Apple Neural Engine (ANE) |
| **Portfolio & Quant Analysis** | Gemma 4 26B A4B MoE | Complex multi-agent debate, risk estimation, and mathematical logic |
| **Monte Carlo / Math Script Generation** | Nemotron-Cascade-2 30B | Generates code executed inside the containerized Docker sandbox |
| **RAG & News Synthesis** | Gemma 4 26B A4B MoE | Large context processing and news sentiment extraction |
| **Final Advisory Summarization** | Nemotron-Cascade-2 30B | Compiles specialist traces into highly-polished customer-facing advisory text |

---

## ❌ Anti-Patterns to Avoid

*   ❌ **Direct Unmanaged MLX Calls**: Bypassing the vMLX server causes memory leaks, context thrashing, and GPU starvation. Always interface via the `vmlx_manager` API.
*   ❌ **Running Both Models Unquantized**: Attempting to run unquantized 26B MoE and 30B models concurrently exceeds the 48GB M4 Pro budget, causing OS memory compaction and severe SSD swapping. Always use 4-bit quantized weights.
*   ❌ **Blocking the Event Loop**: Executing synchronous model utility calls (e.g., local tokenizers or vision pre-processing) inside FastAPI's async thread blocks active SSE streams. Always route via `prepare_vlm_image_async` or thread executors.

---

*See docs/ARCHITECTURE.md for deep system component layouts.*  
*See docs/runbook.md for local vMLX startup and server diagnostics.*  
