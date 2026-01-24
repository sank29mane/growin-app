## 2026-01-18 - Overly Permissive CORS
**Vulnerability:** The FastAPI backend was configured with `allow_origins=["*"]` and `allow_credentials=True`. This configuration allows any website to make authenticated requests to the backend if the user is logged in or has network access (e.g., DNS rebinding or direct access to localhost).
**Learning:** Default copy-paste CORS configurations often use wildcards for convenience during development but expose significant security risks. Native apps don't typically need CORS, but local development tools might.
**Prevention:** Always use a whitelist of allowed origins. Use environment variables to configure origins for different environments (dev/prod).

## 2026-01-19 - Python Sandbox RCE via Introspection
**Vulnerability:** The `SafePythonExecutor` relied on `ast` parsing to block dunder method access (e.g., `__class__`) but allowed `getattr` in the built-ins. An attacker could bypass the AST check by using `getattr(obj, "__class__")` (with string obfuscation to bypass string filters) to access the class hierarchy, find a class with global scope access (like `catch_warnings`), and gain RCE via `__import__` and `os.system`.
**Learning:** String-based and AST-based sandboxes are fundamentally fragile if they allow reflection/introspection mechanisms like `getattr`. Blacklisting patterns is a losing battle; whitelisting safe operations is harder but necessary. Introspection + Obfuscation = Sandbox Escape.
**Prevention:** Remove `getattr`, `hasattr`, `setattr`, `delattr` from safe built-ins. Ensure the sandbox environment is minimal and does not expose any mechanism to walk the object graph back to globals.
