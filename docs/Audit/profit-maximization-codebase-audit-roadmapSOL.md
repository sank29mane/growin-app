# Growin: Profit-Maximization Trading System Audit and Roadmap

**Audit date:** 2026-07-10  
**Scope:** repository architecture, execution correctness, quantitative validity, multi-agent design, production controls, and Trading 212 to ICICI Direct Breeze transition.  
**Primary conclusion:** Growin is not ready for live automated trading. It is an AI research/advisory prototype with a direct broker trigger. The first objective must be capital safety and empirical validity; only then should latency and alpha optimization be scaled.

## 1. Executive Verdict

The stated goal, “extract the most profit from every trade,” is not a sound engineering objective. The implementable objective is:

> Maximize expected net risk-adjusted return after spread, slippage, fees, taxes, FX, latency, rejects, partial fills, and capacity, subject to hard exposure, drawdown, liquidity, regulatory, and operational constraints.

The current system fails that objective in five decisive ways:

1. The live approval path can mark a broker error as success and sends the wrong MCP argument (`action` versus `order_type`) ([ai_routes.py:57](/Users/sanketmane/Codes/Growin%20App/backend/routes/ai_routes.py:57), [trading212_mcp_server.py:560](/Users/sanketmane/Codes/Growin%20App/backend/trading212_mcp_server.py:560)).
2. Broker submission bypasses `LiveTradingLoop`, `PreFlightSimulator`, and `RiskSwarmGate` ([ai_routes.py:55](/Users/sanketmane/Codes/Growin%20App/backend/routes/ai_routes.py:55), [trading_loop.py:211](/Users/sanketmane/Codes/Growin%20App/backend/trading_loop.py:211)).
3. Proposal state is process-local and approval is not atomic or idempotent; duplicate orders and unknown submission outcomes are possible ([app_context.py:39](/Users/sanketmane/Codes/Growin%20App/backend/app_context.py:39), [ai_routes.py:35](/Users/sanketmane/Codes/Growin%20App/backend/routes/ai_routes.py:35)).
4. The Trading 212 adapter is configured for 4 requests/sec, then adds two independent 0.5–2 second sleeps, so it cannot approach 10 submissions/sec ([rate_limiter.py:22](/Users/sanketmane/Codes/Growin%20App/backend/utils/rate_limiter.py:22), [trading212_mcp_server.py:293](/Users/sanketmane/Codes/Growin%20App/backend/trading212_mcp_server.py:293)).
5. Backtest/training claims are not reliable because of random splits, wrapped labels, backward filling, current-bar leakage, unrealistic fills, and missing costs ([scripts/prepare_training_data.py:66](/Users/sanketmane/Codes/Growin%20App/scripts/prepare_training_data.py:66), [backend/backtest_lab/run_comparison.py:27](/Users/sanketmane/Codes/Growin%20App/backend/backtest_lab/run_comparison.py:27)).

**Go-live status: BLOCKED.** Keep live execution disabled until the P0 controls and release gates in this report pass.

## 2. Current System Shape

The actual execution flow is:

`SwiftUI -> FastAPI/SSE -> OrchestratorAgent -> specialist agents -> DecisionAgent -> in-memory proposal -> approval route -> MCP -> Trading212Client -> broker REST API`.

The intended risk/simulation flow is separate:

`tick -> LiveTradingLoop -> pre-flight simulator -> RiskSwarmGate -> broker dispatch -> telemetry`.

The second flow is not connected to the first. Therefore the system’s advertised “simulation-in-the-loop” and adaptive execution controls are not live controls.

The repository also contains significant milestone drift. The roadmap marks adaptive re-quoting complete, but `AdaptiveReQuoter.poll()` is `pass` ([requoter.py:50](/Users/sanketmane/Codes/Growin%20App/backend/simulation/requoter.py:50)) and all four associated tests are empty ([test_requoting.py:4](/Users/sanketmane/Codes/Growin%20App/tests/backend/test_requoting.py:4)). `backend/main.py` is a hello-world entrypoint while `server.py` is the actual application entrypoint.

## 3. Severity-Ranked Findings

### P0: Must fix before paper/live order submission

