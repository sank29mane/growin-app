# Growin Comprehensive Audit & Profit-Maximization Roadmap

**Date:** 2026-07-16  
**Scope:** Full codebase audit for semi-HFT intraday scalping (~10 orders/sec) under multi-agent portfolio management  
**Primary goal:** Extract maximum profit per trade via multi-agentic portfolio framework  
**Broker transition:** Trading 212 → ICICI Direct Breeze API (Indian market context)  
**Method:** Intelligent multi-domain routing (architecture, execution, quant/risk, MAS latency, broker constraints)

---

## 0. Executive Verdict

| Question | Answer |
|----------|--------|
| Is Growin a capable **portfolio intelligence / advisory MAS**? | **Yes** — strong local MLX stack, rich agent roster, Sovereign UI, precision work |
| Can it run **~10 orders/sec scalping** through the multi-agent LLM path? | **No** — decision path is multi-second (DecisionAgent alone documents 5–8s) |
| Can it approach 10 ops/sec on a **numerical hot path**? | **Partially** — `LiveTradingLoop` + GMM + pre-flight can process ticks fast; OMS/broker wiring incomplete |
| Does every trade optimize for maximum extractable profit? | **No** — risk *downscales* size; no EV − cost optimizer; ACE scores debate quality, not PnL |
| Is T212 suitable for the target? | **No for semi-HFT** — Invest/ISA API, intentional jitter 0.5–2s, not microstructure venue |
| Is ICICI Breeze a better fit for 10 ops/sec? | **Better aligned** — official **10 orders/sec** combined limit; **market orders banned**; static IP + SEBI algo rules apply |

**One-line strategy:** Split **Advisory MAS** (LLM, research, HITL, portfolio narrative) from **Execution Kernel** (numerical signals → size → risk → OMS). Agents design and govern strategy offline; the kernel extracts profit online at broker rate limits.

---

## 1. What Growin Actually Is Today

### 1.1 Product DNA (from code + planning)

| Layer | Reality |
|-------|---------|
| Vision docs | “Self-correcting trading weapon for **LSE Leveraged ETFs**” on Apple Silicon |
| Requirements v6 | Explicitly **Out of Scope:** “High-frequency tick updates… not HFT microsecond execution” |
| Data universe | LSE leveraged ETF CSVs (`data/etfs/*_5m.csv`), T212 MCP, Alpaca US bars |
| UI | macOS SwiftUI Sovereign Ledger — chat, ledger, HITL confirm |
| AI | Multi-agent swarm + MLX Gemma-class reasoning + ANE JMCE / GMM |
| Execution | T212 equity orders via MCP; Alpaca mostly **data**; requoter stubs toward Alpaca cancel |

### 1.2 Dual-stack architecture (critical insight)

```
┌─────────────────────────────────────────────────────────────┐
│ PATH A — Advisory MAS (product mainline)                    │
│ Chat/AI routes → OrchestratorAgent → specialists →          │
│ DecisionAgent (LLM 5–8s) → RiskAgent (LLM) → HITL/MCP       │
│ Latency: seconds–tens of seconds                            │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│ PATH B — Numerical live loop (scalable pieces)              │
│ Tick → online vol/spread → Welford → Numba GMM → adapter    │
│ hot-swap → RiskSwarmGate → PreFlightSimulator → broker coro │
│ Latency: sub-ms features; swap 10–50ms; NOT fully wired to  │
│ continuous alpha generator or complete OMS                  │
└─────────────────────────────────────────────────────────────┘
```

There is **no closed loop**: tick → multi-agent consensus → 10 orders/sec.

### 1.3 Agent topology (runtime)

| Component | Role | On hot path? |
|-----------|------|--------------|
| `OrchestratorAgent` | Intent + parallel specialists + synthesis | Yes for chat trades |
| `SwarmOrchestrator` | Experimental 2-stage LLM swarm | Parallel/legacy |
| `QuantAgent` | Indicators + ORB (~5–10ms, no LLM) | Useful if detached |
| `ForecastingAgent` | ML forecast (timeouts up to 30s) | Too slow for scalp |
| `ResearchAgent` / Social / Whale | News & flow | Noise for 100ms edge |
| `PortfolioAgent` | Holdings + qualitative LLM | Portfolio, not scalp |
| `GoalPlannerAgent` | Multi-year MPT | Orthogonal to scalping |
| `DecisionAgent` | Final plan + tool calls | **Bottleneck** |
| `RiskAgent` | LLM critic + wash-sale heuristics | Serial LLM after decision |
| `ACEEvaluator` | Debate robustness score | Not PnL |
| `GovernanceService` | Policy (incomplete agent coverage) | Partial |
| `LiveTradingLoop` | Regime + pre-flight + requoter hook | Best HFT substrate |

