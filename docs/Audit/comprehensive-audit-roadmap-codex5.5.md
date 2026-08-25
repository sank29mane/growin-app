# Growin Comprehensive Audit Report & Roadmap

## Executive Summary

Growin's main goal is to maximize intraday profit extraction using a multi-agent portfolio-management framework with semi-high-frequency execution around 10 order actions/sec. The current codebase has many strong parts: SwiftUI command UI, FastAPI backend, specialist agents, MLX/CoreML experiments, pre-flight simulation modules, Trading 212 MCP, telemetry, and tests.

The core problem is integration maturity. The system currently behaves more like an advanced AI trading assistant than a production execution engine. Several safety, execution, broker, model, and validation components exist, but the live order path can bypass them.

The roadmap should therefore focus on one outcome: a deterministic, broker-safe, audited execution system where agents generate signals, but only a validated execution engine can route orders.

Key external constraint: ICICI Breeze is not a drop-in Trading 212 replacement. Breeze's official docs state 100 API calls/min, 5000/day, 10 combined order actions/sec, static IP requirements, and no market orders.

Sources checked:
- ICICI Breeze API docs: https://api.icicidirect.com/breezeapi/documents/index.html
- Official Breeze Python SDK: https://github.com/Idirect-Tech/Breeze-Python-SDK

## Critical Audit Findings

### 1. Execution Path Is Fragmented

Current issue:
- `LiveTradingLoop.execute_order_pre_flight()` exists, but the actual HITL trade approval route can call the broker directly.
- Pre-flight simulation, spread gates, regime scaling, slippage telemetry, and re-optimization are not mandatory.
- `AdaptiveReQuoter.poll()` is a stub.
- Some tests for re-quoting are empty pass-through tests.

Impact:
- The app cannot safely claim 10 order actions/sec execution readiness.
- Profit extraction is vulnerable to stale quotes, bad fills, direct market orders, missing kill-switch behavior, and unmeasured slippage.

Roadmap:
1. Create one `OrderExecutionService`.
2. Route every trade through it.
3. Remove all direct broker-dispatch shortcuts.
4. Implement re-quoting as a real state machine.
5. Add order lifecycle telemetry: proposal, quote, risk decision, broker request, ack, fill, cancel, reject, slippage.

Acceptance:
- Zero broker orders execute without an execution trace.
- p95 pre-flight decision latency under 100ms in synthetic load.
- Kill switch cancels all active orders and blocks new ones.

### 2. Broker Layer Is Trading 212/Alpaca-Coupled

Current issue:
- Trading 212 client is concrete, not an adapter.
- Routes, config, UI, account model, and MCP resources assume T212 concepts like `invest`, `isa`, pies, T212 tickers.
- Alpaca is assumed as primary market data for many non-UK flows.
- No broker-neutral order/account/instrument model exists.

Impact:
- Breeze migration will become brittle if added directly into T212 code.
- Indian-market semantics will leak everywhere.
- Testing each broker independently will be hard.

Roadmap:
1. Add broker-neutral models:
   - `BrokerProvider`
   - `BrokerAccount`
   - `BrokerPosition`
   - `InstrumentId`
   - `BrokerQuote`
   - `OrderRequest`
   - `OrderResult`
   - `BrokerCapabilities`
2. Add `BrokerClient` protocol.
3. Wrap T212 as `Trading212BrokerAdapter`.
4. Add `BreezeBrokerAdapter` separately.
5. Split trading from market data using `MarketDataProvider`.
6. Replace `/mcp/trading212/config` with generic `/broker/config`, `/broker/active`, `/broker/accounts`.

Acceptance:
- T212 and Breeze pass the same broker contract tests.
- UI can switch broker/account without changing trading logic.
- Goal execution becomes broker-neutral portfolio intent, not T212 pie logic.

### 3. Breeze / Indian Market Transition Needs Its Own Layer

Current issue:
- Existing symbol normalization is US/UK/T212 oriented.
- Existing currency assumptions include USD, GBP, GBX.
- Indian trading requires NSE/BSE identity, INR, market sessions, order/product types, static IP, token/session handling, and rate budgets.
- Breeze docs say market orders are not permitted and combined placement/cancel/modify/square-off is capped at 10/sec.

Roadmap:
1. Add India instrument model:
   - exchange: NSE/BSE
   - stock code
   - ISIN
   - token
   - segment
   - series
   - expiry/strike/right for F&O later
2. Add Indian market calendar:
   - `Asia/Kolkata`
   - NSE holidays
   - pre-open/normal/close sessions
