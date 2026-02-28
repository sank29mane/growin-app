# Growin App: Multi-Agent Systems (MAS) Strategy (2026 SOTA)

This document serves as the centralized knowledge base for the Growin App's multi-agent architecture, coordination, and future roadmap. It is designed to align all agents with the project's long-term vision of becoming a cutting-edge financial intelligence platform.

---

## 1. SOTA Research Summary (2025-2026)

### Key Academic Insights
- **QuantAgents (2025)**: Demonstrated that multi-agent "meetings" (Analyst, Risk Control, Market News, Manager) yield 3x higher returns via collaborative debate compared to single-agent systems.
- **Accuracy Gains**: MAS achieves ~42% better accuracy in complex financial forecasting by simulating the decision processes of multiple human-like roles.
- **Personalized Finance MAS**: Integration of budget optimization agents with investment advisors using news sentiment and technical signals is the current SOTA for retail financial apps.

### Industry Trends
- **Agent-to-Operator Shift**: 2026 is the year where agents move from "predictive tools" to "workflow-integrated operators" with full autonomy in sandbox environments.
- **Trust & Governance**: SOTA systems prioritize **Reasoning Governance**—formal guidelines on how agents route tasks and make decisions.
- **Local Inference Dominance**: Privacy and latency requirements in finance have shifted SOTA toward local deployment on high-performance unified memory architectures (Apple Silicon).

---

## 2. Architecture & Implementation (Phase 16 Baseline)

### Flattened Hierarchy (Unified Orchestration)
As of Phase 16, the system uses a **Unified Orchestrator Agent**. This architecture reduces inter-agent communication overhead by merging routing and reasoning into a single agent lifecycle.
- **Entry Point**: `OrchestratorAgent.py`
- **Parallel Swarm**: Specialists (Quant, Research, etc.) execute concurrently via `asyncio.gather`.
- **Context Injection**: All specialist findings are synthesized into a single `MarketContext`.

### The Critic Pattern (Governance)
We have implemented a mandatory **Review Stage** using the `RiskAgent`.
- **Protocol**: No trade recommendation reaches the user without a JSON-validated risk audit.
- **Safety Gates**: Risk Agent can FLAG or BLOCK suggestions based on exposure, volatility, or compliance rules.

### AG-UI Streaming Protocol
To maintain user trust, the system streams internal state transitions in real-time.
- **Messaging**: `AgentMessenger` broadcasts granular lifecycle events (e.g., `swarm_started`, `risk_review_started`).
- **UX**: SwiftUI `ReasoningTraceView` animates these transitions using `PhaseAnimator`.

---

## 3. Performance & Security (Apple Silicon Optimization)

- **8-bit AFFINE Quantization**: Enforced for all local LLMs (Orchestrator and Risk models) to optimize M4 Pro unified memory performance.
- **Hardware Affinity**: Lightweight routing models are offloaded to the Apple Neural Engine (ANE) via MLX/CoreML.
- **HITL Enforcement**: Trade execution via MCP is protected by HMAC-signed `approval_tokens`, ensuring no autonomous trading without human confirmation.

---

## 4. Future Roadmap

### 🔴 High Priority
- **Agentic Debate**: Implement multi-turn debate between `QuantAgent` and `RiskAgent` for high-stakes trades.
- **Financial Precision Layer**: Standardize all calculations using `Decimal` with 2026-standard rounding (ROUND_HALF_UP).

### 🟡 Medium Priority
- **Sentiment Swarm**: Implement a swarm of micro-agents for high-frequency sentiment analysis across social platforms.
- **Autonomous Goal Rebalancing**: Agents that proactively suggest portfolio adjustments based on long-term goal trajectories.

### 🔵 Low Priority
- **Negotation Agents**: Agents that simulate/negotiate trade execution prices against multiple market makers.

---
**Status**: Strategic Alignment COMPLETE (March 2026)
**Author**: Gemini CLI (Senior AI/ML Engineer)
**Last Updated**: March 1, 2026
