# Growin App: Comprehensive HFT Audit & Roadmap
## Semi-High-Frequency Intraday Trading (10 OPS) | Multi-Agentic Portfolio Management | ICICI Breeze API Transition

---

## Executive Summary

**Current State**: Growin is a sophisticated **AI-powered portfolio intelligence platform** with advanced MLX local inference, Neural JMCE regime detection, and a 6-agent specialist swarm. However, it is **architected for portfolio analysis and advisory**, not for **10 orders/second semi-HFT execution**.

**Critical Gap**: The architecture lacks:
- Sub-millisecond order routing & execution layer
- Direct exchange connectivity (colocation/proximity)
- Market microstructure models (queue position, adverse selection)
- Indian market compliance (SEBI algo framework, static IP, 10 OPS limit)
- ICICI Breeze API integration (replacing Trading 212)

**Target**: Transform into a **profit-maximizing semi-HFT system** operating at ≤10 OPS on NSE/BSE via ICICI Breeze, with every trade extracting maximum alpha through multi-agent consensus.

---

## Part 1: Architecture Audit — Critical Flaws & Gaps

### 1.1 Execution Layer — **CRITICAL: Missing Entirely**

| Component | Current State | Required for 10 OPS | Gap Severity |
|-----------|---------------|---------------------|--------------|
| Order Management System (OMS) | ❌ None (only pre-flight sim) | Smart OMS with order lifecycle mgmt | 🔴 CRITICAL |
| Execution Algorithms | ❌ None | TWAP, VWAP, POV, Implementation Shortfall, Adaptive | 🔴 CRITICAL |
| Smart Order Router (SOR) | ❌ None | Venue selection, sweep logic, dark pool access | 🔴 CRITICAL |
| Latency Budget | ~100ms (MCP → HTTP) | <5ms end-to-end (tick-to-fill) | 🔴 CRITICAL |
| Colocation/Proximity | ❌ Local macOS | AWS Mumbai / NSE colo / BSE colo | 🔴 CRITICAL |
| Market Data Feed | yfinance/Alpaca (slow) | NSE/BSE tick-by-tick (TBQ/TBT) or Breeze WS | 🔴 CRITICAL |
| Order Book Reconstruction | ❌ None | Full L2/L3 depth, queue position tracking | 🔴 CRITICAL |

**Evidence**: `backend/trading_loop.py` only has `PreFlightSimulator` and `AdaptiveReQuoter` — **no actual broker dispatch**. `t212_handlers.py` uses REST over HTTP to localhost MCP server (adds ~50-100ms latency).

### 1.2 Risk & Compliance — **CRITICAL: Non-Compliant for India**

| Requirement | Current | Required (SEBI/NSE Aug 2025) | Gap |
|-------------|---------|------------------------------|-----|
| Static IP Whitelisting | ❌ None | Mandatory (primary + secondary) | 🔴 |
| OAuth 2.0 / 2FA | ❌ Basic MCP | Mandatory for all API access | 🔴 |
| Order Tagging (Algo ID) | ❌ None | Generic ID for ≤10 OPS; Unique ID if >10 | 🔴 |
| Session Auto-Logout EOD | ❌ None | Mandatory | 🔴 |
| 5-Year Audit Trail | ❌ Partial | Mandatory | 🔴 |
| OTR (Order-to-Trade Ratio) Monitoring | ❌ None | SEBI circular Apr 2026 | 🟡 |
| Throttle Limits (BSE/NSE) | ❌ None | 40 MPS free (BSE), tiered pricing | 🟡 |

### 1.3 Multi-Agent Swarm — **HIGH: Not Designed for HFT Consensus**

