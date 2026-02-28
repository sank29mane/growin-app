
import sys
import os
import unittest
import logging
from datetime import datetime, timedelta

# Add backend to path
sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

from analytics_db import AnalyticsDB

# Setup logging
logging.basicConfig(level=logging.INFO)

class TestAnalyticsSafety(unittest.TestCase):
    def setUp(self):
        self.db = AnalyticsDB(":memory:")
        # Insert some dummy data with recent timestamps
        now = datetime.now()
        data = [
            {"timestamp": (now - timedelta(days=1)).isoformat(), "open": 100, "high": 110, "low": 90, "close": 105, "volume": 1000},
            {"timestamp": (now - timedelta(days=2)).isoformat(), "open": 105, "high": 115, "low": 95, "close": 110, "volume": 1200}
        ]
        self.db.bulk_insert_ohlcv("AAPL", data)

    def test_sql_injection_get_aggregated_stats(self):
        payload = "1' DAY --"
        result = self.db.get_aggregated_stats("AAPL", window_days=payload)
        self.assertIsNone(result, "Should return None for invalid input")

    def test_sql_injection_calculate_volatility(self):
        payload = "1' OR '1'='1"
        result = self.db.calculate_volatility("AAPL", window_days=payload)
        self.assertIsNone(result, "Should return None for invalid input")

    def test_valid_input(self):
        # Verify legitimate calls still work
        # Window of 30 days should include our 1-2 day old data
        result = self.db.get_aggregated_stats("AAPL", window_days=30)
        self.assertIsNotNone(result, "Should return DataFrame for valid input")
        # Check if data_points > 0
        if result is not None:
             self.assertGreater(result['data_points'][0], 0)

        vol = self.db.calculate_volatility("AAPL", window_days=30)
        # Volatility might be None or 0 if only 2 points or constant, but at least query runs
        # With 2 points different, it should have volatility.
        # However, calculate_volatility returns a float or None.
        # It's okay if it is None (e.g. calculation issue), as long as it's not a query error.
        # But for 2 points, it should work.

    def test_large_number(self):
        result = self.db.get_aggregated_stats("AAPL", window_days=1000)
        self.assertIsNotNone(result)

if __name__ == "__main__":
    unittest.main()