**Broken execution contract and false success.** The approval route calls `place_market_order` with `action`; the MCP schema/handler requires `order_type`. MCP errors are returned as text content and the route marks the proposal approved without parsing the result ([ai_routes.py:55](/Users/sanketmane/Codes/Growin%20App/backend/routes/ai_routes.py:55), [trading212_mcp_server.py:1209](/Users/sanketmane/Codes/Growin%20App/backend/trading212_mcp_server.py:1209)). Replace stringly typed tool responses with a typed `OrderAck` and fail closed on any non-success status.

**Unauthenticated trade endpoint.** `/api/ai/trade/approve` has no authentication, authorization, request signature, replay protection, or route rate limit. The optional `TradeApprovalRequest.signature` is unused ([schemas.py:114](/Users/sanketmane/Codes/Growin%20App/backend/schemas.py:114)). CORS does not protect an HTTP endpoint. Docker exposes the service broadly ([docker-compose.yml:11](/Users/sanketmane/Codes/Growin%20App/docker-compose.yml:11)). Add authenticated principals, RBAC, single-use approval tokens, and an emergency disable control.

**Agent capability violation.** A “high conviction” model flag can directly execute an MCP tool ([decision_agent.py:697](/Users/sanketmane/Codes/Growin%20App/backend/agents/decision_agent.py:697)). Agents must never hold broker capabilities. They may emit a signed, immutable `TradeIntent`; only the execution service may submit it.

**No atomic order lifecycle.** The route checks `PENDING`, awaits an external call, and then mutates memory. Concurrent approvals can submit twice. A broker-accepted request followed by a timeout is marked failed even though the real state is unknown. Add durable records and transitions:

`INTENT_CREATED -> RISK_APPROVED -> RESERVED -> CLAIMED -> SUBMITTED -> ACKNOWLEDGED -> PARTIALLY_FILLED -> FILLED`, with terminal `REJECTED`, `CANCELLED`, `EXPIRED`, and `UNKNOWN` states.

Every intent needs a unique `client_order_id`, broker/account identity, canonical request hash, sequence number, timestamps, actor, risk decision, broker IDs, and immutable event history.

**No enforced portfolio risk.** No live gate enforces buying power, holdings, shortability, gross/net exposure, per-symbol notional, concentration, daily loss, max open orders, order rate, market hours, stale quotes, price collars, or session health. The existing gate only checks spread and a database multiplier ([swarm_gate.py:31](/Users/sanketmane/Codes/Growin%20App/backend/simulation/swarm_gate.py:31)). Drawdown triggers re-optimization and then execution continues ([trading_loop.py:286](/Users/sanketmane/Codes/Growin%20App/backend/trading_loop.py:286)).

**Secrets and approval secret.** Broker credentials are stored in local `.env`, submitted through API routes, and persisted as JSON in SQLite ([mcp_routes.py:76](/Users/sanketmane/Codes/Growin%20App/backend/routes/mcp_routes.py:76)). A hard-coded approval secret fallback exists ([mcp_routes.py:128](/Users/sanketmane/Codes/Growin%20App/backend/routes/mcp_routes.py:128)). Rotate exposed credentials, use Keychain/secret management, prohibit fallback secrets, and scan history/CI.

### P1: Must fix before meaningful performance claims

**Unrealistic and incomplete execution simulation.** Missing depth uses a quantity-only square-root formula; quantity beyond visible depth receives a fixed 5% penalty; all orders fill completely ([models.py:30](/Users/sanketmane/Codes/Growin%20App/backend/simulation/models.py:30)). There are no commissions, exchange fees, taxes, FX, borrow, queue position, latency drift, rejects, price limits, or partial fills. Tests generate actual fills from the same model, which is circular ([test_simulator.py:135](/Users/sanketmane/Codes/Growin%20App/tests/backend/test_simulator.py:135)).

**Look-ahead and dataset leakage.** Training uses random splits around overlapping forward labels, terminal missing labels become HOLD, `np.roll` wraps targets, and benchmark indicators are backward-filled. Replace with point-in-time data, explicit as-of timestamps, purged/embargoed chronological walk-forward splits, symbol/date isolation, and untouched final periods.