| Agent | Current Role | HFT Requirement | Gap |
|-------|--------------|-----------------|-----|
| `QuantAgent` | Technical indicators (5-10ms) | **Microstructure alpha** (toxicity, VPIN, queue position) | 🔴 |
| `ForecastingAgent` | TTM-R2 (seconds) | **Sub-second regime-aware forecasts** | 🔴 |
| `RiskAgent` | Post-trade audit (Critic) | **Pre-trade risk gate** (sub-ms) | 🔴 |
| `WhaleAgent` | Block trade analysis | **HFT flow toxicity detection** | 🔴 |
| `CoordinatorAgent` | LLM routing (seconds) | **Deterministic consensus** (no LLM in hot path) | 🔴 |
| `StrategySuggestionEngine` | Proactive alpha | **Real-time signal synthesis** | 🟡 |

**Key Finding**: The 2-stage streaming orchestrator (`SwarmOrchestrator`) uses **LLM inference in the hot path** (reflex + synthesis). At 10 OPS, **LLM latency (50-200ms) exceeds inter-order interval (100ms)**. **Must remove LLM from execution hot path**.

### 1.4 ML/Inference Stack — **MEDIUM: Good for Research, Not Execution**

| Component | Current | HFT Requirement |
|-----------|---------|-----------------|
| MLX/Gemma 4 26B | Local inference (GPU) | **Too slow** for per-trade decisions |
| Neural JMCE (ANE) | Regime detection (<10ms) | ✅ Usable for regime gating |
| QLoRA Hot-swap | 10-50ms | Too slow for regime change reaction |
| Rust Core (optional) | Vectorized math | ✅ Leverage for hot path |

**Verdict**: ML stack is excellent for **pre-market regime classification, overnight training, signal generation** — **not for per-tick execution decisions**.

### 1.5 Data Layer — **HIGH: Wrong Data for HFT**

| Source | Latency | HFT Suitability |
|--------|---------|-----------------|
| yfinance | Seconds-minutes | ❌ Useless |
| Alpaca | ~100ms (US) | ❌ Wrong market |
| Trading 212 MCP | ~50-100ms | ❌ Wrong market, REST |
| Breeze WebSocket | **Sub-second (NSE)** | ✅ Required |
| NSE TBT/TBQ | **Microseconds** | ✅ Ideal (colo only) |

---

## Part 2: ICICI Breeze API Transition — Complete Integration Plan

### 2.1 Regulatory Requirements (SEBI/NSE Circular Aug 2025)

**Mandatory for ALL API Trading (Effective Aug 1, 2025):**
1. **Static IP** — Primary + Secondary (max 1 change/week)
2. **OAuth 2.0 + 2FA** — No open APIs, broker-whitelisted IPs only
3. **Daily Session Token** — Expires at midnight, must regenerate daily (TOTP supported)
4. **Order Tagging** — Generic Algo ID for ≤10 OPS (unregistered); Unique ID if >10 OPS
5. **Rate Limits** — 10 orders/sec per exchange/segment (hard limit)
6. **No Market Orders** — Limit orders only
7. **Auto-Logout EOD** — Sessions terminated at market close
8. **5-Year Audit Logs** — All API activity logged

### 2.2 Breeze API Capabilities & Limits

| Feature | Detail |
|---------|--------|
| **Rate Limit** | 75 calls/min, 2000 calls/day (REST); WS separate |
| **Order Limit** | 10 OPS per exchange (NSE/BSE/NFO) |
| **Order Types** | LIMIT only (no MARKET) |
| **Segments** | NSE (EQ), BSE (EQ), NFO (F&O), NDX (Currency), MCX (Commodity) |
| **Data** | 1-sec OHLCV, Option Chain, Live WS (1s/1m/5m/30m) |
| **History** | 10 years (1-sec granularity available) |
| **SDKs** | Python (`breeze-connect`), Java, Node.js |
| **Auth** | App Key + Secret + Daily Session Token (checksum: SHA256(ts + payload + secret)) |
| **WebSocket** | Streaming quotes, depth, order updates |

### 2.3 Python Integration Architecture

