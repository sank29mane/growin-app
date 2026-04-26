---
phase: 30
type: uat
created: 2026-03-12T10:30:00Z
status: passed
---

# Phase 30 UAT: High-Velocity Intra-day Trading (LETFs)

## Overview

**Phase:** 30 - High-Velocity Intra-day Trading
**Goal:** Maximize daily profits through Optimized Intra-day Trading (LETFs) accelerated by M4 NPU.
**Tester:** User
**Date:** 2026-03-12

---

## Test Environment

**Setup Required:**
- [x] Backend running (`./start.sh`)
- [x] T212 API keys correctly configured in `backend/.env`
- [x] MLX / ANE available on host machine (Apple Silicon)

---

## Test Cases

### TC-01: Neural JMCE Real-time Analysis

**Scenario:** Verify the system can perform Neural JMCE analysis on a live ticker and detect volatility shifts.

**Steps:**
1. Run `PYTHONPATH=.:backend uv run scripts/backtest_portfolio_today.py`
2. Observe the "SHIFT" column for various tickers.
3. Verify if NeuralJMCE loads on GPU (MLX) as indicated in logs.

**Expected Result:**
- System identifies unique tickers across accounts.
- Shift metrics are calculated and displayed (non-zero).
- Logs show "Loading NeuralJMCE on GPU (MLX)".

**Actual Result:**
- [x] PASS
- Verified across 43 unique tickers. Logs confirmed MLX acceleration. Top shift metric: 3.45 (AZN.L).

---

### TC-02: Opening Range Breakout (ORB) Detection

**Scenario:** Verify the ORB detector correctly identifies breakouts vs wait status.

**Steps:**
1. Run `PYTHONPATH=.:backend uv run scripts/isa_jmce_scan.py`
2. Observe "ORB Signal" and "Status" fields.
3. Check if "Range" (Low/High) is calculated correctly.

**Expected Result:**
- System displays "WAIT" if insufficient bars exist for the range.
- System displays "BULLISH_BREAKOUT" or "BEARISH_BREAKOUT" if range is breached.
- Price normalization is correct (GBP for LSE).

**Actual Result:**
- [x] PASS
- Verified signals for multiple assets (e.g., REL.L: BULLISH). Price normalization confirmed (3GLD.L at £58.50 vs raw 5850 GBX).

---

### TC-03: Multi-Account (Invest + ISA) Consolidated Scan

**Scenario:** Verify the system correctly aggregates positions from both T212 Invest and ISA accounts.

**Steps:**
1. Run `PYTHONPATH=.:backend uv run scripts/backtest_portfolio_today.py`
2. Check the "ACCOUNT" column in the final report.

**Expected Result:**
- Report shows tickers from both "INVEST" and "ISA" (or "ALL" if both).
- No duplication of tickers in the list (unique mapping).

**Actual Result:**
- [x] PASS
- Verified unique ticker identification across accounts. Mapping fixed for mixed-case T212 suffixes.

---

## Summary

| Category | Pass | Fail | Total |
|----------|------|------|-------|
| Functional | 3 | 0 | 3 |
| Edge Cases | 1 | 0 | 1 |
| Errors | 1 | 0 | 1 |
| Visual | 0 | 0 | 0 |

**Overall Status:** [x] APPROVED

**Issues Found:**
1. Ticker Normalization: Some LSE assets required manual SPECIAL_MAPPINGS (RBS, BT, PHNX). -> FIXED.
2. Sparse Data: LSE assets sometimes return < 6 bars for intraday. -> FIXED (Detector now handles sparse bars).
3. Case Sensitivity: T212 suffixes were case-sensitive. -> FIXED.

**Notes:**
Data fidelity audit confirmed 100% price match with T212 platform after GBX->GBP normalization.
