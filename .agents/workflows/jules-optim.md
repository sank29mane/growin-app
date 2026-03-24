---
description: Jules Optimizer — Dispatches specialized Jules MCP agents for system-wide optimization
argument-hint: "[security | performance | stability | all]"
---

# /gsd:jules-optim Workflow

<role>
You are the Meta-Orchestrator for the Growin App. You leverage the Jules MCP server to dispatch parallel, specialized autonomous agents to optimize the codebase without bloating your own context.
</role>

<objective>
Dispatch specialized Jules sessions to handle technical debt, security audits, and performance profiling.
</objective>

<context>
**Target Source ID:** `github/sank29mane/growin-app`
**Specializations:** Security, Performance, Stability, Multi-Asset Intelligence
</context>

<process>

## 1. Preparation & Sync
1. **GitHub Alignment**: Ensure local `main` is pushed to GitHub. Jules operates on the remote state.
   - Run `git push origin main` if needed.
2. **Context Cleanup**: Clear any stale local logs or temporary build artifacts that might confuse an external agent.

## 2. Dispatch (Parallel Sessions)
Based on the argument, call `create_session` for one or more tracks:

### A. Security Track
`create_session(source_id="github/sank29mane/growin-app", prompt="Scan the FastAPI endpoints, Docker configuration, and middleware for security vulnerabilities. Specifically check for leaked secrets or unsafe CORS/auth patterns.")`

### B. Performance Track
`create_session(source_id="github/sank29mane/growin-app", prompt="Profile the SwiftUI rendering pipeline and backend data fraying logic. Identify bottlenecks preventing consistent 120Hz performance on M4 Pro/Max.")`

### C. Stability Track
`create_session(source_id="github/sank29mane/growin-app", prompt="Audit the `mlx_engine.py` and `risk_engine.py` for memory leaks, unhandled exceptions, or async race conditions.")`

## 3. Monitoring & Filtering
1. **Poll Status**: Call `list_session_history` and `get_session_status` for active sessions.
2. **Evaluate Plan**: When a session reaches `AWAITING_APPROVAL`:
   - Run `list_activities` to see the proposed diff.
   - **Assistant Filter**: Verify the changes against `PROJECT_RULES.md`.
   - **Decision**:
     - *Approved*: `respond_to_plan(approve=True)` if safe/minor.
     - *Requires Review*: Present the diff to the user if major/architectural.

## 4. Synthesis
1. Once sessions complete, summarize the outcomes.
2. Provide CodeCast audio links using `get_audio_summary`.
3. Update `.gsd/STATE.md` with the "Swarm Optimization" results.

</process>
