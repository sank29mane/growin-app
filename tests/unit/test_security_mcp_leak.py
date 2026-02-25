from fastapi.testclient import TestClient
from server import app

# We rely on conftest.py's prevent_db_close fixture to handle DB connection safety.

def test_mcp_status_leak():
    """Test /mcp/status for leaks"""
    secret_value = "SUPER_SECRET_KEY_123"
    server_name = "LeakTestServer_Status"
    
    with TestClient(app) as client:
        # 1. Add a server with a secret via API
        # The API runs in background task for connection, but adds to DB immediately
        response = client.post("/mcp/servers/add", json={
            "name": server_name,
            "type": "stdio",
            "command": "python",
            "args": ["test.py"],
            "env": {"API_KEY": secret_value}
        })
        if response.status_code != 200:
            print(f"\nDEBUG: POST failed: {response.text}")
        assert response.status_code == 200
        
        try:
            # 2. Call /mcp/status
            response = client.get("/mcp/status")
            assert response.status_code == 200
            data = response.json()
        
            # 3. Check for leak
            servers = data.get("servers", [])
            target_server = next((s for s in servers if s["name"] == server_name), None)
            
            if target_server is None:
                print(f"\nDEBUG: Servers found: {[s['name'] for s in servers]}")
            
            assert target_server is not None
        
            # This assertion CONFIRMS the fix (secret is masked)
            env = target_server.get("env", {})
            assert env.get("API_KEY") == "********", "Vulnerability Fix FAILED: Secret was leaked or not masked correctly!"
            
        finally:
            # Cleanup via API
            client.delete(f"/mcp/servers/{server_name}")

def test_additional_routes_leak():
    """Test /mcp/servers for leaks"""
    secret_value = "SUPER_SECRET_KEY_456"
    server_name = "LeakTestServer_Routes"
    
    with TestClient(app) as client:
        # 1. Add server via API
        client.post("/mcp/servers/add", json={
            "name": server_name,
            "type": "stdio",
            "command": "python",
            "args": ["test.py"],
            "env": {"API_KEY": secret_value}
        })
        
        try:
            # 2. Call /mcp/servers
            response = client.get("/mcp/servers")
            assert response.status_code == 200
            data = response.json()
        
            # 3. Check for leak
            servers = data.get("servers", [])
            target_server = next((s for s in servers if s["name"] == server_name), None)
            
            if target_server is None:
                print(f"\nDEBUG: Servers found: {[s['name'] for s in servers]}")
            
            assert target_server is not None
        
            env = target_server.get("env", {})
            assert env.get("API_KEY") == "********", "Vulnerability Fix FAILED in /mcp/servers!"
            
        finally:
            client.delete(f"/mcp/servers/{server_name}")