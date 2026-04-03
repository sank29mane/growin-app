# Phase 37 Context: RL Regime & Backtest Lab

## 🧠 Strategic Intent
The Growin App is transitioning from a "Predictive Advisor" to an **"Autonomous Rebalancing Engine"** specifically for LSE Leveraged ETFs. This phase exploits the MacBook M4 Pro (48GB) hardware to run high-throughput RL policies.

## 🏛 Hardware Context
- **Machine**: MacBook Pro M4 Pro
- **Memory**: 48GB Unified RAM (~35GB allocatable to GPU)
- **Acceleration**: 
    - **MLX**: Native framework for GPU-accelerated LLM inference and RL training.
    - **ANE (Apple Neural Engine)**: Offloading JMCE covariance math.
    - **Metal**: High-bandwidth memory access for 120+ tokens/sec throughput.

## 🛠 Technology Adoption (SOTA 2026)
- **Inference**: Native `vllm-mlx`. This replaces the LM Studio REST API to eliminate latency. It enables **PagedAttention** and **Continuous Batching**, allowing multiple agents to query the brain simultaneously without head-of-line blocking.
- **Model**: **Nemotron-3-Nano (30B MoE)**. Uses only 3.5B active parameters per token, providing the reasoning of a large model with the speed of a tiny one.
- **Forecasting**: **IBM TTM-R2** fused with **Neural JMCE**. Tiny models (<1M parameters) that will be fine-tuned daily using **LoRA heads** based on realized P&L.

## 🎯 Success Criteria
- Native `vllm-mlx` integration running locally.
- JMCE Eigenvalues used for predictive regime detection.
- RL State Vector successfully fusing Technicals, Forecasts, and Time-to-Close.
- Alpha verification in NPU-accelerated backtest.
