---
phase: 06-02-npu-math
plan: 02
type: execute
wave: 2
depends_on: [06-02-01]
files_modified:
  - backend/agents/decision_agent.py
  - backend/utils/math_validator.py
  - backend/telemetry_store.py
autonomous: true
requirements: [NPU-05, NPU-06, NPU-07]
must_haves:
  truths:
    - "DecisionAgent successfully offloads math tasks to MathGeneratorAgent"
    - "Scripts are validated via AST and executed in Docker 'npu' engine"
    - "NPU utilization and success metrics are captured"
  artifacts:
    - path: "backend/utils/math_validator.py"
      provides: "Multi-stage validation pipeline"
  key_links:
    - from: "DecisionAgent"
      to: "MathGeneratorAgent"
      via: "make_decision delegation"
    - from: "MathValidator"
      to: "DockerMCPServer"
      via: "execute_script(engine='npu')"
---

<objective>
Integrate the math delegation workflow into the DecisionAgent, implement a robust validation pipeline, and wire up telemetry for performance monitoring.
</objective>

<tasks>
<task type="auto">
  <name>Task 1: Implement Multi-Stage Validation Pipeline</name>
  <files>backend/utils/math_validator.py</files>
  <action>
    Create `MathValidator` class:
    - Stage 1: AST Syntax Validation (ensure no dangerous calls, correct imports).
    - Stage 2: Sandbox Execution Integration. Call `DockerMCPServer.execute_script` with `engine="npu"`.
    - Stage 3: Result Schema Validation. Ensure the script output matches expected JSON/Numeric format.
  </action>
  <verify>Test with a "benign" script and a "malicious/broken" script.</verify>
  <done>Validation pipeline correctly filters and executes scripts.</done>
</task>

<task type="auto">
  <name>Task 2: Update DecisionAgent for Math Delegation</name>
  <files>backend/agents/decision_agent.py</files>
  <action>
    Modify `make_decision` loop:
    - Detect "complex math" triggers (already has a partial check).
    - Instantiate `MathGeneratorAgent`.
    - Delegate script generation.
    - Pass generated script to `MathValidator`.
    - Inject results back into the DecisionAgent's reasoning context.
  </action>
  <verify>Ask the DecisionAgent to "Run a 10,000 path Monte Carlo for AAPL" and check logs for delegation.</verify>
  <done>DecisionAgent seamlessly offloads math and uses the results.</done>
</task>

<task type="auto">
  <name>Task 3: Integrate Math Telemetry</name>
  <files>backend/telemetry_store.py</files>
  <action>
    Add metrics collection for:
    - `math_generation_success` (bool)
    - `math_execution_time_ms` (float)
    - `npu_utilization_proxy` (RAM footprint check < 60% per RESEARCH.md).
    - `sandbox_exit_code`.
  </action>
  <verify>Check `telemetry_store` or logs after a math execution.</verify>
  <done>Performance metrics are recorded for every delegation event.</done>
</task>
</tasks>
