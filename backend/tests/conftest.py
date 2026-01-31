import pytest
from unittest.mock import MagicMock, AsyncMock
import sys
import os

# Ensure backend is in path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
    
    # Restore (optional, but good practice if we were really closing)
    # But since we share state, we might just leave it mocked or rely on process exit.
    state.chat_manager.close = original_close
