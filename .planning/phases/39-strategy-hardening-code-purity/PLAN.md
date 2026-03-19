# PLAN: Phase 39 - Strategy Hardening & Code Purity

## Wave 1: Architectural Purity & Jules Maintenance
- [ ] **Unified Imports**: Refactor all `backend/` files to use absolute imports (`from backend.utils ...`) and verify with `ruff check .`.
- [ ] **Legacy Deletion**: Remove all unused `LM Studio` and `OpenAI` client adapters replaced by `vllm-mlx`.
- [ ] **Type Safety**: Update all `backend/agents/ppo_agent.py` functions to include 100% strict type hints for MLX arrays.
- [ ] **Jules Review**: Delegate deep linting to Jules (Session 17444431987523978288) to find "hidden" dead code in the `backtest_lab/` directory.

## Wave 2: Strategy Hardening (LSE Leveraged Focus)
- [ ] **Hyperparameter Grid**: Implement a small grid search in `backend/agents/ppo_agent.py` to optimize clipping epsilon (0.1 vs 0.2) and entropy coefficient (0.005 vs 0.02) for 3x ETPs.
- [ ] **Reward Calibration**: Fine-tune the `financial_reward` function to specifically penalize GBX spread and brokerage "slippage" for LSE trades.
- [ ] **Volatility Clustering**: Integrate the `RegimeAgent` output into the RL State Fusion to dynamically adjust risk per-regime (Calm vs Crisis).

## Wave 3: UI-Training Integration
- [ ] **Live Training Stream**: Implement a WebSocket endpoint in `server.py` to stream RL training metrics to SwiftUI.
- [ ] **Visualization**: Update `GrowinApp` (SwiftUI) to display a "Brain Stability" chart (Entropy vs Total Reward).
- [ ] **Policy Inspector**: Add a view to inspect the current "High Conviction" bypass threshold for autonomous trades.

## Verification Tasks
- [ ] Run `uv run pytest tests/backend/test_ppo_training.py` after tuning.
- [ ] Verify `vllm-mlx` server handles concurrent requests during training loops without OOM.
- [ ] Perform a "Zero Debt" audit of the `backend/` directory.
