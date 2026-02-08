import unittest
import asyncio
from unittest.mock import MagicMock, patch
import sys
import os

# Mock dependencies to avoid import errors in environment
sys.modules['utils'] = MagicMock()
sys.modules['utils.ticker_utils'] = MagicMock()
sys.modules['utils.currency_utils'] = MagicMock()
sys.modules['data_models'] = MagicMock()
# Mock Alpaca SDK
sys.modules['alpaca'] = MagicMock()
sys.modules['alpaca.data'] = MagicMock()
sys.modules['alpaca.data.requests'] = MagicMock()
sys.modules['alpaca.data.timeframe'] = MagicMock()
sys.modules['alpaca.trading'] = MagicMock()
sys.modules['alpaca.trading.client'] = MagicMock()
sys.modules['alpaca.data.historical'] = MagicMock()
sys.modules['finnhub'] = MagicMock()

# Set env vars
os.environ["ALPACA_API_KEY"] = "mock_key"
os.environ["ALPACA_SECRET_KEY"] = "mock_secret"

# Import the module under test
# We need to ensure we can import it even if dependencies are missing in the real env
# The sys.modules mocks above should handle imports inside the file.
from backend.data_engine import AlpacaClient, FinnhubClient

class TestDataEngineAsync(unittest.TestCase):
    def setUp(self):
        pass

    def test_fetch_from_yfinance_is_called(self):
        """Test that get_historical_bars calls _fetch_from_yfinance when falling back"""

        # Setup cache mock globally for this test
        mock_cache_mgr = MagicMock()
        mock_cache = MagicMock()
        mock_cache.get.return_value = None
        mock_cache_mgr.cache = mock_cache
        sys.modules['cache_manager'] = mock_cache_mgr

        # We need to instantiate AlpacaClient but prevent __init__ from failing or doing too much
        with patch('backend.data_engine.AlpacaClient.__init__', return_value=None):
            client = AlpacaClient()
            client.data_client = None # Force fallback
            client.trading_client = None

            # Mock _fetch_from_yfinance
            client._fetch_from_yfinance = MagicMock(return_value={"bars": []})

            # Mock normalize_ticker
            with patch('backend.data_engine.normalize_ticker', return_value="AAPL"):
                 # Run async test
                async def run_test():
                    await client.get_historical_bars("AAPL", timeframe="1Day")

                asyncio.run(run_test())

                # Check if called
                client._fetch_from_yfinance.assert_called_once()

                # Verify args
                args, _ = client._fetch_from_yfinance.call_args
                self.assertEqual(args[0], "AAPL") # ticker
                self.assertEqual(args[1], "AAPL") # normalized_ticker
                self.assertEqual(args[2], "1Day") # timeframe

    def test_finnhub_timeframe_mapping(self):
        """Test Finnhub resolution mapping logic"""

        with patch('backend.data_engine.FinnhubClient.__init__', return_value=None):
            fh_client = FinnhubClient()
            fh_client.client = MagicMock()

            # Helper to check resolution passed to stock_candles
            async def check_resolution(tf, expected_res):
                fh_client.client.stock_candles.reset_mock()
                # Mock return
                fh_client.client.stock_candles.return_value = {'s': 'ok', 'c': [100], 't': [1700000000], 'o': [100], 'h': [100], 'l': [100], 'v': [1000]}

                await fh_client.get_historical_bars("LLOY.L", timeframe=tf)

                if fh_client.client.stock_candles.call_count == 0:
                    self.fail(f"stock_candles not called for {tf}")

                args, kwargs = fh_client.client.stock_candles.call_args
                self.assertEqual(kwargs['resolution'], expected_res, f"Failed for {tf}")

            async def run_all():
                await check_resolution("1Min", "1")
                await check_resolution("1Hour", "60")
                await check_resolution("1Day", "D")
                await check_resolution("1Week", "W")
                await check_resolution("1Month", "M")

            asyncio.run(run_all())

if __name__ == '__main__':
    unittest.main()
