---
phase: 06-02-npu-math
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - backend/schemas.py
  - backend/agents/math_generator_agent.py
  - backend/utils/mlx_injections.py
autonomous: true
requirements: [NPU-01, NPU-02, NPU-03, NPU-04]
must_haves:
  truths:
    - "MathGeneratorAgent can load and run inference with Granite-4.0-Tiny-MLX"
    - "MLX Injections library provides pre-optimized functions for stats"
  artifacts:
    - path: "backend/agents/math_generator_agent.py"
      provides: "Math script generation via local MLX model"
    - path: "backend/utils/mlx_injections.py"
      provides: "Optimized MLX snippets for sandbox injection"
  key_links:
    - from: "MathGeneratorAgent"
      to: "MLXInferenceEngine"
      via: "get_mlx_engine()"
---

<objective>
Establish the foundation for NPU-accelerated math delegation by defining data contracts, creating the specialized MathGeneratorAgent, and building a library of pre-optimized MLX injections.
</objective>

<tasks>
<task type="auto">
  <name>Task 1: Define Math Delegation Contracts</name>
  <files>backend/schemas.py</files>
  <action>
    Add Pydantic models:
    - `MathScriptRequest`: Contains `query`, `context_data` (dict), and `required_stats` (list).
    - `MathScriptResponse`: Contains `script`, `explanation`, and `engine_requirement` (fixed to "npu").
  </action>
  <verify>Run a python script to instantiate these models with sample data.</verify>
  <done>Models are defined and validatable.</done>
</task>

<task type="auto">
  <name>Task 2: Implement MathGeneratorAgent</name>
  <files>backend/agents/math_generator_agent.py</files>
  <action>
    Create `MathGeneratorAgent` class.
    - Initialize with Granite-4.0-Tiny model from `/Users/sanketmane/Codes/Growin App/backend/models/mlx/granite-4.0-h-tiny-MLX-8bit`.
    - Implement `generate_math_script(request: MathScriptRequest) -> MathScriptResponse`.
    - Use a structured prompt template emphasizing idiomatic Python + MLX.
    - Instruction: "Use provided MLX injection functions for heavy lifting. Do not reinvent low-level MLX code."
  </action>
  <verify>Mock a request and check if it generates a Python script containing 'import mlx.core'.</verify>
  <done>Agent produces syntactically correct Python scripts optimized for MLX.</done>
</task>

<task type="auto">
  <name>Task 3: Build NPU Injections Library</name>
  <files>backend/utils/mlx_injections.py</files>
  <action>
    Develop a library of string-based Python snippets using `mlx.core` and `mlx.nn`:
    - `monte_carlo_sim`: Vectorized simulation for price paths.
    - `black_scholes_tensor`: Tensorized option pricing.
    - `technical_indicators_vectorized`: MLX-native RSI/MACD/SMA.
    These will be prepended to generated scripts before execution in the sandbox.
  </action>
  <verify>Verify snippets run locally with a small `mlx.core` test.</verify>
  <done>Library contains at least 3 high-performance financial math functions.</done>
</task>
</tasks>
