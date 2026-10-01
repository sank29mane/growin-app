# Growin Scalping Roadmap Audit

## Goal

Turn Growin from an AI portfolio intelligence app into a broker-safe, India-ready, semi-high-frequency intraday trading system targeting up to 10 order actions per second, with every trade routed through measurable expected value, execution quality, and risk controls.

This is not a request to "make the AI more confident." The main gap is that the current codebase has many useful components, but the live execution path does not yet force every order through a unified broker abstraction, pre-trade simulator, risk gate, strategy validation loop, and audit trail.

## Current State

- Frontend: SwiftUI macOS app with a trading execution panel, portfolio ledger, AI strategy streaming, and broker/account UI.
- Backend: FastAPI-style Python backend with agent orchestration, MCP broker tooling, quant indicators, pre-flight simulation, risk gates, telemetry, tests, and Trading 212 integration.
- Broker shape: Trading 212 is embedded directly in routes, MCP defaults, goal execution, account config, and UI naming.
- Strategy shape: agents can classify intraday intent and generate strategy output, but there is no production-grade execution engine that continuously scores alpha, sizes orders, proves risk, and submits broker-native orders.
- Verification shape: there are useful tests for simulator latency, risk gate blocking, API schemas, quant math, security middleware, and MCP behavior, but not enough tests around the full trade lifecycle.

## Critical Findings

1. Live execution bypasses the available safety stack.
   - [ai_routes.py](/Users/sanketmane/Codes/Growin%20App/backend/routes/ai_routes.py:27) approves a trade and directly calls `place_market_order` through MCP.
   - [simulation/engine.py](/Users/sanketmane/Codes/Growin%20App/backend/simulation/engine.py:6) has a pre-flight execution simulator, but that simulator is not enforced in the approval route.
   - [simulation/swarm_gate.py](/Users/sanketmane/Codes/Growin%20App/backend/simulation/swarm_gate.py:5) can block or scale trades by spread/regime, but it is not part of the live approval route.
   - Result: the documented "governed autonomous execution" story is not true end-to-end yet.

2. The execution UI is not wired to real order execution.
   - [ExecutionPanelView.swift](/Users/sanketmane/Codes/Growin%20App/Growin/Views/Trading/ExecutionPanelView.swift:147) shows hard-coded risk metrics.
   - [ExecutionPanelView.swift](/Users/sanketmane/Codes/Growin%20App/Growin/Views/Trading/ExecutionPanelView.swift:175) only prints `Committing order...`.
   - Result: users can see a high-conviction execution surface, but it does not actually submit an order or display backend validation state.

3. Broker coupling is too strong for an India transition.
   - [t212_handlers.py](/Users/sanketmane/Codes/Growin%20App/backend/t212_handlers.py:34) exposes Trading 212-specific routes.
   - [market_routes.py](/Users/sanketmane/Codes/Growin%20App/backend/routes/market_routes.py:66) executes goal plans by creating Trading 212 pies.
   - [server.py](/Users/sanketmane/Codes/Growin%20App/backend/server.py:76) registers Trading 212 as a default MCP server.
   - Result: Breeze cannot be swapped in cleanly. A broker-neutral order/account/positions interface is needed first.

4. India/Breeze changes execution semantics, not just credentials.
   - Official Breeze docs say the API allows 100 calls/min and 5000 calls/day, requires registered static IPs for orders, allows a maximum combined 10 order actions/sec, and does not permit market orders.
   - Result: the current Trading 212 `place_market_order` path conflicts with Breeze constraints. The system must become limit-order-first with explicit order lifecycle management.

5. The quant layer is indicator-heavy, not yet scalping-alpha-heavy.
   - [quant_engine.py](/Users/sanketmane/Codes/Growin%20App/backend/quant_engine.py:308) computes RSI, MACD, Bollinger bands, EMAs, and volume SMA.
   - These are useful features, but they are not enough for profitable 10 orders/sec intraday execution.
   - Missing: order book imbalance, queue position, spread capture probability, fill probability, adverse selection score, cancellation policy, market impact calibration, and per-symbol latency/slippage histograms.

6. There is a good simulator seed, but it needs market-real calibration.
   - [test_simulator.py](/Users/sanketmane/Codes/Growin%20App/tests/backend/test_simulator.py:153) asserts a 50ms pre-flight SLA.
   - That is useful, but the current simulation data is synthetic and not calibrated against real broker fills, exchange ticks, or rejected/cancelled orders.
   - Result: it is a latency test, not yet a profit-quality test.

