import sys
import os
from fastapi.testclient import TestClient

# Add backend to path so we can import server
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from server import app

client = TestClient(app)

def test_security_headers_middleware():
    """Verify that security headers are added to responses."""
    response = client.get("/health")
    assert response.status_code == 200

    headers = response.headers

    # Check for security headers
    assert headers.get("X-Content-Type-Options") == "nosniff"
    assert headers.get("X-Frame-Options") == "DENY"
    assert headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"

    # Basic CSP check
    csp = headers.get("Content-Security-Policy", "")
    assert "default-src 'self'" in csp

def test_security_headers_on_error():
    """Verify headers are present even on error responses."""
    # Force a 404
    response = client.get("/non-existent-endpoint")
    assert response.status_code == 404

    headers = response.headers
    assert headers.get("X-Content-Type-Options") == "nosniff"
    assert headers.get("X-Frame-Options") == "DENY"