Evidence: `backend/agents/decision_agent.py` (“Performance: 5-8s”), `backend/trading_loop.py`, `docs/ARCHITECTURE.md`, `.planning/REQUIREMENTS.md`.

---

## 2. Goal Alignment: “Max Profit per Trade @ ~10 ops/sec”

### 2.1 Latency budget reality

| Target | Budget per cycle |
|--------|------------------|
| 10 orders/sec sustained | ≤ **100 ms** average signal→dispatch |
| Aggressive semi-HFT | Often **1–20 ms** signal; I/O dominates |
| Growin MAS decision | **~5–30+ seconds** typical |
| Growin numerical tick path | **Sub-ms–tens of ms** (features/GMM) |
| T212 intentional jitter | **+500–2000 ms** per order (`_apply_temporal_jitter`) |
| Breeze max order ops | **10/sec** (place+mod+cancel+square-off **combined**) |
| Breeze general API | **100 calls/min**, **5000/day** |
| FastAPI default limiter | **60 req/min**, burst 10 (`rate_limiter.py`) |

**Conclusion:** Even if signal gen is perfect, T212 jitter alone destroys 10 ops/sec. Breeze’s 10 ops/sec is the *ceiling*, and every cancel/modify burns the same budget.

### 2.2 Profit extraction vs current objectives

| Desired | Current | Gap |
|---------|---------|-----|
| Maximize E[PnL] − fees − impact − adverse selection | RiskSwarmGate multiplies size by regime; blocks spread >5% | No EV objective |
| Per-trade optimal size (Kelly / constrained) | Regime leverage coefficients | Risk-averse scaling only |
| Optimal entry/exit (queue, limit vs cross) | Market/limit via T212; requoter `poll()` is **pass** | Incomplete OMS |
| Adaptive exits (TP/SL/trail) | LLM narrative TP/SL | Not machine-enforced |
| Continuous learning from fills | Telemetry logs slippage bps | No closed-loop weight update |
| Multi-agent portfolio allocation | Goal planner years; TLH; rebalance chat | Not concurrent multi-name scalp allocator |

### 2.3 Explicit product contradiction

`.planning/REQUIREMENTS.md` **Out of Scope:**

> High-frequency tick updates (Targeting dynamic order book re-pricing on a sub-second scale but **not HFT microsecond execution**).

The user’s stated goal **redefines product scope**. Roadmap below assumes that redefinition is intentional and phases work accordingly.

---

## 3. Strengths (Assets to Keep)

1. **Hardware-aware inference** — MLX, ANE/CoreML JMCE, adapter hot-swap story (M4 Pro 48GB).
2. **Numerical regime path** — online vol/spread, Welford, Numba GMM (`features/*`, `coreml/fast_gmm.py`).
3. **Pre-flight simulation** — market impact model + drawdown telemetry (`simulation/engine.py`, `models.py`).
4. **QuantAgent / ORB** — pure algorithmic path; Opening Range Breakout with volume + covariance velocity.
5. **Financial precision culture** — Decimal math, price validation, audit logs, HITL gates.
6. **Rich test surface** — risk, slippage, portfolio, requoting stubs, simulation.
7. **DuckDB analytics + ETF 5m history** — good substrate for research (once India universe is added).
8. **Broker abstraction starting point** — MCP tools + shared sensitive tool list (`SENSITIVE_TOOLS`).

---

## 4. Flaw Inventory (Comprehensive)

Severity: **P0** blocks goal · **P1** major · **P2** important · **P3** hygiene

### 4.1 Architecture & latency (P0–P1)

