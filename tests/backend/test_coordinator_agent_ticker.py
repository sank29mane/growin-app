import sys
import os
import unittest
from unittest.mock import patch

sys.path.append(os.path.join(os.getcwd(), 'backend'))

class TestTickerNorm(unittest.TestCase):
    def test_ticker_norm_logic(self):
        # We need to mock growin_core.normalize_ticker because it intercepts the call
        # in TickerResolver.normalize and bypasses the Python logic if it's available.
        # But wait, growin_core is a Rust core. We should test our Python fallback logic.

        # We patch sys.modules to simulate growin_core NOT being available
        with patch.dict('sys.modules', {'growin_core': None}):
            from utils.ticker_utils import TickerResolver
            resolver = TickerResolver()

            # Test original localized fix functionality (trailing dots and alphanumeric extraction)
            self.assertEqual(resolver.normalize("AAPL..."), "AAPL")
            self.assertEqual(resolver.normalize("  #AAPL  "), "AAPL")
            self.assertEqual(resolver.normalize("3GLD."), "3GLD.L")

            # Test special mappings and artifacts
            self.assertEqual(resolver.normalize("VOD_EQ"), "VOD.L")

            # Original AAPL remains AAPL
            self.assertEqual(resolver.normalize("AAPL"), "AAPL")

if __name__ == "__main__":
    unittest.main()