```python
# Required new modules (backend/brokers/breeze/)
breeze/
├── __init__.py
├── client.py           # BreezeConnect wrapper with async support
├── auth.py             # Session token mgmt, TOTP, daily refresh
├── websocket.py        # Async WS client for live data/orders
├── order_manager.py    # OMS: limit orders, modifications, cancellations
├── rate_limiter.py     # Token bucket: 10 OPS hard limit + 75 RPM REST
├── compliance.py       # SEBI checks: static IP, algo ID tagging, OTR
├── models.py           # Pydantic models for orders, positions, quotes
├── exceptions.py       # Breeze-specific errors
└── security_master.py  # Token mapping (download daily from ICICI)
```

### 2.4 Migration Strategy: Trading 212 → ICICI Breeze

| Phase | Action | Timeline |
|-------|--------|----------|
| **0** | Open ICICI Direct account, register Breeze app, get static IP (AWS Mumbai t3.micro + Elastic IP) | Week 1 |
| **1** | Build `BreezeClient` with async WS, OAuth, daily token refresh | Week 2 |
| **2** | Implement `BreezeOrderManager` with 10 OPS rate limiter, limit-order enforcement | Week 2-3 |
| **3** | Port `PortfolioAgent` → `BreezePortfolioAgent` (positions, margins, holdings) | Week 3 |
| **4** | Build `BreezeMarketData` feed (WS 1-sec OHLCV + Option Chain) | Week 3-4 |
| **5** | Compliance layer: algo ID tagging, static IP verification, audit logging | Week 4 |
| **6** | Integration testing: paper trading → live with ₹1L capital | Week 5-6 |
| **7** | Deprecate `t212_handlers.py`, `trading212_mcp_server.py` | Week 6 |

---

## Part 3: HFT Architecture Redesign — 10 OPS Target

### 3.1 Required Latency Budget (End-to-End: Tick → Fill)

```
Target: <50ms median, <100ms p99 (well within 100ms inter-order interval at 10 OPS)

┌─────────────────────────────────────────────────────────────────────┐
│                        LATENCY BUDGET                                │
├────────────────────┬──────────────┬──────────────┬─────────────────┤
│ Stage              │ Target       │ Current      │ Action          │
├────────────────────┼──────────────┼──────────────┼─────────────────┤
│ Market Data Ingest │ <1ms         │ ~50ms (REST) │ Breeze WS       │
│ Signal Computation │ <5ms         │ ~10ms (Quant)│ Rust/MLX hotpath│
│ Risk Pre-Check     │ <1ms         │ ~50ms (LLM)  │ Deterministic   │
│ Order Construction │ <1ms         │ N/A          │ New OMS         │
│ Network (AWS→NSE)  │ ~2-5ms       │ N/A          │ Colo/Proximity  │
│ Exchange Match     │ ~1-3ms       │ N/A          │ Limit orders    │
│ Fill Confirmation  │ <1ms (WS)    │ N/A          │ Breeze WS       │
├────────────────────┼──────────────┼──────────────┼─────────────────┤
│ TOTAL              │ <15ms        │ ~200ms+      │ 13x improvement │
└────────────────────┴──────────────┴──────────────┴─────────────────┘
```

### 3.2 New Hot Path Architecture (Zero-LLM in Execution)

