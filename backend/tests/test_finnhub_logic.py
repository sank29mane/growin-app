import unittest
import sys
import os
from unittest.mock import MagicMock, patch
from datetime import datetime, timedelta

# Ensure backend is in path
sys.path.append(os.path.join(os.getcwd(), 'backend'))

from data_engine import FinnhubClient

class TestFinnhubLogic(unittest.IsolatedAsyncioTestCase):
    async def test_resolution_map(self):
        client = FinnhubClient()
        # Mock the client.stock_candles to avoid actual API call and inspect args
        client.client = MagicMock()
        client.client.stock_candles.return_value = {'s': 'ok', 'c': [], 'v': [], 't': [], 'o': [], 'h': [], 'l': []}

        # Test 1Week
        await client.get_historical_bars("AAPL", "1Week", limit=10)
        args, kwargs = client.client.stock_candles.call_args
        self.assertEqual(kwargs['resolution'], 'W')

        # Test 1Month
        await client.get_historical_bars("AAPL", "1Month", limit=10)
        args, kwargs = client.client.stock_candles.call_args
        self.assertEqual(kwargs['resolution'], 'M')

        # Test 1Day
        await client.get_historical_bars("AAPL", "1Day", limit=10)
        args, kwargs = client.client.stock_candles.call_args
        self.assertEqual(kwargs['resolution'], 'D')

    async def test_time_window_calculation(self):
        client = FinnhubClient()
        client.client = MagicMock()
        client.client.stock_candles.return_value = {'s': 'ok', 'c': [], 'v': [], 't': [], 'o': [], 'h': [], 'l': []}

        # Test 1Week limit 10
        await client.get_historical_bars("AAPL", "1Week", limit=10)
        args, kwargs = client.client.stock_candles.call_args
        from_ts = kwargs['_from']
        to_ts = kwargs['to']

        # 10 weeks + 4 weeks buffer = 14 weeks approx
        diff = to_ts - from_ts
        weeks = diff / (3600*24*7)
        self.assertAlmostEqual(weeks, 14, delta=1)

if __name__ == '__main__':
    unittest.main()
