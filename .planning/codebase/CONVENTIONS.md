# Codebase Conventions: Growin App

## Development Protocol
**SPEC → PLAN → EXECUTE → VERIFY → COMMIT**
- No code without a finalized spec.
- All changes must have empirical verification proof (logs, test output, or screenshot).
- One task = one atomic commit.

## Architectural Mandates
- **Hardware-Awareness**: CPU/GPU/NPU partitioning (CPU for logic, GPU for LLM/Adapters, NPU for Neural JMCE).
- **Resource Lifecycle**: Every asynchronous task or database connection must have an explicit shutdown (`.stop()` or `.close()`).
- **Lazy Initialization**: Background services must not start on module import.

## Coding Style
### Python (Backend)
- **Typing**: Use strict type hints and Pydantic for data models.
- **Dependency Management**: Use `uv` for all operations.
- **Logging**: Use standard `logging` with context-rich failure modes.
- **Performance**: Use vectorization (NumPy/MLX) over Python loops.

### SwiftUI (Frontend)
- **Smoothness**: Use `.equatable()` on views to maintain 120Hz performance.
- **Hardware Integration**: Prefer `Accelerate` and `Metal` for intensive UI/Math tasks.
- **Architecture**: MVVM with distinct separation between Services and ViewModels.

## GSD Operations
- **Context Hygiene**: Keep plans under 50% context usage.
- **State Preservation**: Update `STATE.md` after every significant wave.
- **Search-First**: Always search for relevant snippets before reading entire files.

## Commit Messages
**Format**: `type(scope): description`
- `feat(phase-N)`: New feature.
- `fix(phase-N)`: Bug fix.
- `docs`: Documentation updates.
- `refactor`: Restructure without behavioral change.
- `test`: Adding or updating tests.
