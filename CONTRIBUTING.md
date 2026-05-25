# Contributing to Growin

Thank you for contributing to Growin! This document outlines code standards, architectural constraints, and contribution workflows to maintain system integrity.

---

## 🏗️ Architectural Core Mandates

All contributions must adhere to the core hardware and architectural constraints of the Growin multi-agent framework.

### 1. Unified Memory Rule (M4 Pro/Max/Ultra)
- Local model executions must respect the **60% Unified Memory budget** (≤ 28GB on 48GB platforms).
- Never instantiate unmanaged local model runners. All model routing and completions must go through the centralized vMLX serving layer API (`vmlx_manager`).

### 2. Connection Pool Hygiene
- Never instantiate standalone `httpx.AsyncClient` or standard `requests` clients inside agent files.
- All external outbound HTTP/REST queries must route through the centralized `AgentHttpClient` located in `backend/app_context.py` to prevent TCP socket exhaustion.

### 3. Non-Blocking Event Loop
- Never block FastAPI's async event loop with heavy synchronous computation (e.g., NumPy/pandas iterations or PIL image processing).
- Offload CPU-bound pre-processing or vision tasks to external thread pools using async wrappers like `prepare_vlm_image_async()`.

### 4. Database temporal compatibility
- Avoid using engine-specific date/time interval strings (such as DuckDB's `INTERVAL`) in SQL statements.
- Compute date ranges dynamically using standard Python `datetime` libraries and pass ISO-8601 strings into parameterized queries.

---

## 🛠️ Code Quality Standards

### Python Quality Checklist
- **Typing**: Use static type hints for all function signatures and data structures.
- **Linter & Formatter**: Use **Ruff** for formatting and linting.
- **Exception Handling**: Do not write bare `except:` statements. Always catch standard exceptions explicitly: `except Exception:` or specific sub-classes. This prevents catching OS lifecycle signals (`KeyboardInterrupt`, `SystemExit`) and leaking background processes.

### SwiftUI Quality Checklist
- **Accessibility**: Every interactive view or control component MUST define `.accessibilityLabel()`, `.accessibilityHint()`, and `.accessibilityAddTraits()`.
- **Haptics**: Important confirmations or tactile elements must trigger appropriate native macOS haptic feedback patterns.
- **Display Optimization**: Layouts must compile cleanly and render fluidly at 120Hz ProMotion rates.

---

## 📈 Writing Specialist Agents

To add a new specialist agent to the SwarmOrchestrator roster:

1.  **Register Persona**: Define prompt configurations, tool permissions, and role capability boundaries in `backend/model_config.py`.
2.  **Define Agent Class**: Implement the specialist logic in `backend/agents/`. The class must inherit from the base agent runtime, returning streaming tokens and clear JSON trace structures.
3.  **Implement Governance Limits**: Register the agent's capability bounds in the `GovernanceService` safety filters.
4.  **Register Routing Key**: Update `SwarmOrchestrator` intent mapping lists to route questions to your new agent during the classification phase.

---

## 🧪 Testing Guidelines

Verify your updates pass all system quality tests:
```bash
# Run the complete test suite
uv run pytest

# Run performance benchmarks
uv run pytest tests/benchmarks/ -v
```
- Ensure any added tests do not execute real external API calls. Use mock data or `unittest.mock` configurations aggressively.
