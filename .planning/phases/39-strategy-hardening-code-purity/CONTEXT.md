# CONTEXT: Phase 39 - Strategy Hardening & Code Purity (M4 Pro Optimized)

## Goal
The goal of this phase is to refine the **RL-driven rebalancing policy** for **LSE Leveraged ETFs** (3GLD, 3QQQ, NVD3) while simultaneously cleaning up the architectural debt accumulated during the Phase 37/38 implementation of `vllm-mlx` and the SwiftUI Profit Command Center.

## Objectives
- **Strategy Tuning**: Optimize PPO hyperparameters (clipping, entropy, reward scaling) for specific high-volatility GBX-denominated ETFs.
- **Data Completeness**: Ensure the LSE Price Explorer scraper correctly identifies active leverage factors (3x, 5x) for 2PM GMT rebalancing windows.
- **Architectural Purity**: Unify all backend imports to absolute paths and remove legacy LM Studio / OpenAI-style adapter code.
- **UI Feedback Loop**: Connect the RL training metrics (Loss, Entropy, Reward) to the SwiftUI Command Center for real-time monitoring.

## Success Criteria
1. **Convergence**: RL policy shows stable loss reduction and reward improvement on 3QQQ/3GLD backtests.
2. **Clean Imports**: No more relative import errors (`ImportError: ... beyond top-level package`).
3. **Hardware Efficiency**: Verify `vllm-mlx` zero-copy memory usage remains stable during training + inference loops.
4. **Data Coverage**: All target LSE ETPs correctly listed in `backend/data/lse_etps.json`.
