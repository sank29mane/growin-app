import sys
import os
from unittest.mock import MagicMock, patch
from fastapi.testclient import TestClient

# Adjust path to include backend root
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import app
from app_context import state

def test_chat_error_handling_sanitization():
    """Test that chat endpoint sanitizes exception details."""
    # Use context manager to ensure startup/shutdown
    with TestClient(app) as client:
        with patch("agents.coordinator_agent.CoordinatorAgent") as mock_coordinator:
            mock_instance = MagicMock()
            mock_instance.process_query.side_effect = Exception("SENSITIVE_DB_INFO_LEAKED_CHAT")
            mock_coordinator.return_value = mock_instance

            response = client.post("/api/chat/message", json={"message": "Hello", "conversation_id": "test_conv"})

            # If coordinator fails but system is resilient, it might return 200 via fallback
            # We want to ensure no sensitive DB info is leaked in the response content regardless of code
            assert response.status_code in [200, 500]
            data = response.json()
            detail = str(data.get("detail", data.get("response", "")))

            assert "SENSITIVE_DB_INFO_LEAKED_CHAT" not in detail
            if response.status_code == 500:
                assert "Internal Server Error" in detail

def test_analyze_error_handling_sanitization():
    """Test that analyze endpoint sanitizes exception details."""
    with TestClient(app) as client:
        # Mock MCP session AFTER startup
        state.mcp_client.primary_session_name = "mock"
        state.mcp_client.sessions = {"mock": MagicMock()}

        with patch("agents.coordinator_agent.CoordinatorAgent") as mock_coordinator:
            mock_instance = MagicMock()
            mock_instance.process_query.side_effect = Exception("SENSITIVE_INFO_LEAKED_ANALYZE")
            mock_coordinator.return_value = mock_instance

            response = client.post("/agent/analyze", json={"query": "Analyze AAPL"})

            # If coordinator fails but system is resilient, it might return 200 via fallback
            assert response.status_code in [200, 500]
            data = response.json()
            detail = str(data.get("detail", data.get("final_answer", "")))

            assert "SENSITIVE_INFO_LEAKED_ANALYZE" not in detail
            if response.status_code == 500:
                assert "Internal Server Error" in detail

def test_market_goal_error_handling_sanitization():
    """Test that market goal endpoint sanitizes exception details."""
    with TestClient(app) as client:
        # Mock MCP session just in case
        state.mcp_client.sessions = {"mock": MagicMock()}

        with patch("agents.goal_planner_agent.GoalPlannerAgent") as mock_planner:
            mock_instance = MagicMock()
            mock_instance.analyze.side_effect = Exception("SENSITIVE_INFO_LEAKED_GOAL")
            mock_planner.return_value = mock_instance

            payload = {
                "initial_capital": 1000,
                "target_returns_percent": 10.0,
                "duration_years": 5.0,
                "risk_profile": "MEDIUM"
            }
            response = client.post("/api/goal/plan", json=payload)

            assert response.status_code == 500
            data = response.json()
            detail = str(data.get("detail", ""))

            assert "SENSITIVE_INFO_LEAKED_GOAL" not in detail
            assert "Internal Server Error" in detail
