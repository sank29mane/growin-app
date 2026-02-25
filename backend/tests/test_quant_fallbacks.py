import unittest
from unittest.mock import patch
import numpy as np
import pandas as pd
from backend.quant_engine import QuantEngine

class TestQuantFallbacks(unittest.TestCase):
    def test_pure_pandas_fallback(self):
        # Force fallback to Pure Pandas path
        with patch('backend.quant_engine.PANDAS_TA_AVAILABLE', False):
            with patch('backend.quant_engine.RUST_CORE_AVAILABLE', False):
                engine = QuantEngine()
            
                # Create synthetic OHLCV data
                # 100 days of data
                periods = 100
                start_price = 100.0
                
                # Steady uptrend to trigger RSI high and specific EMAs
                prices = [start_price + i * 1.0 for i in range(periods)]
            
                ohlcv = []
                base_time = 1600000000000
                for i, p in enumerate(prices):
                    ohlcv.append({
                        't': base_time + i * 60000,
                        'o': p,
                        'h': p + 1,
                        'l': p - 1,
                        'c': p,
                        'v': 1000 + i * 10
                    })
            
                result = engine.calculate_technical_indicators(ohlcv)
            
                # Check if error returned
                if "error" in result:
                    self.fail(f"QuantEngine returned error: {result['error']}")
            
                indicators = result.get("indicators", {})
            
                # Validate existence of indicators
                self.assertIn("rsi", indicators)
                self.assertIn("macd", indicators)
                self.assertIn("bb_upper", indicators)
                self.assertIn("ema_50", indicators)
                
                # Logic check:
                # RSI should be high (near 100) for constant uptrend
                if indicators['rsi'] is not None:
                    self.assertTrue(indicators['rsi'] > 90, "RSI should be high for constant uptrend")

                # Bollinger Bands sanity check
                if indicators['bb_upper'] is not None:
                    self.assertTrue(indicators['bb_upper'] > indicators['bb_middle'])
                    self.assertTrue(indicators['bb_middle'] > indicators['bb_lower'])

    def test_pivot_points_fallback(self):
        """Test fallback logic for pivot points when SciPy is unavailable."""
        with patch('backend.quant_engine.SCIPY_AVAILABLE', False):
            engine = QuantEngine()

            # Create synthetic data with clear peaks and troughs
            # Pattern: 10, 12, 14, 12, 10, 8, 6, 8, 10
            # Peak at 14 (index 2), Trough at 6 (index 6)
            prices = [10, 12, 14, 12, 10, 8, 6, 8, 10]
            # Repeat this pattern enough times for order=5 (length > 10)
            # Or use a longer sequence.
            # Let's use a simpler pattern but longer:
            # Rise 0-9, Fall 10-19, Rise 20-29. Peak at 9 (index 9), Trough at 19 (index 19)
            prices = list(range(10)) + list(range(8, -1, -1)) + list(range(1, 10))
            # 0..9, 8..0, 1..9
            # Peak is 9 (at index 9). Neighbors: 7,8 < 9 > 8,7.
            # Trough is 0 (at index 19). Neighbors: 1,2 > 0 < 1,2.

            ohlcv = []
            base_time = 1600000000000
            for i, p in enumerate(prices):
                ohlcv.append({
                    't': base_time + i * 60000,
                    'o': float(p),
                    'h': float(p), # High is price
                    'l': float(p), # Low is price
                    'c': float(p),
                    'v': 1000
                })

            # Use order=2 to make it easier to detect with small data
            # But the method defaults to order=5. Let's pass order=2 explicitly if possible?
            # calculate_pivot_levels signature: (ohlcv_data, order=5)

            try:
                result = engine.calculate_pivot_levels(ohlcv, order=2)
                # Should not crash
                self.assertIn("support", result)
                self.assertIn("resistance", result)

                # Check values
                # Resistance should be near max peak (9)
                self.assertEqual(float(result['resistance']), 9.0)
                # Support should be near min trough (0)
                self.assertEqual(float(result['support']), 0.0)

            except TypeError as e:
                self.fail(f"Crash detected in pivot fallback: {e}")

if __name__ == "__main__":
    unittest.main()
