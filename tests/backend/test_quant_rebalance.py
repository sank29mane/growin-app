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
        # Note: analyze_rebalancing_opportunity does not return target_pct.
        # It returns deviation_pct and amount. 1% deviation = 1.0 deviation_pct
        # However, 1% deviation (0.01) is not strictly greater than 0.01, so it won't generate a rebalance action!
        # The threshold is `abs(deviation) > Decimal("0.01")`.
        self.assertEqual(len(result["rebalance_actions"]), 0)
        self.assertEqual(result["deviations_pct"]["AAPL"], 1.0)

        # Test Case 2: Target > 1 without % (50 -> 0.5)
        current = {"AAPL": "0%"}
        target = {"AAPL": 50.0}
        total = 1000.0

        result = engine.analyze_rebalancing_opportunity(current, target, total)
        # Deviation of 50% is 50.0
        self.assertEqual(result["rebalance_actions"][0]["deviation_pct"], 50.0)

        # Test Case 3: Target < 1 without % (0.5 -> 0.5)
        current = {"AAPL": "0%"}
        target = {"AAPL": 0.5}
        total = 1000.0

        result = engine.analyze_rebalancing_opportunity(current, target, total)
        # Deviation of 50% is 50.0
        self.assertEqual(result["rebalance_actions"][0]["deviation_pct"], 50.0)

if __name__ == '__main__':
    unittest.main()