| ID | Flaw | Severity | Evidence |
|----|------|----------|----------|
| A1 | LLM on critical path for trades | **P0** | Decision 5–8s; Risk LLM critic serial |
| A2 | Advisory stack ≠ execution stack; no bridge | **P0** | `trading_loop` not fed by ORB/quant continuously |
| A3 | Dual orchestrators / doc drift | **P2** | `OrchestratorAgent` vs `SwarmOrchestrator` |
| A4 | Stream vs non-stream specialist mismatch | **P1** | `run()` drops whale/social/goal vs stream |
| A5 | Fixed `asyncio.sleep(0.5)` on stream path | **P1** | `orchestrator_agent.py:612` |
| A6 | Forecast horizon hard-coded days=5 for intraday | **P1** | Orchestrator context |
| A7 | Cache TTLs 60–600s on agents | **P1** | Quant 60s, Research 600s |
| A8 | Global API rate limit 60/min | **P0** for 10/s UX | `rate_limiter.py` |
| A9 | Hardware guard serializes heavy inference | **P1** | Correct for RAM; wrong for 10Hz LLM |

### 4.2 Execution / OMS (P0–P1)

| ID | Flaw | Severity | Evidence |
|----|------|----------|----------|
| E1 | T212 temporal jitter 0.5–2s intentional | **P0** | `trading212_mcp_server.py` `_apply_temporal_jitter` |
| E2 | T212 API: Invest/ISA focus; not CFD HFT venue | **P0** | T212 public API limitations |
| E3 | AdaptiveReQuoter `poll()` is empty stub | **P0** | `simulation/requoter.py` |
| E4 | Requoter uses Alpaca cancel; live path mixed T212 | **P1** | Split brokers |
| E5 | No continuous order scheduler / token bucket for 10 ops | **P0** | Missing OMS |
| E6 | No smart cancel/replace, partial fill handling, OCO | **P1** | Partial T212 tools only |
| E7 | Market-order-first culture vs Breeze ban on market | **P0 for India** | Breeze SEBI rules |
| E8 | No fee model in decision sizing (brokerage, STT, GST, exchange) | **P0 for India** | UK-centric wash sale / ISA |
| E9 | Price validation 3% block too coarse for scalps | **P2** | `t212_handlers` market order |

### 4.3 Alpha / quant / data (P0–P2)

| ID | Flaw | Severity | Evidence |
|----|------|----------|----------|
| Q1 | ORB on 5m bars / 30m open range — session day-trade, not scalp | **P1** | `orb_detector.py` |
| Q2 | No L2 order book / queue position model in live feed | **P1** | Impact model needs depth; often missing → sqrt fallback |
| Q3 | No explicit expected-edge vs cost gate before order | **P0** | Pre-flight estimates fill, not EV |
| Q4 | Look-ahead risk in training/backtests (documented pitfall) | **P1** | `.planning/research/PITFALLS.md` |
| Q5 | Universe = LSE leveraged ETFs / US via Alpaca — not NSE/NFO | **P0 for India** | `data/etfs` |
| Q6 | Research/social on `intraday_trade` needs | **P1** | Adds latency + noise |
| Q7 | GMM features only vol+spread (2D) — thin microstructure | **P2** | `trading_loop.process_tick` |
| Q8 | Overlapping indicator implementations | **P3** | `docs/future/suggestions.MD` |
| Q9 | REQUIREMENTS checkboxes lag roadmap “complete” claims | **P2** | v6 REQ still ⬜ for ACC/EXEC |

### 4.4 Risk / portfolio / multi-agent PM (P0–P2)

| ID | Flaw | Severity | Evidence |
|----|------|----------|----------|
| R1 | RiskAgent is LLM critic, not hard real-time risk | **P0** for scalp | `risk_agent.py` |
| R2 | Wash sale / ISA logic US-UK; wrong for Indian STT/turnover tax | **P1** India |
| R3 | Always `requires_hitl` for trade words | **P1** | Blocks semi-auto HFT |
| R4 | ACE not calibrated to PnL | **P2** | `ace_evaluator.py` |
| R5 | GoalPlanner multi-year vs scalp capital envelope | **P2** | Wrong abstraction on path |
| R6 | No portfolio-level concurrent position netting for multi-ticker scalp | **P1** | Missing |
| R7 | RiskSwarmGate DB dependency hard-blocks if missing policy row | **P1** | `swarm_gate.py` |
| R8 | Incomplete Governance agent policies | **P2** | Missing Risk/Whale/etc. |
| R9 | Conviction-10 autonomous bypass of HITL | **P1** security/risk | `decision_agent` + docs |

### 4.5 Engineering / security / ops (P1–P3)

