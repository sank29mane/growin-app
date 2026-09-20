import sys
import unittest
from decimal import Decimal
sys.path.append('.')
sys.path.append('backend')

from quant_engine import get_quant_engine

class TestQuantEngineRebalance(unittest.TestCase):
    def test_analyze_rebalancing_opportunity(self):
        engine = get_quant_engine()

        # Test Case 1: Target with % sign (1% -> 0.01)
        current = {"AAPL": "0%"}
        target = {"AAPL": "1%"}
        total = 1000.0

        result = engine.analyze_rebalancing_opportunity(current, target, total)
        self.assertEqual(result["deviations_pct"]["AAPL"], 1.0)

        # Test Case 2: Target > 10 without % (50 -> 50) should be out-of-bounds error
        current = {"AAPL": "0%"}
        target = {"AAPL": 50.0}
        total = 1000.0

        result = engine.analyze_rebalancing_opportunity(current, target, total)
        self.assertTrue("error" in result)
        self.assertTrue("out of bounds for explicit weights" in result["error"])

        # Test Case 3: Target < 1 without % (0.5 -> 0.5)
        current = {"AAPL": "0%"}
        target = {"AAPL": 0.5}
        total = 1000.0

        result = engine.analyze_rebalancing_opportunity(current, target, total)
        self.assertEqual(result["deviations_pct"]["AAPL"], 50.0)

if __name__ == '__main__':
    unittest.main()
