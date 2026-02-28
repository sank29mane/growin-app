import sys
import os
from fastapi.testclient import TestClient

# Adjust path to include backend root
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import app

client = TestClient(app)

def test_cors_security_evil_origin():
    """Verify that malicious origins are blocked."""
    headers = {"Origin": "http://evil.com"}
    response = client.get("/", headers=headers)
    acao = response.headers.get("access-control-allow-origin")

    assert acao != "*", "CORS should not allow wildcard origin"
    assert acao != "http://evil.com", "CORS should not allow evil.com"
    assert acao is None, "CORS headers should not be present for disallowed origin"

def test_cors_security_allowed_origin():
    """Verify that allowed origins are accepted."""
    allowed_origin = "http://localhost:8002"
    headers = {"Origin": allowed_origin}
    response = client.get("/", headers=headers)
    acao = response.headers.get("access-control-allow-origin")

    assert acao == allowed_origin, f"CORS should allow {allowed_origin}"