```
┌────────────────────────────────────────────────────────────────────┐
│                    HFT EXECUTION HOT PATH                          │
├────────────────────────────────────────────────────────────────────┤
│                                                                    │
│  Breeze WS (1-sec ticks)                                           │
│       │                                                            │
│       ▼                                                            │
│  ┌─────────────┐    <1ms    ┌─────────────┐    <5ms    ┌────────┐ │
│  │  Tick       │ ─────────▶ │  Signal     │ ─────────▶ │  Risk  │ │
│  │  Normalizer │            │  Engine     │            │  Gate  │ │
│  └─────────────┘            │  (Rust/MLX) │            │ (C++)  │ │
│                             └─────────────┘            └────┬───┘ │
│                                    │                        │      │
│                                    ▼                        ▼      │
│                             ┌─────────────┐    <1ms    ┌────────┐ │
│                             │  Smart      │ ─────────▶ │  OMS   │ │
│                             │  Order      │            │  (Rust)│ │
│                             │  Router     │            └────┬───┘ │
│                             └─────────────┘                 │      │
│                                    │                        ▼      │
│                                    │                  ┌────────┐  │
│                                    └─────────────────▶ │ Breeze │  │
│                                                       │ REST   │  │
│                                                       │ + WS   │  │
│                                                       └────────┘  │
│                                                                    │
└────────────────────────────────────────────────────────────────────┘

PARALLEL (Async, Non-Blocking): Agent Swarm for Signal Generation
├── QuantAgent (microstructure: VPIN, toxicity, queue pos)
├── ForecastingAgent (Neural JMCE regime + 1-sec horizon)
├── WhaleAgent (HFT flow detection via WS depth)
└── RiskAgent (Pre-trade: position limits, drawdown, OTR)
    │
    └─▶ Feeds Signal Engine via Redis Streams / shared memory
```

### 3.3 Technology Stack for Hot Path

| Layer | Technology | Rationale |
|-------|------------|-----------|
| **Market Data** | `breeze-connect` WS + `asyncio` | Native async, sub-second |
| **Signal Engine** | **Rust** (via `pyo3`/`maturin`) + **MLX** (ANE) | Sub-ms, zero-GC, SIMD |
| **Risk Gate** | **C++** or **Rust** (embedded in Python) | Deterministic, <1ms |
| **OMS/SOR** | **Rust** (`tokio`, `crossbeam`) | Lock-free, high throughput |
| **Transport** | Breeze REST (orders) + WS (fills) | Compliance + speed |
| **State/Coordination** | **Redis Streams** (local) | Lock-free pub/sub, persistence |
| **Logging/Audit** | **Apache Arrow** / **Parquet** (local) | Columnar, 5-year retention |

---

## Part 4: Multi-Agentic Profit-Maximization Framework

### 4.1 Agent Roles Redefined for HFT

| Agent | New HFT Role | Input | Output | Latency Budget |
|-------|--------------|-------|--------|----------------|
| **MicrostructureQuant** | VPIN, order flow toxicity, queue position, adverse selection | L2/L3 WS depth, trades | `Signal{alpha_bps, confidence, horizon_ms}` | <5ms |
| **RegimeForecaster** | Neural JMCE (ANE) → regime probs + 1-sec return forecast | 1-sec bars, cov velocity | `RegimeSignal{regime_probs, forecast_bps}` | <10ms |
| **FlowWhale** | HFT footprint detection, sweep prediction, iceberg detection | WS depth updates, trade tape | `FlowSignal{toxicity, sweep_prob}` | <5ms |
| **RiskGate** | **Deterministic** pre-trade checks (no LLM) | Position, PnL, drawdown, OTR, limits | `RiskDecision{allow, max_size, reason}` | <1ms |
| **PortfolioOptimizer** | **Intraday** rebalancing, delta-neutral overlays, hedge ratios | Net positions, regime, risk budget | `TargetPortfolio{weights, hedge_qty}` | <50ms (async) |
| **StrategySynthesizer** | **Consensus engine** (weighted voting, not LLM) | All signals + risk decision | `ExecutionPlan{orders[], priority}` | <5ms |

### 4.2 Consensus Mechanism (Replaces LLM Orchestrator)

