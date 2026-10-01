---
phase: 51
slug: simulation-in-the-loop-research-swarm-gate
status: approved
nyquist_compliant: true
wave_0_complete: true
created: 2026-07-09
---

# Phase 51 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 7.4.4 |
| **Config file** | none |
| **Quick run command** | `pytest tests/backend/test_simulator.py -k "test_preflight_latency or test_swarm_gate_blocks"` |
| **Full suite command** | `pytest tests/backend/test_simulator.py` |
| **Estimated runtime** | ~1 second |

---

## Sampling Rate

- **After every task commit:** Run `pytest tests/backend/test_simulator.py -k "test_preflight_latency or test_swarm_gate_blocks"`
- **After every plan wave:** Run `pytest tests/backend/test_simulator.py`
- **Before `/gsd-verify-work`:** Full suite must be green
- **Max feedback latency:** 5 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| 51-01-01 | 01 | 1 | ACC-03 | — | N/A | unit | `pytest tests/backend/test_simulator.py -k "test_slippage_mse_accuracy"` | ✅ | ✅ green |
| 51-01-02 | 01 | 1 | ACC-03 | — | N/A | unit | `pytest tests/backend/test_simulator.py -k "test_swarm_gate_blocks"` | ✅ | ✅ green |
| 51-02-01 | 02 | 2 | ACC-03 | — | N/A | integration | `pytest tests/backend/test_simulator.py` | ✅ | ✅ green |
| 51-02-02 | 02 | 2 | ACC-03 | — | N/A | unit | `pytest tests/backend/test_simulator.py -k "test_preflight_latency"` | ✅ | ✅ green |
| 51-03-01 | 03 | 3 | ACC-03 | — | N/A | unit | `pytest tests/backend/test_simulator.py` | ✅ | ✅ green |
| 51-03-02 | 03 | 3 | ACC-03 | — | N/A | integration | `pytest tests/backend/test_simulator.py` | ✅ | ✅ green |
| 51-04-01 | 04 | 4 | ACC-03 | — | N/A | unit | `pytest tests/backend/test_simulator.py` | ✅ | ✅ green |
| 51-04-02 | 04 | 4 | ACC-03 | — | N/A | unit | `pytest tests/backend/test_simulator.py -k "test_slippage_mse_accuracy"` | ✅ | ✅ green |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

Existing infrastructure covers all phase requirements.

---

## Manual-Only Verifications

All phase behaviors have automated verification.

---

## Validation Sign-Off

- [x] All tasks have `<automated>` verify or Wave 0 dependencies
- [x] Sampling continuity: no 3 consecutive tasks without automated verify
- [x] Wave 0 covers all MISSING references
- [x] No watch-mode flags
- [x] Feedback latency < 5s
- [x] `nyquist_compliant: true` set in frontmatter

**Approval:** approved 2026-07-09
