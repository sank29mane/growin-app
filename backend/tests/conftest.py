import sys
import os
import importlib.util
from unittest.mock import MagicMock, AsyncMock
import pytest

# Ensure backend is in path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock truly heavy/missing dependencies if they aren't installed
modules_to_mock = [
    "mcp", "mcp.server", "mcp.server.stdio", "mcp.types",
    "mcp.client", "mcp.client.stdio", "mcp.client.sse",
    "granite_tsfm",
    "langchain", "langchain_core", "langchain_openai",
    "langchain_anthropic", "langchain_google_genai", "langchain_ollama",
    "chromadb", "duckdb",
    "newsapi", "tavily", "vaderSentiment", "psutil"
]

for module in modules_to_mock:
    # Check if module is already imported
    if module in sys.modules:
        continue

    # Check if module is installable/available
    try:
        # Handle submodules like mcp.server by checking base package first
        base_module = module.split('.')[0]
        if importlib.util.find_spec(base_module) is None:
            sys.modules[module] = MagicMock()
    except (ImportError, AttributeError, ValueError):
        # If any error during check, assume missing and mock
        sys.modules[module] = MagicMock()

from app_context import state

@pytest.fixture(scope="session", autouse=True)
def mock_mcp_client():
    """
    Mock the MCP client to prevent actual subprocess spawning during tests.
    This prevents tests from hanging if the MCP server fails to start.
    """
    # Create an async context manager mock for connect_all
    mock_connect_all = MagicMock()
    mock_connect_all.__aenter__ = AsyncMock(return_value=None)
    mock_connect_all.__aexit__ = AsyncMock(return_value=None)
    
    state.mcp_client.connect_all = MagicMock(return_value=mock_connect_all)
    state.mcp_client.connect_server = AsyncMock(return_value=True)
    state.mcp_client.call_tool = AsyncMock(return_value=[])
    
    yield state.mcp_client

@pytest.fixture(scope="function", autouse=True)
def prevent_db_close():
    """
    Prevent TestClient lifespan from closing the shared database connection.
    Also ensure connection is open.
    """
    # Mock close to do nothing
    original_close = state.chat_manager.close
    state.chat_manager.close = MagicMock()
    
    # Ensure connection is valid
    try:
        state.chat_manager.conn.cursor()
    except Exception:
        # Re-open if closed
        import sqlite3
        state.chat_manager.conn = sqlite3.connect(state.chat_manager.db_path, check_same_thread=False)
        state.chat_manager.conn.row_factory = sqlite3.Row
        state.chat_manager._init_schema()
        
    yield
    
    # Restore
    state.chat_manager.close = original_close