7. Governance is too shallow for autonomous capital.
   - [governance.py](/Users/sanketmane/Codes/Growin%20App/backend/agents/governance.py:13) defines simple agent permissions.
   - [governance.py](/Users/sanketmane/Codes/Growin%20App/backend/agents/governance.py:49) only checks whether an agent can trade.
   - Missing: per-symbol exposure caps, kill switch, daily loss caps, order-rate budgets, strategy allowlists, max churn, capital-at-risk limits, and broker compliance constraints.

8. Documentation overstates implementation maturity.
   - Docs describe "high-conviction autonomous execution," "audit trail," "strategy suggestion engine," and "governance policy enforcement."
   - Code shows partial pieces, demo/in-memory strategy storage, direct broker calls, and placeholder UI data.
   - Result: the roadmap must first close claim-vs-code gaps before adding more agent complexity.

## Improvement Roadmap

### Phase 1: Make Execution Broker-Neutral

Goal: remove hard Trading 212 assumptions from the live trade path before adding Breeze.

- Define broker-neutral models: `OrderIntent`, `ValidatedOrder`, `BrokerOrderRequest`, `BrokerOrderResult`, `OrderStatus`, `BrokerPosition`, `BrokerFunds`, and `BrokerInstrument`.
- Create a `BrokerAdapter` interface with methods for `get_funds`, `get_positions`, `preview_order`, `place_order`, `modify_order`, `cancel_order`, `get_order`, `list_orders`, and `stream_order_updates`.
- Implement `Trading212Adapter` by wrapping existing MCP/T212 behavior.
- Change `/api/ai/trade/approve` to call the adapter interface, not `state.mcp_client.call_tool("place_market_order", ...)`.
- Replace Trading 212 Pie execution with a portfolio-intent path that can be unsupported per broker rather than hard-coded.
- Verification: mock `Trading212Adapter` and assert the approval route receives a normalized order result without direct MCP calls.

### Phase 2: Add ICICI Breeze Adapter

Goal: support Indian execution without weakening controls.

- Add `BreezeAdapter` with Breeze auth, checksum signing, session token refresh, static-IP readiness checks, and endpoint-level throttles.
- Enforce Breeze constraints in code: no market orders, NSE/FNO support boundaries, 10 combined order actions/sec, 100 calls/min, 5000 calls/day.
- Map Growin order intent to Breeze fields: exchange, stock code, product type, action, order type, quantity, price, validity, stoploss, disclosed quantity, and settlement/product constraints.
- Build security master ingestion for Breeze token/scrip mapping, refreshed daily before market open.
- Add order update stream handling so the app can reconcile pending, partially-filled, filled, cancelled, rejected, and modified states.
- Verification: unit tests for signing, throttling, no-market-order rejection, order mapping, and simulated 429/403 handling.

### Phase 3: Build the Real Execution Engine

Goal: every trade must pass through one deterministic pipeline before broker submission.

- Introduce an `ExecutionEngine` that owns the order lifecycle: propose, validate, simulate, risk-scale, preview, submit, monitor, modify/cancel, reconcile, and audit.
- Wire `PriceValidator`, `PreFlightSimulator`, `RiskSwarmGate`, broker preview, and governance into this engine.
- Require a `TradeDecisionRecord` for every order with model inputs, signal version, expected value, risk score, fill assumptions, broker response, and realized outcome.
- Add a rate-budget ledger: per-second, per-minute, daily, per-symbol, and per-strategy budgets.
- Add kill switches: daily realized loss, drawdown from peak, consecutive failed orders, stale market data, latency breach, spread breach, broker rejects, and manual user toggle.
- Verification: integration test that a pending trade cannot reach broker submission unless simulator, risk gate, rate limiter, broker preview, and audit record all pass.

### Phase 4: Replace Indicator Signals With Scalping Alpha Research

Goal: move from generic technical indicators to execution-aware alpha.

