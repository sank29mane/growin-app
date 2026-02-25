import unittest
from unittest.mock import MagicMock, patch
import asyncio
from decimal import Decimal
import pandas as pd
import numpy as np
import sys
import os

# Add backend to sys.path
sys.path.append(os.path.join(os.getcwd(), 'backend'))

from data_engine import AlpacaClient, PriceData

class TestAlpacaClient(unittest.TestCase):
    def setUp(self):
        # Patch logger to avoid clutter
        self.logger_patcher = patch('data_engine.logger')
        self.mock_logger = self.logger_patcher.start()

    def tearDown(self):
        self.logger_patcher.stop()

    def test_get_historical_bars_yfinance_fallback(self):
        async def run_test():
            # Mock yfinance
            with patch('yfinance.Ticker') as MockTicker:
                # Setup mock data
                dates = pd.date_range(start='2023-01-01', periods=5, freq='D')
                mock_data = pd.DataFrame({
                    'Open': [1000.0, 1010.0, 1020.0, 1030.0, 1040.0],
                    'High': [1050.0, 1060.0, 1070.0, 1080.0, 1090.0],
                    'Low': [950.0, 960.0, 970.0, 980.0, 990.0],
                    'Close': [1025.0, 1035.0, 1045.0, 1055.0, 1065.0],
                    'Volume': [100, 200, 300, 400, 500],
                    'Dividends': [0, 0, 0, 0, 0],
                    'Stock Splits': [0, 0, 0, 0, 0]
                }, index=dates)

                mock_ticker_instance = MagicMock()
                mock_ticker_instance.history.return_value = mock_data
                MockTicker.return_value = mock_ticker_instance

                client = AlpacaClient()
                client.data_client = None  # Force fallback

                # 1. Test UK Stock
                ticker = "TEST.L"
                result = await client.get_historical_bars(ticker, timeframe="1Day", limit=5)

                self.assertIsNotNone(result)
                self.assertEqual(result['ticker'], ticker)
                bars = result['bars']
                self.assertEqual(len(bars), 5)
                # 1000.0 GBX -> 10.00 GBP
                self.assertEqual(bars[0]['open'], Decimal('10.00'))

        asyncio.run(run_test())

    def test_get_batch_bars(self):
        async def run_test():
             with patch('data_engine.AlpacaClient.get_historical_bars') as mock_single_fetch:
                async def side_effect(ticker, timeframe, limit):
                    return {"ticker": ticker, "bars": [], "source": "single"}
                mock_single_fetch.side_effect = side_effect

                client = AlpacaClient()
                client.data_client = None

                tickers = ["AAPL", "LLOY.L"]
                results = await client.get_batch_bars(tickers)

                self.assertEqual(len(results), 2)
                self.assertEqual(mock_single_fetch.call_count, 2)

        asyncio.run(run_test())

    def test_get_portfolio_positions(self):
        async def run_test():
            # 1. Test Mock Fallback (when trading_client is None)
            client = AlpacaClient()
            client.trading_client = None

            positions = await client.get_portfolio_positions()

            self.assertEqual(len(positions), 1)
            self.assertEqual(positions[0]['symbol'], "AAPL")
            self.assertIsInstance(positions[0]['qty'], Decimal)

            # 2. Test with Trading Client
            mock_trading_client = MagicMock()
            client.trading_client = mock_trading_client

            # Create a mock position object
            class MockPosition:
                def __init__(self):
                    self.symbol = "TSLA"
                    self.qty = "5"
                    self.market_value = "1000.0"
                    self.cost_basis = "900.0"
                    self.unrealized_pl = "100.0"
                    self.unrealized_plpc = "0.11"
                    self.current_price = "200.0"
                    self.lastday_price = "190.0"
                    self.change_today = "10.0"

            mock_positions_list = [MockPosition()]

            # Mock the to_thread call again
            async def async_return(*args, **kwargs):
                return mock_positions_list

            with patch('asyncio.to_thread', side_effect=async_return) as mock_to_thread:
                positions = await client.get_portfolio_positions()

                self.assertEqual(len(positions), 1)
                self.assertEqual(positions[0]['symbol'], "TSLA")
                self.assertEqual(positions[0]['qty'], Decimal("5"))
                self.assertEqual(positions[0]['market_value'], Decimal("1000.0"))

        asyncio.run(run_test())

if __name__ == '__main__':
    unittest.main()
