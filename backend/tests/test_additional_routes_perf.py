
import pytest
from unittest.mock import MagicMock, patch
import pandas as pd
import numpy as np
import sys
import os
import asyncio

# Ensure backend is in path
sys.path.append(os.path.join(os.getcwd(), 'backend'))

# We need to mock cache_manager before importing additional_routes or calling the function
# if the function does lazy import.
mock_cache_manager = MagicMock()
mock_cache = MagicMock()
mock_cache_manager.cache = mock_cache
sys.modules['cache_manager'] = mock_cache_manager

# Also mock data_engine and trading212_mcp_server as they are imported at top level in additional_routes
# actually trading212_mcp_server is imported at top level.
# app_context is imported at top level.

mock_state = MagicMock()
mock_app_context = MagicMock()
mock_app_context.state = mock_state
sys.modules['app_context'] = mock_app_context

from routes.additional_routes import get_yfinance_chart_data

@pytest.mark.asyncio
async def test_get_yfinance_chart_data_correctness():
    # Mock yfinance
    with patch('yfinance.Ticker') as mock_ticker_cls:
        mock_ticker = MagicMock()
        mock_ticker_cls.return_value = mock_ticker

        # Create sample data
        rows = 10
        dates = pd.date_range(start='2023-01-01', periods=rows, freq='D')
        data = {
            'Close': np.array([100.0 + i for i in range(rows)]),
            'High': np.array([105.0 + i for i in range(rows)]),
            'Low': np.array([95.0 + i for i in range(rows)]),
            'Open': np.array([98.0 + i for i in range(rows)]),
            'Volume': np.array([1000 + i for i in range(rows)])
        }
        history = pd.DataFrame(data, index=dates)
        history.index.name = "Date"

        # Configure mock to return history
        mock_ticker.history.return_value = history

        # We need to make sure loop.run_in_executor calls the function
        # The real run_in_executor should work if we don't mock it,
        # but the function f inside get_yfinance_chart_data creates a Ticker and calls history.
        # Since we mocked yfinance.Ticker, it should work.

        # Call function
        result = await get_yfinance_chart_data("TEST", "1Day", 100, "test_key")

        # Verify result
        assert len(result) == rows
        assert result[0]['timestamp'] == dates[0].isoformat()
        assert result[0]['close'] == 100.0
        assert result[0]['high'] == 105.0
        assert result[0]['low'] == 95.0
        assert result[0]['open'] == 98.0
        assert result[0]['volume'] == 1000

        # Verify last element
        assert result[-1]['close'] == 100.0 + (rows - 1)

        print("Test passed!")

if __name__ == "__main__":
    # Manually run if executed as script (though async makes it harder)
    pass
