# Phase 37 Plan: RL Regime & Backtest Lab

## Wave 1: Inference Engine & Data Harvesting
- [ ] **Deploy `vllm-mlx` Server**
    - Install `vllm-mlx` via `uv`.
    - Create `backend/vllm_mlx_engine.py` to manage the lifecycle of the Nemotron-3-Nano process.
    - Configure PagedAttention block size and GPU memory fraction (~0.7).
- [ ] **LSE Leveraged ETF Scraper**
    - Execute `scripts/fetch_leveraged_etf_data.py`.
    - Build `data/etfs/lse_leveraged.json` with ticker mappings (e.g., TSL3 -> Tesla 3x Long).
    - Implement GBX-to-GBP normalization in `utils/currency_utils.py`.
- [ ] **State Fusion Logic**
    - Complete `backend/agents/rl_state.py`.
    - Integrate **2:00 PM GMT Smart Money** boolean indicator.
    - Normalize JMCE Mu/Sigma vectors using MLX `mx.norm`.

## Wave 2: Predictive Regime Detection
- [ ] **JMCE Eigenvalue Integration**
    - Update `RegimeAgent` to ingest JMCE Cholesky factor `L`.
    - Calculate $\Sigma = LL^T$ and extract the Spectral Radius (Largest Eigenvalue).
    - Define Regime Thresholds: `CALM` (Low Variance), `DYNAMIC` (Expansion), `CRISIS` (Contagion).
- [ ] **Nightly Calibration (ReFT)**
    - Implement `backend/agents/calibration_agent.py`.
    - Create a feedback loop: Compare `MarketContext.forecast` vs `PriceData.actual` at LSE close.
    - Update LoRA adapters for TTM-R2 decoder head using `mlx.optimizers`.

## Wave 3: RL Policy & NPU Backtest
- [ ] **PPO Action Head**
    - Build a 3-layer MLP in MLX as the "Actor".
    - Output Space: `[-1.0, 1.0]` (Target Portfolio Weight).
    - Objective: Maximize Cumulative Alpha - (Transaction Cost + Volatility Tax).
- [ ] **Parallel Backtester**
    - Script: `scripts/npu_backtest_lab.py`.
    - Use MLX `mx.vmap` to run 1,000 simulations in a single GPU pass.
    - Calibration: Find the optimal "Regime-based" rebalance frequency (5m, 15m, or 30m).

## Verification Plan
- **Latency Test**: Verify `vllm-mlx` handles 3 concurrent agent queries in <500ms.
- **Backtest Audit**: Ensure RL policy beats "Buy and Hold" for 3x ETPs over a 14-day window.
- **Memory Audit**: Confirm total resident memory stays <40GB during full swarm execution.
