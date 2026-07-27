---
phase: 53
plan: 04
subsystem: execution
tags: [sqlite, paper-execution, admission, reservations, reconciliation, p256]

requires:
  - phase: 53-02
    provides: durable immutable paper OMS ledger and restart-safe claims
  - phase: 53-03
    provides: purpose-bound signed paper approval
provides:
  - deterministic simulation/risk admission evidence bound to immutable intents
  - atomic Decimal paper budgets and workspace-scoped reservations
  - signed workspace kill-switch clear and typed fill reconciliation
affects: [phase-53-verification, india-rollout, broker-gateway]

tech-stack:
  added: []
  patterns: [SQLite BEGIN IMMEDIATE reservations, append-only evidence, monotonic reconciliation]

key-files:
  created:
    - backend/execution/models.py
    - backend/execution/ledger.py
    - backend/execution/service.py
    - backend/execution/approval.py
    - tests/backend/test_execution_admission.py
    - tests/backend/test_execution_reservations.py
    - tests/backend/test_workspace_kill_switch.py
    - tests/backend/test_execution_reconciliation.py
  modified:
    - tests/backend/test_execution_ledger.py
    - tests/backend/test_signed_approval.py
    - tests/backend/test_signed_approval_routes.py
    - tests/backend/test_durable_execution_service.py
    - tests/backend/test_hitl_trade_routes.py

key-decisions:
  - "Require explicit workspace/account/currency paper budgets; no default buying power is invented."
  - "Reject SELL admission until position reservations are separately scoped."
  - "UNKNOWN keeps the active reservation and cannot be retried without typed reconciliation evidence."
  - "Workspace control clearing uses a separate purpose-bound P-256 challenge, never an order signature."

requirements-completed: [ACC-03, SAFE-01, EXEC-03]

duration: ~2h 30m
completed: 2026-07-22
---

# Phase 53 Plan 04: Paper OMS Admission, Reservations, Controls, and Reconciliation Summary

**Paper execution now follows deterministic admission → explicit Decimal reservation → signed approval/claim → PaperDispatcher → typed reconciliation, with UNKNOWN and workspace controls fail-closed.**

## Performance

- **Tasks:** 4 complete
- **Files modified/created in plan:** 13
- **Full backend suite:** 327 passed, 13 skipped, 24 warnings

## Accomplishments

- Added immutable simulator/risk admission records with evidence hashes, finite Decimal conversion, stale/zero/non-finite rejection, and SELL fail-closed behavior.
- Added durable explicit paper budgets, atomic reservations, monotonic workspace controls, and a distinct signed control-clear ceremony.
- Added exact monotonic reconciliation for acknowledgement, partial fill, fill, cancel, rejection, and UNKNOWN outcomes, including reservation accounting and paper positions.
- Migrated signed approval, durable service, route, ledger, and HITL fixtures away from unsigned or Trading 212 mutation paths.

## Task Commits

1. **Task 1: Enforce deterministic admission before any reservation or dispatch** - `2e93456`
2. **Task 2: Add atomic paper buying-power reservations and workspace controls** - `12e60a3`
3. **Task 3: Implement exact order, fill, cancel, and UNKNOWN reconciliation** - `2616b7d`
4. **Task 4: Run the Phase 53 integrity regression and containment gate** - verification-only (no source commit)

Additional scoped cleanup/export commits: `4410403`, `27b0a6f`.

## Verification

- Focused admission/reservation/control/reconciliation tests: **15 passed**.
- Integrated ledger/signed approval/routes/durable/HITL tests: **35 passed, 7 skipped** (legacy Trading 212 mutation fixtures are explicitly skipped and replaced by signed paper coverage).
- Broker containment and signed route regressions: **20 passed**.
- Full backend suite: **327 passed, 13 skipped, 24 warnings**.
- Ruff passes for all plan-owned execution and new test files.
- Exact scoped Ruff command reports four pre-existing `backend/trading212_mcp_server.py` import-order/duplicate-import findings; no plan-owned Ruff findings remain.
- `git diff --check` passes for plan-owned files.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Preserve active reservation during UNKNOWN reconciliation**
- **Found during:** Task 3
- **Issue:** The first implementation labeled the reservation `UNKNOWN`, which obscured that funds remained actively held.
- **Fix:** Keep reservation state `ACTIVE` while the order is UNKNOWN; retry remains blocked by order state until evidence resolves it.
- **Files modified:** `backend/execution/ledger.py`
- **Verification:** `test_unknown_retains_reservation_and_requires_evidence` and full suite pass.
- **Committed in:** `27b0a6f`

**2. [Rule 2 - Missing critical] Force public unsigned approval to fail closed**
- **Found during:** Task 2 fixture migration
- **Issue:** The legacy public `approve` path could dispatch without signed approval when `require_approval=False`.
- **Fix:** Public approval now always returns the existing signed-approval-required execution error; only the signed paper path can claim/dispatch.
- **Files modified:** `backend/execution/service.py`, plan-owned route fixtures
- **Verification:** durable service and HITL route tests pass; no dispatcher is called.
- **Committed in:** `2e93456`, `12e60a3`

**Total deviations:** 2 auto-fixed.

## Issues Encountered

- The mandated `uv` runner initially could not access its existing cache in the sandbox; the same commands were rerun with approved escalation.
- Legacy HITL Trading 212 mutation tests were retained only as explicitly skipped historical fixtures; the active route regression asserts fail-closed behavior and no broker call.

## User Setup Required

None. No credentials, broker transport, network mutation, or package installation was used.

## Next Phase Readiness

Automated paper integrity gates are green. Manual macOS UAT remains pending: exercise the local software P-256 Keychain signer and review the approval UI; Secure Enclave/Touch ID enrollment remains deferred hardware verification. Trading 212 remains default-disabled/read-only and no live dispatcher is enabled.

---
*Phase: 53 — Execution Integrity Containment*
*Plan: 04 — Paper OMS Admission, Reservations, Controls, and Reconciliation*

## Self-Check: PASSED

- Summary file exists.
- All task and cleanup commit hashes resolve in Git history.
