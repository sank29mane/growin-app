---
description: The Synchronizer — Poll, Review, and Apply Jules worker patches
---

# /jules-sync Workflow

<role>
You are a task synchronization orchestrator. You poll the status of active Jules worker sessions, review their proposed plans, and apply completed patches to the local repository.
</role>

<objective>
Maintain local repository consistency by efficiently merging work from the asynchronous Jules swarm.
</objective>

<context>
**State:** Reads `.gsd/STATE.md` for active Session IDs.
**Tool:** Uses `jules` CLI.
</context>

<process>

## 1. Poll Active Swarm

1. **List Sessions:**
   Run `jules remote list --session` to see the status of all remote workers.
   Compare with the "Active Jules Swarm" table in `.gsd/STATE.md`.

---

## 2. Review Proposed Plans

1. **Identify Ready Sessions:**
   Look for sessions in `AWAITING_APPROVAL` or `COMPLETED`.

2. **Safety Audit (CRITICAL):**
   For sessions awaiting approval, pull the diff to a temporary file:
   ```bash
   jules remote pull --session <ID> > jules_audit.patch
   ```
   Inspect `jules_audit.patch` against current local files.
   - **Check:** Did Jules delete or revert any SOTA 2026 logic (e.g., Orchestrator flattening, 8-bit AFFINE)?
   - **Check:** Are the line numbers and context blocks consistent with current local code?
   - **Reject:** If the patch is based on a stale version of the file, reject it and update the GitHub remote.

---

## 3. Apply and Verify

1. **Pull & Apply:**
   For approved or completed sessions, run:
   ```bash
   jules remote pull --session <ID> --apply
   ```

2. **Local Validation:**
   Run relevant tests and linting to ensure the patch didn't introduce regressions.
   ```bash
   uv run pytest
   ```

---

## 4. Update Project Memory

1. **Update State:**
   Remove the Session ID from the "Active Jules Swarm" table in `.gsd/STATE.md`.
   Move the task to "Recently Completed" in `STATE.md`.

2. **Update Roadmap:**
   Mark the corresponding task as `[x]` in the active `PLAN.md` file.

</process>
