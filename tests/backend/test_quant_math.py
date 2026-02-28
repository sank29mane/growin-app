import numpy as np
import sys
import os
import pytest
from unittest.mock import MagicMock
from decimal import Decimal

# Add backend to path (parent directory)
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from agents.quant_agent import QuantAgent
from utils.financial_math import create_decimal

@pytest.mark.asyncio
async def test_quant_agent_full_analysis():
    """
    Test the full analysis flow of QuantAgent.
    """
    agent = QuantAgent()

    # Mock OHLCV data (100 bars)
    import time
    now_ms = int(time.time() * 1000)
    mock_ohlcv = [
        {"t": now_ms - (100-i)*86400000, "o": 100.0+i, "h": 105.0+i, "l": 99.0+i, "c": 103.0+i, "v": 1000000}
        for i in range(100)
    ]

    result = await agent.execute({"ticker": "AAPL", "ohlcv_data": mock_ohlcv})
    
    assert result.success, f"QuantAgent failed: {result.error}"
    data = result.data
    
    # Check that indicators are present and are Decimals (serialized to strings in model_dump usually, 
    # but QuantAgent returns raw data from model_dump() which has Decimals if not using json mode)
    # Actually AgentResponse.data is Dict[str, Any].
    
    assert "rsi" in data
    assert "macd" in data
    assert "bollinger_bands" in data
    assert "signal" in data
    
    # Check signal format
    assert data["signal"] in ["BUY", "SELL", "HOLD", "NEUTRAL"]

@pytest.mark.asyncio
async def test_insufficient_data():
    """Test handling of insufficient data"""
    agent = QuantAgent()
    
    # Only 10 bars
    mock_ohlcv = [{"t": i, "o": 100, "h": 105, "l": 95, "c": 102, "v": 1000} for i in range(10)]
    
    # Use different ticker to avoid cache hit
    result = await agent.execute({"ticker": "TSLA", "ohlcv_data": mock_ohlcv})
    assert not result.success
    assert "Insufficient data" in result.error

def test_decimal_precision_logic():
    """Test that financial math helpers handle Decimals correctly."""
    from utils.financial_math import create_decimal, safe_div
    
    d1 = create_decimal(0.1)
    d2 = create_decimal(0.2)
    assert d1 + d2 == Decimal('0.3')
    
    # Division by zero safety
    assert safe_div(d1, Decimal('0')) == Decimal('0')
    assert safe_div(d1, d2) == Decimal('0.5')