| ID | Flaw | Severity | Evidence |
|----|------|----------|----------|
| S1 | Duplicate CircuitBreaker modules | **P2** | v5 audit |
| S2 | DecisionAgent MCP without CB (partially addressed claims vary) | **P2** | future_suggestions |
| S3 | DuckDB thread-safety fragility | **P1** | CONCERNS.md |
| S4 | Broker keys in .env | **P2** | standard risk |
| S5 | No hardware-specific CI for ANE | **P3** | CONCERNS |
| S6 | Coordinator.orig / patch scripts / bak files | **P3** | repo hygiene |

---

## 5. Target Architecture (Recommended)

### 5.1 Bifurcation principle

```
                    ┌──────────────────────┐
                    │ Strategy Lab (async) │
                    │ MAS + LLM + Research │
                    │ backtest, QLoRA, UI  │
                    └──────────┬───────────┘
                               │ publishes StrategySpec
                               ▼
┌──────────────────────────────────────────────────────────────┐
│ EXECUTION KERNEL (≤100ms budget, no LLM)                     │
│  Market Data Bus → Feature Engine → Signal Ensemble          │
│       → EV/Cost Gate → Position Sizer → Hard Risk            │
│       → OMS (limit-only India) → Broker Adapter              │
│       → Fill Feedback → Online Calibration                   │
└──────────────────────────────────────────────────────────────┘
                    ▲
                    │ capital limits, kill switches, daily loss
         ┌──────────┴──────────┐
         │ Portfolio Governor  │  (multi-agent PM, slower loop)
         │ exposure, regime,   │
         │ session risk budget │
         └─────────────────────┘
```

### 5.2 Multi-agent portfolio management — correct roles

| Agent (logical) | Cadence | Function |
|-----------------|---------|----------|
| **Portfolio Governor** | 1–60s | Net exposure, sector/index beta, cash, margin |
| **Regime Agent** | tick/100ms | GMM/JMCE regime → risk multipliers |
| **Alpha Ensemble** | tick–1s | ORB, microstructure, short-horizon ML (no LLM) |
| **Execution Agent** | order event | Limit placement, re-quote, cancel, IOC |
| **Cost Agent** | pre-trade | Brokerage, STT, impact, opportunity cost |
| **Risk Sentinel** | always | Hard blocks: DD, position, order rate, IP/session |
| **LLM Strategist** | offline / EOD | Narrative review, feature research, adapter training |
| **Human** | HITL tiers | Live capital arming; not every 100ms order |

“Multi-agentic PM” for 10 ops/sec means **parallel specialized numerical agents**, not multi-LLM debate per order.

### 5.3 Profit objective (formal)

For each candidate trade \(i\):

\[
EV_i = p_{win} \cdot R_{win} - p_{loss} \cdot R_{loss} - Fees_i - E[Impact_i] - E[AdverseSelection_i]
\]

**Execute only if** \(EV_i > \tau\) (threshold) and risk gates pass.  
Size with constrained Kelly / CVaR budget from portfolio governor.

Wire `PreFlightSimulator` impact into this EV equation (today it only produces fill estimates).

---

## 6. Roadmap (Phased)

### Milestone M0 — Scope Freeze & Metrics (1 week)

- [ ] Formally adopt dual-path architecture in PROJECT.md / REQUIREMENTS.md
- [ ] Define SLOs: p50/p99 signal→order, ops/sec, fill quality bps, net PnL after costs
- [ ] Paper trading definition of done (no live capital until M3+)
- [ ] Kill list: remove LLM from order critical path as requirement

**Exit:** Written SLO + mode flags: `ADVISORY | PAPER_SCALP | LIVE_SCALP`

---

### Milestone M1 — Execution Kernel Skeleton (2–3 weeks)

| Workstream | Deliverables |
|------------|--------------|
| OMS core | Order state machine: NEW→WORKING→PARTIAL→FILLED/CANCELLED |
| Rate governor | Token bucket: **10 ops/sec global** (place+mod+cancel), per-symbol caps |
| Broker interface | `BrokerAdapter` ABC: place_limit, modify, cancel, positions, fills, quotes |
| T212 adapter | Existing MCP wrapped; **disable jitter in paper**; document live limits |
| Event bus | Tick/bar/fill events; LiveTradingLoop as consumer |
| Telemetry | p99 latencies, reject reasons, EV estimates vs realized |

