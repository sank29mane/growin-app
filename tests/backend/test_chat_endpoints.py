
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
import model_registry_testkit as kit
import tempfile
import secrets

class TestChatEndpoints(unittest.IsolatedAsyncioTestCase):
    
    def setUp(self):
        # Models come from the registry (Phase 67); the stub URL is never contacted
        # because the orchestrator is mocked below.
        self._tmp = tempfile.TemporaryDirectory()
        self._old_key = os.environ.get(kit.XAI_KEY_ENV)
        os.environ[kit.XAI_KEY_ENV] = "stubkey-" + secrets.token_hex(8)
        from pathlib import Path
        kit.activate(kit.make_registry(Path(self._tmp.name), "http://127.0.0.1:9"))

        # Use in-memory DB for testing
        self.chat_manager = ChatManager(db_path=":memory:")
        state.chat_manager = self.chat_manager
        
        # Mock mcp_client to avoid connection errors
        state.mcp_client = MagicMock()
        state.mcp_client.session = True # Simulate connected
        
        # Mock Orchestrator Agent
        # Note: They are imported inside the function, so we must patch the source modules
        self.orchestrator_patcher = patch('agents.orchestrator_agent.OrchestratorAgent')
        self.MockOrchestrator = self.orchestrator_patcher.start()
        
        # Setup Mock behaviors
        self.mock_orchestrator_instance = self.MockOrchestrator.return_value
        
        # Configure run as AsyncMock
        mock_context = MagicMock()
        mock_context.model_dump.return_value = {}
        self.mock_orchestrator_instance.run = AsyncMock(return_value={
            "content": "This is a test response.",
            "response_id": "test_id",
            "context": mock_context
        })

    def tearDown(self):
        kit.activate(None)
        if self._old_key is None:
            os.environ.pop(kit.XAI_KEY_ENV, None)
        else:
            os.environ[kit.XAI_KEY_ENV] = self._old_key
        self._tmp.cleanup()
        self.chat_manager.close()
        self.orchestrator_patcher.stop()

    async def test_chat_message_success_and_timestamp(self):
        """Test that chat_message returns success and valid ISO timestamp"""
        request = ChatMessage(message="Hello")
        
        # Mock update_conversation_title_if_needed to do nothing or return success
        with patch('routes.chat_routes.update_conversation_title_if_needed') as mock_title:
             response = await chat_message(request, accept="application/json")
        
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

    async def test_error_handling_generic_failure(self):
        """Test that specific error messages are raised"""
        request = ChatMessage(message="Crash me")

        # Make Orchestrator raise an exception mimicking a total failure
        err_msg = "Total failure... model could not be initialized."
        self.mock_orchestrator_instance.run.side_effect = RuntimeError(err_msg)
        
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            await chat_message(request, accept="application/json")
    
        self.assertEqual(cm.exception.status_code, 500)
        self.assertIn("Internal Server Error", cm.exception.detail)
if __name__ == '__main__':
    unittest.main()
