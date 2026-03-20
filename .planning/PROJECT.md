# PROJECT: Growin App
Status: IN_DEVELOPMENT

## Vision
**Growin** is a sophisticated, native macOS application built specifically for **Apple Silicon (M4 optimized)**. It combines advanced AI capabilities with real-time financial data to provide:
- Intelligent portfolio analysis.
- Automated trading insights via local LLMs.
- Neural JMCE for volatility regime detection.
- Autonomous "High Conviction" trade execution.

The mission is to transform financial intelligence through the power of local AI and Apple Silicon, focusing on **Maximum Local Extraction** (privacy-first, low-latency, high-performance).

## Core Requirements

### Financial Intelligence
- **Neural JMCE**: Joint Mean-Covariance Estimator for real-time volatility and correlation analysis.
- **TTM-R2 Forecasting**: High-fidelity price prediction using multivariate inputs.
- **Regime Detection**: Automated classification of market states (Calm, Dynamic, Crisis).

### Agentic Autonomy
- **Decision Swarm**: Multi-agent coordination (Quant, Forecast, Risk, Decision).
- **High Conviction Bypass**: Autonomous trade execution for 10/10 setups.
- **MLX Weight Adapters**: Daily local GPU re-calibration based on market feedback.

### Platform & Performance
- **macOS Native**: SwiftUI + Accelerate + Metal for 120Hz smooth performance.
- **M4 Partitioning**: CPU (Orchestration), GPU (MLX Reasoning), NPU (Neural JMCE).
- **Zero-Copy Memory**: vllm-mlx with PagedAttention for efficient resource usage.

## Current Scoped Requirements (Phase 37+)
- **RL-driven Rebalancing**: Using PPO Action Heads in MLX.
- **LSE Leveraged ETF Focus**: Specialized strategy for LSE 3x ETPs (3GLD, 3QQQ, NVD3).
- **vllm-mlx Integration**: Native inference server for Nemotron-3-Nano.

## Success Criteria (Milestone)
1. RL Agent rebalances target ETPs with >15% annualized Alpha in backtest.
2. `vllm-mlx` delivers >100 tokens/sec concurrent performance on M4 Pro.
3. SwiftUI Dashboard provides real-time transparency of agent "thoughts" and P&L.