**Exit:** Simulated tick source → numerical signal → paper orders at ≥5–10 ops/sec locally (no LLM).

---

### Milestone M2 — Alpha & EV Profit Loop (3–4 weeks)

| Workstream | Deliverables |
|------------|--------------|
| Signal ensemble | ORB (session), short-horizon momentum/mean-reversion, GMM regime filter |
| EV gate | Fees + impact + min edge threshold |
| Sizing | Regime leverage × portfolio risk budget × Kelly fraction |
| Exit engine | Time stop, hard SL/TP, trail, session flatten |
| Bridge | Quant/ORB → kernel (not DecisionAgent) |
| Offline MAS | LLM generates StrategySpec YAML validated by backtest |

**Exit:** Walk-forward paper PnL > costs on 1–3 liquid symbols; kill-switch tests.

---

### Milestone M3 — Broker India: Breeze Parallel Track (4–6 weeks, overlaps M1–M2)

See §7 for full transition plan.

**Exit:** Breeze paper/sandbox (or tiny live size) limit orders end-to-end; static IP; session auth; market-order path removed for India mode.

---

### Milestone M4 — Multi-Agent Portfolio Governor (3 weeks)

- Concurrent multi-ticker risk: max gross/net, correlation clustering, index beta
- Capital allocation across strategies (not multi-year MPT on hot path)
- Daily loss halt, consecutive loss halt, news blackout windows
- Replace LLM RiskAgent on hot path with deterministic RiskEngine + optional async critic

**Exit:** Portfolio-level simulation with 5–20 symbols without breach.

---

### Milestone M5 — Adaptive Execution Quality (3 weeks)

- Complete AdaptiveReQuoter (vol collars, cancel/replace under 10 ops budget)
- Breeze GTT / stoploss order types where available
- Partial fill handling; IOC vs DAY policies for scalps
- Fill quality learning → online impact model calibration

**Exit:** Slippage error distribution tracked; re-quote improves fill vs passive baseline.

---

### Milestone M6 — Research → Production Learning (ongoing)

- Chronological splits only (no look-ahead)
- Off-market QLoRA / JMCE recal (Phase 53)
- ACE replaced or dual-tracked with **PnL attribution**
- India cost models: STT, exchange, GST, SEBI turnover, brokerage slabs

**Exit:** Drift detection triggers retrain; live vs sim residual within tolerance.

---

### Milestone M7 — Live Capital Ramp (gated)

| Stage | Cap | Requirements |
|-------|-----|--------------|
| L0 Paper | 0 | 20 trading days positive expectancy after costs |
| L1 Micro | min lot | Manual arm; kill switch; IP registered |
| L2 Scale | % NAV | Max DD < X%; ops compliance |
| L3 Full | strategy budget | Audit trail + SEBI algo classification if required |

---

## 7. Broker Transition: Trading 212 → ICICI Breeze

### 7.1 Why transition (or dual-broker)

| Dimension | Trading 212 | ICICI Breeze |
|-----------|-------------|--------------|
| Order rate | Not designed for 10 ops/sec HFT | **Hard max 10 ops/sec** combined |
| Market orders | Supported on Invest API (evolving) | **Prohibited** via API (SEBI) |
| Instruments | Global stocks/ETFs (Invest/ISA) | **NSE equity + F&O**; BSE/MCX not on Breeze |
| Data | Portfolio + limited; yfinance/Alpaca used | Quotes, hist charts, websockets, ticks |
| Auth | API keys | OAuth session + checksum + **static IP** |
| API budget | Rate limited (vendor) | **100/min**, **5000/day** non-order |
| Jurisdiction | UK retail | India retail + algo rules |
| Growin code today | **Primary execution** | **Absent** |

Sources: Breeze official docs (api.icicidirect.com), T212 API limitations.

### 7.2 Hard constraints for Indian semi-HFT design

1. **Limit orders only** — rewrite all market-order paths for India mode.
2. **10 OPS budget** includes cancel and modify — re-quote thrashing will self-throttle.
3. **Static IP** registered with ICICI; weekly change limit.
4. **Unregistered algos:** single API key routing constraint.
5. **Algo classification:** strategies near 10 OPS may need registration — legal research required (SEBI circulars).
6. **Session tokens** expire; need re-login flow (checksum SHA256 of timestamp+payload+secret).
7. **No BSE/MCX** on Breeze currently — universe = NSE cash + NFO.
8. **Product types:** cash, futures, options, btst — map carefully.
9. **Square-off** endpoints for positions.
10. **GTT** available for conditional exits — prefer exchange-side vs software-only SL.

