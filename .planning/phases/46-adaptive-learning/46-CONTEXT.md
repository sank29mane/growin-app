# Phase 46: Adaptive Learning & Alpha Engineering (Unsloth) - Context

**Gathered:** 2026-06-05
**Updated:** 2026-06-17
**Status:** Assumptions Audited & Ready for Planning

<domain>
## Phase Boundary

Establishing local model fine-tuning (LoRA adapters) and automated alpha feature engineering from historical DuckDB ETF market data. The phase covers setting up a hardware-optimized native MLX training environment, formulating DuckDB regime classification pipelines, and implementing dynamic adapter routing inside the local inference/serving tier.
</domain>

<decisions>
## Implementation Decisions

- **D-16 (Fine-Tuning Engine):** Standardized on native MLX fine-tuning (`mlx-lm.lora`) utilizing Metal GPU acceleration on macOS, bypassing CUDA-bound Unsloth to avoid Docker/VM performance loss.
- **D-17 (Dynamic Adapter Switching):** Standardized on in-memory weight patching for LoRA adapters in `MLXInferenceEngine` via `model.update(tree_unflatten(list(weights.items())))` and `mx.eval(model.parameters())` to avoid reload latency (dropping it to 10-50ms) and eliminate 2x base model RAM spikes.
- **D-18 (NeuralJMCE Hardware Partitioning):** Execute neural network feature extraction on ANE (via CoreML) and partition mathematical transformations (Cholesky decomposition, FFTs) to run on the CPU (using Accelerate) or GPU (using Metal/MPS) to avoid custom CoreML layer overhead.
- **D-19 (Local Concurrency Limits):** Throtle memory-intensive operations between LM Studio and MLX runtime elements to keep system-wide VRAM and RAM consumption below the 60% memory threshold (28GB threshold on M4 Pro).
</decisions>

<research_items>
## Resolved Research Items

1. **Model Selection & Quantization for Tuning:**
   - *Resolution:* Standardized on 4-bit quantized base models (like LFM 2.5B or Granite 2B) running on native MLX GPU/Metal. Memory footprints are minimized to allow concurrent slow/fast loop execution within the 60% rule.
2. **DuckDB Alpha Extraction & Pipeline:**
   - *Resolution:* Standardized features extracted from `backend/data/analytics.duckdb` (historical LSE ETF datasets), calculating rolling volatility, RSI, ATR, and CVD metrics mapped to forward-looking return labels.
3. **Dynamic LoRA Adapter Switching:**
   - *Resolution:* In-memory parameter updates (`model.update()`) resolve standard reload latency, achieving hot-swapping under 50ms without instantiating model graph duplicates.
4. **ANE Compatibility for NeuralJMCE:**
   - *Resolution:* Mathematical/Cholesky transforms must execute on CPU (Accelerate) or GPU (MPS/Metal) instead of compiling custom layers for ANE, avoiding high data transfer penalties.
</research_items>

<canonical_refs>
## Canonical References

- `backend/mlx_engine.py` — Location of local MLX inference engine to implement D-17 in-memory adapter hot-swapping.
- `backend/agents/llm_factory.py` — Standardized entry point for local and cloud LLM providers.
- `backend/analytics_db.py` — DuckDB manager for feature extraction queries.
- `backend/utils/jmce_model.py` — Implementation of NeuralJMCE and CoreML loading factory logic.
- `.planning/phases/46-adaptive-learning/46-DISCUSSION-LOG.md` — Detailed assumptions audit trail.
- `.planning/STATE.md` — Active tracking state.
</canonical_refs>

---

*Phase: 46-adaptive-learning*
*Context updated: 2026-06-17*
