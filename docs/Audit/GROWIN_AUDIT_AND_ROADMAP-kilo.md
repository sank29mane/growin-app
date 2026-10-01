# Growin App — Comprehensive Code Audit, Profit-Maximization Roadmap & Broker Transition Plan

**Date:** 2026-07-10
**Scope:** Full-stack review of the Growin multi-agentic portfolio management + semi-high-frequency intraday scalping app (Swift UI + Python FastAPI backend + T212 MCP server).
**Method:** Static code audit of the live execution path, the agent swarm, the data/ML stack, plus external deep-research on HFT execution, multi-agent trading, and the ICICI Breeze API.
**Primary Goal Under Review:** Extract maximum profit from semi-high-frequency intraday trading (~10 orders/sec) via a multi-agentic framework, with a transition from Trading 212 to ICICI Direct (Breeze API).

---

## 1. Executive Summary

The codebase is an **impressive research/advisory assistant** (10+ specialist agents, GMM regime detection, MLX adapter hot-swap, NeuralODE, TTM-R2 forecasting, DuckDB analytics, Rust/ANE acceleration paths). It is **not a trading engine** and, as written, **cannot place a single live order at any speed**, let alone 10/sec.

Three structural facts dominate every other finding:

1. **The entire risk/simulation/execution layer is dead code on the live path.** `LiveTradingLoop.execute_order_pre_flight` is defined but never called. `process_tick` has zero callers. `AdaptiveReQuoter.poll()` is an empty `pass` stub. Orders actually fire through `t212_handlers.place_market_order → Trading212Client.place_market_order` directly.
2. **The throughput path is throttled by design to <1 order/sec.** Every market order pays a stacked 500–2000 ms random "temporal jitter" (`trading212_mcp_server.py:293-299`) **plus** a second identical 500–2000 ms jitter inside the rate-limiter's acquire (`utils/rate_limiter.py:85-88`), all on a budgeter that is **hard-capped at 4 req/sec and strictly serialized** (`rate_limiter.py:30,62-95`).
3. **Decisions are LLM-generated prose, gated by human approval.** The DecisionAgent takes 5–24s per decision (`decision_agent.py:140-142,666`), and the RiskAgent **forces HITL on every mention of BUY/SELL** (`risk_agent.py:123-128`). There is no fast, deterministic signal→order path.

The good news: the *offline bones* (Numba GMM/Welford online features, adapter hot-swap design, DuckDB storage, region-routed data engine, the multi-agent message bus) are salvageable. The roadmap below turns them into a real scalping engine.

---

## 2. Architecture As-Built (What Exists)

| Layer | Component | File(s) | Status |
|---|---|---|---|
| UI | Swift views, SSE streaming, portfolio/execution panels | `Growin/` | Solid, presentation-only |
| API | FastAPI routers (chat, agent, market, ai, mcp, status) | `backend/routes/*` | Solid |
| Orchestrator | Fans out to 7+ specialists, debate loop, ACE score | `agents/orchestrator_agent.py` | Over-built for trading |
| Brain | LLM DecisionAgent (LM Studio / granite-tiny / gpt) | `agents/decision_agent.py` | Too slow for hot path |
| Specialists | Quant, Forecast/TTM, Research, Social, Whale, Portfolio, Goal, Risk | `agents/*.py` | Mixed quality |
| Live loop | GMM regime, MLX adapter swap, risk leverage, pre-flight sim | `trading_loop.py` | **Never wired** |
| Simulation | PreFlightSimulator, MarketImpactModel, RiskSwarmGate, telemetry | `backend/simulation/*` | **Dead / stubbed** |
| Broker | T212 MCP (REST) | `trading212_mcp_server.py`, `t212_handlers.py` | Rate-limited, no live book |
| Data | Alpaca (US) / Finnhub (UK .L) / yfinance (fallback) | `data_engine.py` | **Blocking SDK, no live stream** |
| ML | GMM (Numba), NeuralODE (Torch), TTM-R2 (subprocess), JMCE | `coreml/`, `mlx/`, `models/`, `forecaster.py` | Offline-tuned |
| Storage | DuckDB OHLCV + features + telemetry | `analytics_db.py` | Batch-only, global lock |