### 7.3 Recommended dual-broker architecture

```
                    BrokerRouter
                   /            \
          T212Adapter        BreezeAdapter
        (EU/UK Invest)      (India NSE/NFO)
                   \            /
                 Unified Portfolio View
              (currency: GBP vs INR silos)
```

- **Do not** merge cash across jurisdictions naively.
- Feature flag: `BROKER_MODE=t212|breeze|dual`
- Shared: OMS, risk, signals; specialized: symbol maps, fees, sessions, holidays.

### 7.4 Implementation plan (deep)

#### Phase B1 — Adapter & Auth (week 1–2)

- [ ] `backend/brokers/base.py` — abstract interface
- [ ] `backend/brokers/breeze_client.py` — wrap `breeze-connect` or raw REST
- [ ] Session manager: AppKey, Secret, SessionToken, checksum, clock skew <60s
- [ ] Static IP operational checklist
- [ ] Secrets: Keychain / env; never log checksum inputs
- [ ] Unit tests with recorded fixtures (no live keys in CI)

#### Phase B2 — Market Data India (week 2–3)

- [ ] Security master daily download (ICICI SecurityMaster.zip)
- [ ] Symbol map: NSE codes vs Growin tickers vs Yahoo `RELIANCE.NS`
- [ ] Websocket ticks + candle stream → same tick bus as LiveTradingLoop
- [ ] HistoricalCharts v1/v2 for backfill (1m/5m)
- [ ] IST session calendar (pre-open, open, close, muhurat, holidays)
- [ ] Replace Alpaca-primary assumptions for India mode

#### Phase B3 — Order Path Limit-Only (week 3–4)

- [ ] Map `place_market_order` → synthetic limit at aggressive offset **or hard fail** in India mode
- [ ] Implement place/modify/cancel with OPS governor
- [ ] Order notifications websocket for fills
- [ ] Square-off + end-of-day flatten
- [ ] GTT for SL/TP when supported
- [ ] Preview brokerage charges API before large sizes

#### Phase B4 — Cost & Risk Localization (week 4–5)

- [ ] Fee engine: brokerage plan + STT + exchange + GST + SEBI charges
- [ ] Margin calculator integration for F&O
- [ ] Remove wash-sale-as-primary; add Indian tax notes (consult CA — not software advice)
- [ ] Position limits, freeze quantity, circuit filters

#### Phase B5 — UI & HITL (week 5–6)

- [ ] SwiftUI: broker selector, INR ledger, session status, IP warning
- [ ] Arming switch for LIVE_SCALP
- [ ] Order blotter with OPS usage meter (X/10 per second)

#### Phase B6 — Decommission / Parallel (ongoing)

- [ ] Keep T212 for UK portfolio advisory if still used
- [ ] Feature-flag dead code paths; do not hard-delete until M3 stable
- [ ] Update MCP tool names to broker-agnostic (`place_limit_order` with venue)

### 7.5 Mapping Growin tools → Breeze

| Growin / T212 concept | Breeze equivalent |
|-----------------------|-------------------|
| `place_market_order` | **Unavailable** → limit/IOC strategies |
| `place_limit_order` | `OrderPlacement` order_type=limit |
| `place_stop_order` | order_type=stoploss (+ GTT) |
| `cancel_order` | OrderCancellation |
| `get_all_positions` | PortfolioPositions / DematHoldings |
| `get_account_cash` | GetFunds / GetMargins |
| Portfolio pies | N/A — remove or mock |
| Wash sale gate | Replace with India tax module |
| Alpaca L1 quotes | Breeze Quotes + tick stream |

### 7.6 Parallel vs replace decision matrix

| Strategy | Pros | Cons |
|----------|------|------|
| **Replace** T212 | Focus, simpler OMS | Lose UK holdings automation |
| **Parallel** (recommended) | Continuity; comparative research | Dual maintenance |
| **T212 advisory + Breeze execution** | Clean split | Two logins, currency silos |

**Recommendation:** Parallel adapters; default India execution to Breeze; keep T212 as portfolio data source if accounts remain active.

---

## 8. Deep Research Agenda (Every Aspect)

