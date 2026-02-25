import numpy as np
import pytest
from unittest.mock import MagicMock
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agents.quant_agent import QuantAgent

@pytest.mark.asyncio
async def test_quant_agent_full_performance():
    """Test performance of the full agent flow."""
    agent = QuantAgent()
    
    # Large dataset
    N = 500
    now_ms = 1600000000
    data = [
        {"t": now_ms + i*60000, "o": 100.0, "h": 105.0, "l": 95.0, "c": 102.0, "v": 1000}
        for i in range(N)
    ]
    
    import time
    start = time.time()
    result = await agent.execute({"ticker": "AAPL", "ohlcv_data": data})
    duration = (time.time() - start) * 1000
    
    assert result.success
    # Should be fast (MLX/Rust optimized)
    assert duration < 500 # ms
    print(f"QuantAgent execution for {N} bars: {duration:.2f}ms")

def test_decimal_return_types():
    """Ensure QuantAgent returns data in a structure that supports Decimal serialization."""
    # This is a placeholder for actual return type verification if needed
    pass
