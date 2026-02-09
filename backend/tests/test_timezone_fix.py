
import unittest
import pandas as pd
import numpy as np
import sys
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

# Mock yfinance before importing backend.data_engine or running the method
sys.modules["yfinance"] = MagicMock()

# We need to ensure we can import data_engine.
# Depending on where this is run from, we might need to adjust path.
import os
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from data_engine import AlpacaClient

class TestTimezoneFix(unittest.IsolatedAsyncioTestCase):
    async def test_get_historical_bars_timezone_fix(self):
        # Setup
        client = AlpacaClient()
        # Force fallback to yfinance by ensuring data_client is None
        client.data_client = None

        # Mock yfinance Ticker and history
        mock_ticker = MagicMock()

        # Create naive dataframe (mimicking New York time)
        # 09:30:00 (Naive)
        dates = pd.date_range(start="2023-01-01 09:30:00", periods=1, freq="D")
        df = pd.DataFrame({
            'Open': [100.0],
            'High': [110.0],
            'Low': [90.0],
            'Close': [105.0],
            'Volume': [1000]
        }, index=dates)

        # Ensure it is naive
        self.assertIsNone(df.index.tz)

        mock_ticker.history.return_value = df

        # Patch yfinance.Ticker to return our mock
        with patch("yfinance.Ticker", return_value=mock_ticker):
            result = await client.get_historical_bars("AAPL", timeframe="1Day")

            self.assertIsNotNone(result)
            bars = result["bars"]
            self.assertEqual(len(bars), 1)

            bar = bars[0]
            ts_iso = bar["timestamp"]

            # 2023-01-01 09:30:00 ET is 14:30:00 UTC (Standard Time, offset -5)
            # Or if DST, it's 13:30. Jan is Standard Time.
            # 09:30 + 5 hours = 14:30.

            expected_ts = "2023-01-01T14:30:00+00:00"
            self.assertEqual(ts_iso, expected_ts)

    async def test_get_historical_bars_timezone_fix_uk(self):
        # Setup
        client = AlpacaClient()
        client.data_client = None

        mock_ticker = MagicMock()

        # Create naive dataframe (mimicking London time)
        # 08:00:00 (Naive)
        dates = pd.date_range(start="2023-06-01 08:00:00", periods=1, freq="D")
        df = pd.DataFrame({
            'Open': [100.0],
            'High': [110.0],
            'Low': [90.0],
            'Close': [105.0],
            'Volume': [1000]
        }, index=dates)

        mock_ticker.history.return_value = df

        with patch("yfinance.Ticker", return_value=mock_ticker):
            # Test with UK ticker
            result = await client.get_historical_bars("VOD.L", timeframe="1Day")

            bar = result["bars"][0]
            ts_iso = bar["timestamp"]

            # 2023-06-01 is BST (British Summer Time) => UTC+1
            # 08:00 BST is 07:00 UTC.

            expected_ts = "2023-06-01T07:00:00+00:00"
            self.assertEqual(ts_iso, expected_ts)

if __name__ == "__main__":
    unittest.main()