Use this as a multi-week research program. Each item: **question → method → acceptance**.

### R1. Market microstructure (India NSE/NFO)

| Question | Method | Done when |
|----------|--------|-----------|
| What is realistic edge after costs for liquid NSE names / index options? | Tick data study, cost model backtest | Net edge distribution documented |
| Best scalp horizon (1s / 5s / 1m / 5m)? | Horizon grid search | Horizon with highest EV after costs |
| Impact of circuit limits / auction periods | Event study | Kernel blackout rules |
| Options vs cash for 10 OPS strategies | Compare turnover, margin, STT | Written strategy selection |

### R2. Broker & regulatory

| Question | Method | Done when |
|----------|--------|-----------|
| When does SEBI require algo registration for 10 OPS? | Legal/primary circulars + broker FAQ | Compliance memo |
| Breeze tick quality vs paid vendors (TrueData, etc.) | Latency & gap comparison | Data vendor decision |
| Static IP / VPS co-location options | Infra research | Runbook |
| Session reliability & reconnect | Chaos tests | SLA metrics |

### R3. Alpha research

| Question | Method | Done when |
|----------|--------|-----------|
| Does ORB transfer to NSE open (9:15 IST)? | Rebuild ORB with India open | Validated or rejected |
| GMM vol-spread regimes on Nifty constituents | Retrain GMM | Regime stability report |
| JMCE/ANE models on India features | Retrain/export CoreML | Latency + IC metrics |
| Ensemble vs single signal | Ablation | Production ensemble config |
| Avoid look-ahead | Strict time splits | PITFALLS checklist enforced in CI |

### R4. Execution research

| Question | Method | Done when |
|----------|--------|-----------|
| Optimal re-quote interval under 10 OPS | Simulation of cancel tax | Policy table |
| Limit offset as function of vol | Grid + online learning | Collar function |
| IOC vs DAY for scalps | Fill rate vs adverse selection | Default policy |
| GTT vs software SL | Failure mode analysis | Primary exit method |

### R5. Multi-agent PM research

| Question | Method | Done when |
|----------|--------|-----------|
| What agent topology maximizes PnL not debate score? | Paper comparisons | Topology ADR |
| Portfolio governor cadence | Sim 100ms vs 1s vs 10s | Chosen cadence |
| Conflict resolution without LLM | Voting / veto / hierarchical | Spec implemented |
| When LLM adds value | Offline only A/B | Scope boundary ADR |

### R6. Risk & capital

| Question | Method | Done when |
|----------|--------|-----------|
| Optimal daily loss limit | Bootstrap historical | Config |
| Correlation shocks (index days) | Stress tests | Kill rules |
| Leverage on F&O | Margin calculator backtests | Max leverage table |

### R7. Systems & latency

| Question | Method | Done when |
|----------|--------|-----------|
| Python GIL vs Rust core for features | Benchmark | Hot path language decision |
| Event loop vs dedicated process for OMS | Architecture spike | ADR |
| p99 tick→decision on M4 Pro | Continuous profiling | Dashboard |
| DuckDB vs in-memory ring buffer for live | Benchmark | Live storage choice |

### R8. Product & UX

| Question | Method | Done when |
|----------|--------|-----------|
| What HITL is safe at 10 OPS? | Arming model design | UX flows |
| OPS meter & kill switch UX | Prototype | User test |
| Multi-currency portfolio display | Design | Spec |

### R9. Evaluation science

| Question | Method | Done when |
|----------|--------|-----------|
| Metrics beyond Sharpe (fill quality, capacity, tail) | Define scorecard | Adopted KPI set |
| Sim-to-live gap | Shadow mode | Residual < threshold |
| ACE vs PnL correlation | Historical labels | Keep/kill ACE on hot path |

---

## 9. Concrete Near-Term Engineering Backlog (Prioritized)

### P0 (do first)

1. Define `ExecutionKernel` module; ban LLM imports inside it.
2. Wire Quant/ORB/GMM → `execute_order_pre_flight` continuous paper loop.
3. Implement OMS rate governor (10 ops/sec).
4. Implement EV − cost gate using impact model + fee stub.
5. Complete AdaptiveReQuoter `poll()` or remove dead claim.
6. Remove/conditionalize T212 temporal jitter for paper/low-latency mode.
7. Start `BreezeAdapter` + limit-only path.
8. Separate FastAPI rate limits from trading OMS limits.