3. Add Breeze session/auth service.
4. Add Breeze websocket market-data ingestion.
5. Add Breeze order adapter:
   - limit orders only
   - modify/cancel support
   - order notifications
   - strict request budget
6. Add India-specific validation:
   - tick size
   - lot size
   - product type
   - circuit limits
   - market status
   - margin/funds
   - stale quote block

Acceptance:
- Breeze order cannot be built without exchange, stock code, product type, quantity, price, validity, and risk collar.
- Static-IP/session-token failures fail closed.
- Rate limiter enforces both 100 calls/min and 10 order actions/sec.

### 4. Agent System Is Not Yet Execution-Grade

Current issue:
- Multiple orchestration paths exist: `OrchestratorAgent`, `CoordinatorAgent`, `SwarmOrchestrator`.
- Docs and runtime behavior do not fully match.
- LLM text can be converted into trade proposals using heuristics.
- A response containing buy/sell terms can become a proposal with weak defaults.
- Agents are too slow/unbounded for a 100ms execution loop.

Impact:
- Agents are useful for analysis, but unsafe as direct order initiators.
- Execution must not depend on long LLM calls.

Roadmap:
1. Unify orchestration paths or clearly retire older paths.
2. Make agents produce structured signals only.
3. Replace text extraction with schema-first `TradeProposal`.
4. Require every proposal to include:
   - ticker/instrument
   - broker/account
   - side
   - quantity/notional
   - order type
   - limit/collar
   - expected alpha
   - confidence
   - horizon
   - stop/exit condition
   - risk caps
   - source agents
5. Keep agents outside the hot execution lane.
6. Hot lane should use deterministic features, cached model scores, broker state, and hard risk gates.

Acceptance:
- Incomplete proposals are rejected.
- No default quantity fallback.
- Same query produces same specialist set and risk review across streaming/non-streaming paths in deterministic tests.

### 5. Risk Controls Are Too Narrow

Current issue:
- `RiskSwarmGate` mainly checks spread and regime multiplier.
- It does not fully enforce buying power, max notional, position exposure, daily loss, order duplication, stale quote, open order cap, cancel/replace cap, or kill-switch state.
- Risk metrics exist but are not always execution gates.

Roadmap:
1. Add deterministic order-admission gates:
   - max notional/order
   - max symbol exposure
   - max gross/net exposure
   - daily loss limit
   - buying power/margin check
   - stale quote age
   - max spread bps
   - max predicted slippage bps
   - max open orders
   - max cancel/replace rate
   - duplicate/idempotency block
   - kill-switch block
2. Make risk failures explainable and logged.
3. Make high-risk telemetry feed model retraining/research.

Acceptance:
- Unit tests cover every gate.
- Blocked orders never call broker.
- All risk decisions are attached to the order trace.

### 6. Backtesting Is Not Strong Enough For Profit Claims

Current issue:
- Backtests are mostly scripts, one-off scans, or synthetic fixtures.
- Slippage tests are partly self-referential.
- No robust walk-forward framework proves profitability after costs.
- No live-vs-backtest drift loop.

Roadmap:
1. Build event-replay backtester.
2. Add transaction costs:
   - brokerage
   - taxes/charges
   - STT for Indian context
   - slippage
   - spread
   - latency
   - failed/partial fills
3. Add walk-forward validation by regime.
4. Add paper/live reconciliation.
5. Track:
   - net Sharpe
   - max drawdown
   - turnover
   - hit rate
   - profit factor
   - cost-adjusted alpha
   - p95 slippage
   - capacity
   - live-vs-backtest drift

Acceptance:
- No strategy promotion without out-of-sample improvement.
- Strategy metrics include costs and execution realism.
- Paper fills are compared against simulated fills.

### 7. Model Governance Is Prototype-Level

Current issue:
- Some JMCE/CoreML paths use synthetic or freshly initialized model flows.
- Forecast fallbacks may hide degraded signal quality.
- `clears_hurdle` and model lineage are not consistently propagated into decision logic.

Roadmap:
1. Add model registry.
2. Every forecast records:
   - model id
   - version
   - data window
   - feature set
   - fallback reason
   - calibration bucket
   - `clears_hurdle`
   - MAE/MAPE
   - directional accuracy
   - expected value after costs
3. Export CoreML only from trained weights.
4. Add MLX/CoreML parity tests.
5. Add promotion/rollback gates.
6. Add per-agent attribution and ablation scoring.

Acceptance:
- No production model uses synthetic lineage.
- No adapter/model promotion without holdout improvement and drawdown non-regression.
- Forecast quality is visible to execution gates.

### 8. Production Security & Operations Are Not Ready

