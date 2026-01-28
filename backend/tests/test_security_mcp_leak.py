import pytest
import os
from fastapi.testclient import TestClient
from app_context import state
from chat_manager import ChatManager
from server import app

# Setup a temporary DB for the test
TEST_DB = "test_security_repro.db"

@pytest.fixture(scope="module")
def client():
    # Setup
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)

    # Override global state chat_manager
    original_manager = state.chat_manager
    state.chat_manager = ChatManager(db_path=TEST_DB)

    # Create TestClient
    with TestClient(app) as c:
        yield c

    # Teardown
    state.chat_manager.close()
    state.chat_manager = original_manager
    if os.path.exists(TEST_DB):
        os.remove(TEST_DB)

def test_mcp_status_leak(client):
    # 1. Add a server with a secret
    secret_value = "SUPER_SECRET_KEY_123"
    state.chat_manager.add_mcp_server(
        name="LeakTestServer",
        type="stdio",
        command="python",
        args=["test.py"],
        env={"API_KEY": secret_value}
    )

    # 2. Call /mcp/status
    response = client.get("/mcp/status")
    assert response.status_code == 200
    data = response.json()

    # 3. Check for leak
    servers = data.get("servers", [])
    target_server = next((s for s in servers if s["name"] == "LeakTestServer"), None)
    assert target_server is not None

    # This assertion CONFIRMS the fix (secret is masked)
    env = target_server.get("env", {})
    assert env.get("API_KEY") == "********", "Vulnerability Fix FAILED: Secret was leaked or not masked correctly!"

def test_additional_routes_leak(client):
    # 1. Server already added in previous test (scope module)

    # 2. Call /mcp/servers
    response = client.get("/mcp/servers")
    assert response.status_code == 200
    data = response.json()

    # 3. Check for leak
    servers = data.get("servers", [])
    target_server = next((s for s in servers if s["name"] == "LeakTestServer"), None)
    assert target_server is not None

    env = target_server.get("env", {})
    assert env.get("API_KEY") == "********", "Vulnerability Fix FAILED in /mcp/servers!"
