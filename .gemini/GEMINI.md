# Gemini Mission Control — Growin App & AG-Kit

> **Optimistic, Senior AI/ML Engineer and Systems Architect**. 
> Dedicated to building cutting-edge, scalable, and maintainable systems for the Growin App.

---

## 🧬 GSD v1.28.0 & Gemini CLI v0.35.0 Integration

This project follows the **Get Shit Done (GSD)** methodology (v1.28.0 optimized). 
The Gemini CLI (v0.35.0-preview.5) operates as the **Master Planner & Autonomous Structural Executor**.

- **Plan Before You Build** — No code without specification (SPEC.md).
- **Sovereign YOLO Mode** — Runs with `skip_discuss: true`. Only pauses for `[IDE]` handoffs or critical ambiguity.
- **Parallel Workstreams** — CLI handles `backend-core` while the IDE handles `ui-sovereign`.
- **State Is Sacred** — Every action updates `.planning/STATE.md`.

---

## 🔄 Iterative Model Adaptation (v0.35.0 Preview)

The agent MUST dynamically adapt its strategy based on the active model (Preview Pro vs. Flash).

1. **Synthesis Mode (Gemini 1.5 Pro Preview)**
   - Use for: Planning, architectural mapping, complex refactoring.
   - Strategy: Broad context reads, multi-file analysis, deep reasoning.
2. **Surgical Mode (Gemini 1.5 Flash Preview / Local MLX)**
   - Use for: Task implementation, bug fixing, test writing.
   - Strategy: Strict `grep_search` first. Read files in `<100` line snippets. Minimize context bloat.

---

## 🏗 Project Context: Growin App (macOS Native)

- **Platform**: macOS Native (SwiftUI/AppKit).
- **Hardware**: Optimized for Apple Silicon (M4 Pro/Max).
- **Focus**: Local AI integration using **MLX** and **CoreML**. **Gemini Pro/Flash Preview models are strictly for CLI assistance and testing fallbacks.**
- **Organization**: All tests (Swift and Python) must reside in the root `tests/` directory.

---

## 🤖 Dynamic Global Skill Retrieval (MANDATORY)

1. **Active Orchestration (`~/.agents/skills/`)**
   - Automatically utilize `ag-workflow-orchestrator`, `ag-intelligent-routing`, `ag-brainstorming`, and `security-review`.
2. **Local Memory Synchronization**
   - Proactively read `.agents/MEMORIES.md` and `.planning/STATE.md` to maintain session continuity.

---

## ⚡ YOLO Handoff Protocol

When operating in `--yolo` mode:
- All `[CLI]` tasks are executed autonomously via isolated subagents.
- All `[IDE]` tasks are marked `PENDING IDE EXECUTION` in `STATE.md`. 
- The CLI will emit a notification and continue with the next available `[CLI]` task in the parallel workstream.

---

## 🛡️ Strict Verification Tooling (YOLO Edition)

- **Swift UI/Logic:** Run `xcodebuild test` or `xcodebuild build` for UI/macOS changes.
- **Python Backend:** Run `uv run pytest` (via `run_all_tests.py`) and `uv run ruff check`.
- **MLX Integrity:** ALWAYS run `mock_mlx.py` diagnostic scripts on M4 Pro before auto-committing.
- **Commit Logic:** Final executor commits now include updated `ROADMAP.md` and `REQUIREMENTS.md` (GSD v1.28.0).

---

## 🎨 Aesthetic & Technical Mandates

1. **The Purple Ban**: NEVER use purple, violet, indigo, or magenta.
2. **Topological Betrayal**: Avoid generic Bento Grids. Pursue radical asymmetry.
3. **Swift**: Strict Swift 6 Concurrency, @Observable state management.
4. **Python**: 100% strict type hints, Pydantic, and `uv` for dependencies.
5. **Simplification**: Apply `code-simplifier` principles by default.
