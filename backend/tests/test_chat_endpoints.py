
import unittest
from unittest.mock import MagicMock, patch, AsyncMock
import sys
import os
from datetime import datetime

# Add backend to sys.path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app_context import state, ChatMessage
from chat_manager import ChatManager
from routes.chat_routes import chat_message, list_conversations, get_conversation_history

class TestChatEndpoints(unittest.IsolatedAsyncioTestCase):
    
    def setUp(self):
        # Use in-memory DB for testing
        self.chat_manager = ChatManager(db_path=":memory:")
        state.chat_manager = self.chat_manager
        
        # Mock mcp_client to avoid connection errors
        state.mcp_client = MagicMock()
        state.mcp_client.session = True # Simulate connected
        
        # Mock Coordinator and Decision Agent
        # Note: They are imported inside the function, so we must patch the source modules
        self.coordinator_patcher = patch('agents.coordinator_agent.CoordinatorAgent')
        self.decision_patcher = patch('agents.decision_agent.DecisionAgent')
        self.MockCoordinator = self.coordinator_patcher.start()
        self.MockDecision = self.decision_patcher.start()
        
        # Setup Mock behaviors
        self.mock_coordinator_instance = self.MockCoordinator.return_value
        # Configure process_query as AsyncMock
        self.mock_coordinator_instance.process_query = AsyncMock(return_value=MagicMock(
            user_context={},
            model_dump=lambda: {},
            dict=lambda: {}
        ))
        
        self.mock_decision_instance = self.MockDecision.return_value
        # Configure methods as AsyncMock
        self.mock_decision_instance.make_decision = AsyncMock(return_value="This is a test response.")
        self.mock_decision_instance.generate_response = AsyncMock(return_value="Test Title")

    def tearDown(self):
        self.chat_manager.close()
        self.coordinator_patcher.stop()
        self.decision_patcher.stop()

    async def test_chat_message_success_and_timestamp(self):
        """Test that chat_message returns success and valid ISO timestamp"""
        request = ChatMessage(message="Hello", model_name="test-model")
        
        # Mock update_conversation_title_if_needed to do nothing or return success
        with patch('routes.chat_routes.update_conversation_title_if_needed') as mock_title:
             response = await chat_message(request)
        
        self.assertIn("response", response)
        self.assertEqual(response["response"], "This is a test response.")
        self.assertIn("timestamp", response)
        
        # Verify timestamp format (ISO 8601 with Z)
        ts = response["timestamp"]
        # Should end with Z
        self.assertTrue(ts.endswith("Z"))
        # Should be parseable
        try:
            # Python's fromisoformat doesn't handle Z until 3.11, replace Z with +00:00 for test
            datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            self.fail(f"Timestamp {ts} is not valid ISO 8601")

    async def test_conversation_history_timestamps(self):
        """Test that history returns correct timestamps"""
        cid = self.chat_manager.create_conversation("Test Chat")
        self.chat_manager.save_message(cid, "user", "Hello")
        
        history = await get_conversation_history(cid)
        self.assertTrue(len(history) > 0)
        
        msg = history[0]
        self.assertIn("timestamp", msg)
        self.assertIn("message_id", msg) # Verify ID alias
        ts = msg["timestamp"]
        
        # Check format: YYYY-MM-DDTHH:MM:SSZ
        import re
        self.assertTrue(re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", ts), f"Timestamp {ts} format incorrect")

    async def test_list_conversations_timestamps(self):
        """Test listing conversations returns correct timestamp format"""
        self.chat_manager.create_conversation("Test Chat 1")
        
        conversations = await list_conversations()
        self.assertTrue(len(conversations) > 0)
        
        conv = conversations[0]
        self.assertIn("created_at", conv)
        ts = conv["created_at"]
        
        import re
        self.assertTrue(re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", ts), f"Timestamp {ts} format incorrect")

    async def test_error_handling_native_mlx(self):
        """Test that specific error messages are raised"""
        request = ChatMessage(message="Crash me", model_name="native-mlx")
        
        # Make DecisionAgent raise an exception mimicking the fallback failure
        self.mock_decision_instance.make_decision.side_effect = RuntimeError("Total failure... native-mlx fallback failed.")
        
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            await chat_message(request)
    
        self.assertEqual(cm.exception.status_code, 500)
        self.assertIn("MLX Model Error", cm.exception.detail)
if __name__ == '__main__':
    unittest.main()
