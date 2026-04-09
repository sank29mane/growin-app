# 42-01: Model Performance Comparison

## Goal
Compare `vllm-mlx` output performance for Gemma 4 vs Nemotron 3 to select the core model for v5.0.

## Metadata
**Phase:** Phase 42 - Model Performance Comparison
**Required_Skills:**
- `backend-specialist` (Source: Global)
- `performance-optimizer` (Source: Global)

## Tasks

### Wave 1: Foundation
- [ ] Task 1: Create the `VLLMInferenceEngine` in `backend/vllm_engine.py`.
      - **Context:** `CLI`
      - **Skill:** `backend-specialist`
      - **Instruction:** Implement the base class for `vllm-mlx` inference based on `mlx_vlm_engine.py` pattern.
      - **Verify:** `ls backend/vllm_engine.py`

- [ ] Task 2: Update `backend/model_config.py` with benchmark model IDs.
      - **Context:** `CLI`
      - **Skill:** `backend-specialist`
      - **Instruction:** Add `Gemma-4-26B-A4B-MoE-4bit` and `Nemotron-3-Nano-30B-A3B-MoE-4bit` to the registry.
      - **Verify:** `grep "Gemma-4" backend/model_config.py`

- [ ] Task 3: Update `LLMFactory` in `backend/agents/llm_factory.py` to support `vllm` provider.
      - **Context:** `CLI`
      - **Skill:** `backend-specialist`
      - **Instruction:** Integrate the new engine into the factory.
      - **Verify:** No syntax errors in `backend/agents/llm_factory.py`.

### Wave 2: Benchmarking
- [ ] Task 4: Create `scripts/benchmark_vllm_performance.py`.
      - **Context:** `CLI`
      - **Skill:** `performance-optimizer`
      - **Instruction:** Implement a benchmarking script to measure TTFT, TPS, and Accuracy.
      - **Verify:** `ls scripts/benchmark_vllm_performance.py`

- [ ] Task 5: Run Benchmark for `Gemma-4-26B-A4B-MoE-4bit`.
      - **Context:** `CLI`
      - **Skill:** `performance-optimizer`
      - **Instruction:** Execute benchmark and record results.
      - **Verify:** Log file generated.

- [ ] Task 6: Run Benchmark for `Nemotron-3-Nano-30B-A3B-MoE-4bit`.
      - **Context:** `CLI`
      - **Skill:** `performance-optimizer`
      - **Instruction:** Execute benchmark and record results.
      - **Verify:** Log file generated.

### Wave 3: Analysis & Selection
- [ ] Task 7: Generate and record final Model Selection report.
      - **Context:** `CLI`
      - **Skill:** `backend-specialist`
      - **Instruction:** Compile findings and select the v5.0 core model.
      - **Verify:** Create `.planning/phases/42-model-performance-comparison/42-SUMMARY.md`.

## Done When
- [ ] Benchmark comparison report completed.
- [ ] Final model for v5.0 core engine selected.
- [ ] `VLLMInferenceEngine` verified as stable.
