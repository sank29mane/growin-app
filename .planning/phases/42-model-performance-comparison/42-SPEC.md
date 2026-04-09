# Phase 42 SPEC: Model Performance Comparison

**Status:** FINALIZED
**Requirement IDs:** INTEL-01, PERF-01 (Partial)
**Date:** 2026-04-10

## 🎯 Goal
Compare `vllm-mlx` output performance for Gemma 4 26B A4B MoE vs NVIDIA Nemotron 3 Nano 4-bit MLX to select the core reasoning model for Milestone v5.0.

## 🏗 Requirements

### 1. VLLM Inference Engine
- **REQ-01**: Implement a dedicated `VLLMInferenceEngine` in `backend/vllm_engine.py`.
- **REQ-02**: Must support standard inference interface (`load_model`, `generate`, `unload`).
- **REQ-03**: Must utilize `vllm-mlx` with PagedAttention and FP8 KV-cache optimizations.
- **REQ-04**: Must enforce 80% unified memory cache limit (for M4 Pro 48GB).

### 2. Benchmarking Utility
- **REQ-05**: Create a standalone script `scripts/benchmark_vllm_performance.py`.
- **REQ-06**: Measure:
    - Time to First Token (TTFT).
    - Tokens per second (TPS).
    - Reasoning Accuracy (Synthetic Trading Query set).
- **REQ-07**: Capture raw logs and structured benchmark report.

### 3. Model Configuration
- **REQ-08**: Update `backend/model_config.py` with benchmarked model IDs.
- **REQ-09**: Update `backend/agents/llm_factory.py` to support `vllm` provider.

## 🧱 Design
- **Engine Type**: `VLLMInferenceEngine` inheriting (or mirroring) established patterns in `mlx_engine.py`.
- **Target Models**:
    - `mlx-community/Gemma-4-26B-A4B-MoE-4bit`
    - `mlx-community/Nemotron-3-Nano-30B-A3B-MoE-4bit`

## 🧪 Verification Criteria
- **V-01**: `VLLMInferenceEngine` successfully loads and generates tokens from both models.
- **V-02**: `scripts/benchmark_vllm_performance.py` executes successfully and outputs a valid comparison report.
- **V-03**: `LLMFactory` can instantiate a `vllm` provider without error.

## 📈 Success Criteria
- Completed benchmark report comparing the two models.
- Decision record confirming the final model selection for Milestone v5.0.
