
import sys
import os
import unittest
from unittest.mock import MagicMock

# Mock dependencies that might be missing or problematic in the test environment
# We only need 'backend.trading212_mcp_server' to import successfully
sys.modules['mcp.server'] = MagicMock()
sys.modules['mcp.server.stdio'] = MagicMock()
sys.modules['mcp.types'] = MagicMock()
# sys.modules['httpx'] = MagicMock() # We installed this
# sys.modules['yfinance'] = MagicMock() # We installed this

# Add backend to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from trading212_mcp_server import normalize_ticker

class TestTickerNormalization(unittest.TestCase):
    def test_us_tickers(self):
        """Test that US tickers are NOT normalized to UK"""
        self.assertEqual(normalize_ticker("SMCI"), "SMCI")
        self.assertEqual(normalize_ticker("MSTR"), "MSTR")
        self.assertEqual(normalize_ticker("AAPL"), "AAPL")
        self.assertEqual(normalize_ticker("TSLA"), "TSLA")
        self.assertEqual(normalize_ticker("PLTR"), "PLTR")
        self.assertEqual(normalize_ticker("COIN"), "COIN")
        self.assertEqual(normalize_ticker("HOOD"), "HOOD")
        self.assertEqual(normalize_ticker("ARM"), "ARM")
        self.assertEqual(normalize_ticker("IBM"), "IBM")

    def test_uk_tickers(self):
        """Test that UK tickers ARE normalized to UK"""
        self.assertEqual(normalize_ticker("VOD"), "VOD.L")
        self.assertEqual(normalize_ticker("LLOY"), "LLOY.L")
        self.assertEqual(normalize_ticker("BARC"), "BARC.L")
        self.assertEqual(normalize_ticker("SSLN"), "SSLN.L")
        self.assertEqual(normalize_ticker("SGLN"), "SGLN.L")

    def test_already_normalized(self):
        """Test that already normalized tickers are untouched"""
        self.assertEqual(normalize_ticker("VOD.L"), "VOD.L")
        self.assertEqual(normalize_ticker("AAPL"), "AAPL")

    def test_edge_cases(self):
        """Test edge cases and existing mappings"""
        self.assertEqual(normalize_ticker("LLOY1"), "LLOY.L") # Strips 1
        self.assertEqual(normalize_ticker("SGLN1"), "SGLN.L") # Strips 1
        # Leveraged should be preserved or handled?
        # Current logic: "3GLD" -> "3GLD" in special_mappings?
        # "3GLD": "3GLD" is in the map.
        # But wait, 3GLD is 4 chars. If it's not in US_EXCLUSIONS, does it get .L?
        # The map returns "3GLD". Then `is_likely_uk` checks.
        # If I fix `is_likely_uk`, I need to make sure 3GLD doesn't become 3GLD.L if it shouldn't.
        # 3GLD is "WisdomTree Physical Gold 3x Daily Leveraged". It trades on LSE as 3GLD.L?
        # Let's check existing behavior.
        pass

if __name__ == '__main__':
    unittest.main()
