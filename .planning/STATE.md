# GSD STATE MEMORY

## Current Position
- **Phase**: Phase 39 - Strategy Hardening & Code Purity
- **Task**: Phase Execution
- **Status**: PLANNED
- **Branch**: `main`

## Summary
- **Phase 37/38 Status**: Phase 37/38 are in the roadmap and partially planned, but Phase 39 has been fast-tracked for architectural stabilization and RL policy refinement.
- **Architectural Cleanup**: Migration to absolute imports and removal of LM Studio/OpenAI legacy debt is the immediate priority to prevent maintenance friction.
- **RL Optimization**: Grid search for PPO hyperparameters on M4 Pro is prepared to ensure convergence for LSE ETP rebalancing.
- **Monitoring**: Real-time training metrics via WebSocket are planned to provide the SwiftUI dashboard with brain stability indicators.

## Active Tasks
| Task | Description | Status |
|------|-------------|--------|
| Absolute Imports | Convert `backend/agents/` to absolute paths | TO_START |
| Legacy Removal | Delete `lm_studio_client.py` and old adapters | TO_START |
| PPO Grid Search | Hyperparameter optimization script for MLX | PLANNED |
| Training Stream | WebSocket endpoint for RL training metrics | PLANNED |

## Success Criteria (Phase 39)
1. 100% absolute imports in `backend/`.
2. PPO hyperparameter grid search results ranked by financial reward.
3. Stable WebSocket training stream with Brain Stability metrics.
