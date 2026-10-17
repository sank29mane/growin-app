# Agent Operating Contract

This repository uses GSD as the durable project-management system and treats
`.planning/` artifacts as the source of truth for scope, decisions, plans,
verification, and handoff state.

Before non-trivial work, read [the Agent Cookbook](.agents/AGENT-COOKBOOK.md).
Apply its task-routing, safety, verification, token-efficiency, and Growin
execution-integrity rules. For a small, isolated change, follow the cookbook's
quick-task path; do not create unnecessary planning overhead.

GSD is the project record and the quality gates, not a reason to split work.
The roadmap sets each phase's workflow tier. Plan coarsely, execute by stage
with one agent per stage (not one per plan), and have a different model verify
the result. Details are in cookbook sections 2, 3, and 6.

Non-negotiable rules:

1. Do not work outside the current approved phase or task boundary. Capture
   adjacent ideas as deferred work.
2. Do not run two agents or runtimes against overlapping files at once. Use
   committed artifacts and Git status for handoffs.
3. Never treat a test run as proof unless it exercises the changed behavior.
4. Do not bypass risk, simulation, approval, authentication, authorization, or
   security controls to make an integration "work".
5. Do not submit, enable, or simulate real trades without explicit user
   authorization and the mandatory pre-flight controls described in the
   cookbook.
6. Keep reports concise: result, changed files, verification, risks, and next
   action.

## Cloud Agent (Linux)

Cloud Agents run Linux, not macOS. Scope is the **Python FastAPI backend** in
`backend/` (SwiftUI/Xcode and on-device MLX/CoreML are out of scope here).

- **Install:** `bash .cursor/cloud-agent-install.sh` (or `uv sync --project backend --all-groups` after `uv` is on PATH).
- **Start API:** `bash .cursor/start.sh` (listens on `0.0.0.0:8002`; health at `/health`).
- **Tests:** match CI env (`CI=true`, `GROWIN_ANALYTICS_ENABLED=false`, `PYTHONPATH` includes `backend`), then:
  `uv run --project backend pytest tests/backend/ --ignore=tests/backend/test_adapter.py --ignore=tests/backend/test_mlx_hotswap.py`
- **Secrets:** optional for health and most unit tests. Do not enable live trading
  or bypass execution controls without explicit user authorization (cookbook).

## 📝 Communication Tone (Mandatory)
Apply "Unslop" principles to all text generation:
- **No AI Tell-Words:** BANNED WORDS: delve, pivotal, testament, tapestry, showcase, vibrant, foster, enhance.
- **Rhythm & Soul:** Vary sentence lengths. Be opinionated and specific. Avoid the "rule of three" and perfectly symmetrical paragraphs. Let a little mess in. Use "I".
- **Formatting Constraints:** No em-dashes. Do not use "serves as" or "stands as" when "is" works.
