## 2026-10-27 - Sandbox Escape via Dunder String Literals
**Vulnerability:** AST validation in safe_python.py and math_validator.py only blocked direct attribute access for dangerous dunder methods but allowed string literals starting and ending with `__`.
**Learning:** Malicious code could use string constants with built-ins to bypass AST checks.
**Prevention:** Block string literals starting and ending with `__` (via `ast.Constant`) to prevent sandbox escapes.
