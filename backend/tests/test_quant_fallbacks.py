
import unittest
import pandas as pd
import numpy as np
from backend.quant_engine import QuantEngine

class TestQuantFallbacks(unittest.TestCase):
    def test_pure_pandas_fallback(self):
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
        self.assertIn("ema_200", indicators) # Might be None if not enough data, but we have 100 pts.

        # With 100 points, EMA 200 should be None or calculated if span handles it (pandas ewm does)
        # But QuantEngine logic:
        # indicators['ema_200'] = ... if len >= 200 else None
        # Wait, pure pandas fallback:
        # indicators['ema_200'] = close.ewm(span=200, ...).iloc[-1]
        # It doesn't check length explicitly in the fallback!
        # But ewm works on smaller series (just less accurate).
        # Let's check what I implemented.

        # Logic check:
        # RSI should be high (near 100) for constant uptrend?
        # A constant increment (linear) -> diff is constant (1.0).
        # Gain = 1.0, Loss = 0.0.
        # RS = inf. RSI = 100.
        print(f"RSI: {indicators['rsi']}")
        self.assertTrue(indicators['rsi'] > 90, "RSI should be high for constant uptrend")

        # EMA 50 should be close to price
        print(f"EMA 50: {indicators['ema_50']}")
        self.assertTrue(indicators['ema_50'] < prices[-1], "EMA 50 should lag below price in uptrend")

        # MACD
        print(f"MACD: {indicators['macd']}")

        # Bollinger Bands
        print(f"BB Upper: {indicators['bb_upper']}")
        print(f"BB Middle: {indicators['bb_middle']}")
        print(f"BB Lower: {indicators['bb_lower']}")

        self.assertTrue(indicators['bb_upper'] > indicators['bb_middle'] > indicators['bb_lower'])

if __name__ == "__main__":
    unittest.main()
