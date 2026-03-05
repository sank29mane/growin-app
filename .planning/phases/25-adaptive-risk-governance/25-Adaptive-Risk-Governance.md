# Phase 25: Adaptive Risk Governance (Institutional Baseline)

## Goal
Transition the Growin App from basic portfolio concentration checks to an institutional-grade risk governance framework. This phase implements real-time liquidity constraints (ADV-based) and macro-economic "Risk-Off" triggers (VIX & Treasury Spreads) within the Adversarial Debate Loop.

## Requirements
- **RISK-INST-01 (Liquidity Constraints)**: Calculate 30-day Average Daily Volume (ADV) and flag orders > 1% of ADV as "High Market Impact."
- **RISK-INST-02 (Institutional Macro Triggers)**: Monitor VIX levels and 10Y-2Y Treasury Yield spreads.
- **RISK-INST-03 (Dynamic ACE Scoring)**: Automatically downgrade Adversarial Confidence Estimation (ACE) scores and trigger [ACTION_REQUIRED:TRADE_APPROVAL] when systemic risk thresholds are breached.

## Plan 25-01: Institutional Risk Infrastructure & Data Integration

### 1. Market Context Extension
- Update `MarketContext` in `backend/market_context.py` to include a new `RiskGovernanceData` model.
- Fields: `liquidity_ratio` (order_size / ADV), `vix_level`, `yield_spread_10y2y`, `systemic_risk_level` (NORMAL, ELEVATED, EXTREME).

### 2. Data Fabrication Enhancements
- Update `DataFabricator` in `backend/data_fabricator.py` to fetch Macro indicators.
- **VIX**: Fetch from yFinance (`^VIX`).
- **Treasury Yields**: Fetch `^TNX` (10Y) and `^IRX` (13W/3M) or similar benchmarks for spreads.
- **ADV Calculation**: Add `calculate_adv_30d` to `QuantEngine` to compute volume-weighted averages from 30 days of historical bars.

### 3. Risk Agent Logic Upgrade
- Modify `RiskAgent.analyze` in `backend/agents/risk_agent.py`.
- Implement logic to compare proposed order sizes (from Orchestrator's suggestion) against calculated ADV.
- Inject VIX and Yield Spread data into the "The Contrarian" persona's audit prompt.
- **Thresholds**: 
    - VIX > 30 -> FLAGGED (Systemic Volatility).
    - Yield Spread < 0 (Inversion) -> FLAGGED (Recessionary Risk).
    - Order > 1% ADV -> FLAGGED (Liquidity Constraint).

### 4. ACE Evaluator Integration
- Update `ACEEvaluator.calculate_score` in `backend/agents/ace_evaluator.py`.
- Apply a "Systemic Risk Multiplier" (e.g., *0.8 if VIX > 30) to the final robustness score.

## Verification Strategy
- **Unit Test**: `tests/backend/test_risk_governance.py` to verify ADV calculation accuracy.
- **Integration Test**: Mock high VIX levels and verify `RiskAgent` automatically flags previously "APPROVED" strategies.
- **End-to-End**: Trigger an adversarial debate and observe the ACE score downgrade in the Reasoning Trace UI.