```python
# backend/agents/consensus.py
class SignalConsensus:
    """
    Deterministic weighted voting — NO LLM in hot path.
    Weights learned offline via backtest optimization.
    """
    WEIGHTS = {
        "microstructure": 0.35,
        "regime": 0.25,
        "flow": 0.20,
        "portfolio": 0.15,
        "risk": 0.05,  # veto power
    }
    
    VETO_AGENTS = {"risk"}  # Any veto = block
    
    def decide(self, signals: Dict[str, Signal], risk_decision: RiskDecision) -> ExecutionPlan:
        # 1. Risk veto check
        if not risk_decision.allow:
            return ExecutionPlan(blocked=True, reason=risk_decision.reason)
        
        # 2. Weighted alpha aggregation
        total_alpha = sum(s.alpha_bps * self.WEIGHTS[k] for k, s in signals.items())
        total_conf = sum(s.confidence * self.WEIGHTS[k] for k, s in signals.items())
        
        # 3. Threshold gating
        if total_alpha < self.min_alpha_bps or total_conf < self.min_confidence:
            return ExecutionPlan(blocked=True, reason="Insufficient conviction")
        
        # 4. Size from risk gate
        qty = risk_decision.max_size
        
        # 5. Construct order(s)
        return ExecutionPlan(
            orders=[Order(ticker, side, qty, order_type="LIMIT", 
                         price=signals["microstructure"].fair_price)],
            priority=total_conf,
            metadata={"alpha_bps": total_alpha, "confidence": total_conf}
        )
```

### 4.3 Profit-Maximization Per Trade — Microstructure Edge

| Edge Source | Implementation | Expected Alpha (bps/trade) |
|-------------|----------------|---------------------------|
| **Queue Position** | Track L2 depth, estimate fill probability at each price level | 2-5 |
| **Adverse Selection (VPIN)** | Volume-synchronized PIN; avoid toxic flow | 3-8 |
| **Spread Capture** | Post limit orders at bid/ask (maker rebates where available) | 1-3 |
| **Regime Timing** | Neural JMCE regime probs → scale aggression | 5-15 |
| **Flow Toxicity** | Whale sweep detection → front-run or avoid | 5-20 |
| **Cross-Venue Arb** | NSE vs BSE latency arb (if colo both) | 2-10 |
| **Option Hedging** | Delta-neutral gamma scalping on NFO | 10-50 |

**Total Target**: **15-50 bps/trade** net of costs (brokerage + STT + exchange fees + slippage)

---

## Part 5: Deep Research Plan — Per Aspect

### 5.1 Market Microstructure (NSE/BSE Specific)
- [ ] **Order Book Dynamics**: NSE NEAT vs BSE BOLT matching engine differences
- [ ] **Queue Position Modeling**: NSE price-time priority, disclosed quantity (iceberg) mechanics
- [ ] **VPIN/Toxicity Calibration**: Indian market volume buckets, 1-sec vs 1-min horizons
- [ ] **Auction Sessions**: Pre-open (9:00-9:15), Closing (3:30-3:40) — special handling
- [ ] **F&O Microstructure**: NFO liquidity, option chain dynamics, gamma exposure
- [ ] **References**: NSE Tick-by-Tick (TBT) spec, BSE ETI manual, academic papers on Indian HFT

### 5.2 ICICI Breeze API Deep Dive
- [ ] **WebSocket Protocol**: Message formats, heartbeat, reconnection, order update channels
- [ ] **Checksum Algorithm**: Exact SHA256 implementation (timestamp + JSON + secret)
- [ ] **Session Token**: TOTP automation (SEBI allows), daily refresh scheduler
- [ ] **Rate Limit Semantics**: 75 RPM REST burst vs sustained; WS separate limits
- [ ] **Error Codes**: Retryable vs fatal; order rejection codes (RMS, OPS limit, etc.)
- [ ] **Security Master**: Daily download, token mapping, symbol changes
- [ ] **Sandbox/UAT**: `https://uatapi.icicidirect.com` for testing

### 5.3 SEBI/NSE Compliance Architecture
- [ ] **Static IP Management**: AWS Elastic IP + route53 health checks + automated failover
- [ ] **Algo ID Tagging**: Generic ID format, registration flow for >10 OPS
- [ ] **OTR Monitoring**: Real-time order/trade ratio calculation per exchange
- [ ] **Audit Log Schema**: 5-year retention, immutable storage (S3 + Glacier)
- [ ] **Kill Switch**: Exchange-mandated, broker-implemented, test monthly
- [ ] **Cybersecurity**: OAuth 2.0 + 2FA implementation, password rotation

