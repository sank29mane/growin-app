# PROJECT_RULES.md — GSD Canonical Rules

> **Single Source of Truth** for the Get Shit Done methodology.
> 
> Model-agnostic. All adapters and extensions reference this file.

---

## Core Protocol

**SPEC → PLAN → EXECUTE → VERIFY → COMMIT**

1. **SPEC**: Define requirements in `.gsd/SPEC.md` until status is `FINALIZED`
2. **PLAN**: Decompose into phases in `.gsd/ROADMAP.md`, then detailed plans
3. **EXECUTE**: Implement with atomic commits per task
4. **VERIFY**: Prove completion with empirical evidence
5. **COMMIT**: One task = one commit, message format: `type(scope): description`

**Planning Lock**: No implementation code until SPEC.md contains "Status: FINALIZED".

---

## Proof Requirements

Every change requires verification evidence:

| Change Type | Required Proof |
|-------------|----------------|
| API endpoint | curl/HTTP response |
| UI change | Screenshot |
| Build/compile | Command output |
| Test | Test runner output |
| Config | Verification command |

**Never accept**: "It looks correct", "This should work", "I've done similar before".

**Always require**: Captured output, screenshot, or test result.

---

## Resource Lifecycle Mandate (CRITICAL)

**Rule:** Every asynchronous task, subprocess, or database connection MUST have an explicit shutdown mechanism.

1. **Explicit Teardown**: Call `.stop()` or `.close()` in test teardowns and lifespan `finally` blocks.
2. **Lazy Init**: Do not start background services on module import.
3. **Mocking**: Global mocks for Docker and External APIs in unit tests.

See [docs/LIFECYCLE_MANDATES.md](docs/LIFECYCLE_MANDATES.md) for full specs.

---

## Search-First Discipline

**Before reading any file completely:**

1. **Search first** — Use grep, ripgrep, or IDE search to find relevant snippets
2. **Evaluate snippets** — Determine if full file read is justified
3. **Targeted reads** — Only read specific line ranges when needed

**Benefits:**
- Reduces context pollution
- Faster understanding of large codebases
- Prevents reading irrelevant code

**Anti-pattern**: Reading entire files "to understand the context" without searching first.

---

## Wave Execution

Plans are grouped into **waves** based on dependencies:

| Wave | Characteristic | Execution |
|------|----------------|-----------|
| 1 | Foundation tasks, no dependencies | Run in parallel |
| 2 | Depends on Wave 1 | Wait for Wave 1, then parallel |
| 3 | Depends on Wave 2 | Wait for Wave 2, then parallel |

**Task Tagging**: All tasks in `PLAN.md` MUST be explicitly tagged as either `[AG]` (Antigravity Specialist Agent) or `[CLI]` (Direct CLI execution).
- `[AG]` — Complex, multi-file, or research-intensive tasks, UI, Web scraping, visualization tasks.
- `[CLI]` — Sequential, coding heavy, surgical, or environment-setup tasks.

**Wave Completion Protocol:**
1. All tasks in wave verified
2. State snapshot created
3. Commit all wave work
4. Update STATE.md with position

---

## State Snapshots

At the end of each wave or significant work block, create a state snapshot:

```markdown
## Wave N Summary

**Objective:** {what this wave aimed to accomplish}

**Changes:**
- {change 1}
- {change 2}

**Files Touched:**
- {file1}
- {file2}

**Verification:**
- {command}: {result}

**Risks/Debt:**
- {any concerns}

**Next Wave TODO:**
- {item 1}
- {item 2}
```

---

## Model Independence

**Absolute Rule**: No rule, workflow, or skill may require a specific model provider.

**Allowed:**
- Optional adapters with provider-specific enhancements
- Capability-based recommendations (e.g., "use a reasoning model for planning")
- Examples mentioning specific models as illustrations

**Forbidden:**
- Hard dependencies on provider features
- Breaking behavior when a specific model is unavailable
- Duplicating canonical rules in adapters

**Adapter Pattern:**
```
.gemini/adapters/
├── CLAUDE.md    # Optional Claude enhancements
├── GEMINI.md    # Optional Gemini enhancements
└── GPT_OSS.md   # Optional GPT/OSS enhancements
```

Each adapter must begin with:
> "Everything in this file is optional. For canonical rules, see PROJECT_RULES.md."

---

## Commit Conventions

**Format:**
```
type(scope): description
```

**Types:**
| Type | Usage |
|------|-------|
| `feat` | New feature |
| `fix` | Bug fix |
| `docs` | Documentation only |
| `refactor` | Code restructure (no behavior change) |
| `test` | Adding/updating tests |
| `chore` | Maintenance, dependencies |

**Rules:**
- One task = one commit
- Verify before commit
- Scope = phase number for phase work (e.g., `feat(phase-1): ...`)

---

## Repository Structure

```
PROJECT_RULES.md          # ← This file (canonical rules)
docs/GSD-STYLE.md         # Style and conventions

.agent/
├── workflows/            # Slash commands (/plan, /execute, etc.)
└── skills/               # Agent specializations

.gemini/                  # Gemini-specific configuration
├── adapters/             # Optional model-specific enhancements
.gsd/                     # Project state and artifacts
├── SPEC.md               # Requirements (must be FINALIZED)
├── ROADMAP.md            # Phases and progress
├── STATE.md              # Session memory
├── templates/            # Document templates
└── examples/             # Usage examples

docs/                     # Operational documentation
scripts/                  # Utility scripts
```

