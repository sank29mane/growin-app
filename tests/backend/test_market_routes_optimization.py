import pytest
import pandas as pd
import numpy as np
from unittest.mock import MagicMock, patch
import sys
import os

# Ensure backend is in path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from backend.routes.market_routes import get_portfolio_history

@pytest.mark.asyncio
async def test_get_portfolio_history_vectorized_dataframe():
    # Setup mock
    with patch('backend.routes.market_routes.state') as mock_state:
        mock_account_context = MagicMock()
        mock_state.account_context = mock_account_context
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

                # Execute
                result = await get_portfolio_history(days=days)

                assert result is not None
                assert len(result) == days
                # 150*10 + (600/100)*100 + 200*5 + 5000 = 1500 + 600 + 1000 + 5000 = 8100
                assert result[0]["total_value"] == 8100.0

@pytest.mark.asyncio
async def test_get_portfolio_history_single_ticker_series():
    # Setup mock
    with patch('backend.routes.market_routes.state') as mock_state:
        mock_account_context = MagicMock()
        mock_state.account_context = mock_account_context
        mock_account_context.get_account_or_default.return_value = 'invest'

        with patch('backend.routes.market_routes.get_live_portfolio') as mock_get_live:
            mock_get_live.return_value = {
                "positions": [{"ticker": "AAPL", "quantity": 10}],
                "summary": {"cash_balance": {"free": 1000.0}}
            }

            days = 5
            dates = pd.date_range(start="2023-01-01", periods=days, freq="D")
            data = {"AAPL": np.array([100.0, 101.0, 102.0, 103.0, 104.0])}
            df = pd.DataFrame(data, index=dates)

            async def mock_run_in_executor(*args, **kwargs):
                return df

            with patch('asyncio.get_running_loop') as mock_get_loop:
                mock_loop = MagicMock()
                mock_loop.run_in_executor = mock_run_in_executor
                mock_get_loop.return_value = mock_loop

                result = await get_portfolio_history(days=days)

                assert len(result) == 5
                # 100*10 + 1000 = 2000
                assert result[0]["total_value"] == 2000.0
                # 104*10 + 1000 = 2040
                assert result[-1]["total_value"] == 2040.0
