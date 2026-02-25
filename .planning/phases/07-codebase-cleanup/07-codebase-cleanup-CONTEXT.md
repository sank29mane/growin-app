# Phase Context: 07-codebase-cleanup

## Goal
Identify and catalog project files for deletion or retention to reduce codebase noise and eliminate legacy artifacts from previous development iterations.

## Scope
- Analyze all project files, including root level `.md` files and hidden management directories.
- Evaluate necessity based on current project scope and dependencies.
- Distinguish between active GSD files and legacy "agent" or "Jules" artifacts.

## Deliverables
- List of files to delete.
- List of files to retain.
- Phase plan for the actual cleanup execution.

## Decisions
- **Retention Strategy**: If a file's purpose cannot be verified as supporting the current "Growin App" functionality or the active GSD orchestration, it should be marked for deletion or archival.
- **Archive vs Delete**: High-value legacy docs should be moved to a `legacy/` or `archive/` folder instead of outright deletion if they contain useful historical context.
- **Documentation Consolidation**: Many root `.md` files should likely be moved to `docs/` or consolidated.