Current issue:
- Sensitive endpoints lack production auth/RBAC.
- Rate limiting is narrow and in-memory.
- Audit logs are local mutable files with concurrency risks.
- Docker/compose has deployment and secret-handling issues.
- Debug logs can be exposed without strong auth.

Roadmap:
1. Add auth/RBAC:
   - admin
   - trader
   - read-only
2. Protect:
   - trade approval
   - broker config
   - MCP tool execution
   - debug/log endpoints
   - model operations
   - status/system endpoints
3. Move rate limiting to Redis-backed middleware.
4. Make audit logs concurrent-safe and append-only or externally shipped.
5. Fix Docker build/healthchecks/secrets.
6. Add release gate:
   - unit tests
   - integration tests
   - security tests
   - dependency audit
   - container build
   - compose smoke test
   - auth/rate-limit tests

Acceptance:
- Financial operations fail closed if audit write fails.
- No hardcoded production approval secret fallback.
- CI blocks release on security/deployment failures.

## Prioritized Roadmap

### Phase 1: Stop Unsafe Execution Paths

Goal:
Make every trade pass through one controlled execution service.

Tasks:
- Build `OrderExecutionService`.
- Fix HITL order schema mismatch.
- Block direct broker calls.
- Add idempotency keys.
- Add mandatory pre-flight/risk trace.

Done when:
- No order can reach broker without trace, risk decision, and approval record.

### Phase 2: Broker Abstraction Foundation

Goal:
Prepare clean T212-to-Breeze migration.

Tasks:
- Add broker-neutral models and protocol.
- Wrap T212 behind adapter.
- Add broker capability matrix.
- Add generic broker config routes.
- Start UI account model migration.

Done when:
- Existing T212 behavior works through broker abstraction.

### Phase 3: Deterministic Risk & Re-Quoting

Goal:
Make execution safe under intraday speed.

Tasks:
- Implement full risk gate set.
- Implement re-quoter state machine.
- Add kill switch.
- Add structured order telemetry.
- Benchmark synthetic 10 order actions/sec.

Done when:
- Re-quoting and kill switch pass lifecycle tests.
- p95 execution admission stays under target.

### Phase 4: Breeze / India Integration

Goal:
Add ICICI Breeze safely.

Tasks:
- Add Breeze adapter.
- Add Breeze auth/session handling.
- Add NSE/BSE instrument identity.
- Add Indian market calendar.
- Add Breeze websocket ingestion.
- Add limit-order-only execution.
- Add Breeze rate budgeter.

Done when:
- Mock Breeze fixtures pass.
- Dry-run/paper mode works without live order risk.

### Phase 5: Strategy Validation & Model Governance

Goal:
Prove strategies survive realistic costs and slippage.

Tasks:
- Build walk-forward event replay.
- Add cost model for Indian context.
- Add forecast registry.
- Add MLX/CoreML parity.
- Add model promotion/rollback.
- Add agent contribution attribution.

Done when:
- Strategies cannot be promoted without out-of-sample, cost-adjusted evidence.

### Phase 6: Production Hardening

Goal:
Make live usage operationally defensible.

Tasks:
- Add auth/RBAC.
- Add Redis rate limits.
- Harden secrets.
- Fix Docker/compose.
- Make audit logs durable.
- Add observability dashboards and runbook.
- Add CI release gate.

Done when:
- Sensitive endpoints are protected.
- Deployment is reproducible.
- Audit/telemetry survives concurrent trading load.

## Research Tracks

Required research before live Breeze trading:
- Breeze order lifecycle, reject codes, rate limits, session expiry, websocket stability.
- NSE/BSE microstructure, tick sizes, circuit limits, session windows.
- Indian charges/taxes: brokerage, STT, exchange fees, GST, stamp duty.
- Limit-order execution tactics: collars, queue risk, adverse selection, cancel/replace timing.
- Data quality: stale quote detection, dropped ticks, symbol master refresh.
- Strategy robustness: walk-forward, drift, regime splits, capacity.
- Compliance and safety: static IP, audit retention, manual override, emergency stop.

## Final Target State

The intended final system is:

- Agents research, debate, forecast, and propose.
- A schema validator turns only complete proposals into execution candidates.
- A deterministic execution engine validates risk, quote freshness, broker rules, and expected value.
- Broker adapters handle T212 and Breeze without leaking broker-specific logic into agents.
- Breeze execution is limit-order, India-aware, rate-budgeted, and fully audited.
- Profit claims are backed by walk-forward, cost-adjusted, paper/live reconciled evidence.

That is the roadmap required to move Growin toward its main goal: maximum intraday profit extraction with controlled, measurable, broker-safe execution.
