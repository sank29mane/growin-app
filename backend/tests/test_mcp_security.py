from unittest.mock import MagicMock
import os
import sys

# Add backend to path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# MOCK DEPENDENCIES BEFORE IMPORTING APP
# This is necessary because we haven't installed heavy ML libs in the test environment
mock_modules = [
    "duckdb",
    "xgboost", "prophet",
    "torch", "transformers", "mlx", "mlx_lm",
    "rapidfuzz", "newsapi", "tavily", "vaderSentiment",
    "alpaca_trade_api", "granite_tsfm", "langchain",
    "langchain_core", "langchain_openai", "langchain_anthropic",
    "langchain_google_genai", "langchain_ollama"
]

for mod in mock_modules:
    sys.modules[mod] = MagicMock()

from fastapi.testclient import TestClient
from app_context import state
from chat_manager import ChatManager
from server import app

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
        # If this fails with 200, it means the vulnerability exists
        assert response.status_code == 400, f"Expected 400 Bad Request, got {response.status_code}. Vulnerability confirmed if 200."
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

    try:
        # payload with safe command
        payload = {
            "name": "Safe Server",
            "type": "stdio",
            "command": "python",
            "args": ["safe_script.py"],
            "env": {},
            "url": None
        }

        response = client.post("/mcp/servers/add", json=payload)

        # Should be allowed (200)
        assert response.status_code == 200

    finally:
        state.chat_manager.close()
        state.chat_manager = original_manager
