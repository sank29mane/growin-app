# Research Phase 18: Adversarial Reasoning & Institutional Alpha

## 1. Adversarial Reasoning (The Debate Model)
### SOTA 2026 Implementation (via Growin Research Notebook)
- **Architecture**: Generator-Critic pattern managed by the Orchestrator.
- **Roles**:
    - **Generator**: Orchestrator synthesizes the initial thesis from the Specialist Swarm.
    - **Critic (Risk Agent)**: Instantiates adversarial personas (e.g., *The Contrarian*) to refute the thesis.
- **Logic Flow**:
    1. Initial Thesis generation.
    2. Risk Agent Audit (Critique).
    3. If FLAGGED: Orchestrator attempts one revision (The Debate).
    4. Second Critique.
    5. **ACE Score**: Adversarial Confidence Estimation based on thesis robustness against critiques.
- **Exit Condition**: Max 2 turns or ACE > 0.8.

## 2. Verified Institutional Intelligence
### Data Sources
- **Regulatory News (RNAs)**: Shift from Twitter/Reddit sentiment to official LSE/SEC regulatory news for institutional tracking.
- **Macro Sentiment**: Integrate Geopolitical Risk (GPR) and VIX as baseline tail-risk indicators in the Orchestrator's fabrication phase.

## 3. Alpha Analytics (DuckDB Persistence)
- **Goal**: Correlate `agent_telemetry` traces with historical OHLCV.
- **Implementation**:
    - Query `agent_telemetry` for `reasoning_started` timestamps and tickers.
    - Join with `ohlcv_history` to calculate forward 1-day and 5-day price change.
    - Store the "Alpha Result" back into an `agent_performance` table.

## 4. Institutional Whale Watch 2.0
- **Requirement**: Quarterly 13F Filings (SEC EDGAR).
- **Logic**: Use the `ResearchAgent` to periodically scrape or fetch 13F data for high-conviction holdings and inject into `WhaleAgent` context.
