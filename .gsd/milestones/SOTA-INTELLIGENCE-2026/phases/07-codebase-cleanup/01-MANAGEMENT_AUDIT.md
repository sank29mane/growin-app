# Management Directory and Config Audit

Generated on: 2026-02-25
Phase: 07-codebase-cleanup
Plan: 01

## Management Directory Audit

| Directory | Status | Rationale |
|-----------|--------|-----------|
| `.Jules/` | **DELETE** | Legacy management artifacts (`bolt.md`, `sentinel.md`) from Feb 18. Not part of current GSD. |
| `.abacusai/`| **DELETE** | Empty directory from Feb 4. Legacy artifact. |
| `.agent/` | **ARCHIVE** | Contains older GSD workflows and skills (Feb 22). Replaced by `gsd-tools` and `.planning/`. |
| `.agents/` | **RETAIN** | Contains specialized skills (cto-advisor, docker-expert). Should be moved to `.agent/` or consolidated. |
| `.claude/` | **RETAIN** | Contains `settings.local.json` used by Claude environments. |
| `.gemini/` | **RETAIN** | Contains `GEMINI.md` and tools config for Gemini environments. |
| `.gsd/` | **ARCHIVE** | Previous GSD structure (SPEC, ROADMAP). Content has been migrated to `.planning/`. |
| `.opencode/`| **RETAIN** | Active Opencode environment (`node_modules`, `bun.lock`). |
| `.planning/`| **RETAIN** | Current active GSD management directory. |

## Root Configuration Audit

| File | Status | Rationale |
|------|--------|-----------|
| `opencode.json` | **RETAIN** | Required for Opencode provider configuration. |
| `pyrightconfig.json` | **RETAIN** | Required for Python static analysis/type checking. |
| `model_capabilities.yaml` | **RETAIN** | Active guidance for model selection across the project. |
| `.agents.md` | **RETAIN** | Active log of architectural learnings and optimization (updated Feb 24). |
| `PROJECT_RULES.md` | **RETAIN** | Canonical GSD rules. |
| `GSD-STYLE.md` | **RETAIN** | Active style guide. |
| `ARCHITECTURE.md` | **RETAIN** | Primary architectural documentation. |
| `AGENTS.md` | **DELETE** | Legacy agent documentation from Feb 4. Superseded by `.agents.md` and current GSD docs. |
| `TODO.md` | **DELETE** | Legacy root TODO list. All active tasks are in `.planning/todos/`. |
