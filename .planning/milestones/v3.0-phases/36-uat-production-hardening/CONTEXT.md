# Phase 36: UAT & Production Hardening - Context

## Overview
Phase 36 transitions the Multi-Modal Swarm from mocked verification to real-world execution on Apple Silicon (M4 Pro/Max). This phase focuses on live traces, performance benchmarking, and system resilience, integrating SOTA 2026 best practices for local VLM inference and Multi-Agent Systems (MAS).

## Implementation Decisions

### 1. VLM Inference Strategy & Hardening
- **Primary Model**: `Qwen2.5-VL-7B-Instruct-4bit` (High Fidelity).
- **Shadow/Quick Mode**: `Qwen2-VL-2B` (Low Latency/Validation).
- **Inference Optimization**:
    - **Content-Based Prefix Caching**: Enable in `mlx-vlm` to eliminate redundant vision encoding for repeated queries on the same chart (28x potential speedup).
    - **Memory Cache Limit**: Use `mlx.core.set_cache_limit` to cap MLX at 80% of available unified memory to prevent system-wide swap death.
- **Loading Policy**: "Lazy Load with Keep-Alive" (10-minute TTL).
- **Security Guardrails**:
    - **Weight Integrity**: Verify `.safetensors` checksums on engine initialization.
    - **Visual Prompt Injection Guard**: Implement a lightweight "Guardrail" check on VLM output to detect "Ignore previous instructions" patterns hidden in visual data.

### 2. UAT Scenarios & "Shadow Mode"
- **Shadow UAT Phase**: Before live execution, the system must run in "Shadow Mode" where all trade commands are intercepted, logged, and compared against a 14-day P&L/Risk benchmark without committing capital.
- **Target Assets**: 
    - **LETFs**: TQQQ, SQQQ (High volatility, critical for daily rebalancing).
    - **Mega-Caps**: TSLA, NVDA (High retail sentiment/visual pattern density).
- **Timeframes**: 15m (Entry) and 1H (Trend).
- **Negative Testing**: Include "Noise" charts and "Corrupted/Blurred" images to verify Magentic's rejection logic.

### 3. Decision Weighting & Fusion (SOTA 2026)
- **Hybrid Fusion Logic**:
    - **40% Quant/Technicals** (RSI, MACD, Support/Resistance).
    - **30% Forecasting** (TTM-R2 Predictions).
    - **30% Visual/Sentiment** (VLM Pattern Analysis).
- **Conviction Multiplier**: Visual patterns with >0.85 confidence grant a **1.2x Conviction Multiplier**. 
- **Traceable Reasoning Chain**: Every decision must export a `reasoning_trace.json` mapping: *Data Input -> Agent Thought -> Agent Critique -> Final Consensus*.

### 4. Concurrency & Hardware Policy
- **Single-Worker Queue**: Process VLM tasks sequentially via a priority queue to avoid NPU/GPU contention.
- **Thermal Awareness**: Monitor system thermals; if throttling is detected, downgrade to "Shadow/Quick Mode" (2B model) to reduce load.

## Code Context & Integration Points
- **`backend/mlx_vlm_engine.py`**: Implement `set_cache_limit`, prefix caching, and the checksum verification.
- **`backend/agents/vision_agent.py`**: Add the Visual Prompt Injection guardrail and Shadow Mode interceptor.
- **`backend/agents/decision_agent.py`**: Update to support the Traceable Reasoning Chain export and the 30/30/40 fusion weighting.
- **`scripts/uat_live_trace.py`**: Refactor into a comprehensive "Shadow Mode" simulation harness.

## Success Criteria
- **End-to-End Latency**: <1s TTFT (Time to First Token) for cached vision queries.
- **Memory Stability**: Zero "Out of Memory" errors during 24-hour continuous shadow trading.
- **Alignment**: 99.7% alignment between Swarm decisions and Risk Agent constraints during Shadow UAT.
- **Fidelity**: DecisionAgent reasoning must cite specific visual pattern coordinates in >80% of successful detections.

