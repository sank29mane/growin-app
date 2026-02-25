# Documentation and Log Audit (Phase 07-02)

This document catalogs and evaluates root-level documentation, logs, and secondary directories for retention, consolidation, or deletion.

## Root Markdown Files (.md)

| File | Status | Recommendation | Rationale |
| :--- | :--- | :--- | :--- |
| `README.md` | **Current** | Retain | Primary project entry point. |
| `PROJECT_RULES.md` | **Current** | Retain | Source of truth for GSD methodology in this repo. |
| `ARCHITECTURE.md` | **Current** | Move to `docs/` | Contains high-level design; better placed in documentation folder. |
| `TODO.md` | **Obsolete** | Delete | Superceded by `.planning/todos/` and `STATE.md`. |
| `2-UAT.md` | **Archive** | Move to `docs/history/` | Contains UAT report for Phase 2. Useful history, but cluttered in root. |
| `AGENTS.md` | **Redundant** | Delete | Info overlaps with `README.md` and `PROJECT_RULES.md`. GSD-specific agent rules are in `.gemini/`. |
| `CODE_MATURITY_SCORECARD.md` | **Archive** | Move to `docs/history/` | Point-in-time assessment (Jan 2026). Useful reference for progress. |
| `FIXES_APPLIED.md` | **Archive** | Move to `docs/history/` | Historical log of fixes from Feb 2026. Useful context. |
| `GSD-STYLE.md` | **Current** | Move to `docs/` | Methodology details; better placed in documentation folder. |
| `IMPROVEMENTS.md` | **Archive** | Move to `docs/history/` | Older improvement report. |
| `MAS_Strategy.md` | **Current** | Move to `docs/` | Strategic document for MAS architecture. |

## Logs and Ephemeral Files

| File | Status | Recommendation | Rationale |
| :--- | :--- | :--- | :--- |
| `audit.log` | **Ephemeral** | Delete | Contains decision logs from Feb 24/25. Ephemeral runtime artifact. |
| `startup.log` | **Ephemeral** | Delete | Contains Finnhub connection errors. Ephemeral runtime artifact. |
| `growin.db` | **Unknown** | Investigate/Delete | Likely a legacy local SQLite database. Main DB is in `data/` or `growin_rag_db/`. |

## Secondary Directories

| Directory | Status | Recommendation | Rationale |
| :--- | :--- | :--- | :--- |
| `future/` | **Current** | Move to `docs/future/` | Contains `suggestions.MD`. Valuable roadmap items. |
| `adapters/` | **Current** | Move to `.gemini/` | Contains provider-specific rules (CLAUDE.md, etc.). Belongs with GSD config. |
| `.Jules/` | **Current** | Move to `docs/learnings/` | Contains valuable architectural "learnings" (Bolt, Palette, Sentinel). Highly relevant but hidden. |

## Next Steps
1. Consolidate identified Markdown files into `docs/` or `docs/history/`.
2. Move strategy and learnings to structured locations.
3. Clean up root logs and obsolete files in the next plan (07-03).
