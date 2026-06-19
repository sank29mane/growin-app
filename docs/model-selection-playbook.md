# Model Selection & Routing Playbook

This playbook provides guidance for routing tasks across Growin's local and remote models. It details our dual-model local execution strategy, memory budgets, and task-specific model routing.

---

## 🧠 Local Dual-Model Architecture

Growin employs a dedicated **dual-model architecture** running directly on Apple Silicon GPUs using `mlx_lm` and `mlx_vlm` for local inference. This dual-model approach balances extreme cognitive reasoning with fast execution.

### 1. Primary Reasoning Model: **Gemma 4 26B A4B MoE**
- **Role**: Primary reasoning, deep financial query processing, indicator analysis, and multi-agent debate coordination.
- **Why**: Extreme logical reasoning capabilities in a highly-optimized Mixture-of-Experts (MoE) footprint, ensuring high reasoning density.
- **VLM Support**: Loaded via `mlx_vlm.load()` for vision-language tasks (detected automatically by model path).

### 2. Executive Synthesis Model: **Nemotron-Cascade-2 30B**
- **Role**: Code generation (for math sandboxes), executive summarization, structured output formulation, and final user advisory synthesis.
- **Why**: Superior code writing capabilities and structured JSON/spec generation.

---

## ⚡ MLX Inference & Memory Budgets (M4 Pro 48GB)

To run dual-model inference locally without SSD swap latency, Growin enforces the **60% Memory Budget Rule** for Apple Silicon unified memory.

```
Total Physical RAM: 48GB (M4 Pro)
   ├── OS & SwiftUI Frontend: ~8GB
   ├── Backend Server & Redis: ~2GB
   └── MLX Inference Budget (Max 60%): 28GB  <-- HARD CAP
          ├── Gemma 4 26B MoE weights + KV cache: ~12GB (4-bit quantized)
          └── Nemotron-Cascade-2 weights + KV cache: ~14GB (4-bit quantized)
```

### MLX Memory Optimization Policies
- **Lazy Model Loading**: Models load on first use via `MLXInferenceEngine.load_model()`. Prevents test runners from spawning full inference stacks.
- **ResourceGuard Concurrency Limiter**: Monitors system memory pressure. If unified memory usage exceeds 85%, lower-priority tasks (e.g. background news sentiment indexing) are paused to prioritize real-time user-facing SSE chat streams.
- **Metal Cache Management**: `mx.metal.clear_cache()` called on model unload to reclaim GPU memory immediately.
- **Async Generation**: `asyncio.to_thread()` wraps blocking `mlx_lm.generate()` calls to prevent event loop stalling.

---

## 🔄 QLoRA Adapter Routing (Phase 46)

Regime-specific QLoRA adapters can be hot-swapped in-memory without reloading the base model:

| Adapter | Regime | Swap Latency |
|---------|--------|-------------|
| `high_vol_bull` | High volatility bull market | 10-50ms |
| `low_vol_range` | Low volatility ranging market | 10-50ms |
| `crisis_mode` | Market panic / correction | 10-50ms |

**Mechanism**: `MLXInferenceEngine.switch_adapter(adapter_path)` patches weights via `model.update(tree_unflatten(...))` followed by `mx.eval()`.

---

## 🎯 Task-Specific Routing Matrix

Growin's `SwarmOrchestrator` routes incoming user intent dynamically to the optimal model configuration:

| Task Type | Core Model | Routing Logic |
|-----------|------------|---------------|
| **Intent Classification** | Optimized Small Language Model (SLM) | sub-50ms token routing pinned to Apple Neural Engine (ANE) |
| **Portfolio & Quant Analysis** | Gemma 4 26B A4B MoE | Complex multi-agent debate, risk estimation, and mathematical logic |
| **Monte Carlo / Math Script Generation** | Nemotron-Cascade-2 30B | Generates code executed inside `SafePythonExecutor` sandbox |
| **RAG & News Synthesis** | Gemma 4 26B A4B MoE | Large context processing and news sentiment extraction |
| **Final Advisory Summarization** | Nemotron-Cascade-2 30B | Compiles specialist traces into highly-polished customer-facing advisory text |

---

## ❌ Anti-Patterns to Avoid

*   ❌ **Running Both Models Unquantized**: Attempting to run unquantized 26B MoE and 30B models concurrently exceeds the 48GB M4 Pro budget, causing OS memory compaction and severe SSD swapping. Always use 4-bit quantized weights.
*   ❌ **Blocking the Event Loop**: Executing synchronous model utility calls inside FastAPI's async thread blocks active SSE streams. Always route via `asyncio.to_thread()` or use `MLXInferenceEngine.generate()` (which wraps in `to_thread` internally).
*   ❌ **Skipping Model Unload**: When switching base models, always call `MLXInferenceEngine.unload()` first to free Metal cache and prevent memory fragmentation.

---

*See docs/ARCHITECTURE.md for deep system component layouts.*  
*See docs/runbook.md for MLX startup and diagnostics.*  
