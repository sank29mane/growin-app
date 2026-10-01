# Phase 38: SwiftUI Profit Command Center & RL Training Integration - Context

## Objective
High-fidelity Profit Command Center (SwiftUI) for real-time alpha tracking and a hardened RL PPO training loop.

## Architecture: Split-Brain Inference
To optimize the M4 Pro (48GB RAM), we partition intelligence tasks by hardware affinity:
- **GPU (Metal/MLX):** `VLLMMXEngine` running Nemotron-3-Nano or JMCE models. Handles high-dimensional state reasoning, RL policy execution, and training.
- **CPU (Ollama):** Running `ibm-granite`. Handles auxiliary coordination, tool orchestration, and news parsing without competing for GPU KV cache.

## Architecture: PPO (Proximal Policy Optimization) Training Loop
A standard PPO implementation mapped to MLX for local Metal acceleration:
- **Policy Head:** MLX-based neural network for rebalance actions.
- **Trajectory Collection:** In-memory buffer of (state, action, reward, next_state) tuples.
- **GAE (Generalized Advantage Estimation):** Smoothed reward signal for financial time series.
- **Loss Function:** Clipping (surrogate objective) + Entropy bonus (to prevent collapse).
- **Reward Function:** $Reward = \Delta Alpha - (Transaction Cost + Volatility Tax)$.
    - Volatility Tax = $0.5 \sigma^2$.

## Decisions
- [LOCKED] Use `SwiftUI.Canvas` + `TimelineView(.animation)` for 120Hz rendering to bypass view-tree diffing overhead.
- [LOCKED] WebSocket 'Alpha-Stream' for metric delivery (avoiding polling lag).
- [LOCKED] Smart Money Window anchored to 14:00 UTC (9:00 AM ET / 2:00 PM GMT).
- [DISCRETION] UI layout: Priority on "Alpha vs Benchmark" and "Regime Confidence".
- [DISCRETION] Reward hyperparameters: `tc_penalty` = 0.0005.

## Deferred Ideas
- Multi-User support (Deferred to Phase 40+).
- Options Greeks integration (Deferred to Backlog).
