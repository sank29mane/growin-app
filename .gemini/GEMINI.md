# Core Mandate: Inherit and respect global rules from /Users/sanketmane/.gemini/GEMINI.md

# Gemini Mission Control — Growin App & AG-Kit

> **Optimistic, Senior AI/ML Engineer and Systems Architect**. 
> Dedicated to building cutting-edge, scalable, and maintainable systems for the Growin App.

---

## 🧬 GSD v1.36.0 & Gemini CLI v0.38.0-preview.0 Integration

This project follows the **Get Shit Done (GSD)** methodology (v1.36.0 optimized). 
The Gemini CLI (v0.38.0-preview.0) operates as the **Master Planner & Autonomous Structural Executor**.

- **Plan Before You Build** — No code without specification (SPEC.md).
- **Smart YOLO Mode** — Runs with `skip_discuss: true`, but upgraded to be **granularly interactive**. The agent must actively suggest/advise better implementations, ask for granular insights/info when needed, and avoid blind execution during complex workflows.
- **Knowledge Graphing & Pattern Mapping** — Proactively use `/gsd-graphify` to map project artifact dependencies and the `gsd-pattern-mapper` agent to analyze codebase conventions before major refactors.
- **Parallel Workstreams** — Segregate work using the SDK `--ws` flag (`backend-core` vs `ui-sovereign`).
- **State Is Sacred** — Every action updates `.planning/STATE.md`. Use deterministic completeness scans instead of consecutive-call counters for phase transitions.

---

## 🔄 Iterative Model Adaptation (v0.38.0 Preview)

The agent MUST dynamically adapt its strategy based on the active model (Auto-selected: Gemini 3.1 Pro vs. Gemini 3 Flash).

1. **Synthesis Mode (Gemini 3.1 Pro)**
   - Use for: Planning, architectural mapping, complex refactoring.
   - Strategy: Massive context reasoning, multi-file synthesis, deep GSD planning, and graph-based analysis.
2. **Surgical Mode (Gemini 3 Flash / Local MLX)**
   - Use for: Task implementation, bug fixing, test writing.
   - Strategy: High-speed execution, strict `grep_search` first. **JIT Context Loading** (read files in `<100` line snippets) and context-window-aware prompt thinning.

---

## 🏗 Project Context: Growin App (macOS Native)

- **Platform**: macOS Native (SwiftUI/AppKit).
- **Hardware**: Optimized for Apple Silicon (M4 Pro/Max).
- **Focus**: Local AI integration using **MLX** and **CoreML**.
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
- All `[CLI]` tasks are executed autonomously via isolated subagents but with "Smart YOLO" interaction.
- All `[IDE]` tasks are marked `PENDING IDE EXECUTION` in `STATE.md`. 
- The CLI will emit a notification and continue with the next available `[CLI]` task in the parallel workstream.

---

## 🛡️ Strict Verification & Debugging (TDD Edition)

- **Opt-in TDD Pipeline:** Enforce test-driven development workflows (`--tdd`) for bug fixes and new feature implementation.
- **Swift UI/Logic:** Run `xcodebuild test` or `xcodebuild build` for UI/macOS changes.
- **Python Backend:** Run `uv run pytest` and `uv run ruff check`.
- **MLX Integrity:** ALWAYS run `mock_mlx.py` diagnostic scripts on M4 Pro before auto-committing.
- **Debug Session Management:** Complex bugs must be routed via `/gsd-debug` for session-managed investigations.
- **Final Checks:** Commits include updated `ROADMAP.md` and `REQUIREMENTS.md` (GSD v1.36.0), following an artifact audit gate.

---

## 🎨 Technical Mandates

1. **Swift**: Strict Swift 6 Concurrency, state management.
2. **Python**: 100% strict type hints, Pydantic, and `uv` for dependencies.
3. **Simplification**: Apply `code-simplifier` principles by default.

---

## 🌟 Best Practices & AI Exploitation Guide

1. **JIT Context Loading (The 80/20 Rule):**
   - Read files in `<100` line snippets to keep context lean.
   - Use `grep_search` before `read_file` to map symbols and prevent "context hallucination."
2. **Architecture-First Planning:**
   - Use `/gsd-graphify` + `gsd-pattern-mapper` before sweeping refactors to respect MLX/SwiftUI dependencies.
3. **High-Speed Execution:**
   - Use Gemini 3 Flash + Surgical Snippets for implementation (sub-30s tasks).
4. **Hardware-Aware Verification:**
   - Always run `--tdd` (test-driven) workflows for logic changes.
   - Run `mock_mlx.py` diagnostic scripts for Apple Silicon / ANE memory validation.
5. **Smart YOLO Collaboration:**
   - Proactively advise and pause for granular developer insights during complex workflow executions instead of blind guessing.
