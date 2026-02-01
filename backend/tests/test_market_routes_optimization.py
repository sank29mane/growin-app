
import pytest
import asyncio
import pandas as pd
import numpy as np
from unittest.mock import MagicMock, patch

# We need to ensure app_context is mockable
import sys
# Only mock if not already mocked/imported (to handle re-running in same session if needed, though pytest usually isolates)
if 'app_context' not in sys.modules or not isinstance(sys.modules['app_context'], MagicMock):
    mock_app_context = MagicMock()
    mock_account_context = MagicMock()
    mock_app_context.account_context = mock_account_context
    sys.modules['app_context'] = mock_app_context
else:
    mock_app_context = sys.modules['app_context']
    mock_account_context = mock_app_context.account_context

from backend.routes.market_routes import get_portfolio_history

@pytest.mark.asyncio
async def test_get_portfolio_history_vectorized_dataframe():
    # Setup mock
    mock_account_context.get_account_or_default.return_value = 'invest'

    with patch('backend.routes.market_routes.get_live_portfolio') as mock_get_live:
        mock_get_live.return_value = {
            "positions": [
                {"ticker": "AAPL", "quantity": 10},
                {"ticker": "VOD.L", "quantity": 100},
                {"ticker": "TSLA", "quantity": 5}
            ],
            "summary": {"cash_balance": {"free": 5000.0}}
        }

        days = 10
        dates = pd.date_range(start="2023-01-01", periods=days, freq="D")
        data = {
            "AAPL": np.full(days, 150.0),
            "VOD.L": np.full(days, 600.0),
            "TSLA": np.full(days, 200.0)
        }
        df = pd.DataFrame(data, index=dates)

        async def mock_run_in_executor(*args, **kwargs):
            return df

        with patch('asyncio.get_running_loop') as mock_get_loop:
            mock_loop = MagicMock()
            mock_loop.run_in_executor = mock_run_in_executor
            mock_get_loop.return_value = mock_loop

            result = await get_portfolio_history(days=10)

            assert len(result) == 10
            assert result[0]['total_value'] == 8100.0
            assert result[0]['cash_balance'] == 5000.0

@pytest.mark.asyncio
async def test_get_portfolio_history_single_ticker_series():
    """Test when yfinance returns a Series (single ticker)."""
    # Setup mock
    mock_account_context.get_account_or_default.return_value = 'invest'

    with patch('backend.routes.market_routes.get_live_portfolio') as mock_get_live:
        # Only one holding
        mock_get_live.return_value = {
            "positions": [
                {"ticker": "AAPL", "quantity": 10}
            ],
            "summary": {"cash_balance": {"free": 1000.0}}
        }

        days = 10
        dates = pd.date_range(start="2023-01-01", periods=days, freq="D")
        # Return a Series
        series = pd.Series(np.full(days, 150.0), index=dates, name="Close")

        async def mock_run_in_executor(*args, **kwargs):
            return series

        with patch('asyncio.get_running_loop') as mock_get_loop:
            mock_loop = MagicMock()
            mock_loop.run_in_executor = mock_run_in_executor
            mock_get_loop.return_value = mock_loop

            result = await get_portfolio_history(days=10)

            assert len(result) == 10
            # 150 * 10 = 1500 + 1000 (cash) = 2500
            assert result[0]['total_value'] == 2500.0
            assert result[0]['cash_balance'] == 1000.0
