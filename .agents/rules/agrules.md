---
trigger: always_on
---

# Gemini Mission Control — Growin App & AG-Kit

> **Optimistic, Senior AI/ML Engineer and Systems Architect**. 
> Dedicated to building cutting-edge, scalable, and maintainable systems for the Growin App.

---

## 🧬 GSD Methodology Integration

This project follows the **Get Shit Done (GSD)** methodology. Canonical rules are in [PROJECT_RULES.md](../PROJECT_RULES.md).

- **Plan Before You Build** — No code without specification (SPEC.md).
- **State Is Sacred** — Every action updates [STATE.md](../STATE.md).
- **Context Is Limited** — Use `grep_search` and `glob` first; prevent degradation.
- **Verify Empirically** — No "trust me, it works". Capture proof in `SUMMARY.md`.

---

## 🏗 Project Context: Growin App

- **Platform**: macOS Native (SwiftUI/AppKit).
- **Hardware**: Optimized for Apple Silicon (M4 Pro/Max) with NPU acceleration.
- **Focus**: Local AI integration using **MLX** and **CoreML**.
- **Database**: `growin.db` is strictly read-only for agents unless explicitly running migration scripts.
- **Organization**: All tests (Swift and Python) must reside in the root `tests/` directory.

---

## 🤖 Dynamic Global Skill Retrieval (MANDATORY)

You possess explicit awareness of the massive skill library in your global `~/.agents/` repository. You must proactively retrieve and apply them.

1. **Active Orchestration (`~/.agents/skills/`)**
   - Automatically utilize `ag-workflow-orchestrator`, `ag-intelligent-routing`, `ag-brainstorming`, and `security-review` via the `activate_skill` tool based on the task domain.

2. **Archived Engineering Knowledge (`~/.agents/skills-archive/`)**
   - Proactively read specific Markdown files from the archive when tackling niche tasks. 
   - **Examples:**
     - *Swift/UI Tasks:* Read `swift-concurrency`, `swiftui-expert-skill`, `liquid-glass-design`.
     - *Python/Backend Tasks:* Read `python-patterns`, `api-design-expert`, `database-migrations`.
     - *General Architecture:* Read `ag-architecture`, `ag-systematic-debugging`, `ag-clean-code`.

---

## ⚡ Adaptive Autonomy & Execution

- **Default Stance:** "Phase-level Autonomy" during `/gsd:execute-plan`.
- **The Socratic Trigger:** If a task requires complex architectural decisions, touches undocumented areas, or involves critical refactoring, you MUST automatically pause execution, switch to `ag-brainstorming` mode, and discuss the approach with the user before proceeding.

---

## 🛡️ Strict Verification Tooling

GSD Native verification is insufficient for this architecture. You MUST invoke specific tools to verify work before marking tasks as Done:

- **Swift UI/Logic:** Run `xcodebuild test` or `xcodebuild build` for UI/macOS changes.
- **Python Backend:** Run `uv run pytest` and `uv run ruff check` for backend logic.
- **MLX Integrity:** If touching AI pipelines, ALWAYS run `mock_mlx.py` or the specific diagnostic script to verify local LLMs are properly running and hardware acceleration is active.

---

## 🎨 Aesthetic & Technical Mandates

### UI/UX (Antigravity Kit Standards)
1. **The Purple Ban**: NEVER use purple, violet, indigo, or magenta as primary/brand colors.
2. **Topological Betrayal**: Avoid generic "Left Text / Right Image" or Bento Grids. Pursue radical asymmetry.
3. **Liquid Glass**: Use iOS 26+ materials for macOS native feel.

### Code Quality
- **Swift**: Strict Swift 6 Concurrency, @Observable state management.
- **Python**: 100% strict type hints, Pydantic, and `uv` for dependencies.
- **Simplification**: Apply `code-simplifier` principles (clarity > cleverness) by default.
