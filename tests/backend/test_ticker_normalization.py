import sys
import os
import unittest
from unittest.mock import MagicMock, patch

# Add backend to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# We rely on conftest.py for basic mocking.
# To test Python fallback, we patch growin_core availability in ticker_utils.

from backend.utils.ticker_utils import normalize_ticker

class TestTickerNormalization(unittest.TestCase):
    def test_us_tickers(self):
        """Test that US tickers are NOT normalized to UK"""
        # Ensure we are testing the Python logic by patching sys.modules locally if needed
        with patch.dict('sys.modules', {'growin_core': None}):
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
        with patch.dict('sys.modules', {'growin_core': None}):
            self.assertEqual(normalize_ticker("VOD"), "VOD.L")
            self.assertEqual(normalize_ticker("LLOY"), "LLOY.L")
            self.assertEqual(normalize_ticker("BARC"), "BARC.L")
            self.assertEqual(normalize_ticker("SSLN"), "SSLN.L")
            self.assertEqual(normalize_ticker("SGLN"), "SGLN.L")

    def test_already_normalized(self):
        """Test that already normalized tickers are untouched"""
        with patch.dict('sys.modules', {'growin_core': None}):
            self.assertEqual(normalize_ticker("VOD.L"), "VOD.L")
            self.assertEqual(normalize_ticker("AAPL"), "AAPL")

    def test_edge_cases(self):
        """Test edge cases and existing mappings"""
        with patch.dict('sys.modules', {'growin_core': None}):
            self.assertEqual(normalize_ticker("LLOY1"), "LLOY.L") # Strips 1
            self.assertEqual(normalize_ticker("SGLN1"), "SGLN.L") # Strips 1

if __name__ == '__main__':
    unittest.main()
