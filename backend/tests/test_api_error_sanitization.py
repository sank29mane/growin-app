import sys
import os
import pytest
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

# Adjust path to include backend root
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import app
from app_context import state

def test_list_conversations_sanitization():
    """Test that listing conversations sanitizes database errors."""
    with TestClient(app) as client:
        # Mock chat_manager.list_conversations to raise exception
        with patch.object(state.chat_manager, 'list_conversations', side_effect=Exception("DB_CONNECTION_STRING_LEAK")):
            response = client.get("/conversations")

            assert response.status_code == 500
            detail = response.json().get("detail")
            assert detail == "Internal Server Error"
            assert "DB_CONNECTION_STRING_LEAK" not in str(response.content)

def test_conversation_history_sanitization():
    """Test that getting history sanitizes database errors."""
    with TestClient(app) as client:
        with patch.object(state.chat_manager, 'load_history', side_effect=Exception("SENSITIVE_SQL_QUERY")):
            response = client.get("/conversations/bad-id/history")

            assert response.status_code == 500
            detail = response.json().get("detail")
            assert detail == "Internal Server Error"
            assert "SENSITIVE_SQL_QUERY" not in str(response.content)

def test_delete_conversation_sanitization():
    """Test that deleting conversation sanitizes errors."""
    with TestClient(app) as client:
        with patch.object(state.chat_manager, 'delete_conversation', side_effect=Exception("DELETE_ERROR_LEAK")):
            response = client.delete("/conversations/bad-id")

            assert response.status_code == 500
            detail = response.json().get("detail")
            assert detail == "Internal Server Error"
            assert "DELETE_ERROR_LEAK" not in str(response.content)

def test_mcp_status_sanitization():
    """Test that MCP status sanitizes errors."""
    with TestClient(app) as client:
        with patch.object(state.chat_manager, 'get_mcp_servers', side_effect=Exception("ENV_VAR_LEAK")):
            response = client.get("/mcp/status")

            assert response.status_code == 500
            detail = response.json().get("detail")
            assert detail == "Internal Server Error"
            assert "ENV_VAR_LEAK" not in str(response.content)

def test_update_t212_config_sanitization():
    """Test that config update sanitizes errors."""
    with TestClient(app) as client:
        # Create a mock connection
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.execute.side_effect = Exception("API_KEY_LEAK_IN_TRACEBACK")
        mock_conn.cursor.return_value = mock_cursor

        # Patch the conn attribute of chat_manager
        with patch.object(state.chat_manager, 'conn', mock_conn):
            response = client.post("/mcp/trading212/config", json={"account_type": "invest"})

            assert response.status_code == 500
            detail = response.json().get("detail")
            assert detail == "Internal Server Error"
            assert "API_KEY_LEAK_IN_TRACEBACK" not in str(response.content)

def test_add_mcp_server_sanitization():
    """Test that adding MCP server sanitizes errors."""
    with TestClient(app) as client:
        # Mock chat_manager.add_mcp_server to raise exception
        with patch.object(state.chat_manager, 'add_mcp_server', side_effect=Exception("SENSITIVE_ARGS_LEAK")):
            response = client.post("/mcp/servers/add", json={"name": "test", "type": "stdio"})

            assert response.status_code == 500
            detail = response.json().get("detail")
            assert detail == "Internal Server Error"
            assert "SENSITIVE_ARGS_LEAK" not in str(response.content)

def test_market_forecast_sanitization():
    """Test that market forecast sanitizes errors."""
    with TestClient(app) as client:
        # Import the router to patch the agent
        from routes.market_routes import forecast_agent

        with patch.object(forecast_agent, 'analyze', side_effect=Exception("FORECAST_MODEL_PATH_LEAK")):
            response = client.get("/api/forecast/AAPL")

            # This endpoint returns 200 with error key
            assert response.status_code == 200
            error_msg = response.json().get("error")
            assert error_msg == "Internal Server Error"
            assert "FORECAST_MODEL_PATH_LEAK" not in str(response.content)
