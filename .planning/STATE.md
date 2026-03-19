# GSD STATE MEMORY

## Current Position
- **Phase**: Phase 38 - SwiftUI Profit Command Center
- **Task**: Phase Planning & Research
- **Status**: PLANNED
- **Branch**: `main`

## Summary
- **Phase 37 Complete**: Successfully implemented the RL Regime & Backtest Lab, including `vLLM-MLX` engine, RL State Fusion, and NPU-accelerated backtesting.
- **Pivot to Local Profit**: The project has shifted from SaaS/Supabase bloat to **Maximum Local Extraction** on M4 Pro (48GB RAM). 
- **Adopted Technology**: Native **`vllm-mlx`** engine with PagedAttention for zero-copy memory access and persistent KV cache.
- **Goal**: Build the SwiftUI Profit Command Center for real-time Alpha tracking and RL policy visualization.

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
