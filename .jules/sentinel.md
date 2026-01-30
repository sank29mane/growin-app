## 2026-01-18 - Overly Permissive CORS
**Vulnerability:** The FastAPI backend was configured with `allow_origins=["*"]` and `allow_credentials=True`. This configuration allows any website to make authenticated requests to the backend if the user is logged in or has network access (e.g., DNS rebinding or direct access to localhost).
**Learning:** Default copy-paste CORS configurations often use wildcards for convenience during development but expose significant security risks. Native apps don't typically need CORS, but local development tools might.
**Prevention:** Always use a whitelist of allowed origins. Use environment variables to configure origins for different environments (dev/prod).

## 2026-01-20 - API Key Leakage via Status Endpoint
**Vulnerability:** The `/mcp/status` and `/mcp/servers` endpoints returned the full configuration of MCP servers, including the `env` dictionary which contains sensitive API keys (e.g., `HF_TOKEN`, `TRADING212_API_KEY`).
**Learning:** Returning internal configuration objects directly to the client often leaks secrets. Even "status" endpoints can be dangerous if they dump raw configuration data.
**Prevention:** Implement strict serialization logic (DTOs) or sanitization helpers for API responses. Never return raw configuration objects that might contain secrets.

## 2026-01-24 - Information Leakage via Exception Details
**Vulnerability:** Several API endpoints (`/conversations/*`, `/mcp/status`) were catching generic exceptions and returning `str(e)` in the HTTP 500 response. This exposed internal database errors, SQL queries, and potentially partial secrets or file paths to the client.
**Learning:** Returning raw exception messages is a common "developer convenience" that becomes a security risk in production. It provides attackers with details about the technology stack (SQLite), schema, and internal logic.
**Prevention:** Catch exceptions and log them server-side with full tracebacks. Return a generic "Internal Server Error" message to the client. Use distinct error handling for expected errors (4xx) vs unexpected ones (5xx).
