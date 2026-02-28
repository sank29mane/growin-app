# Plan: Create Jules Delegation Workflow

**Objective:** Implement a CLI workflow (`/jules-delegate`) to facilitate task delegation to the Jules MCP server, following the strategy in `JULES_DELEGATION_PLAN.md`.

## Tasks

### 1. Create the Workflow Definition
- [ ] Create `.agent/workflows/jules-delegate.md`.
- [ ] Define the role, objective, and context for delegation.
- [ ] Implement the process steps:
    - Discovery: Identify target source and worker agents.
    - Dispatch: Create a new Jules session with a specific prompt.
    - Monitoring: Poll status and list activities.
    - Verification: Review proposed plans.
    - Finalization: Approve or reject plans.

### 2. Update Project State
- [ ] Update `.gsd/STATE.md` to reflect the completion of this quick task.

### 3. Commit Changes
- [ ] Commit the new workflow and state updates.

## Verification
- [ ] Verify the file exists and is correctly formatted.
- [ ] (Manual) Check if `/jules-delegate` is recognized by the system.