**Wrong or stale market data.** 5-minute and 15-minute requests use 1-minute bars while retaining the requested label ([data_engine.py:328](/Users/sanketmane/Codes/Growin%20App/backend/data_engine.py:328)). The chart route polls at 10 seconds and does not provide the depth contract required by pre-flight simulation ([chart_routes.py:304](/Users/sanketmane/Codes/Growin%20App/backend/chart_routes.py:304)). Introduce a canonical quote event with exchange time, receive time, source, bid/ask/depth, sequence, completeness, session, and maximum age.

**Accounting is not transaction-derived.** Portfolio history applies current holdings to historical prices; local updates omit correct cost basis, realized P&L, and costs ([market_routes.py:286](/Users/sanketmane/Codes/Growin%20App/backend/routes/market_routes.py:286), [portfolio_agent.py:249](/Users/sanketmane/Codes/Growin%20App/backend/agents/portfolio_agent.py:249)). Build a fill-level, double-entry ledger for cash, positions, fees, FX, realized/unrealized P&L, dividends, deposits, and corporate actions.

**Unimplemented emergency control.** Re-quoting is empty, and the current kill switch cancels only an in-memory set of Alpaca orders ([requoter.py:27](/Users/sanketmane/Codes/Growin%20App/backend/simulation/requoter.py:27)). A kill switch must be independent, durable, fail closed, broker-aware, restart-safe, and able to prevent new submissions before cancelling all visible open orders.

**Unbounded multi-agent runtime.** Each request constructs specialists, global subscriber state is mutated, and fan-out lacks a strict concurrency budget and execution deadline ([orchestrator_agent.py:45](/Users/sanketmane/Codes/Growin%20App/backend/agents/orchestrator_agent.py:45), [orchestrator_agent.py:331](/Users/sanketmane/Codes/Growin%20App/backend/agents/orchestrator_agent.py:331)). LLM work must be off the order path. Use agents for research, ranking, and parameter proposals; use deterministic code for admission and order management.

### P2: Quality, maintainability, and operations

- T212 is coupled into account types, symbols, credentials, UI, routes, portfolio schemas, and MCP routing; no broker-neutral protocol exists.
- MCP routing tries sessions rather than using an authoritative broker/tool registry ([mcp_client.py:142](/Users/sanketmane/Codes/Growin%20App/backend/mcp_client.py:142)).
- SQLite/DuckDB writes occur synchronously on async paths; relative paths differ between launch scripts and Docker persistence is incomplete.
- `/health` checks object existence rather than dependency readiness ([server.py:247](/Users/sanketmane/Codes/Growin%20App/backend/server.py:247)).
- CI runs tests but does not enforce coverage, type/lint checks, Docker build, image scanning, or trading-specific load/chaos gates ([.github/workflows/ci.yml:36](/Users/sanketmane/Codes/Growin%20App/.github/workflows/ci.yml:36)).
- Swift services duplicate HTTP logic, hard-code backend configuration, and present optimistic rather than reconciled order status.

## 4. Target Architecture

Separate the platform into five planes:

1. **Research plane:** historical data, feature computation, training, walk-forward evaluation, experiment registry, model approval.
2. **Decision plane:** agents produce typed `TradeIntent` proposals with evidence, expected return distribution, horizon, confidence calibration, and expiry. No broker credentials or tools.
3. **Risk/admission plane:** deterministic, synchronous checks against fresh market state and reserved portfolio state. This is the only path allowed to authorize an order.
4. **Execution plane:** per-account single writer, broker adapters, request budgets, idempotency, order state machine, retries only when safe, and reconciliation.
5. **Operations plane:** kill switch, audit/event log, metrics, alerts, deployment, secrets, and human controls.

Minimum interfaces:

```text
BrokerAdapter
  capabilities() -> BrokerCapabilities
  submit(OrderRequest) -> OrderAck
  modify(OrderId, OrderRequest) -> OrderAck
  cancel(OrderId) -> CancelAck
  get_order(OrderId) -> BrokerOrder
  list_open_orders() -> list[BrokerOrder]
  list_fills(cursor) -> list[Fill]
  get_positions() -> list[Position]
  get_balances() -> Balances

MarketDataAdapter
  subscribe(InstrumentId, depth_level) -> QuoteStream
  snapshot(InstrumentId) -> QuoteSnapshot

Canonical models
  InstrumentId, AccountId, OrderIntent, OrderRequest, OrderAck,
  BrokerOrder, Fill, Position, Balance, RiskDecision, BrokerError
```

