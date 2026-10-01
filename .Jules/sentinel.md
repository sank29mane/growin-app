## 2024-09-24 - Sandbox Escape via Dunder Strings and Getattr
**Vulnerability:** Python sandboxes were vulnerable to RCE via string literals representing dunder methods (e.g., __class__), dunder names (e.g. __builtins__) and the getattr function.
**Learning:** In Python AST-based sandboxes, blocking direct attribute access to dangerous dunder methods is insufficient; one must also block string literals starting and ending with __ (via ast.Constant) and block getattr to prevent sandbox escapes.
**Prevention:** Thoroughly validate all possible pathways to sensitive Python internals, including string-based dynamic attribute access and explicit dunder references.
