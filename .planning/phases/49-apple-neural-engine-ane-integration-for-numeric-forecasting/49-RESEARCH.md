# Phase 49 Research: Apple Neural Engine (ANE) Integration for Numeric Forecasting

## 1. CoreML Model Resolution Analysis
- **Current Situation**: The factory function `get_jmce_model()` in [jmce_model.py](file:///Users/sanketmane/Codes/Growin%20App/backend/utils/jmce_model.py) checks for resolution-specific paths matching `jmce_{n_assets}_{resolution}.mlmodel`. 
- **The Gap**: The repository contains only a single pre-compiled model package: `models/coreml/NeuralJMCE.mlpackage`. Since `.mlpackage` is the macOS 12+ package format, `get_jmce_model` never finds this file and always falls back to GPU execution via MLX.
- **The Fix**: Expand the possible search paths in `get_jmce_model` to search for:
  1. `jmce_{n_assets}_{resolution.value}.mlmodel`
  2. `jmce_{n_assets}_{resolution.value}.mlpackage`
  3. `NeuralJMCE.mlpackage` (the default fallback model)
  4. `NeuralJMCE.mlmodel`

## 2. Hardware Resource Isolation (MLX vs. CoreML)
- **VRAM Constraints**: The local fast-loop agent reasoning runs on the GPU via MLX. To prevent VRAM resource conflicts or out-of-memory crashes on Apple Silicon (M4 Pro 48GB, under 28GB active VRAM limits), we must keep CoreML off the GPU.
- **ANE Enforcement**: Configure `CoreMLRunner` in [coreml_inference.py](file:///Users/sanketmane/Codes/Growin%20App/backend/coreml_inference.py) with `self._compute_units = ct.ComputeUnit.CPU_AND_NE` (CPU and Neural Engine). This locks the model out of the GPU entirely.

## 3. High-Throughput & Latency Requirements
- **Goal**: Achieved sub-millisecond core predictions on ANE.
- **Stress-Testing Strategy**: Run a 1,000-iteration performance validation run.
  - Skip the first 10 warmup runs (CoreML paging/compilation time).
  - Measure steady-state latency of the remaining 990 runs.
  - Calculate average and P95/P99 tail latency.
  - Verify that running 1,000 runs does not trigger memory reference leaks or memory growth.