---

## 3. Comprehensive Flaws & Audit Findings

### 3.1 Execution & Simulation Path (CRITICAL — blocks the 10/sec goal)

**P0 — Throughput is capped at <1 order/sec by design**
- `trading212_mcp_server.py:293-299` — `_apply_temporal_jitter()` unconditionally `await asyncio.sleep(random.uniform(0.5, 2.0))` **before every** order (market, limit, stop, stop-limit).
- `utils/rate_limiter.py:85-88` — the budgeter adds a **second** `random.uniform(0.5, 2.0)` sleep *specifically for `PRIORITY_EXECUTION`* inside `acquire()`.
- `utils/rate_limiter.py:30` — `T212RequestBudgeter(capacity=20, refill_rate=4.0)` = hard **4 req/sec ceiling**.
- `utils/rate_limiter.py:62-95` — the budgeter holds `async with self.lock:` across the `await asyncio.sleep(...)` refills, so **only one order can ever be in flight** → strict serialization.
- Net: best case ~1.0s/order (≤1/sec); mean ~2.5s/order (~0.4/sec). **10/sec is physically impossible.**

**P0 — Dead / never-wired execution layer**
- `trading_loop.py:211` `execute_order_pre_flight` is **defined but never called** (repo-wide grep confirms). The real path is the un-gated MCP `place_market_order`.
- `trading_loop.py:128` `process_tick` has **zero callers** — the live feature/regime pipeline is not connected to any ingestion loop.
- `simulation/requoter.py:50-51` `AdaptiveReQuoter.poll()` is `pass` (stub). `start()` spins a loop that awaits the no-op then sleeps 5s forever. All re-quote/collar logic (`requoter.py:36-48`) is dead.
- `trading_loop.py:54,73,114` `alpaca_client` parameter is **never wired** to the T212 dispatch; if `kill_switch` ever ran it would hit the wrong broker or raise `AttributeError`.

**P0 — RiskSwarmGate will block 100% of trades**
- `swarm_gate.py:46` queries `scaling_policies` table, but **no `CREATE TABLE scaling_policies` exists anywhere** in the repo → query fails → `scale_multiplier = 0.0` → all trades blocked.
- `swarm_gate.py:32` hard-blocks any trade when `current_spread_pct > 0.05` (5%) — freezes scalping during exactly the high-edge volatile windows.
- `swarm_gate.py:53-57` returns `0.0` on a missing regime row (fail-closed) — a missing DB row nukes all flow instead of defaulting to a safe multiplier.
- `swarm_gate.py:39-41` also blocks everything if `db_connection is None`; no in-memory fallback policy.
- `swarm_gate.py:46` runs a **synchronous SQLite query inline inside the async hot path** with no `run_in_executor` and no cache.

**P1 — Correctness bugs**
- `simulation/models.py:46-53` — synthetic L2 book assumes a fixed **10bps per level** even for BUY at level 0; every order pays ≥10bps guaranteed slippage. No partial-at-mid.
- `simulation/models.py:39` — fallback impact `mid * 0.0002 * (abs_size ** 0.5)` treats *share count* as a participation fraction → arbitrary magnitudes, not a real square-root impact model.
- `simulation/engine.py:85-94` — drawdown is **stateless per-call** from caller-supplied `peak_equity`; if the caller doesn't maintain it (default falls back to `current_equity`), drawdown is *always 0* → `trading_loop.py:287` re-opt trigger never fires.
- `trading_loop.py:177-191` — `adapter_id = min(dominant_regime, max(available_adapters))` **collapses every regime ≥ max adapter onto one adapter**; also `max(...)` on non-int (UUID) adapter keys would `TypeError`. No backoff on swap failure → per-tick retry storm (`trading_loop.py:184-191`).
- `trading_loop.py:313-314` — `slippage_error_bps` and `actual_deviation_bps` are **identical expressions** (one is dead).

