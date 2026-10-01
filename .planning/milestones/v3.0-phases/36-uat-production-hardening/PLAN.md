# Phase 36: UAT & Production Hardening - Plan

## Objective
Harden the Multi-Modal Swarm for production readiness on Apple Silicon (M4 Pro/Max) by implementing SOTA 2026 inference optimizations, memory safety guards, and a 14-day "Shadow Mode" UAT framework.

## Wave 1: Core Engine & Agent Hardening
**Goal**: Implement low-level stability and security for VLM inference.

- [ ] **Task 1.1: MLX Memory & Cache Optimization**
    - Update `backend/mlx_vlm_engine.py`.
    - Implement `mlx.core.set_cache_limit` (cap at 80%).
    - Enable content-based prefix caching for 28x vision speedup.
    - **Verification**: `python scripts/validate_mlx_limits.py` (Verify memory cap and cache hits).
- [ ] **Task 1.2: Model Integrity & TTL Management**
    - Add `.safetensors` checksum verification on engine startup.
    - Implement 10-minute "Keep-Alive" TTL for model residency.
    - **Verification**: Logs showing checksum pass and auto-unload after 10m.
- [ ] **Task 1.3: Visual Prompt Injection Guardrail**
    - Add a lightweight regex/heuristic check to `VisionAgent` to detect "Ignore previous instructions" in VLM outputs.
    - **Verification**: `pytest tests/test_vision_guardrails.py` with adversarial text inputs.

## Wave 2: Decision Fusion & Traceability
**Goal**: Implement weighted decision logic and audit-ready reasoning traces.

- [ ] **Task 2.1: 30/30/40 Hybrid Fusion**
    - Update `backend/agents/decision_agent.py` to weight inputs: 40% Quant, 30% Forecast, 30% Visual.
    - Implement the **1.2x Conviction Multiplier** for visual patterns >0.85 confidence.
    - **Verification**: Compare decision output with and without high-confidence visual patterns.
- [ ] **Task 2.2: Traceable Reasoning Chain (JSON)**
    - Implement `reasoning_trace.json` export for every decision.
    - Map: *Input -> Agent Thoughts -> Critique -> Consensus*.
    - **Verification**: Verify valid JSON schema and content for 5 different test queries.

## Wave 3: Shadow Mode Infrastructure
**Goal**: Build the framework for safe production testing without capital risk.

- [ ] **Task 3.1: Shadow Mode Interceptor**
    - Implement a bypass in the execution logic to intercept trade commands and log them instead of sending to Trading212.
    - **Verification**: Execute a "Buy" signal and confirm it appears in `shadow_trades.log` but not in T212 history.
- [ ] **Task 3.2: 14-day Benchmark Harness**
    - Create `scripts/shadow_uat_harness.py` to automate the shadow trading loop.
    - Integrate with the 14-day P&L/Risk benchmark comparison.
    - **Verification**: Successful 1-hour dry run of the harness with real data feeds.

## Wave 4: Live UAT Trace & Benchmarking
**Goal**: Final verification against target assets and production success criteria.

- [ ] **Task 4.1: Target Asset Stress Test**
    - Run the swarm against TQQQ, SQQQ, TSLA, and NVDA (15m/1H charts).
    - **Verification**: Capture latency (<1s TTFT) and memory stability metrics.
- [ ] **Task 4.2: Negative & Error Testing**
    - Feed "Noise" and "Corrupted" images to the swarm.
    - **Verification**: Verify rejection logic (Low confidence / Rejection reasons).
- [ ] **Task 4.3: Final Phase 36 Validation**
    - Review against Success Criteria: 99.7% Risk Alignment, 0 OOM errors, >80% Fidelity.

## Success Criteria
- **Latency**: <1s TTFT for cached queries.
- **Stability**: Zero OOM crashes in 24h.
- **Alignment**: 99.7% Risk Agent compliance.
- **Fidelity**: >80% visual coordinate citation in reasoning.
