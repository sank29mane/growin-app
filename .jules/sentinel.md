
## 2025-02-18 - Missing Security Headers in Backend
**Vulnerability:** The backend FastAPI application was missing standard security headers like `Strict-Transport-Security` (HSTS) and `X-XSS-Protection`.
**Learning:** These headers provide important defense-in-depth against attacks like Man-in-the-Middle and cross-site scripting. Without HSTS, applications are vulnerable to downgrade attacks. Without X-XSS-Protection, older browsers may lack necessary filtering for reflected XSS.
**Prevention:** Ensure all applications, even when running behind reverse proxies, explicitly configure security middleware to inject these fundamental headers by default.

## 2025-02-18 - Incorrect Security Header Configurations
**Vulnerability:** The application was setting `Strict-Transport-Security` with `preload` on loopback HTTP requests and using `X-XSS-Protection: 1; mode=block`.
**Learning:** `preload` should only be used if the host is actively submitted to the HSTS preload list; otherwise, it is inert. Additionally, modern security guidance recommends `X-XSS-Protection: 0` because the XSS auditor has been removed from modern browsers and can actually introduce vulnerabilities in some contexts when enabled.
**Prevention:** Avoid blindly applying generic security headers without considering the specific deployment environment (e.g., loopback vs public) and modern browser behaviors.
