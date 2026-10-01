# Phase 21: AI-Driven Dividend Optimization - Research

**Researched:** 2026-03-01
**Domain:** Quantitative Finance / Dividend Capture
**Confidence:** HIGH

## <user_constraints>
### Locked Decisions (from CONTEXT.md)
- **Decision Frequency**: Intra-week to Intra-month analysis.
- **Hybrid Order Strategy**: Phase A: Limit Orders (2-3 days prior); Phase B: Market Orders (2 hours before ex-dividend).
- **Multi-Model Consensus**: TTM-R2 (Granite), XGBoost, and Monte Carlo.
- **Abort Triggers**: Max Drawdown >5% more than dividend; Confidence <40%.
- **HITL**: Push notifications for manual approval before "panic" exits.

### Deferred Ideas (OUT OF SCOPE)
- Standalone Intraday Portfolio Management.
- Leverage-based dividend capture.
- Full autonomy mode (Zero HITL).
</user_constraints>

## Summary
Phase 21 focuses on transforming passive income into an active yield engine. The core innovation is the **Consensus-Bottleneck Asset Pricing Model (CB-APM)**, which synthesizes seasonal trends from IBM Granite TTM-R2 with technical momentum from XGBoost. Post-dividend price recovery is modeled via **Neural ODEs** to predict 'Recovery Velocity'.

## Standard Stack
- `ibm-granite-ttm`: TTM-R2.1 Forecasting.
- `xgboost`: Technical Validation.
- `torchdyn` / `PyTorch`: Neural ODEs for recovery modeling.
- `scipy.signal`: SEMPO EASD for spectral analysis.

## Technical Patterns
- **Consensus-Bottleneck Asset Pricing Model (CB-APM)**: Routes TTM-R2 and XGBoost signals through an interpretable bottleneck before prediction.
- **Low-Rank Neural ODEs**: Models price $dh/dt = f(h(t), t, 	heta)$ for continuous recovery time estimation.
- **SEMPO EASD**: Separates periodic dividend cycles from high-frequency market noise.
- **Smart DRIP**: Log-Weighted Carry logic across cross-asset universe.
- **Robust IQR Scaling**: $f_t = (f_t - 	ext{median}) / 	ext{IQR}$ for sparse data.

## Implementation Guardrails
1. **Dividend Trap Protection**: Hard exit if drawdown > (Dividend + 5%).
2. **Data Sparsity**: Fallback to XGBoost-only if TTM context < 512 points.
3. **HITL Command Center**: Translucent probability clouds and one-tap approval cards in SwiftUI.
