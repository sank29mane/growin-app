---
phase: 49
plan: 1
wave: 1
---

# Plan 49.1: Apple Neural Engine (ANE) Integration for Numeric Forecasting

## Objective
Calibrate and serve the Time-Series forecasting model on ANE (Apple Neural Engine) achieving sub-millisecond execution times and complete resource isolation from the MLX GPU pipeline.

## Context
- .planning/phases/49/RESEARCH.md
- backend/utils/jmce_model.py
- backend/coreml_inference.py
- tests/backend/test_coreml_inference_phase46.py

## Tasks

<task type="auto">
  <name>Model Factory and Compute Unit Calibration</name>
  <files>
    <file>backend/utils/jmce_model.py</file>
    <file>backend/coreml_inference.py</file>
  </files>
  <action>
    1. In `backend/utils/jmce_model.py`, modify `get_jmce_model()` to search for both `.mlmodel` and `.mlpackage` variants of `jmce_{n_assets}_{resolution.value}` and add `NeuralJMCE.mlpackage` as the default package fallback.
    2. In `backend/coreml_inference.py`, update `CoreMLRunner.__init__()` to lock ComputeUnits to `ct.ComputeUnit.CPU_AND_NE` (CPU and Neural Engine only) instead of `ALL`. This ensures that CoreML does not claim GPU resources.
  </action>
  <verify>python -m py_compile backend/utils/jmce_model.py backend/coreml_inference.py</verify>
  <done>Code compiles and can load the NeuralJMCE.mlpackage on CPU/ANE successfully.</done>
</task>

<task type="auto">
  <name>Stress Testing and Performance Benchmarking</name>
  <files>
    <file>tests/backend/test_coreml_inference_phase46.py</file>
  </files>
  <action>
    1. Update `tests/backend/test_coreml_inference_phase46.py` to implement a 1,000-iteration performance benchmark.
    2. Discard the first 10 runs as warmup. Measure steady-state latency for the remaining 990 runs.
    3. Calculate and print Mean, P95, and P99 latency.
    4. Assert that mean latency is under 3.0ms (accounting for Python-to-C wrapper bridging overhead) and verify memory footprint stability.
  </action>
  <verify>PYTHONPATH=backend backend/.venv/bin/pytest tests/backend/test_coreml_inference_phase46.py -s</verify>
  <done>Tests pass, displaying average latency and verifying resource stability.</done>
</task>

## Success Criteria
- [ ] CoreML model factory correctly resolves `NeuralJMCE.mlpackage` on Apple Silicon.
- [ ] Compute units are locked to CPU + ANE (Neural Engine) to isolate execution from GPU.
- [ ] 1,000-iteration stress test runs successfully, reporting average execution latency and verifying zero memory reference leak.
