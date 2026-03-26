import sys
import unittest
from decimal import Decimal
sys.path.append('.')
sys.path.append('backend')

from backend.quant_engine import get_quant_engine

class TestQuantEngineRebalance(unittest.TestCase):
    def test_analyze_rebalancing_opportunity(self):
        engine = get_quant_engine()

        # Test Case 1: Target with % sign (1% -> 0.01)
        current = {"AAPL": "0%"}
        target = {"AAPL": "1%"}
        total = 1000.0

        result = engine.analyze_rebalancing_opportunity(current, target, total)
        self.assertEqual(result["rebalance_actions"][0]["target_pct"], Decimal("0.01"))

        # Test Case 2: Target > 1 without % (50 -> 0.5)
        current = {"AAPL": "0%"}
        target = {"AAPL": 50.0}
        total = 1000.0

        result = engine.analyze_rebalancing_opportunity(current, target, total)
        self.assertEqual(result["rebalance_actions"][0]["target_pct"], Decimal("0.5"))

        # Test Case 3: Target < 1 without % (0.5 -> 0.5)
        current = {"AAPL": "0%"}
        target = {"AAPL": 0.5}
        total = 1000.0

        result = engine.analyze_rebalancing_opportunity(current, target, total)
        self.assertEqual(result["rebalance_actions"][0]["target_pct"], Decimal("0.5"))

if __name__ == '__main__':
    unittest.main()
