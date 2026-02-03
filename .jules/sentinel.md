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

## 2026-02-05 - Missing Security Headers
**Vulnerability:** The application lacked standard security headers (`X-Content-Type-Options`, `X-Frame-Options`, `Referrer-Policy`, `Content-Security-Policy`), leaving it vulnerable to clickjacking and MIME-type sniffing.
**Learning:** FastAPI does not include these headers by default. A dedicated middleware is the cleanest way to enforce them globally.
**Prevention:** Use a `SecurityHeadersMiddleware` to inject headers into every response. Enforce strict CSP where possible.

## 2026-02-14 - Information Leakage via Exception Details (Recurring)
**Vulnerability:** Several endpoints in `mcp_routes`, `chat_routes`, and `market_routes` were catching generic exceptions and returning `str(e)` in the JSON body or HTTP exception detail. This exposed internal error states, potentially leaking file paths, partial keys, or database schema info.
**Learning:** The "Information Leakage via Exception Details" pattern persists because it's the easiest way to debug during development. Developers copy-paste exception handling blocks.
**Prevention:** Strictly enforce a "Sanitize All Errors" policy. Use `logging.error(..., exc_info=True)` for debugging, but ALWAYS return "Internal Server Error" to the client for 500s. I've updated the test suite to explicitly check for this leakage.

## 2026-02-20 - Command Injection via MCP Configuration
**Vulnerability:** The `/mcp/servers/add` endpoint accepted any string as the `command` for a new MCP server. This allowed defining servers that execute dangerous commands (e.g., `bash`, `rm`) instead of valid tools.
**Learning:** Even in "safe" subprocess calls (list-based args), the executable itself must be validated. Furthermore, `os.path.basename` is platform-specific; a Windows path like `C:\Windows\cmd.exe` is treated as a single filename on Linux, bypassing blocklists that check the basename.
**Prevention:** Implement a strict blocklist (or allowlist) for executables. normalize paths by replacing backslashes with forward slashes before splitting to ensure cross-platform safety.

## 2026-02-21 - Command Injection via Interpreter Arguments
**Vulnerability:** The MCP configuration allowed setting `command` to `python` (which is allowed) but permitted passing dangerous flags like `-c` or `-e` in the `args` array, enabling inline code execution that bypassed the script file requirement.
**Learning:** Validating the executable name is insufficient for interpreters (Python, Node, etc.). The arguments must also be sanitized to prevent "flag injection" attacks that turn a safe interpreter invocation into a command execution primitive.
**Prevention:** For known interpreters, strictly whitelist allowed arguments or blocklist dangerous flags (e.g., `-c`, `-e`, `-r`).
