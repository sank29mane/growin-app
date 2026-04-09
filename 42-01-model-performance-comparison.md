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

<task type="auto" effort="medium">
  <name>Task 1: Create the VLLMInferenceEngine in backend/vllm_engine.py</name>
  <files>backend/vllm_engine.py</files>
  <action>
    Implement the base class for vllm-mlx inference based on mlx_vlm_engine.py pattern.
    USE: vllm-mlx library with PagedAttention and FP8 KV-cache.
  </action>
  <verify>ls backend/vllm_engine.py</verify>
  <done>File exists and contains VLLMInferenceEngine class.</done>
</task>

<task type="auto" effort="low">
  <name>Task 2: Update backend/model_config.py with benchmark model IDs</name>
  <files>backend/model_config.py</files>
  <action>
    Add Gemma-4-26B-A4B-MoE-4bit and Nemotron-3-Nano-30B-A3B-MoE-4bit to the registry.
  </action>
  <verify>grep "Gemma-4" backend/model_config.py</verify>
  <done>Models are defined in the configuration registry.</done>
</task>

<task type="auto" effort="low">
  <name>Task 3: Update LLMFactory in backend/agents/llm_factory.py</name>
  <files>backend/agents/llm_factory.py</files>
  <action>
    Integrate the new vllm engine into the LLMFactory.
    Register 'vllm' as a valid provider.
  </action>
  <verify>python3 -m py_compile backend/agents/llm_factory.py</verify>
  <done>LLMFactory supports the vllm provider.</done>
</task>

### Wave 2: Benchmarking

<task type="auto" effort="medium">
  <name>Task 4: Create scripts/benchmark_vllm_performance.py</name>
  <files>scripts/benchmark_vllm_performance.py</files>
  <action>
    Implement a benchmarking script to measure TTFT, TPS, and Reasoning Accuracy.
    Use a synthetic trading query set for accuracy testing.
  </action>
  <verify>ls scripts/benchmark_vllm_performance.py</verify>
  <done>Benchmarking script is implemented.</done>
</task>

<task type="auto" effort="high">
  <name>Task 5: Run Benchmark for Gemma-4-26B-A4B-MoE-4bit</name>
  <files>scripts/benchmark_vllm_performance.py</files>
  <action>
    Execute the benchmark script against the Gemma 4 model.
    Capture output to a log file.
  </action>
  <verify>test -f gemma4_benchmark.log</verify>
  <done>Benchmark metrics for Gemma 4 are recorded.</done>
</task>

<task type="auto" effort="high">
  <name>Task 6: Run Benchmark for Nemotron-3-Nano-30B-A3B-MoE-4bit</name>
  <files>scripts/benchmark_vllm_performance.py</files>
  <action>
    Execute the benchmark script against the Nemotron 3 model.
    Capture output to a log file.
  </action>
  <verify>test -f nemotron3_benchmark.log</verify>
  <done>Benchmark metrics for Nemotron 3 are recorded.</done>
</task>

### Wave 3: Analysis & Selection

<task type="auto" effort="low">
  <name>Task 7: Generate and record final Model Selection report</name>
  <files>.planning/phases/42-model-performance-comparison/42-SUMMARY.md</files>
  <action>
    Compile findings from benchmarks and select the v5.0 core model.
    Document the rationale for selection.
  </action>
  <verify>cat .planning/phases/42-model-performance-comparison/42-SUMMARY.md</verify>
  <done>Summary report exists with a final model selection decision.</done>
</task>

## Done When
- [ ] Benchmark comparison report completed.
- [ ] Final model for v5.0 core engine selected.
- [ ] `VLLMInferenceEngine` verified as stable.
