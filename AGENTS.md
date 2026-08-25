# Agent Operating Contract

This repository uses GSD as the durable project-management system and treats
`.planning/` artifacts as the source of truth for scope, decisions, plans,
verification, and handoff state.

Before non-trivial work, read [the Agent Cookbook](.agents/AGENT-COOKBOOK.md).
Apply its task-routing, safety, verification, token-efficiency, and Growin
execution-integrity rules. For a small, isolated change, follow the cookbook's
quick-task path; do not create unnecessary planning overhead.

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

