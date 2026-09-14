## 2026-09-14 - AST Sandbox Escape
**Vulnerability:** AST parser only blocked attribute access to dangerous dunder methods, allowing bypass via string literals (e.g. `getattr(obj, "__class__")`).
**Learning:** String literals can be used dynamically with built-ins to access restricted properties.
**Prevention:** Always block string literals containing restricted patterns when creating an AST-based blocklist for sandboxed execution.
