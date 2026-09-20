## 2024-05-24 - [Sandbox Escapes via string literals]
**Vulnerability:** AST-based sandbox allowed strings starting and ending with `__` which could bypass dunder blocking.
**Learning:** Checking for `ast.Attribute` is not enough as attributes can be accessed via `getattr` and string representations or by calling `__builtins__` functions directly via string evaluation. We must also block `ast.Constant` containing string literals matching dunder patterns.
**Prevention:** In AST-based sandboxes, explicitly block `ast.Constant` values that are strings starting and ending with `__` to prevent string-based bypasses.
