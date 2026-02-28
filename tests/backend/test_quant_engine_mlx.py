
import sys
import os
import unittest
from unittest.mock import MagicMock, patch
import pandas as pd

# Add backend to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.quant_engine import QuantEngine

class TestQuantEngineMLX(unittest.TestCase):
    def setUp(self):
        self.engine = QuantEngine()
        # Create dummy OHLCV data
        self.data = []
        for i in range(100):
            self.data.append({
                't': 1600000000000 + i * 60000,
                'o': 100.0 + i,
                'h': 105.0 + i,
                'l': 95.0 + i,
                'c': 102.0 + i,
                'v': 1000 + i * 10
            })

    def test_calculate_indicators_runs(self):
        """Test that indicators calculation runs without error (MLX or fallback)"""
        result = self.engine.calculate_technical_indicators(self.data)
        self.assertIn('indicators', result)
        self.assertIn('signals', result)
        self.assertIn('current_price', result)
        
        indicators = result['indicators']
        # Check basic indicators are present (either from MLX, Rust, or Pandas)
        self.assertIn('bb_upper', indicators)
        self.assertIn('volume_sma', indicators)
        self.assertIn('rsi', indicators)

    def test_mlx_import_logic(self):
        """Test that MLX availability flag is respected"""
        # This test just confirms the module loaded successfully and set the flag
        import backend.quant_engine as qe
        print(f"MLX Available: {qe.MLX_AVAILABLE}")
        # We don't assert True/False because it depends on the environment
        # But we assert that the attribute exists
        self.assertTrue(hasattr(qe, 'MLX_AVAILABLE'))

if __name__ == '__main__':
    unittest.main()
