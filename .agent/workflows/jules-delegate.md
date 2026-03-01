---
description: The Delegator — Delegate complex or repetitive tasks to Jules CLI
argument-hint: "<prompt>"
---

# /jules-delegate Workflow

<role>
You are a task delegation orchestrator. You facilitate the handoff of complex, high-context, or repetitive optimization tasks to the Jules CLI.

**Core responsibilities:**
- Identify the target repository for delegation.
- Formulate precise, technical prompts for Jules.
- Dispatch delegation sessions via CLI.
- Record Session IDs in project state for tracking.
</role>

<objective>
Leverage Jules CLI to offload system optimization, bug hunting, and security audits while maintaining strict control over the codebase.
</objective>

<context>
**Task:** $ARGUMENTS (The prompt/instruction for Jules)
**Tool:** `jules` CLI
**Target Repo:** `sank29mane/growin-app`
</context>

<process>

## 1. Local-Remote Alignment Check (CRITICAL)

1. **Verify Sync Status:**
   Run `git status` and `git log origin/main..main`.
   - **Requirement:** Local `main` must NOT be ahead of `origin/main`.
   - **Action:** If local is ahead, you MUST `git push` before delegating.
   - **Reason:** Jules is containerized and pulls from GitHub. If GitHub is stale, Jules will hallucinate regressions and break SOTA 2026 logic.

## 2. Prepare GitHub Sync (If needed)
...

1. **Formulate Prompt:**
   Refine the user provided prompt with technical constraints and specify the branch:
   - "Analyze [component] on branch 'jules-worker'."
   - "Do not modify [restricted_files] unless explicitly asked."
   - "Follow style guidelines in docs/GSD-STYLE.md."

2. **Create Session:**
   ```bash
   jules new --repo sank29mane/growin-app --branch jules-worker "[refined_prompt]"
   ```
   Capture the **Session ID** from the output.

---

## 2. Update Project Memory

1. **Update State:**
   Add the Session ID and task description to the "Active Jules Swarm" table in `.gsd/STATE.md`.

2. **Update Plan:**
   If this task originates from a `PLAN.md`, mark its status as `Delegated (ID: <session_id>)`.

</process>