**P2 — Concurrency / telemetry**
- `process_tick` mutates shared `vol_tracker`, `spread_tracker`, both Welfords, `current_regime`, `risk_leverage_coefficient` with no locking; cross-task write/read of `requoter.current_regime` has no happens-before.
- `trading_loop.py:243` offloads a microsecond-scale stateless simulator to a thread pool (net loss at HFT rates).
- `simulation/telemetry.py:61` `pickle.dumps(tick_window)` into SQLite BLOB — storage bloat + **RCE vector** on `pickle.load`. No per-trade realized PnL/commission/per-instrument attribution.

### 3.2 Agent Architecture & Profit Path

**P0 — LLM hot-path cannot meet the latency budget**
- `decision_agent.py:140-142,666` DecisionAgent = "5-8s (LLM reasoning time)", `_run_agentic_loop` runs **up to 3 turns** (`:666`), each a full `llm.chat`. Plus routing LLM, risk-review LLM, possibly rebuttal LLM. **~20-40s per decision** vs the 100ms budget at 10/sec (~200-400× too slow).
- `decision_agent.py:89,956-958` tiny models (`granite-tiny`, "Nano" persona) are used for trade synthesis and the risk critic — high hallucination risk for entry/exit/conviction.
- `decision_agent.py:992-996` the LLM **authors its own** Target Entry / TP / SL in free text; `_validate_prices` (`:885-897`) only *warns*, never corrects. Shadow mode **hardcodes** 5%/3% off last price (`:853-854`).
- `decision_agent.py:1230-1234` trade extraction uses crude regex + **default quantity of 1.0 share** unless "X shares" found — sizing is essentially not extracted.

**P0/P1 — HITL and risk gating kill throughput**
- `risk_agent.py:123-128` forces `requires_hitl = True` on **any** BUY/SELL/ORDER/TRADE word regardless of size/conviction.
- `decision_agent.py:697-718` the **only** way to actually place an order is the "autonomous bypass" gated on `conviction_level == 10` + the LLM emitting a correctly-formatted `[TOOL:...]`.
- `orchestrator_agent.py:434-468` Contrarian debate adds a rebuttal LLM turn and can BLOCK; `risk_agent.py:148-153` even a RiskAgent *exception* forces HITL.
- `decision_agent.py:203-204,461-462,612-615` `USE_SHADOW_LLM=1` replaces output with a **fake template** — zero real orders possible in shadow mode.

**P1 — Signal quality gaps**
- `quant_agent.py` computes **only RSI/MACD/Bollinger + 30-min ORB** (`:65,80,84`). No order-flow, no L2, no microstructure, no volume-profile-within-bar.
- `quant_agent.py:89-114` "NPU Covariance Velocity" is built on `n_assets=1` single-ticker log returns — a scalar variance proxy, not cross-asset covariance, passed decoratively to ORB.
- `forecasting_agent.py:88-132` TTM whole-series unit-fix heuristic (>50×median ÷100, <0.02× ×100) can **silently corrupt legitimate moves** and double-correct already-normalized UK data.

**P1 — Architecture scaling**
- `orchestrator_agent.py:57-70` constructs **all 9 agents in `__init__`** per OrchestratorAgent instance; no persistent pool, so per-request cost is 9 constructions + multiple LLM client inits.
- `orchestrator_agent.py:331-372` fans out **every** needed specialist (4-6 agents for a trade) even though a scalp needs only quant + ORB + book; research/social/whale add latency with zero PnL contribution.
- `base_agent.py:137-226` + orchestrator emit ~10+ broadcast messages **persisted to DuckDB under a global lock per event** (`messenger.py:73-88`) → 100+ serialized DB writes/sec at 10/sec.

**P1 — Profit leakage**
- Trades are **text proposals**, not auto-executed (default → HITL). No OCO/stop/limit orders ever placed (`shared_types.py` lists the tools but nothing calls them). TP/SL exist only as prose.
- `decision_agent.py:986` persona says "adjust size for slippage" but **no code does**; slippage is displayed, never fed back into sizing.
- `decision_agent.py:710-715` sensitive tool calls wrap MCP in a 15s timeout + circuit breaker; one slow T212 call trips it and silently drops the trade.

