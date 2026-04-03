from unittest.mock import MagicMock
import os
import sys

# Ensure backend is in path for absolute imports if needed, 
# but we should rely on PYTHONPATH=.
from fastapi.testclient import TestClient
from app_context import state
from chat_manager import ChatManager
from server import app

# Use TestClient (lifespan is triggered on first request if not specified)
client = TestClient(app)

def test_block_malicious_commands():
    """
    Test that dangerous commands like 'bash' are blocked.
    """
    # Use in-memory DB for this test
    original_manager = state.chat_manager
    state.chat_manager = ChatManager(":memory:")

    try:
        # payload with dangerous command
        payload = {
            "name": "Malicious Server",
            "type": "stdio",
            "command": "bash",
            "args": ["-c", "echo hacked"],
            "env": {},
            "url": None
        }

        response = client.post("/mcp/servers/add", json=payload)

        # Should be blocked (400)
        assert response.status_code == 400
        assert "not allowed" in response.json().get("detail", "").lower()

    finally:
        state.chat_manager.close()
        state.chat_manager = original_manager

def test_allow_safe_command():
    """
    Test that safe commands like 'python' are allowed.
    """
    # Use in-memory DB for this test
    original_manager = state.chat_manager
    state.chat_manager = ChatManager(":memory:")
    
    # Create a dummy safe_script.py so the background connection task doesn't complain
    safe_script_path = os.path.join(os.getcwd(), "safe_script.py")
    with open(safe_script_path, "w") as f:
        f.write("print('safe')")

    try:
        # payload with safe command
        payload = {
            "name": "Safe Server",
            "type": "stdio",
            "command": "python3", # Using python3 for better compatibility
            "args": ["safe_script.py"],
            "env": {},
            "url": None
        }

        response = client.post("/mcp/servers/add", json=payload)

        # Should be allowed (200)
        assert response.status_code == 200

    finally:
        if os.path.exists(safe_script_path):
            os.remove(safe_script_path)
        state.chat_manager.close()
        state.chat_manager = original_manager
