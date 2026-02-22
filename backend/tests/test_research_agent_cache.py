import asyncio
import os
import sys
import unittest
from unittest.mock import MagicMock, patch, mock_open

# Add backend to path
sys.path.append(os.path.abspath("backend"))

# Mock imports that might fail or are irrelevant
mock_lm_client_module = MagicMock()
sys.modules["lm_studio_client"] = mock_lm_client_module

mock_vader = MagicMock()
sys.modules["vaderSentiment"] = mock_vader
sys.modules["vaderSentiment.vaderSentiment"] = mock_vader

# Mock heavy dependencies
sys.modules["chromadb"] = MagicMock()
sys.modules["chromadb.config"] = MagicMock()
sys.modules["chromadb.utils"] = MagicMock()
sys.modules["sklearn"] = MagicMock()
sys.modules["sklearn.preprocessing"] = MagicMock()
sys.modules["sklearn.linear_model"] = MagicMock()
sys.modules["pandas"] = MagicMock()

# Mock other potential dependencies
sys.modules["newsapi"] = MagicMock()
sys.modules["tavily"] = MagicMock()
sys.modules["httpx"] = MagicMock()

# Mock peer agents to avoid their dependencies loading
sys.modules["agents.quant_agent"] = MagicMock()
sys.modules["agents.portfolio_agent"] = MagicMock()
sys.modules["agents.forecasting_agent"] = MagicMock()
sys.modules["agents.social_agent"] = MagicMock()
sys.modules["agents.whale_agent"] = MagicMock()
sys.modules["agents.goal_planner_agent"] = MagicMock()

# We also need to mock app_context because it imports rag_manager -> chromadb
sys.modules["app_context"] = MagicMock()
sys.modules["rag_manager"] = MagicMock()

# Also mock market_context which ResearchAgent imports
# Actually ResearchAgent imports from market_context, so we might need it real or mocked
# market_context.py defines dataclasses, which might depend on pydantic (installed)
# Let's see if we can use the real one or if it pulls in stuff.
# market_context imports are simple usually.

from agents.research_agent import ResearchAgent

class TestResearchAgentCache(unittest.TestCase):
    def setUp(self):
        # Setup environment
        self.env_patcher = patch.dict(os.environ, {
            "NEWSAPI_KEY": "valid_key_length_greater_than_10",
            "TAVILY_API_KEY": "valid_key_length_greater_than_10",
            "NEWSDATA_API_KEY": "valid_key_length_greater_than_10"
        })
        self.env_patcher.start()

        self.agent = ResearchAgent()

    def tearDown(self):
        self.env_patcher.stop()

    def test_cache_initialization(self):
        """Test that _prompt_template is initialized to None."""
        self.assertIsNone(self.agent._prompt_template)

    def test_smart_query_caching(self):
        """Test that the prompt template is cached after the first call."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        # Configure the mock LMStudioClient
        mock_client_instance = mock_lm_client_module.LMStudioClient.return_value

        # Async methods on the client need to return awaitables (Futures)
        f_conn = asyncio.Future()
        f_conn.set_result(True)
        mock_client_instance.check_connection.return_value = f_conn

        f_models = asyncio.Future()
        f_models.set_result([{"id": "model-1"}])
        mock_client_instance.list_models.return_value = f_models

        f_chat = asyncio.Future()
        f_chat.set_result({"content": '{"q": "test query"}'})
        mock_client_instance.chat.return_value = f_chat

        # Mock open() to track file access
        m_open = mock_open(read_data="template {{query}}")

        with patch('builtins.open', m_open):
            # First call
            loop.run_until_complete(self.agent._generate_smart_query("test query"))

            # Verify template is cached
            self.assertEqual(self.agent._prompt_template, "template {{query}}")

            # Verify file was opened
            self.assertTrue(m_open.called, "File should have been opened on first call")
            first_call_count = m_open.call_count

            # Second call
            loop.run_until_complete(self.agent._generate_smart_query("test query"))

            # Verify file was NOT opened again
            self.assertEqual(m_open.call_count, first_call_count, "File should not be opened on second call")

        loop.close()

if __name__ == "__main__":
    unittest.main()