**Contradiction:** DecisionAgent persona = *"aggressive hunter, maximize ROI"* (`decision_agent.py:972-997`) vs RiskAgent persona = *"find reasons the strategy is WRONG/DANGEROUS"* + forced HITL + 5% cap + wash-sale blocks (`risk_agent.py:48-73,123-128`). Structurally opposed mandates in the same loop → the conservative critic almost always wins → profit persona is decorative.

### 3.3 Data & ML/Quant Path

**P0 — No live data exists**
- `LiveTradingLoop.process_tick` has no producer; `AdaptiveReQuoter.poll()` is a stub → **0 ticks/sec** delivered.
- `data_engine.py:689-720` `get_real_time_quote` returns `high=low=open=change=0` (no usable live OHLC); everything is **blocking `asyncio.to_thread` SDK calls** (`:358,392,479,512,534,559,652`), not streaming.
- `FinnhubClient.websocket` is `None` and never opened (`data_engine.py:730`).
- `get_historical_bars` caches `ttl=300` (`:395`) and `get_batch_bars` caches 300s (`:501`) — 5-min staleness by design.
- yfinance is the universal fallback (`:389-392,117,218`) and is rate-limited + multi-second latency.

**P1 — ML/forecast unfit for scalping**
- `forecaster.py:231-279` dispatches TTM-R2 to a **subprocess over a stdin/stdout JSON pipe**, serializes the *entire* OHLCV list, blocks up to `timeout=45s` → **multi-hundred-ms to multi-second**, explicitly designed for 5-min-cached periodic forecasting, not per-tick.
- Every forecast targets **24h/48h/7d horizons** (`forecasting_agent.py:165-174`); GMM input is only 2D `[vol, spread]` (`trading_loop.py:154`) — cannot see order flow.
- `models/neural_ode.py` uses `torchdiffeq.odeint_adjoint` (tens-to-hundreds of ms on CPU, untrained) — unsuitable for per-tick.
- Only the **Numba GMM/Welford feature path** meets scalping latency (sub-10µs) — and even that is fed by `process_tick`, which is dead.

**P1 — Feature engineering inadequate**
- `features/` contains only `online_vol`, `online_spread`, `welford`. **No VWAP, order-flow, book-imbalance, volume-profile, tick-velocity** anywhere in the repo.
- `data_engine.py:149,238` divides UK prices by 100, *and* `CurrencyNormalizer.normalize_price` (`:804-807`) may do it again → double-correction risk; `forecasting_agent.py:88-131` heuristic can corrupt.
- `analytics_db.py` DuckDB stores **only daily 14-day** features (`rolling_spread = (high-low)/close` = bar-range proxy, **not** a bid/ask spread); no tick/order-book store, single global lock (`:16-17`).

---

## 4. Deep Research Findings (External Best Practice)

### 4.1 HFT/Semi-HFT Execution (from research subagent)
- **Latency budget @10/sec** (engineering guidance, 2025-2026): market-data ingest <50ms (WS tick→strategy), strategy <10ms in-process (no hot-path DB writes), order submit <100ms on a warm persistent connection, fills via order/trade WS. Single async event loop (Python `asyncio`/`anyio`, or Rust/Tokio). **Physical proximity to broker metro** is the #1 lever (measured ~1.2ms Amsterdam box vs ~88ms US-East).
- **Broker throughput for 10/sec:** Trading 212 is **disqualified** (market 50/min, limit/stop 1/2s, REST-only, no WS order book). ICICI Breeze ≈ **10 orders/sec combined** (purpose-built), Angel SmartAPI ≈ 9/sec, Alpaca ≈ 3.3/sec sustained (batch/pace). All Indian brokers now require **static IP + kill switch** under SEBI's Feb-2025 algo-trading circular.
- **Microstructure edge:** L2 depth/imbalance (bid:ask 2:1-3:1 flags pressure), spread + queue position (capture as maker), VWAP deviation + SD bands for mean-reversion, maker-vs-taker, simulate fills against a **local reconstructed book**, not mid-price. **Small sizes → negligible market impact** = you exploit microstructure without moving the book.
- **Why pure TA fails @10/sec:** RSI/MACD are smoothed, candle-derived, lagging, carry no order-flow info. Signal must be **event-driven from the book/tape**.
- **Risk that preserves throughput:** pre-trade checks **in-process, sub-ms** (cached position/size/collar lookups, no network) per SEC 15c3-5 / MiFID II RTS 6. Max size/notional, per-instrument & aggregate limits, price collar, order-to-trade throttle, **kill switch**, intraday drawdown halt. **Never gate on LLM/HITL in the loop.**
- **Failure modes:** overfitting (100 variations drops genuine-Sharpe probability 60%→<30%), survivorship bias (delisted names removed), underestimated slippage/fees/turnover, latency arbitrage against you, broker 429 retry storms (capped exponential backoff + circuit breaker that halts).

