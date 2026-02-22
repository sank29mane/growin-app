# SPEC.md
Status: FINALIZED

## Objective
Establish the canonical GSD (Get Shit Done) meta-prompting framework directories, state files, and workflows for the `growin-app` repository to ensure AI/LLM agents (specifically Gemini CLI) can operate autonomously, predictably, and with context preservation.

## Requirements
1. **Directory Structure**: Ensure `.gsd/` exists for project state and `.agent/workflows/` exists for slash commands.
2. **State Management**: Create `STATE.md` to hold session memory, current objective, and wave status.
3. **Execution Roadmap**: Create `ROADMAP.md` dividing the implementation into logical phases.
4. **Bootstrapping Workflow**: Create `.agent/workflows/gsd-setup.md` to define the `/gsd-setup` slash command, teaching future agents how to verify and initialize the environment.

## Context
Growin uses a sophisticated, native macOS, hybrid algorithmic-LLM architecture. The GSD framework ensures that as we scale the agentic swarm, core operational rules (defined in `PROJECT_RULES.md`) are strictly adhered to by any LLM operating in the repository.

## Acceptance Criteria
- [x] `.gsd/SPEC.md` exists and is FINALIZED.
- [ ] `.gsd/ROADMAP.md` outlines the setup and next immediate feature phases.
- [ ] `.gsd/STATE.md` tracks the current active wave and context.
- [ ] `.agent/workflows/gsd-setup.md` exists and is syntactically correct for the orchestrator.