- Build a research track around market microstructure: spread dynamics, order book imbalance, queue position, fill probability, short-horizon reversal/momentum, adverse selection, and cancellation timing.
- Add feature streams: bid/ask, top-of-book depth, candle stream, order updates, realized spread, slippage, partial fills, rejection codes, and latency.
- Create strategy families: opening range breakout, liquidity imbalance fade, spread capture, volatility breakout, mean reversion after exhaustion, news/sentiment shock filter, and regime-gated momentum.
- Train only after data discipline exists. First baseline deterministic strategies, then compare ML/RL policies against simple baselines.
- Verification: every strategy must beat a no-trade baseline and a naive indicator baseline after costs, slippage, brokerage, taxes, and failed fills.

### Phase 5: Backtesting, Replay, and Paper Trading

Goal: prove expected value before live capital.

- Store tick/candle/order/fill data in DuckDB with immutable raw tables and normalized clean tables.
- Add event-driven replay that replays market data, agent decisions, broker constraints, order queue, cancellations, and fills.
- Add walk-forward testing by symbol and regime: train window, validation window, out-of-sample window.
- Add paper mode using the exact same `ExecutionEngine` and broker adapter interface, with broker submission swapped for simulated fills.
- Track metrics that matter: net PnL after all costs, hit rate, average win/loss, max drawdown, Sharpe/Sortino, turnover, fill ratio, adverse selection, cancel-to-fill ratio, rejected orders, and latency percentiles.
- Verification: CI should fail if a strategy has no replay report, no cost model, or no out-of-sample result.

### Phase 6: Multi-Agent Portfolio Manager Hardening

Goal: agents should debate and explain, but deterministic controls must own capital.

- Split roles cleanly: SignalAgent proposes, RiskAgent blocks/scales, ExecutionAgent routes, PortfolioAgent allocates capital, ComplianceAgent enforces broker/regulatory rules, Auditor records outcomes.
- Prevent agents from placing orders directly. Agents emit typed proposals; `ExecutionEngine` alone can submit.
- Add agent performance accounting: by strategy, symbol, regime, time of day, and broker.
- Add self-review loops only after trade outcomes exist: agents can adjust confidence, but not bypass risk gates.
- Verification: test that no agent except `ExecutionEngine` can call a broker adapter submission method.

### Phase 7: Swift UI Transition

Goal: make the app show real order state and India broker controls.

- Replace hard-coded risk rows in `ExecutionPanelView` with backend pre-flight results.
- Change slide-to-confirm to call the trade approval/preflight endpoint and show validation states.
- Add broker selector: Trading 212, ICICI Breeze, paper broker.
- Add India-specific fields: exchange, product type, validity, limit price, stoploss, disclosed quantity, and segment eligibility.
- Add live order blotter: pending, open, partial, filled, rejected, cancelled, modified.
- Verification: UI tests for disabled execution when broker is disconnected, market order disabled under Breeze, rejected preflight visible to user, and successful paper order lifecycle.

### Phase 8: Production Operations

Goal: make live trading observable and stoppable.

- Add structured audit tables for order intent, validation, submission, broker response, order update, final fill, and realized PnL.
- Add dashboards for latency, rate limits, fill quality, strategy PnL, risk limits, broker errors, stale data, and daily loss.
- Add a physical/manual kill switch in UI plus backend process-level kill flag.
- Add startup readiness checks: broker auth, static IP, market status, instrument map age, data stream freshness, clock drift, and rate limiter state.
- Add runbooks for Indian market open, broker outage, failed auth, partial fills, stuck orders, and regulatory limit breach.
- Verification: chaos tests for broker timeout, order reject, market data stale, auth expiry, and kill switch activation.

## ICICI Breeze Transition Plan

### Source Constraints To Respect

Official Breeze documentation states:

- Breeze supports live/historical data, automated trading strategies, and real-time portfolio monitoring.
- Normal API rate limits are 100 API calls per minute and 5000 calls per day.
- Orders must come from registered static IP addresses.
- A maximum combined limit of 10 order actions/sec applies to placement, cancellation, modification, and square-off.
- Market orders are not permitted.
- NSE, equity futures, and equity options are supported; BSE and MCX securities are currently not available through Breeze API.

### Required Design Changes

- Replace `MARKET` order assumptions with `LIMIT`, `STOPLOSS`, and controlled modification/cancellation workflows.
- Treat 10 orders/sec as a hard shared budget, not only placement speed. Cancels and modifications consume the same budget.
- Add order price calculation and tick-size rounding for Indian instruments.
- Add session lifecycle and static-IP readiness before allowing live mode.
- Add `PaperBreezeAdapter` before live Breeze to test order mapping and lifecycle without capital.
- Add broker capability discovery so the UI can disable unsupported order types automatically.