### 4.2 Multi-Agent Trading (from research subagent)
- **LLM helps offline, hurts hot-path.** LLMs excel at research/synthesis/narrative risk/strategy discovery, but are non-deterministic, hallucination-prone, and 1-5s/call — physically impossible at 100ms/order. **LLM trading alpha is largely an illusion** (Profit Mirage, arXiv:2510.07920: Sharpe decays 51-62% post-cutoff; FINSABER: edges vanish over 20-yr/100-symbol tests). Best practice: **LLM offline (strategy/code gen), deterministic policy online.**
- **Orchestration:** debate/critic improves calibration but adds latency and is correlated-error-prone in regimes — use a *fast, pre-computed* risk overlay, not a synchronous blocker. Keep persistent agent pools (not per-request), async bus with a semaphore, structured docs not long dialogues, research/social/whale run *offline* and feed outputs as features.
- **Replace LLM hot-path with:** RL execution (JPM LOXM ~32% exec-cost save), meta-controller / hierarchical RL (Hi-DARTS, HARL-TRADE 42% ret / Sharpe 4.19, MARS ensemble), or supervised-signal + rule execution (HARLF fuses LLM sentiment + DRL).
- **Risk governance:** tiered machine-native (Tier-1 autonomous kill-switch fires in 200-500ms on position/PnL/feed-toxicity, no human keystroke; Tier-2 auto-resizes; Tier-3 human after containment). **"Never let an LLM decide trade size."**
- **HITL tradeoffs:** kills scalping throughput at <100ms windows. Acceptable only for large size / new-strategy validation / edge-case escalation. Make HITL **async/exception-only**, route bulk flow to HOOTL.
- **Eval:** offline backtest with bias controls (Deflated Sharpe / PBO, walk-forward, point-in-time, delisting-inclusive) + paper/shadow mode (≥48hr) + execution-realistic sims (slippage/partial-fill/latency). Continuously A/B the full swarm vs a bare RL/rule baseline to prove the agents earn their latency budget.

---

## 5. Transition Section — Trading 212 → ICICI Direct (Breeze API)

### 5.1 Why transition
Trading 212 is **REST-only, rate-limited (market 50/min, limit 1/2s), has no WebSocket order book, and is primary-currency (GBP) UK/US only**. It is structurally incapable of 10/sec and offers no Indian-market access. **ICICI Direct's Breeze API is purpose-built for Indian algo trading with a ~10 orders/sec combined cap, WebSocket order-flow status, and 1-second historical OHLCV** — the right partner for INR intraday scalping.

