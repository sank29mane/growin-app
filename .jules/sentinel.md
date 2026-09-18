## 2024-05-18 - Block Python AST Sandbox Escapes via Strings
**Vulnerability:** Python sandbox code execution could bypass dunder method checks by using string literals and `getattr` (e.g., `getattr(obj, '__class__')`).
**Learning:** Checking only `ast.Attribute` is insufficient in AST-based sandboxes because `__builtins__` or dunder methods can be passed as strings to functions like `getattr`.
**Prevention:** Explicitly block `ast.Constant` string literals that start and end with `__` to ensure dangerous properties cannot be accessed dynamically by name.
