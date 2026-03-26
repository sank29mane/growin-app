# Context: Phase 22 - Leveraged Dividend Capture & Advanced Portfolio Simulation

## Phase Goal
Implement leveraged dividend capture logic, integrate Neural ODE for post-dividend price action recovery velocity, and build an advanced Monte Carlo + ML stress testing simulation engine.

## Key Deliverables
1. **Dividend Capture Engine (Leveraged)**: Targets ex-day premiums and execution alpha.
2. **Neural ODE Integration**: Continuous-time modeling of recovery velocity post-dividend.
3. **Hybrid MC-ML Simulation Framework**: Large-scale portfolio stress testing with ML tail-loss forecasting.
4. **Margin-Aware Yield Optimization**: CME Prisma-style portfolio margining for retail users.

## Decisions

### Locked Decisions
- **Institutional Execution**: Target 40bp execution gap using solver-based routing.
- **Hedging**: Mandatory Delta-Neutral overlays (covered calls) and Index Future Netting.
- **Neural ODE**: Use Adjoint Sensitivity Method for O(1) memory cost.
- **Simulation**: Agentic GPU acceleration for 1000x faster inference.
- **Margin**: Implement SA-CCR & EPE modeling for real-time risk buffers.

### Claude's Discretion
- Selection of specific Neural ODE library (e.g., `torchdiffeq` or custom implementation).
- Design of the `SimulationEngine` class structure within `quant_engine.py`.
- Specific implementation of the `MPoR` (Margin Period of Risk) segments.

## References
- `.planning/phases/22-leveraged-dividend/RESEARCH.md`
- `backend/dividend_bridge.py`
- `backend/quant_engine.py`
