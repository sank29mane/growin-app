## 2024-05-18 - Python AST Sandbox Escape via getattr
**Vulnerability:** Python AST-based sandboxes (`safe_python.py`, `math_validator.py`) were blocking direct dunder method access (e.g., `obj.__class__`) but missed string literals. This allowed bypasses using `getattr(obj, '__class__')` because `getattr` string arguments were not checked.
**Learning:** Blocking `ast.Attribute` is insufficient for python sandboxing; string constants used with dynamic attribute lookup functions can bypass attribute-level checks.
**Prevention:** Block `ast.Constant` string literals that match dunder patterns (starting and ending with `__`) in addition to blocking `ast.Attribute`.
