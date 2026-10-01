## 2026-09-22 - Fix Information Disclosure in MCP Tool Execution
**Vulnerability:** The `/mcp/tool/call` endpoint was returning raw exception messages directly to clients (`detail=str(e)`). This could inadvertently expose sensitive information like API keys if a tool failed and included its arguments or environment in the error traceback.
**Learning:** Raw exception traces from tool failures can leak secrets.
**Prevention:** Return a generic `Internal Server Error` detail to the client (this is what main does for all MCP routes) and keep full detail in server-side logs, which the existing `SecretMaskingFormatter` protects.

## 2024-09-24 - Sandbox Escape via Dunder Strings and Getattr
**Vulnerability:** Python sandboxes were vulnerable to RCE via string literals representing dunder methods (e.g., __class__), dunder names (e.g. __builtins__) and the getattr function.
**Learning:** In Python AST-based sandboxes, blocking direct attribute access to dangerous dunder methods is insufficient; one must also block string literals starting and ending with __ (via ast.Constant) and block getattr to prevent sandbox escapes.
**Prevention:** Thoroughly validate all possible pathways to sensitive Python internals, including string-based dynamic attribute access and explicit dunder references.
