# Phase 42: Model Performance Comparison - Context

**Gathered:** 2026-04-10 (assumptions mode)
**Status:** Ready for planning

<domain>
## Phase Boundary

Goal: Compare vllm-mlx output performance for Gemma 4 26B A4B MoE vs NVIDIA Nemotron 3 Nano 4-bit MLX.
This phase focuses exclusively on benchmarking and model selection for the v5.0 core engine.
</domain>

<decisions>
## Implementation Decisions

### Architectural Integration
- **D-01:** Implement a dedicated `VLLMInferenceEngine` in `backend/vllm_engine.py`. This modular approach follows the established pattern in `mlx_engine.py` and `mlx_vlm_engine.py`.

### Benchmarking Strategy
- **D-02:** Use a standalone utility script at `scripts/benchmark_vllm_performance.py` for performance tests. This ensures hardware isolation and detailed logging without the overhead of the standard test suite.

### Model Acquisition & Quantization
- **D-03:** Standardize on 4-bit pre-quantized models from `mlx-community` on Hugging Face (Gemma 4 26B A4B MoE and Nemotron 3 30B A3B MoE).

### Provider Orchestration
- **D-04:** Update `LLMFactory` (`backend/agents/llm_factory.py`) to support a `vllm` provider, allowing seamless switching to the high-throughput engine.

### Resource Management
- **D-05:** Configure a 0.8 (80%) unified memory cache limit. On the M4 Pro (48GB RAM), this provides ~38GB for inference, protecting against OOM crashes during concurrent MAS tasks.

### Claude's Discretion
- Standardize the benchmark metrics on: Latency (TTFT), Throughput (Tokens/s), and Reasoning Accuracy (via a synthetic trading query set).
- Use `sysctl` overrides if needed during benchmarks to maximize GPU performance.
</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

- `.planning/PROJECT.md` (Milestone v5.0 Vision)
- `.planning/research/SUMMARY.md` (Hardware & Model benchmarks)
- `backend/mlx_vlm_engine.py` (Established engine pattern)
- `backend/agents/llm_factory.py` (Central provider entry point)
</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `MLXVLMInferenceEngine` (`backend/mlx_vlm_engine.py`): Template for the new VLLM engine.
- `LLMFactory` (`backend/agents/llm_factory.py`): Orchestration logic for new providers.
- `scripts/simulate_leveraged_etfs_mlx.py`: Pattern for standalone performance scripts.

### Established Patterns
- Modular inference engines with standard `load_model`, `generate`, and `unload` methods.
- Strict 80% memory warning thresholds.
- Centralized model registry in `model_config.py`.

### Integration Points
- `LLMFactory` update to route to the new `vllm` engine.
- `model_config.py` updates to include the new benchmarked models.
</code_context>

<specifics>
## Specific Ideas

- Focus on the **A4B** MoE architecture for Gemma 4 to ensure we are testing the latest "Mixture of Experts" performance.
- Verify **macOS Tahoe (v26)** specific MLX optimizations during the benchmark.
</specifics>

<deferred>
## Deferred Ideas

- Full Core Engine integration (Phase 43).
- Unsloth fine-tuning pipeline (Phase 45).
- Vision Intelligence (Phase 46).

### Reviewed Todos (not folded)
- None.
</deferred>
