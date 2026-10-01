
## 2025-02-18 - Missing Security Headers in Backend
**Vulnerability:** The backend FastAPI application was missing standard security headers like `Strict-Transport-Security` (HSTS) and `X-XSS-Protection`.
**Learning:** These headers provide important defense-in-depth against attacks like Man-in-the-Middle and cross-site scripting. Without HSTS, applications are vulnerable to downgrade attacks. Without X-XSS-Protection, older browsers may lack necessary filtering for reflected XSS.
**Prevention:** Ensure all applications, even when running behind reverse proxies, explicitly configure security middleware to inject these fundamental headers by default.