Use an immutable event store plus transactional projections. Redis may coordinate leases and queues; it must not be the source of truth for orders or fills.

## 5. Roadmap

### Phase 0: Containment and evidence baseline

- Force `PAPER/SHADOW` mode by default and remove all direct MCP calls from agents/routes.
- Rotate credentials and approval secrets; block startup when live secrets or a static IP/production policy are missing.
- Correct the MCP contract, typed error handling, and approval authentication.
- Inventory every broker call, data source, model, fallback, and source-of-truth table.
- Produce a baseline report: current test pass rate, latency distribution, data freshness, simulated net P&L, and known gaps.

**Exit:** no live order can bypass the execution service; direct unauthenticated approval returns 401/403; broker errors cannot be marked success.

### Phase 1: Durable execution and risk kernel

- Add order intents/events, atomic claims, idempotency, reservations, and restart recovery.
- Implement pre-trade checks for buying power, holdings/shortability, exposure, concentration, daily loss, drawdown, stale data, spread, price collar, market session, order count, and broker capability.
- Implement a durable kill switch and reconciliation worker.
- Add contract tests through the real MCP/adapter boundary, duplicate-approval tests, timeout-after-acceptance tests, crash recovery, partial-fill, reject, and cancel tests.

**Exit:** 100 concurrent approvals for one intent produce one broker submission; every ambiguous outcome becomes `UNKNOWN` and is reconciled before new risk is released.

### Phase 2: Broker-neutral adapter boundary

- Define canonical models and capabilities.
- Put T212 behind `Trading212Adapter`; remove T212-specific terms from core domain models and UI.
- Add per-broker request budgets from response headers and documented limits; remove artificial “human jitter.”
- Add broker-aware instrument, session, order-type, error, and reconciliation mappings.

**Exit:** the same canonical order can be validated and rendered for T212 and a fake broker without conditional logic in risk code.

### Phase 3: Research and backtest rebuild

- Rebuild data ingestion with point-in-time snapshots and data-quality checks.
- Rebuild event-driven simulation: completed-bar signal time, next eligible quote, latency, spread, impact, queue/participation, partial fills, cancels, rejects, fees, taxes, FX, borrow, price bands, and session rules.
- Use purged walk-forward splits and a final untouched holdout.
- Measure net expectancy, hit rate, payoff, turnover, capacity, drawdown, tail loss, calibration, implementation shortfall, and regime-specific performance.

**Exit:** no model is promoted on aggregate accuracy alone; it must beat a cost-aware benchmark out of sample and survive parameter perturbation, regime, and capacity tests.

### Phase 4: Market-data and latency engineering

- Use broker-native or approved streaming quotes for execution; keep UI polling out of the order path.
- Record exchange and receive timestamps and measure tick-to-decision, decision-to-submit, submit-to-ack, and ack-to-fill.
- Use bounded queues, backpressure, per-account sequencing, and isolated inference workers.
- Benchmark admission, not just model inference. The goal is bounded p99 latency and zero duplicate/unknown leakage, not a headline sub-millisecond model call.

**Exit:** a 30-minute soak at the broker’s approved order budget has zero duplicates, zero lost events, bounded queue age, and complete reconciliation.

### Phase 5: Alpha and portfolio optimization

- Replace prose-derived sizing with calibrated return distributions and deterministic sizing.
- Optimize after-cost expected utility subject to risk and capacity constraints.
- Add ensemble diversity, regime stability, decay monitoring, online TCA, and model/version rollback.
- Use agents to challenge assumptions and search hypotheses; accept a strategy only through the research promotion gate.

**Exit:** a strategy has stable out-of-sample net performance, known capacity, calibrated probabilities, and a measured edge after all costs.

### Phase 6: Controlled paper, shadow, and canary operation