### 5.4 Infrastructure & Deployment
- [ ] **AWS Mumbai Architecture**: 
  - EC2 C6i/C7i (compute optimized) for signal engine
  - ElastiCache Redis (cluster mode) for state
  - MSK (Kafka) for audit streams
  - VPC + TGW for static IP egress
- [ ] **Colocation Options**: NSE Colocation (EMC), BSE Colocation, AWS Direct Connect
- [ ] **Network Tuning**: `tcp_low_latency`, `busy_poll`, kernel bypass (DPDK/Solarflare) if colo
- [ ] **Monitoring**: Prometheus + Grafana (latency percentiles, OPS, fill rate, PnL attribution)

### 5.5 ML/Model Research
- [ ] **Neural JMCE on NSE Data**: Retrain on 1-sec NSE data (Breeze provides)
- [ ] **Regime-Specific Adapters**: QLoRA per regime (bull/bear/sideways/high-vol)
- [ ] **Microstructure Features**: Order flow imbalance, trade sign autocorrelation, Kyle's lambda
- [ ] **Online Learning**: Incremental GMM updates, concept drift detection
- [ ] **Backtest Framework**: Event-driven, realistic fill simulation (queue position, latency)

---

## Part 6: Phased Implementation Roadmap

### Phase 0: Foundation (Weeks 1-2) ✅ **START HERE**
| Task | Owner | Deliverable |
|------|-------|-------------|
| Open ICICI Direct account, register Breeze app | You | App Key, Secret Key |
| Provision AWS Mumbai: EC2 (c6i.2xlarge), Elastic IP, VPC | DevOps | Static IP whitelisted |
| Set up `breeze-connect` dev environment | Backend | `pip install breeze-connect` |
| Implement `BreezeAuth` (daily token + TOTP) | Backend | Auto-refresh at 00:00 IST |
| Download & parse Security Master | Backend | `token → symbol` mapping DB |

### Phase 1: Market Data & Connectivity (Weeks 2-4)
| Task | Owner | Deliverable |
|------|-------|-------------|
| `BreezeWebSocketClient`: async WS, auto-reconnect, heartbeat | Backend | Live 1-sec OHLCV + depth |
| `BreezeMarketDataFeed`: normalize → internal `Tick` format | Backend | Unified feed interface |
| Replace `yfinance/Alpaca` in `QuantAgent` with Breeze feed | Backend | Sub-second data in agents |
| Implement 10 OPS token bucket rate limiter | Backend | Hard limit enforcement |
| Paper trading: subscribe to WS, log all ticks | QA | Data quality report |

### Phase 2: Execution Layer (Weeks 4-7)
| Task | Owner | Deliverable |
|------|-------|-------------|
| `BreezeOrderManager`: LIMIT orders, modify, cancel, OCO | Backend | Full order lifecycle |
| `SmartOrderRouter`: venue selection (NSE vs BSE), sweep logic | Backend | Best execution |
| `RiskGate` (Rust): position limits, drawdown, OTR, kill switch | Backend/Rust | <1ms pre-trade check |
| `OMS` (Rust): order state machine, fill reconciliation | Backend/Rust | Audit-ready |
| Compliance: algo ID tagging, static IP verification, audit logs | Backend | SEBI-compliant |

### Phase 3: Signal Engine & Consensus (Weeks 7-10)
| Task | Owner | Deliverable |
|------|-------|-------------|
| Port `QuantAgent` → `MicrostructureQuant` (Rust + MLX) | Quant/ML | VPIN, toxicity, queue pos |
| Neural JMCE retrain on NSE 1-sec data → ANE/CoreML | ML | Regime probs <10ms |
| `FlowWhale` agent: HFT flow detection from WS depth | Quant | Sweep/iceberg signals |
| `SignalConsensus`: deterministic weighted voting | Backend | Replaces LLM orchestrator |
| Integration: Redis Streams for signal bus | Backend | Lock-free pub/sub |