### 5.2 Breeze API essentials (from research)
- **Auth:** OAuth2-style manual browser step. `generate_session(api_secret, session_token)` where `api_session` comes from `https://api.icicidirect.com/apiuser/login?api_key=…`, then `customerdetails` yields the real `session_token` (base64 `user_id:token`). Every REST call needs `X-AppKey`, `X-SessionToken`, `X-Timestamp` (ISO8601 UTC ±60s), `X-Checksum = SHA256(timestamp + json_payload + secret_key)` prefixed `"token "`. **Sessions don't auto-refresh — add a daily bootstrap/refresh job.**
- **Order placement:** `place_order(stock_code, exchange_code="NSE", product="cash", action="buy"/"sell", order_type="limit"/"stoploss", quantity, price, stoploss, validity, disclosed_quantity, …)` → returns `{'Success': {'order_id': …}}`. **SEBI algo-safeguards prohibit genuine market orders — anything sent as "market" becomes an aggressive limit order.** Poll via `get_order_detail` or subscribe to the order WebSocket (`orderFlow` statuses A/R/Q/O/P/E/J/X/C).
- **Rate limits:** **hard cap 10 orders/sec combined** (place+cancel+modify+square-off); 100 REST calls/min, 5000/day. Use a token-bucket limiter; use the order WS for status (don't poll).
- **Market data:** `get_historical_data_v2` with intervals `1second`/`1minute`/`5minute` (max 1000 candles/request, ~3yrs of second-level LTP); `get_quotes` + `subscribe_feeds` for streaming OHLC/tick. **Not corporate-action adjusted.**
- **Symbology:** daily `SecurityMaster.zip` (regenerated 08:00 IST) maps symbol→ISIN→exchange→WS token. **Only NSE and NFO supported — BSE and MCX are NOT on Breeze.** F&O hedging uses `product="futures"/"options"`, `expiry_date`, `right`, `strike_price`.
- **Indian specifics:** equity hours **09:15–15:30 IST**; intraday equity `product="cash"`; **no explicit "MIS" flag** — ICICI RMS auto-squares-off open intraday positions (~15:10–15:15); leverage broker-provided; STT/exchange/SEBI/stamp/GST charges; short-selling restricted on ASM/GSM; no fractional shares; **static IP mandatory**.

### 5.3 Build plan: `breeze_mcp_server.py` (mirror of `trading212_mcp_server.py`)
Mirror the MCP tool surface: `authenticate`, `get_portfolio_positions`, `get_portfolio_holdings`, `get_quotes`, `get_historical_data`, `place_order`, `get_order_detail/list`, `cancel_order`, `modify_order`, `square_off`, `get_funds`, `get_margin`. Wrap `breeze-connect` SDK in async MCP tools, persist credentials, add a **daily session-refresh job**, and a **token-bucket limiter at exactly 10 OPS**.

### 5.4 What Breeze CANNOT do (keep T212 as partner, don't fully replace)
No pies/ISA, no fractional/synthetic instruments, no multi-currency (INR only), no BSE/MCX, no true market orders, no Margin/OptionPlus. **Treat Breeze as a partner for INR intraday scalping**, not a full T212 replacement. The cleanest architecture: **T212 (GBP/USD swing & advisory) + Breeze (INR intraday scalp)** behind a unified `BrokerAdapter` interface, with the Indian path bearing the 10/sec load.

### 5.5 Transition risks
- **10 OPS is the ceiling, not headroom** — strict throttling + WS order status + graceful 429/backoff required; design for a *handful of symbols scalped in parallel*, not mass multi-leg HFT.
- **Static-IP + kill-switch are now SEBI-mandated** — bake a hardware kill switch into the deployment, not the strategy layer (Knight Capital lesson).
- **Session expiry** mid-day would halt all trading — daily refresh + a watchdog that fails safe (kill switch) on auth loss.
- **Breeze market orders become aggressive limits** — your pre-trade collar must account for this; you lose true marketable execution.

---

## 6. Prioritized Roadmap (Phased)

### Phase 0 — Stop the Bleeding (P0, ~1-2 weeks)
Goal: make the path *capable* of 10/sec and stop blocking itself.
1. **Remove the double temporal jitter** (`trading212_mcp_server.py:293-299` + `utils/rate_limiter.py:85-88`). If anti-clustering is needed, use a sub-ms randomized *sub-ms* offset off the critical path.
2. **De-serialize + raise the budgeter** (`rate_limiter.py:30,62-95`): `refill_rate ≥ 10`, release `self.lock` before any `await sleep` so the lock guards only token math.
3. **Make RiskSwarmGate fail-open** (`swarm_gate.py`): create/seed `scaling_policies`, default `scale_multiplier = 1.0` (not 0.0) on missing row, run the query via `run_in_executor` behind a cached in-memory policy, fix the ambiguous 5% unit handling.
4. **Wire `execute_order_pre_flight` into the real order path** (`trading_loop.py:211`) so risk gates actually protect live orders; or delete it if intentionally unused.
5. **Build a live tick/L2 producer** (WebSocket, not `to_thread` snapshots) feeding `LiveTradingLoop.process_tick` (`trading_loop.py:128`); replace the `AdaptiveReQuoter.poll()` stub with a real subscriber.
6. **Introduce `BrokerAdapter` interface**; implement `Trading212Adapter` (current) + `BreezeAdapter` (Phase 5) behind it.

### Phase 1 — Real-Time Data & Online Features (P0/P1, ~2-3 weeks)
7. WebSocket market-data clients (Alpaca `StockDataStream` US; Finnhub/ICICI WS for INR) pushing ticks + quotes + L2 into an async queue; consumer task calls `process_tick` at ≥10/sec.
8. **Add a tick/order-flow feature module** under `features/`: VWAP deviation, trade imbalance, book (bid/ask size) imbalance, quote velocity, volume profile. Expand GMM input from 2D `[vol,spread]` to a richer micro-feature vector.
9. **Online feature store + tick store** (in-memory, sub-ms reads; columnar/Parquet later) — keep DuckDB for batch. Partition/avoid the global lock for live writes.
10. **Single source of truth for units** (`currency_utils.py`); delete the duplicate ÷100 in `data_engine.py:149,238` and the in-place heuristic in `forecasting_agent.py:88-131`. Populate live OHLC in `get_real_time_quote`.
11. Remove the 300s cache from live reads; isolate live path from cached historical path.

### Phase 2 — Deterministic Signal→Order Engine (P1, ~3-4 weeks)
12. **Decouple decision latency from order rate.** QuantAgent + ORB run continuously (every 5s/bar) and publish a typed `Signal(ticker, side, entry, tp, sl, size, confidence)` to an in-memory stream. The executor subscribes and acts in <50ms. LLM used only to *generate* the plan off-band.
13. **Add risk-tiered gating** (`risk_agent.py:123-128`): small-size / high-Sharpe / liquid names auto-execute; only large/illiquid/wash-adjacent require HITL. Define `max_auto_size` + `auto_approved_confidence` instead of forced HITL on every trade word.
14. **Automate TP/SL** — when a signal is approved, place OCO stop+limit via the broker MCP (`shared_types.py` already lists the tools). This is the single biggest leakage fix.
15. **Replace TTM-R2 (daily, 45s subprocess) with a short-horizon intraday model** served in-process (small MLX/TorchScript predicting seconds–minutes), invoked per-tick; keep TTM only for slow macro context. Retire/repurpose NeuralODE for live use.
16. **Persistent agent pool + selective fan-out** (`orchestrator_agent.py:57-70,331-372`): don't rebuild 9 agents per request; for a scalp call only quant + ORB + book; run research/social/whale offline and feed as features.
17. **Make HITL async/exception-only**; route bulk flow to HOOTL.

### Phase 3 — Profit, Risk & Hardening (P1/P2, ~2-3 weeks)
18. **Fix slippage model** (`simulation/models.py`): remove guaranteed +10bps/level synthetic book, use real participation-rate square-root impact fed by actual L2; validate vs historical fills.
19. **Make drawdown stateful** (`engine.py:85-94`, `trading_loop.py:287`): track `peak_equity` across cycles so the re-opt trigger fires; add realized-PnL reconciliation feeding risk.
20. **Fix regime→adapter mapping** (`trading_loop.py:177-191`) with an explicit safe-keyed dict + backoff on swap failure; provide CPU/quantized adapter fallback (currently Apple-Silicon-only, dummy weights).
21. **Implement or remove `AdaptiveReQuoter`** (`requoter.py`); wire `kill_switch` to the real broker `cancel_order`.
22. **Telemetry overhaul** (`telemetry.py`): stop `pickle`-ing tick windows (compact encoding), add per-trade realized PnL + commission + per-regime/per-instrument attribution, split "scaled-down" vs "blocked".
23. **Run `simulate_execution` inline** (`trading_loop.py:243`) — drop the unnecessary `run_in_executor`.
24. **Pre-trade risk as hard-coded module** (sub-ms, in-process): max size/notional, per-instrument & aggregate limits, price collar, order-to-trade throttle, kill switch, intraday drawdown halt — never LLM/HITL-gated.

### Phase 4 — ICICI Breeze Integration (P1, ~1-2 weeks)
25. Implement `breeze_mcp_server.py` (§5.3) with OAuth session bootstrap + daily refresh, token-bucket at 10 OPS, WS order status.
26. Add `BreezeAdapter` to the `BrokerAdapter` interface; keep T212 as partner for GBP/USD.
27. Bake SEBI-mandated static-IP + hardware kill-switch into deployment; fail-safe on auth loss.
28. Handle Breeze-specific constraints: market→aggressive-limit, NSE/NFO only, RMS square-off, INR-only, no fractional.

### Phase 5 — Validation & Research Loop (Continuous)
29. **Backtest with bias controls:** walk-forward, point-in-time, delisting-inclusive, realistic slippage/fees/turnover (not mid-price).
30. **Shadow/paper mode ≥48hr** before any live capital; A/B the full swarm vs a bare RL/rule baseline to prove the agents earn their latency budget.
31. **Execution-realistic sims:** slippage, partial-fill, latency stress tests per regime/strategy.
32. Continuous drift monitoring: warm GMM/Welford from live distribution, not frozen offline stats; regime/feature health telemetry.

---

## 7. Deep Research Plan (Research Every Aspect)

A standing, repeatable research program to de-risk each roadmap phase. Run before/parallel to implementation.

| # | Aspect | Question | Method | Owner |
|---|---|---|---|---|
| R1 | **Broker selection** | Which broker(s) for 10/sec INR + GBP/USD? Breeze vs Angel vs Upstox vs Alpaca pacing | Web research + rate-limit sandbox probe | Research agent |
| R2 | **Breeze API internals** | Exact `place_order` params, WS `orderFlow` schema, session-refresh cadence, second-level LTP fidelity | Read SDK `breeze-connect` source + docs; mock-session probe | Integration agent |
| R3 | **Microstructure signals** | Which order-flow features (imbalance, VWAP, absorption) actually predict 1-5min INR moves? | Literature review (quantstrate.io, flytradr) + own feature-backtest | Quant agent |
| R4 | **Execution modeling** | Realistic fill/slippage/queue-position simulator for INR names | Build local book from WS depth; validate vs Breeze fills | Simulation agent |
| R5 | **Meta-controller** | RL/hierarchical-RL vs supervised+rule vs LLM-offline for regime→strategy selection | Reproduce Hi-DARTS/HARL-TRADE/MARS recipes offline | Research agent |
| R6 | **Pre-trade risk** | Sub-ms in-process checks meeting SEC 15c3-5 / MiFID II RTS 6 | Legal/reg review + latency bench | Risk agent |
| R7 | **LLM role boundary** | Prove LLM-offline earns its keep vs deterministic baseline | A/B swarm vs RL/rule on shadow data | Eval harness |
| R8 | **Backtest integrity** | Deflated Sharpe / PBO / walk-forward harness for agentic system | Build SysTradeBench-style harness | Analytics agent |
| R9 | **SEBI compliance** | Static-IP, kill-switch, algo-registration, square-off rules for Indian algo trading | Reg reading + broker compliance call | Governance |
| R10 | **Cost model** | Brokerage + STT + GST + slippage + spread per strategy; net-Sharpe after costs | Per-trade telemetry (Phase 3.22) + simulation | Portfolio agent |

Each research item produces a `RESEARCH.md` note consumed by the planning phase before implementation. Findings feed back into the roadmap (the GSD `spec-phase → discuss-phase → plan-phase → execute-phase → verify` loop is the intended carrier).

---

## 8. One-Line Verdict

The system is a sophisticated **advisory chatbot with a dead trading spine**: fix the stacked ~2.5s/order jitter + 4/sec-serialized budgeter (P0), wire the never-called `process_tick`/`execute_order_pre_flight` to a real WebSocket tick producer, replace the LLM-hot-path with a deterministic signal→OCO-order engine, and stand up ICICI Breeze as the 10/sec INR partner behind a `BrokerAdapter` — *then* the multi-agent swarm can be pointed at extracting profit instead of authoring prose nobody executes.
