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

## 2. Architecture & Implementation (Phase 20 Baseline)

### Flattened Hierarchy (Unified Orchestration)
As of Phase 20, the system uses a **Unified Orchestrator Agent**. This architecture reduces inter-agent communication overhead by merging routing and reasoning into a single agent lifecycle.
- **Entry Point**: `OrchestratorAgent.py`
- **Parallel Swarm**: Specialists (Quant, Research, Portfolio, Social) execute concurrently via `asyncio.gather`.
- **Trajectory Stitching**: All specialist findings are synthesized into a single cohesive chronological timeline before reasoning.

### The Critic Pattern & ACE (Governance)
We have implemented a mandatory **Review Stage** using the `RiskAgent`.
- **Adversarial Debate**: Risk Agent acts as 'The Contrarian', forcing the Orchestrator to defend or revise its thesis.
- **ACE Scoring**: An `ACEEvaluator` calculates the robustness of the strategy (0.0 to 1.0) based on debate outcomes.
- **Safety Gates**: Risk Agent can FLAG or BLOCK suggestions based on exposure, volatility, wash-sale rules, or compliance.

### Jules Swarm Engine (Asynchronous Delegation)
- **Containerized Workers**: Heavy-lifting tasks (integration tests, security audits, micro-agent implementation) are dispatched to the `jules` CLI worker.
- **Safety Guardrails**: Strict local-remote alignment checks prevent context-drift regressions from stale remote branches.

---

## 3. Performance & Security (Apple Silicon Optimization)

- **8-bit AFFINE Quantization**: Enforced for all local LLMs (Orchestrator and Risk models) to optimize M4 Pro unified memory performance.
- **Hardware Affinity**: Lightweight routing models are offloaded to the Apple Neural Engine (ANE) via MLX/CoreML.
- **HITL Enforcement**: Trade execution via MCP is protected by HMAC-signed `approval_tokens`, ensuring no autonomous trading without human confirmation.

---

## 4. Future Roadmap

### 🔴 High Priority
- **AI-Driven Dividend Optimization**: Agents focused on maximizing passive income yield while maintaining capital preservation (Phase 21).
- **Macro-Economic Agents**: Integration of Geopolitical Risk (GPR) indices into the baseline context.

### 🟡 Medium Priority
- **Autonomous Goal Rebalancing**: Agents that proactively suggest portfolio adjustments based on long-term goal trajectories.

### 🔵 Low Priority
- **Negotiation Agents**: Agents that simulate/negotiate trade execution prices against multiple market makers.

---
**Status**: Strategic Alignment COMPLETE (March 2026)
**Author**: Gemini CLI (Senior AI/ML Engineer)
**Last Updated**: March 1, 2026
