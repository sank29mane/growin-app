# SPEC.md - Phase 11: SOTA Verification, Edge-Case Hardening & Performance Benchmarking

Status: DRAFT
Phase: 11

## Objective
Establish a rigorous, automated, and SOTA-aligned testing framework to verify the Growin App's resilience, accuracy, and performance. This phase covers unit, integration, and E2E testing with a specific focus on edge cases, concurrency, and hardware-accelerated performance benchmarks.

## Requirements

### 1. Test Automation Framework (SYS-01)
- **Frontend (SwiftUI)**: Implement automated UI tests using `XCUITest` for critical user flows.
- **Backend (Python)**: Use `pytest` for unit/integration tests and `uv` for dependency management.
- **SOTA Streaming Verification**: Integrate **Playwright MCP** to observe DOM state changes during incremental AG-UI streaming (SSE).
- **AI Evaluation**: Use **DeepEval** or **Promptfoo** to verify R-Stitch reasoning traces and output accuracy.

### 2. SOTA Streaming & SSE Stability (SYS-02)
- **Network Resilience**: Test "Network Drop/Resume" scenarios. Verify that `EventSource` correctly resumes sessions using `session_id` without restarting workflows.
- **Error Handoff**: Verify distinction between recoverable (RateLimit/Timeout) and non-recoverable (Validation) errors in the AG-UI stream.
- **Buffering Checks**: Ensure `X-Accel-Buffering: no` is strictly enforced to prevent stream stalling.

### 3. R-Stitch & AI Logic Verification (SYS-03)
- **Entropy Routing**: Verify that the R-Stitch framework correctly delegates high-entropy tokens to the LLM and low-entropy to the SLM.
- **Speedup Benchmarks**: Confirm the 3-4x speedup target for hybrid reasoning trajectories compared to full LLM calls.
- **Smart Merge Validation**: Test "Partial Stream Interrupts" to ensure user corrections are merged into agent state without data loss.

### 4. Concurrency & Optimistic UI (SYS-04)
- **Conflict Resolution**: Test "Rule-Based Precedence" (Human > AI) for simultaneous trade/strategy intents.
- **Graceful Rollbacks**: Verify that failed optimistic updates trigger "emotionally supportive" microcopy and smooth UI reversions.
- **CDC Sync Latency**: Measure sub-second delta synchronization latency across multiple session instances.

### 5. Hardware-Accelerated Performance (SYS-05)
- **120Hz Fluidity**: Benchmark SwiftUI/Metal views under high data density (up to 1M data points).
- **Main-Thread Isolation**: Verify that GPGPU compute tasks (e.g., technical indicators) do not block the UI main thread using OffscreenCanvas/Metal Compute.
- **Thermal/Power Profiling**: Use `Xcode Instruments` (Metal System Trace) and `powermetrics` to monitor efficiency during simultaneous charting and local inference.

## Test Scope & Edge Cases

### Unit Testing
- **Boundary Values**: Zero balances, max integer trade sizes, empty reasoning traces.
- **Invalid Inputs**: Malformed SSE payloads, invalid ticker symbols, expired session IDs.
- **Math Precision**: Decimal arithmetic validation for P&L and cost basis.

### Integration Testing
- **Mocked AG-UI**: Simulate slow, fast, and jittery SSE streams.
- **R-Stitch Transitions**: Test rapid model switching during a single reasoning trajectory.
- **API Resilience**: Verify backend behavior when MCP servers are offline.

### E2E Testing (Playwright/XCUITest)
- **Full Trajectory**: Market Event -> Agent Reasoning -> AG-UI Stream -> User Challenge -> Restitch -> Final Execution.
- **Concurrency**: Simultaneous UI interactions during an active AG-UI stream.

## Acceptance Criteria
- [ ] 100% pass rate for critical path E2E tests.
- [ ] Successful "Network Drop/Resume" verification for SSE.
- [ ] R-Stitch speedup verified to be >3x for complex queries.
- [ ] 120Hz frame rates maintained during high-density rendering (monitored via Performance Overlay).
- [ ] Automated CI/CD pipeline executing all tests on every PR.
- [ ] Comprehensive coverage report showing >80% code coverage.