### P1

9. Fix orchestrator `run()` specialist parity; drop research/social from scalp needs.
10. Remove stream `sleep(0.5)` or gate it.
11. Intraday forecast horizons in bars.
12. Intent-based cache TTLs (scalp: 0–1s quant).
13. Deterministic RiskEngine hard gates on hot path.
14. Portfolio governor for multi-ticker.
15. India symbol master + IST calendar.
16. Fee engine India.
17. Unify broker cancel path (stop Alpaca-only requoter assumption).

### P2

18. Unify CircuitBreakers; complete Governance policies.
19. PnL-calibrated evaluation replacing ACE on live.
20. REQUIREMENTS.md sync with completed phases.
21. Shadow trading mode + sim residual monitoring.
22. Documentation rewrite: dual-path architecture as SoT.

### P3

23. Repo hygiene (.orig, .bak).
24. Stress tests for concurrent agents.
25. WASM sandbox long-term for SafePython.

---

## 10. KPI Scorecard (Adopt as North Star)

| KPI | Target (example) | Measures |
|-----|------------------|----------|
| Signal→order p99 | < 100 ms (kernel) | Latency |
| Sustained OPS | ≤ 9/s (leave headroom under 10) | Throughput |
| Net expectancy / trade | > 0 after all costs | Profit |
| Slippage error vs sim | < X bps median | Sim fidelity |
| Max daily drawdown | < Y% | Risk |
| Kill-switch engage time | < 1s | Safety |
| Fill rate (limit) | Track by regime | Execution |
| Capacity (₹ turnover) | Before edge dies | Scalability |
| LLM offline only | 100% of live orders | Architecture hygiene |

---

## 11. Risk Register (Project-Level)

| Risk | Impact | Mitigation |
|------|--------|------------|
| Chasing HFT with LLM swarm | Never hits latency | Bifurcate paths |
| SEBI/broker account ban | Capital freeze | Compliance research; OPS headroom; static IP |
| Overfitting India data | Live losses | Walk-forward, costs-first |
| 10 OPS cancel thrash | Zero fills | OPS-aware re-quote policy |
| Dual broker complexity | Bugs | Abstract adapter + integration tests |
| Leveraged ETF UK models on NSE | Wrong alpha | Full retrain |
| Static IP / home network | Outages | VPS with registered IP |
| Market order legacy code | Rejects / compliance | India mode hard-fail market |

---

## 12. Summary Recommendations

1. **Accept the product pivot:** Growin today is an **AI portfolio platform**; becoming a **profit-max semi-HFT system** requires a new **Execution Kernel**, not more agents on the decision path.
2. **Keep MAS for strategy R&D, governance narrative, and portfolio briefing** — not per-order consensus.
3. **Build profit as EV − costs**, enforced before every order; use existing impact simulator as a building block.
4. **Move execution to ICICI Breeze** for India with **limit-only**, **OPS governor**, **static IP**, and full cost model; keep T212 optional for UK advisory.
5. **Run the research agenda (R1–R9)** in parallel with M0–M3 so capital is not deployed on unvalidated edges.
6. **Gate live capital** with paper → micro → scale criteria; treat 10 orders/sec as a **scarce resource** (every cancel costs an order slot).

---

## 13. Appendix — Key File Index

| Area | Paths |
|------|-------|
| Live loop | `backend/trading_loop.py` |
| Requoter | `backend/simulation/requoter.py` |
| Pre-flight | `backend/simulation/engine.py`, `swarm_gate.py`, `models.py` |
| T212 | `backend/trading212_mcp_server.py`, `t212_handlers.py`, `mcp_client.py` |
| Agents | `backend/agents/*` especially `orchestrator_agent.py`, `decision_agent.py`, `risk_agent.py` |
| Quant | `backend/quant_engine.py`, `utils/orb_detector.py`, `features/*` |
| Risk math | `backend/utils/risk_engine.py` |
| Rate limit | `backend/rate_limiter.py` |
| Planning | `.planning/PROJECT.md`, `REQUIREMENTS.md`, `ROADMAP.md` |
| Docs | `docs/ARCHITECTURE.md`, `docs/MAS_Strategy.md` |

---

*Audit generated 2026-07-16 for Growin App. Not investment advice. Broker regulatory conclusions must be verified with ICICI Direct / SEBI primary sources before live trading.*
