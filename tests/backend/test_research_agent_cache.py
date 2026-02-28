import asyncio
import os
import sys
import unittest
from unittest.mock import MagicMock, patch, mock_open, AsyncMock

# Add backend to path
sys.path.append(os.path.abspath("backend"))

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

        # Mock LMStudioClient correctly within this test context
        with patch('agents.llm_factory.LLMFactory.create_llm', new_callable=AsyncMock) as mock_factory:
            mock_client_instance = MagicMock()
            mock_factory.return_value = mock_client_instance

            # Async methods on the client need to return awaitables (Futures)
            mock_client_instance.check_connection = AsyncMock(return_value=True)
            mock_client_instance.list_models = AsyncMock(return_value=[{"id": "model-1"}])
            mock_client_instance.chat = AsyncMock(return_value={"content": '{"q": "test query"}'})

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
