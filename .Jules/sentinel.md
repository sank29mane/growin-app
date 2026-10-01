## 2026-09-22 - Fix Information Disclosure in MCP Tool Execution
**Vulnerability:** The `/mcp/tool/call` endpoint was returning raw exception messages directly to clients (`detail=str(e)`). This could inadvertently expose sensitive information like API keys if a tool failed and included its arguments or environment in the error traceback.
**Learning:** Raw exception traces from tool failures can leak secrets.
**Prevention:** Return a generic `Internal Server Error` detail to the client (this is what main does for all MCP routes) and keep full detail in server-side logs, which the existing `SecretMaskingFormatter` protects.