---

## Context Management

**Context Quality Thresholds:**

| Usage | Quality |
|-------|---------|
| 0-30% | **PEAK** — Comprehensive, thorough work |
| 30-50% | **GOOD** — Solid, confident output |
| 50-70% | **DEGRADING** — Efficiency mode |
| 70%+ | **POOR** — Rushed, incomplete |

**Context Hygiene Rules:**
- Keep plans under 50% context usage
- Fresh context for each plan execution
- After 3 debugging failures → state dump → fresh session
- STATE.md = memory across sessions

---

## Token Efficiency Rules

**Goal:** Minimize token consumption while maintaining output quality.

### Loading Rules

| Action | Rule |
|--------|------|
| Before reading file | Search first (grep, ripgrep) |
| File >200 lines | Use outline, not full file |
| File already understood | Reference summary, don't reload |
| >5 files needed | Stop, reconsider approach |

### Budget Thresholds

| Usage | Action Required |
|-------|-----------------|
| 0-50% | Proceed normally |
| 50-70% | Switch to outline mode, compress context |
| 70%+ | State dump required, recommend fresh session |

### Compression Protocol

After understanding a file:
1. Create summary in STATE.md or task notes
2. Reference summary instead of re-reading
3. Only reload specific sections if needed

### Per-Wave Efficiency

- Start each wave with minimal context
- Load files just-in-time (when task requires)
- Compress/summarize before moving to next wave
- Document token usage in state snapshots (optional)

**Anti-patterns:**
- Loading files "just in case"
- Re-reading files already understood
- Full file reads when snippets suffice
- Ignoring budget warnings

---

## Quick Reference

```
Before coding    → SPEC.md must be FINALIZED
Before file read → Search first, then targeted read
After each task  → Commit + update STATE.md
After each wave  → State snapshot
After 3 failures → State dump + fresh session
Before "Done"    → Empirical proof captured
```

---

*GSD Methodology — Model-Agnostic Edition*
*Reference implementation for multi-LLM environments*

---

## 🧠 Unified GSD Execution Protocol (CLI & IDE Symbiosis)

Because the Antigravity IDE natively supports GSD commands, we operate a seamless, unified workflow. The Gemini CLI acts as the Master Planner and Structural Executor, while the Antigravity IDE acts as the Visual/Creation Executor. Both environments utilize the exact same `/gsd` command structure.

### 1. Mandatory Task Tagging Syntax
When the Gemini CLI (`/gsd:plan-phase`) generates or updates a `PLAN.md`, **every task** MUST be explicitly tagged with its Execution Context and Required Skill. This ensures that when `/gsd:execute` is run, the system automatically loads the correct domain expertise.

**Required Format:**
```markdown
- [ ] Task 1: Initialize Database Schema
      - **Context:** `CLI`
      - **Skill:** `ag-database-architect`
      - **Instruction:** Create the backend migration files and update STATE.md.

- [ ] Task 2: Build SwiftUI Onboarding View
      - **Context:** `IDE`
      - **Skill:** `ag-frontend-specialist`
      - **Instruction:** Implement the visual layout based on SPEC.md.
```

### 2. The Division of Execution (`Context`)
- **`Context: CLI`**: Backend coding, DB migrations, state updates, test running, and git operations. The Gemini CLI executes these automatically during its own `/gsd:execute-phase` loop.
- **`Context: IDE`**: UI/UX development, greenfield features, and complex multi-file refactoring. The Gemini CLI MUST hand these off.

### 3. The Command-Based Handoff Loop
During phase execution (`/gsd:execute-phase`) in the Gemini CLI terminal:
1. **If Context is `CLI`**: The CLI autonomously executes the task, tests it, and commits.
2. **If Context is `IDE`**: The CLI MUST hard-pause. It updates `.gsd/STATE.md` and outputs: 
   *"Handoff Required: Please open the Antigravity IDE Agent panel and run `/gsd:execute task [Task ID]`. Type 'done' here when finished."*
3. **IDE Execution**: The user runs the command in the IDE. The IDE's GSD integration automatically reads the `Skill` tag (e.g., `ag-frontend-specialist`), loads the persona, and completes the work.
4. **Verification**: Upon the user's return to the CLI, the CLI runs native verification (`pytest`, `xcodebuild`, etc.) before moving to the next task.


---

## 🛰️ Smart Skill Planning & Routing Protocol (OPTIMIZED)

To maximize performance and token efficiency, the following protocol must be followed during any **Plan** or **Research** phase:

1. **Required Skill Analysis:** Every phase plan created by '/gsd:plan-phase' MUST include a metadata block at the top:
   ```yaml
   Phase: [PHASE_NAME]
   Required_Skills:
     - [SKILL_NAME] (Source: Global/Local/Archive)
   ```
2. **Proactive Activation:** Before starting any sub-task, the agent MUST check if a 'Required_Skill' exists and invoke 'activate_skill'.
3. **Hierarchy of Skills:** 
   - 1. **Local:** '.agents/skills/' (Project-specific expert).
   - 2. **Global:** '~/.agents/skills/' (Current stable library).
   - 3. **Archive:** '~/.agents/skills-archive/' (Niche/Deep expertise).
4. **No Manual Memory Save:** Never use 'save_memory' for Growin App facts. Instead, append new persistent knowledge to '.agents/MEMORIES.md'.