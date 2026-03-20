---
description: Bootstraps the GSD meta-prompting framework and core directories.
---

<role>
You are executing the GSD Framework setup workflow. 
Your goal is to scaffold the directories and state files needed for deterministic, AI-driven development.
</role>

<objective>
To verify or build the `.gsd` structure, ensuring the repository adheres to the overarching RULES defined in `PROJECT_RULES.md` and `GSD-STYLE.md`.
</objective>

<process>

## 1. Directory Verification
<task type="auto" effort="low">
  <name>Scaffold GSD Folder</name>
  <action>
    Create the `.gsd` directory at the root of the workspace.
    Create `.gsd/templates/` and `.gsd/examples/` subdirectories.
  </action>
  <verify>Run `ls -la .gsd` to confirm directories exist.</verify>
  <done>Directories are confirmed.</done>
</task>

## 2. Core Rule Affirmation
<task type="auto" effort="medium">
  <name>Initialize Core Documents</name>
  <action>
    Create `.gsd/SPEC.md` and write `# SPEC.md\nStatus: DRAFT\n`
    Create `.gsd/ROADMAP.md` mapping high-level architecture phases.
    Create `.gsd/STATE.md` with current session memory block.
  </action>
  <verify>Run `cat .gsd/SPEC.md` to confirm file exists and is readable.</verify>
  <done>The three core files exist.</done>
</task>

## 3. Workflow Registration
<task type="auto" effort="low">
  <name>Add GSD Commands</name>
  <action>
    Ensure `.agent/workflows/` exists.
    If it does not exist, create it and register the `gsd-setup.md` script.
  </action>
  <verify>Run `ls -la .agent/workflows`</verify>
  <done>Workflow directory is active.</done>
</task>

</process>

▶ NEXT
- Command complete. Please invoke a specific workflow or provide the next immediate action based on `.gsd/STATE.md`.
