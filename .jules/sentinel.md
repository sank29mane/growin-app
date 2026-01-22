## 2026-01-18 - Overly Permissive CORS
**Vulnerability:** The FastAPI backend was configured with `allow_origins=["*"]` and `allow_credentials=True`. This configuration allows any website to make authenticated requests to the backend if the user is logged in or has network access (e.g., DNS rebinding or direct access to localhost).
**Learning:** Default copy-paste CORS configurations often use wildcards for convenience during development but expose significant security risks. Native apps don't typically need CORS, but local development tools might.
**Prevention:** Always use a whitelist of allowed origins. Use environment variables to configure origins for different environments (dev/prod).
