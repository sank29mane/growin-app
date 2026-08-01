# 🏆 TASK: Comprehensive SOTA Performance & Security Evaluation (Milestone v5.0)

## 🎯 MISSION
Perform an end-to-end evaluation of the Growin App's intelligence layer (vMLX/MLX) and multi-agent swarm architecture. Your goal is to measure reasoning quality, prediction accuracy, hardware efficiency, and security resilience using 2026 SOTA benchmarks.

## 🛠 CORE INSTRUCTIONS
1. **SYNC**: Pull the latest `main` branch immediately.
2. **PLAN**: Create a `PHASE-44-EVAL-PLAN.md` documenting your tactical approach before acting.
3. **IMPLEMENT**: Execute the plan step-by-step, generating detailed reports for each category.

## 📋 TACTICAL EVALUATION MODULES

### 1. Reasoning & Logic Quality (CoT)
- **Objective**: Measure "Information Gain" (InfoGain) and logic consistency.
- **Task**: Run 20 complex financial reasoning queries (e.g., "Analyze the impact of a 50bps rate hike on 3x Leveraged Nasdaq ETFs given current yield curve inversion").
- **Metric**: Score 1-10 on logic coherence, step-by-step verification (CoT), and hallucination resistance.

### 2. Prediction Accuracy (FinQA)
- **Objective**: Verify numeric precision and financial forecasting.
- **Task**: Compare agent predictions against historical OHLCV data in DuckDB for the last 7 days.
- **Metric**: Accuracy % on direction (Long/Short) and price target proximity. Use `backend/utils/financial_math.py` for verification.

### 3. Hardware-Aware Performance (M4 Pro)
- **Objective**: Validate the "60% Rule" (Weight + KV <= 28GB) and latency.
- **Task**: Measure `time_to_first_token`, tokens per second, and peak memory usage during a "Swarm Surge" (5 concurrent agents).
- **Metric**: Ensure TTFT < 2.0s and zero memory swaps.

### 4. Edge Case & Stress Testing
- **Objective**: Test system stability under extreme conditions.
- **Task**: 
    - Scenario A: Ticker not found/Delisted.
    - Scenario B: API Timeout/Circuit Breaker Trigger.
    - Scenario C: Conflicting data between Fast (Quant) and Slow (Sentiment) agents.

### 5. Security Vulnerability Audit (Red Teaming)
- **Objective**: Identify prompt injection and data leakage risks.
- **Task**: 
    - Attempt "Indirect Prompt Injection" via a mock news article.
    - Test for "System Prompt Leakage" (LLM07).
    - Verify "Excessive Agency" (LLM06) - Can an agent trigger a trade without valid HITL approval?

## 📊 DELIVERABLES
1. `PHASE-44-EVAL-PLAN.md` (Strategy)
2. `PHASE-44-PERFORMANCE-REPORT.md` (Reasoning, Accuracy, Hardware)
3. `PHASE-44-SECURITY-AUDIT.md` (Vulnerabilities, Red Teaming)
4. `scripts/reproduction/stress_test_swarm.py` (Automation script)

**Use Google Search as needed to fetch the latest 2026 financial evaluation datasets or specific security payloads for MLX models.**

Execute autonomously but provide a summary after each major module completion.
