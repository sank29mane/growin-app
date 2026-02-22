# GSD ROADMAP

This document outlines the high-level phases for the GSD integration and ongoing Growin App development.

---

## Phase 0: GSD Orchestration Setup (Current)
- Scaffolding the core `.gsd` memory files.
- Implementing `.agent/workflows/` slash commands to bootstrap the AI.
- Finalizing the `SPEC.md` for this setup.

## Phase 1: Swarm AI Architecture Alignment
- Audit existing Python backend services (Coordinator Agent, Specialist Swarm, Decision Moderator) against GSD standards.
- Ensure all logic paths use the Apple Silicon MLX local inference models properly.
- Update `/backend/tests/` to reflect strict verification rules defined in `PROJECT_RULES.md`.

## Phase 2: Frontend & Telemetry Integration
- Map the SwiftUI application components to specific GSD wave implementations.
- Enforce the 120Hz smooth interface and accessibility standards across all views.
- Implement structured telemetry and tracing to observe the AI reasoning chain consistently.

## Phase 3: Financial Precision Validation
- Ensure the Rust Native Core and Vectorized Python Agents maintain the 100% accurate P&L and balance tracking.
- Test Decimal arithmetic against edge cases (e.g., zero float errors).