### Phase 4: Portfolio & Risk Management (Weeks 10-12)
| Task | Owner | Deliverable |
|------|-------|-------------|
| `PortfolioOptimizer`: intraday rebalance, delta-neutral hedges | Quant | Target weights per regime |
| `RiskAgent`: real-time PnL, Greeks (F&O), scenario analysis | Risk | Pre-trade + intra-trade |
| Margin monitoring: SPAN + exposure, auto-liquidation triggers | Risk | Breeze margin API integration |
| Tax-loss harvesting (India: STCG/LTCG, Section 111A/112A) | Tax | Automated EOD |

### Phase 5: Testing & Go-Live (Weeks 12-16)
| Task | Owner | Deliverable |
|------|-------|-------------|
| Backtest: event-driven, 1-year NSE data, realistic fills | Quant | Sharpe >2, max DD <5% |
| Paper trading: 2 weeks, ₹10L notional | QA | Live signal quality |
| Load test: 10 OPS sustained, chaos engineering (network fail) | DevOps | <100ms p99 latency |
| SEBI compliance audit: static IP, OAuth, logs, kill switch | Compliance | Sign-off |
| Go-live: ₹50L capital, ramp over 4 weeks | You | Live PnL tracking |

---

## Part 7: Code-Level Refactoring Plan

### 7.1 Files to DELETE (Trading 212 Legacy)
```
backend/trading212_mcp_server.py
backend/t212_handlers.py
backend/mcp_client.py (replace with BreezeClient)
backend/routes/mcp_routes.py
backend/utils/ticker_utils.py (T212-specific mappings)
```

### 7.2 Files to REFACTOR (Core → HFT)
| File | Changes |
|------|---------|
| `backend/trading_loop.py` | Remove `PreFlightSimulator` → integrate `RiskGate` + `OMS` |
| `backend/simulation/engine.py` | Replace with `SignalEngine` (Rust) |
| `backend/simulation/requoter.py` | Replace with `SmartOrderRouter` |
| `backend/agents/orchestrator.py` | Replace `SwarmOrchestrator` with `SignalConsensus` |
| `backend/agents/quant_agent.py` | Rewrite as `MicrostructureQuant` (Rust) |
| `backend/agents/risk_agent.py` | Rewrite as `RiskGate` (deterministic, no LLM) |
| `backend/agents/portfolio_agent.py` | Port to `BreezePortfolioAgent` |
| `backend/quant_engine.py` | Split: `QuantEngine` (research) vs `SignalEngine` (hot path) |

### 7.3 New Files to CREATE
```
backend/brokers/breeze/           # Complete Breeze integration
backend/execution/                # Hot path: OMS, SOR, RiskGate (Rust)
backend/signals/                  # MicrostructureQuant, RegimeForecaster, FlowWhale
backend/consensus/                # SignalConsensus, ExecutionPlanner
backend/compliance/               # SEBI: StaticIP, AlgoID, OTR, AuditLog
backend/infrastructure/           # Redis Streams, Kafka, monitoring
backend/rust/                     # Cargo workspace for hot path
```

---

## Part 8: Risk Assessment & Mitigation

| Risk | Likelihood | Impact | Mitigation |
|------|------------|--------|------------|
| **SEBI regulation changes** (stricter OPS) | High | High | Design for 5 OPS headroom; modular registration |
| **Breeze API instability** (downtime, bugs) | Medium | High | Fallback: manual trading; multi-broker (Zerodha/Upstox) |
| **Static IP failure** (ISP, AWS) | Medium | High | Secondary IP + automated failover (Route53 health checks) |
| **Model decay** (regime change, alpha decay) | High | Medium | Online learning; monthly retrain; A/B testing |
| **Latency spikes** (GC, network) | Medium | High | Rust hot path; `jemalloc`; busy polling; colo |
| **Capital loss** (bug, flash crash) | Low | Critical | Kill switch; max position limits; daily loss limit |
| **Talent/knowledge gap** (Rust, HFT, Indian market) | Medium | High | Hire/consult; pair programming; extensive docs |

