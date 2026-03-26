import sys
import os
import unittest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

# Ensure backend path is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from backend.agents.coordinator_agent import CoordinatorAgent
from backend.utils.ticker_utils import TickerResolver

class TestCoordinatorTickerFix(unittest.IsolatedAsyncioTestCase):

    async def test_attempt_ticker_fix_normalization(self):
        """Test that _attempt_ticker_fix uses TickerResolver.normalize first."""
        agent = CoordinatorAgent(mcp_client=MagicMock())

        # Test cases that TickerResolver handles
        self.assertEqual(await agent._attempt_ticker_fix("VOD"), "VOD.L")
        self.assertEqual(await agent._attempt_ticker_fix("LLOY1"), "LLOY.L")
        self.assertEqual(await agent._attempt_ticker_fix("AAPL"), "AAPL")

    async def test_attempt_ticker_fix_cleaning(self):
        """Test that _attempt_ticker_fix cleans noise."""
        agent = CoordinatorAgent(mcp_client=MagicMock())

        # Noise cleaning
        self.assertEqual(await agent._attempt_ticker_fix("AAPL$"), "AAPL")
        self.assertEqual(await agent._attempt_ticker_fix("VOD.L."), "VOD.L")

    async def test_attempt_ticker_fix_fallback_search(self):
        """Test that _attempt_ticker_fix falls back to search for very short/broken tickers."""
        mcp_client = MagicMock()
        # Mock search_instruments tool
        mcp_client.call_tool = AsyncMock(return_value=MagicMock(content=[MagicMock(text='[{"ticker": "TSLA", "name": "Tesla"}]')]))

        agent = CoordinatorAgent(mcp_client=mcp_client)

        # Let s try something definitely broken
        with patch.object(CoordinatorAgent, "_resolve_ticker_via_search", new_callable=AsyncMock) as mock_search:
            mock_search.return_value = "FIXED"
            self.assertEqual(await agent._attempt_ticker_fix("!"), "FIXED")
            mock_search.assert_called_once_with("!")

    async def test_coordinator_ticker_fix_in_process_query(self):
        """Test that process_query triggers ticker fix for malformed tickers."""
        agent = CoordinatorAgent(mcp_client=MagicMock())

        # Mock dependencies of process_query
        agent._classify_intent = AsyncMock(return_value={"type": "price_check", "needs": ["quant"], "primary_ticker": "AAPL$"})
        agent.data_fabricator.fabricate_context = AsyncMock()

        # We want to see if _attempt_ticker_fix is called
        with patch.object(agent, "_attempt_ticker_fix", new_callable=AsyncMock) as mock_fix:
            mock_fix.return_value = "AAPL"

            # Setup context mock
            mock_context = MagicMock()
            mock_context.ticker = "AAPL$"
            mock_context.get_summary.return_value = "Summary"
            agent.data_fabricator.fabricate_context.return_value = mock_context

            # Mock other parts to avoid full execution
            with patch("backend.agents.coordinator_agent.get_messenger"), \
                 patch("backend.agents.coordinator_agent.get_governance"):
                await agent.process_query("What is the price of AAPL$?")

            mock_fix.assert_called_once_with("AAPL$")
            self.assertEqual(mock_context.ticker, "AAPL")

if __name__ == "__main__":
    unittest.main()