- Run paper mode, then shadow mode where the inactive broker receives rendered requests without submission.
- Canary one account and a small allow-list with low notional, explicit limit orders, strict session gates, and end-of-day reconciliation.
- Require multiple independent sessions with zero unexplained order/fill/position/cash discrepancies before increasing allocation.

**Exit:** risk owner signs the release checklist; rollback leaves T212 read-only and disables new orders immediately.

## 6. Trading 212 to ICICI Direct Breeze Transition

### Broker facts that change the design

The official Breeze documentation currently states: 100 API calls/minute and 5,000/day; NSE equity, equity futures, and equity options are supported; BSE and MCX are not currently available; orders require a registered static IP; a combined maximum of 10 order operations/sec includes placement, cancellation, modification, and square-off; and market orders are not permitted. See the [Breeze API reference](https://api.icicidirect.com/breezeapi/documents/index.html) and [official Python SDK](https://github.com/Idirect-Tech/Breeze-Python-SDK).

The 10 OPS figure is a ceiling, not a target. It is shared by actions and applied by the broker’s clock. At 10 OPS, the 100 calls/minute budget is exhausted quickly; polling, re-quotes, and reconciliation must be budgeted separately. Start with a materially lower operating rate and only increase after broker approval and measured capacity.

India’s retail algo framework also matters. NSE’s implementation standards require static IP handling, daily API logout, RMS checks, and initially define a 10 OPS threshold; above that, client algos require exchange registration. See [NSE circular NSE/INVG/67858](https://nsearchives.nseindia.com/content/circulars/INVG67858.pdf), [NSE FAQ](https://nsearchives.nseindia.com/web/sites/default/files/inline-files/FAQ_Retail%20Algo_03112025_NSE.pdf), and [SEBI timeline circular](https://www.sebi.gov.in/sebi_data/attachdocs/sep-2025/1759232056254.pdf). Obtain written ICICI/API and compliance confirmation before production; this report is not legal advice.

### Required adapter work

1. **Credentials/session:** store AppKey/secret in Keychain or a secret manager; implement OAuth login, CustomerDetails session generation, expiry state, daily logout, clock-skew checks, re-authentication, and feed reconnect. Breeze requires signed requests with timestamp/checksum/session token.
2. **Static network identity:** deploy the execution service on a fixed primary/secondary static IP; monitor egress identity and treat IP changes as a hard stop.
3. **Instrument master:** ingest the official master/security file; map display symbol to exchange, segment, expiry, strike, right, product, tick size, lot size, freeze quantity, and broker stock token. Never submit a display ticker as an execution identifier.
4. **Canonical order mapping:** represent side separately from order type; support NSE cash/F&O fields, limit/stop-loss, quantity/lot rules, validity, disclosed quantity, expiry, strike, and option right. Reject unsupported Margin/Option Plus operations explicitly.
5. **Market data:** use Breeze WebSocket feeds for quote/depth where permitted; retain exchange timestamps and sequence/freshness checks. Do not use yfinance as an execution feed.
6. **Rate budget:** one shared account budget for placement, modification, cancellation, square-off, reconciliation, and feed/session requests. Admission must reserve budget before submitting.
7. **Reconciliation:** use order list, order detail, trade list, positions, holdings, funds, and cursor checkpoints. Reconcile after every ambiguous response and at start/end of day.

### Transition sequence

`T212 read/write -> T212 adapter -> canonical execution service -> Breeze adapter`

- **Read-only parallel:** compare balances, positions, instruments, quotes, and history; record normalization discrepancies.
- **Shadow:** render the same canonical intent for both adapters; submit only to T212; compare order legality, prices, quantities, and expected costs.
- **Breeze paper/certification:** use approved test facilities and a static-IP deployment; exercise session expiry, rate limits, rejects, partial fills, cancel/modify, and daily logout.
- **Canary:** enable only NSE cash or one tightly defined segment, allow-listed instruments, low notional, limit orders, one writer, and end-of-day reconciliation.
- **Progressive cutover:** expand only after repeated sessions with zero unexplained discrepancies. Keep T212 read-only as rollback until Breeze stability is proven.

## 7. Deep-Research Program

Each workstream needs a written artifact, dataset/code evidence, and a decision owner.

| Workstream | Questions to answer | Required output |
|---|---|---|
| Market microstructure | What spread, depth, queue, impact, tick, lot, price-band, and liquidity conditions exist for the target universe? | Instrument liquidity/capacity model |
| Broker/API | What are exact Breeze limits, order semantics, outages, session rules, rejected fields, and certification requirements? | Versioned broker capability matrix |
| Regulation | Which entity, client, algo, API, hosting, tagging, registration, audit, and retention obligations apply? | Jurisdiction/compliance sign-off |
| Data quality | Are timestamps, corporate actions, survivorship, adjustments, gaps, duplicates, and vendor provenance correct? | Point-in-time data contract and QA report |
| Signal research | Which hypotheses have causal/temporal justification and survive multiple regimes? | Research cards with pre-registered tests |
| Backtest validity | Does an event-driven simulator reproduce realistic order and fill behavior? | Simulator validation and sensitivity report |
| Portfolio/risk | What sizing, drawdown, tail, concentration, correlation, and capacity limits preserve survival? | Risk policy and limit configuration |
| TCA | What implementation shortfall, fill ratio, adverse selection, latency, and cost decomposition occur? | Rolling TCA dashboard |
| Agent evaluation | Do agents improve decisions versus deterministic baselines without increasing risk or latency? | Ablation and calibration report |
| Reliability/security | Can the system recover from restart, network partition, stale data, broker timeout, bad model, and credential expiry? | Chaos, threat-model, and recovery report |
| Operations | Can an operator observe, stop, reconcile, and roll back every account? | Runbook, alert matrix, and incident drills |

Use primary sources for broker/regulatory facts, signed broker responses for operational facts, and reproducible local experiments for performance claims. Mark every result as `observed`, `simulated`, `inferred`, or `unverified`.

## 8. Release Gates and Metrics

No live release until all are true:

- No unauthenticated or agent-held broker capability.
- Zero duplicate orders in concurrency and restart tests.
- Zero unexplained order/fill/position/cash discrepancies after reconciliation.
- All order submissions pass deterministic risk, freshness, market-session, capability, and budget gates.
- Kill switch blocks new submissions and cancels broker-visible orders after restart.
- Walk-forward, untouched holdout, costs, and capacity tests are reproducible.
- Paper/shadow TCA is within approved implementation-shortfall limits.
- p50/p95/p99 timing is measured from market event to final broker ack; 10 OPS is tested as an upper constraint, not assumed as profitable capacity.
- Secrets are rotated, stored securely, and absent from logs, source, images, and persisted configuration.
- CI includes contract, type, lint, security, Docker, load, chaos, and trading-invariant tests.

Minimum dashboards: quote age, feed gaps, decision latency, admission latency, broker latency, request budget, queue age, open orders, unknown orders, fill ratio, partial fills, slippage, implementation shortfall, gross/net exposure, drawdown, daily loss, realized/unrealized P&L, reconciliation lag, model version, regime, and kill-switch state.

## 9. Assumptions and Decisions Needed

This roadmap assumes the initial Indian scope is NSE cash equities, with F&O added only after the cash path is proven; the desired autonomy is guarded autonomy; and the objective is net risk-adjusted return. If the intended scope is global multi-asset or unrestricted autonomy, the broker, compliance, data, and risk plans become materially larger.

The current repository should be treated as an evidence source, not as proof that completed roadmap phases are production-complete. The next implementation milestone should be Phase 0 containment, followed by the durable execution/risk kernel. Alpha expansion, model fine-tuning, and agent proliferation should wait until those gates pass.

## 10. Verification Snapshot

- Targeted tests passed: 18 tests covering simulator, placeholder re-quoting tests, HITL routes, T212 MCP, slippage, and risk modules.
- Full backend suite via the project’s intended command, `uv run --project backend pytest -q tests/backend --disable-warnings`, completed with 250 passed, 6 skipped, and 22 warnings in 147.68 seconds. Direct invocation through the repository root `.venv` was invalid because it is not the project’s configured environment.
- Static review found empty tests and unimplemented production paths; the green suite therefore does not establish trading safety.
