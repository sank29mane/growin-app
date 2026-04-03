import sys
import os
import unittest
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch

# Ensure backend path is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "../..")))

from utils.ticker_utils import TickerResolver

class TestTickerResolverSearch(unittest.IsolatedAsyncioTestCase):

    async def test_resolver_search_fallback(self):
        """Test that TickerResolver.search returns a normalized fallback."""
        resolver = TickerResolver()

        # Test basic search fallback
        results = await resolver.search("VOD")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["ticker"], "VOD.L")
        self.assertEqual(results[0]["name"], "VOD")

        results = await resolver.search("AAPL")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["ticker"], "AAPL")
        self.assertEqual(results[0]["name"], "AAPL")

if __name__ == "__main__":
    unittest.main()
