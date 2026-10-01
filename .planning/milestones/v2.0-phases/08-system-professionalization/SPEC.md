# SPEC.md - Phase 08: System Professionalization & Observability

Status: DRAFT
Phase: 08

## Objective
Elevate the Growin App's backend to production-grade standards by implementing structured observability, real-time feedback loops, and a robust architectural foundation.

## 1. Structured & Tamper-Evident Audit Logging
- **Standard**: Follow SOTA 2026 financial logging practices (VeritasChain/BSI TR-02102-1).
- **Integrity**: Implement hash-chaining with a 64-zero Genesis `prev_hash`. Use **SHA-256** on canonical JSON (**RFC 8785**).
- **Precision**: Represent all financial values (price, qty) as **strings** in the JSON payload to prevent float inaccuracies.
- **Scope**: Log all agent transitions, trade validations, and account switches.
- **Resilience**: Design for epoch-based signing to provide forward secrecy and prevent log truncation.

## 2. Real-time Response Streaming (SSE) & Telemetry
- **Standard**: Adopt the **AG-UI Lifecycle Pattern** (RUN_STARTED -> STEP_STARTED -> DATA -> STEP_FINISHED).
- **Transport**: Set `X-Accel-Buffering: no` and use `AsyncGenerator` for non-blocking token delivery.
- **Efficiency**: Use **UUID v7** (time-ordered) for request IDs and implement `STATE_DELTA` updates for telemetry.
- **Backpressure**: Include `recoverable: boolean` and `retryAfterMs: int` in error payloads to manage client-side retries.

## 3. Reasoning Trace UI (Observability)
- **Requirement**: Create a structured data format for the "Reasoning Trace" that can be visualized in the frontend.
- **Detail**: The trace should include agent names, their status (working/ready/error), and a brief summary of their contribution to the final decision.
- **Telemetry**: Leverage `correlation_id` to link all specialist agent actions to a single user request.

## 4. Architectural Refactor: Dependency Injection
- **Requirement**: Reduce reliance on the global `state` object in `app_context.py`.
- **Approach**: Transition toward a Dependency Injection (DI) pattern, where `AppState` is passed into agents and route handlers.
- **Scope**: Start with `CoordinatorAgent` and `DecisionAgent` to make them more testable and less coupled to global state.

## Acceptance Criteria
- [ ] `backend/data/audit.log` contains a verifiable hash chain of events.
- [ ] `/api/chat/message` supports SSE and streams both tokens and agent telemetry.
- [ ] `DecisionAgent` and `CoordinatorAgent` take their dependencies (MCP client, state) as arguments rather than importing `state`.
- [ ] A new endpoint `/api/telemetry/trace/{id}` returns the full reasoning chain for a given request.