---

## Part 9: Success Metrics (KPIs)

| Metric | Target | Measurement |
|--------|--------|-------------|
| **End-to-end latency (tick→fill)** | <50ms p50, <100ms p99 | Telemetry (Rust → Redis → Grafana) |
| **Order fill rate** | >95% (limit orders) | OMS audit logs |
| **Slippage vs mid** | <2 bps | Fill price vs arrival mid |
| **Net alpha per trade** | >15 bps | PnL attribution (signal vs market) |
| **Daily Sharpe (intraday)** | >2.0 | Rolling 30-day |
| **Max intraday drawdown** | <2% | Real-time risk monitor |
| **SEBI compliance** | 100% | Audit log review |
| **System uptime (market hours)** | 99.9% | Prometheus alerts |

---

## Part 10: Immediate Next Steps (This Week)

1. **[ ]** Open ICICI Direct account → Register Breeze app → Get static IP (AWS Mumbai)
2. **[ ]** Create `backend/brokers/breeze/` module structure
3. **[ ]** Implement `BreezeAuth` with daily TOTP token refresh (cron at 00:05 IST)
4. **[ ]** Build `BreezeWebSocketClient` with auto-reconnect + heartbeat
5. **[ ]** Download Security Master → Parse into DuckDB `security_master` table
6. **[ ]** Write integration test: subscribe to NIFTY 1-sec WS, log 10k ticks
7. **[ ]** Benchmark: measure WS tick-to-callback latency (target <1ms)

---

## Appendix A: Breeze API Quick Reference

```python
# Minimal working example (from ICICI docs)
from breeze_connect import BreezeConnect

breeze = BreezeConnect(api_key="YOUR_APP_KEY")
# Generate session via browser: https://api.icicidirect.com/apiuser/login?api_key=YOUR_APP_KEY
# Get session_token from redirect
breeze.generate_session(api_secret="YOUR_SECRET", session_token="SESSION_FROM_REDIRECT")

# WebSocket
breeze.ws_connect()
breeze.subscribe_feeds(exchange_code="NSE", stock_code="NIFTY", 
                       product_type="", get_exchange_quotes=True, 
                       get_market_depth=True, interval="1second")

# Place LIMIT order (MARKET not allowed)
breeze.place_order(
    stock_code="RELIANCE",
    exchange_code="NSE",
    product="cash",           # or "margin" for intraday
    action="buy",
    order_type="limit",
    quantity="1",
    price="2500.00",          # LIMIT price mandatory
    validity="day",
    disclosed_quantity="0",   # iceberg
    validity_date=""          # ignored for day orders
)
```

---

## Appendix B: SEBI Compliance Checklist (Pre-Go-Live)

- [ ] Static IP registered with ICICI (primary + secondary)
- [ ] OAuth 2.0 + 2FA implemented (no basic auth)
- [ ] Daily session token auto-refresh (TOTP)
- [ ] Algo ID tagged on every order (generic for ≤10 OPS)
- [ ] 10 OPS rate limiter enforced (token bucket)
- [ ] LIMIT orders only (no MARKET)
- [ ] Auto-logout at 15:35 IST (EOD)
- [ ] 5-year immutable audit logs (S3 + Glacier)
- [ ] Kill switch tested monthly
- [ ] OTR monitoring dashboard
- [ ] Broker (ICICI) compliance sign-off obtained

---

**Document Version**: 1.0  
**Date**: July 10, 2026  
**Status**: DRAFT — For Review & Execution  
**Owner**: Sanket Mane / Growin App Team