### Migration Sequence

1. Create broker-neutral adapter interface and models.
2. Wrap Trading 212 behind the adapter without changing external behavior.
3. Add paper broker adapter and route UI execution through it.
4. Add Breeze auth/signing/token map/order mapping in disabled-by-default mode.
5. Enable Breeze paper mode with real market data and simulated fills.
6. Enable Breeze live mode for one symbol, one strategy, small capital, and manual approval only.
7. Expand to multi-symbol semi-HFT only after replay, paper, and live pilot metrics pass thresholds.

## Deep Research Plan

1. Broker and regulatory research.
   - Breeze order API, rate limits, static IP, supported segments, order types, fees, taxes, margin, RMS rejects, and session handling.
   - Trading 212 public API limits and order semantics if keeping it as a secondary broker.
   - India-specific charges: brokerage, STT/CTT, exchange transaction charges, GST, SEBI charges, stamp duty, DP charges, and intraday margin rules.

2. Market microstructure research.
   - Indian NSE tick behavior, auction/open volatility, liquidity by symbol, bid-ask spread behavior, lot sizes, tick sizes, and top-of-book depth quality.
   - Literature on limit order placement, adverse selection, queue position, and latency erosion.

3. Data research.
   - Breeze market data stream quality and retention.
   - Alternative NSE data vendors if Breeze tick data is insufficient for scalping research.
   - Storage schema for ticks, candles, order book snapshots, order events, and fills.

4. Strategy research.
   - Baseline deterministic strategies before ML: ORB, imbalance, spread capture, volatility breakout, micro mean reversion.
   - Agent role design after deterministic baselines exist.
   - ML/RL only after replay environment and cost model are stable.

5. Safety research.
   - Kill switch design, capital-at-risk policy, order throttling, compliance checks, and failure-mode taxonomy.
   - Human-in-the-loop thresholds for live pilots.

## Acceptance Criteria

- No live order can be placed without a `TradeDecisionRecord`.
- No broker-specific API is called directly from route handlers.
- Breeze live mode blocks market orders at schema validation time.
- The execution path enforces pre-flight simulation, risk gate, rate budgets, broker preview, and audit logging.
- UI execution shows real validation and order lifecycle state.
- Paper trading and replay use the same execution engine as live trading.
- Every strategy report includes costs, slippage, rejected orders, fill ratio, and out-of-sample results.
- Live mode starts with manual approval only and has a tested kill switch.

## Recommended First Implementation Plan

1. Add broker-neutral models and `BrokerAdapter`.
   - Verify: tests instantiate fake broker and validate order model serialization.

2. Refactor `/api/ai/trade/approve` to use `ExecutionEngine`.
   - Verify: test proves direct MCP `place_market_order` is not called from route code.

3. Wire `PriceValidator`, `PreFlightSimulator`, `RiskSwarmGate`, and audit logging into `ExecutionEngine`.
   - Verify: blocked spread, stale data, failed price validation, and missing DB policy all stop submission.

4. Add `PaperBrokerAdapter`.
   - Verify: UI/backend can complete an order lifecycle without a real broker.

5. Add Breeze capability model and disabled-by-default adapter skeleton.
   - Verify: Breeze rejects market orders and respects 10 order-actions/sec in tests.

6. Replace Swift hard-coded execution metrics with backend preflight response.
   - Verify: slide-to-confirm shows pending, blocked, submitted, rejected, and filled states.

7. Add replay report artifact for each strategy.
   - Verify: CI fails if a strategy lacks cost-adjusted out-of-sample metrics.

8. Run a paper-trading pilot before any live capital.
   - Verify: at least 20 trading sessions with tracked fill quality, net PnL after costs, and no kill-switch breach.

## External References

- ICICI Direct Breeze API documentation: https://api.icicidirect.com/breezeapi/documents/index.html
- Cont and Kukanov, "Optimal order placement in limit order markets": https://arxiv.org/abs/1210.1625
- Gonzalez and Schervish, "Instantaneous order impact and high-frequency strategy optimization in limit order books": https://arxiv.org/abs/1707.01167
- Lehalle and Mounjid, "Limit Order Strategic Placement with Adverse Selection Risk and the Role of Latency": https://arxiv.org/abs/1610.00261
