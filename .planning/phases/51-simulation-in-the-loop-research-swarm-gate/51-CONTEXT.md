# Phase 51: Simulation-in-the-Loop Research & Swarm Gate - Context

**Gathered:** 2026-07-09
**Status:** Ready for planning

<domain>
## Phase Boundary

An in-memory simulation engine integrated into the Growin App algorithmic trading backend. It runs pre-flight trade backtesting and evaluates capital scaling policies (the "risk swarm gate") before submitting orders to the broker.

</domain>

<spec_lock>
## Requirements (locked via SPEC.md)

**16 requirements are locked.** See `51-AI-SPEC.md` for full requirements, boundaries, and acceptance criteria.

Downstream agents MUST read `51-AI-SPEC.md` before planning or implementing. Requirements are not duplicated here.

**In scope (from SPEC.md):** 
- In-memory event-driven simulator built on NumPy/Pandas.
- Pre-flight simulation < 50ms.
- Dynamic Swarm Gate for capital scaling based on GMM regime.
**Out of scope (from SPEC.md):** 
- Complex object-oriented backtesting wrappers like Backtrader or Zipline.

</spec_lock>

<decisions>
## Implementation Decisions

### Simulation State Management
- **D-01:** **Stateless (Pure Functional)**. The engine must maintain 100% thread-safety across concurrent AI tasks, avoiding state leakage by passing portfolio state and tick windows explicitly. Rely on M4 optimizations to keep overhead < 50ms.

### Slippage & Market Impact Modeling
- **D-02:** **Dynamic Spread + Non-Linear Impact**. Hook into Order Book (L2) depth data to model the spread impact of large orders on thin leveraged ETFs (like 3LSE, 3SQQ).

### Swarm Gate Policy Configuration
- **D-03:** **Dynamic Config (SQLite/DuckDB)**. Swarm Gate scaling rules must be queryable dynamically so the Off-Market AI (Phase 53) can update them daily without needing code deployments.

### the agent's Discretion
None

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Architecture
- `.planning/phases/51-simulation-in-the-loop-research-swarm-gate/51-AI-SPEC.md` — Locked requirements, framework selection (NumPy/Pandas Custom Simulator), and evaluation strategy.

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `backend/features/online_vol.py` & `backend/features/online_spread.py`: Utilize the live tick feed features built in Phase 50 for the simulator's historical window.

### Established Patterns
- High-performance numeric processing relying on vectorized NumPy and Numba JIT (as established in Phase 50).

### Integration Points
- `backend/trading_loop.py` — The pre-flight simulator needs to intercept order generation here to scale or block orders before broker dispatch.

</code_context>

<specifics>
## Specific Ideas

No specific requirements — open to standard approaches as long as latency stays < 50ms.

</specifics>

<deferred>
## Deferred Ideas

None — discussion stayed within phase scope.

</deferred>

---

*Phase: 51-Simulation-in-the-Loop Research & Swarm Gate*
*Context gathered: 2026-07-09*
