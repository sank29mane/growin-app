# GSD STATE MEMORY

## Current Position
- **Phase**: Phase 37 - RL Regime & Backtest Lab
- **Task**: Phase Initiation & vllm-mlx Architecture Prep
- **Status**: PLANNED
- **Branch**: `main`

## Summary
- **Pivot to Local Profit**: The project has shifted from SaaS/Supabase bloat to **Maximum Local Extraction** on M4 Pro (48GB RAM). 
- **Adopted Technology**: Native **`vllm-mlx`** engine with PagedAttention for zero-copy memory access and persistent KV cache.
- **Goal**: Implement a self-correcting RL swarm for LSE Leveraged ETF rebalancing (3GLD, 3QQQ, NVD3).
- **Delegation**: Maintenance and Import Unification delegated to Jules (Session 17444431987523978288).

## Active Tasks
| Task | Description | Status |
|------|-------------|--------|
| vllm-mlx Engine | Deploy native MLX inference server for Nemotron-3-Nano | TO_START |
| RL State Fusion | Fuse JMCE and TTM-R2 into a unified state for RL Action Head | PLANNED |
| LSE Scraper | Harvest active Leveraged ETP list from LSE Price Explorer | PLANNED |

## Success Criteria (Weekend)
1. RL Agent rebalances 3GLD/3QQQ in native backtest lab with >15% annualized Alpha.
2. `vllm-mlx` delivers >100 tokens/sec for concurrent agent calls on M4 Pro.
3. SwiftUI Dashboard displays real-time RL confidence and Profit/Loss.
