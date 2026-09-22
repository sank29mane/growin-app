## 2026-09-22 - Fix Information Disclosure in MCP Tool Execution
**Vulnerability:** The `/mcp/tool/call` endpoint was returning raw exception messages directly to clients (`detail=str(e)`). This could inadvertently expose sensitive information like API keys if a tool failed and included its arguments or environment in the error traceback.
**Learning:** Even internal tool execution errors must be sanitized before being returned over the network. Unhandled or overly verbose exceptions are a common source of data leakage.
**Prevention:** Always use generic error messages (like "Internal Server Error") for unhandled exceptions on API endpoints, while logging the full exception context securely on the server-side.
