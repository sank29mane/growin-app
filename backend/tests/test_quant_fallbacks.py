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

if __name__ == "__main__":
    unittest.main()