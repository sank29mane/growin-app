import sys
import os
import unittest
from unittest.mock import MagicMock, patch, AsyncMock

# Ensure backend is in path
sys.path.append(os.path.join(os.getcwd(), 'backend'))

from agents.coordinator_agent import CoordinatorAgent

class TestCoordinatorFixes(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Mock dependencies
        self.mock_mcp_client = MagicMock()
        self.agent = CoordinatorAgent(self.mock_mcp_client, model_name="test-model")
        # Disable LLM for these tests
        self.agent.llm = None
        # Mock methods that are async
        self.agent._classify_intent = AsyncMock(return_value={"type": "analytical", "needs": []})
        self.agent._fetch_ohlcv = AsyncMock(return_value=[])

    async def test_resolve_ticker_from_history_dots(self):
        history = [
            {"role": "user", "content": "What do you think about VOD.L?"}
        ]
        ticker = self.agent._resolve_ticker_from_history(history)
        self.assertEqual(ticker, "VOD.L")

    async def test_resolve_ticker_from_history_digits(self):
        history = [
            {"role": "user", "content": "Analyze 3GLD please."}
        ]
        ticker = self.agent._resolve_ticker_from_history(history)
        self.assertEqual(ticker, "3GLD")

    async def test_resolve_ticker_from_history_mixed(self):
        history = [
            {"role": "user", "content": "Compare AAPL and MSFT"}
        ]
        ticker = self.agent._resolve_ticker_from_history(history)
        self.assertEqual(ticker, "MSFT")

    async def test_resolve_ticker_from_history_ignore_stops(self):
        history = [
            {"role": "user", "content": "Is this good?"}
        ]
        ticker = self.agent._resolve_ticker_from_history(history)
        self.assertIsNone(ticker)

    @patch('status_manager.status_manager')
    async def test_process_query_normalization(self, mock_status):
        # Mock the import of normalize_ticker inside the method
        with patch.dict(sys.modules, {'trading212_mcp_server': MagicMock()}):
            mock_normalize = MagicMock()
            mock_normalize.side_effect = lambda x: x + ".L" if x == "VOD" else x
            sys.modules['trading212_mcp_server'].normalize_ticker = mock_normalize

            context = await self.agent.process_query("Analyze VOD", ticker="VOD")

            mock_normalize.assert_called_with("VOD")
            self.assertEqual(context.ticker, "VOD.L")

if __name__ == '__main__':
    unittest.main()
