
import pytest
import pandas as pd
from unittest.mock import MagicMock, patch
import sys
import asyncio

# Ensure backend is in path
import os
from routes.market_routes import get_portfolio_history

@pytest.mark.asyncio
async def test_get_portfolio_history_vectorized():
    """
    Verify that the vectorized implementation of get_portfolio_history
    calculates values correctly, including currency conversion.
    """

    # 1. Mock get_live_portfolio to return specific positions
    # Portfolio:
    # - 10 shares of AAPL (US)
    # - 100 shares of LLOY.L (UK, priced in pence)
    mock_portfolio_data = {
        "positions": [
            {"ticker": "AAPL", "quantity": 10},
            {"ticker": "LLOY.L", "quantity": 100}
        ],
        "summary": {
            "cash_balance": {"free": 1000.0}
        }
    }

    # 2. Mock yfinance data
    # Create a DataFrame with 2 timestamps
    dates = pd.to_datetime(["2024-01-01", "2024-01-02"])
    data = {
        "AAPL": [150.0, 155.0],      # USD
        "LLOY.L": [4500.0, 4600.0]   # GBX (pence) - clearly > 500
    }
    df = pd.DataFrame(data, index=dates)

    # 3. Setup mocks
    with patch('routes.market_routes.get_live_portfolio', new_callable=MagicMock) as mock_get_live:
        # Mocking async result
        f = asyncio.Future()
        f.set_result(mock_portfolio_data)
        mock_get_live.return_value = f

        # Patch yfinance inside the function execution
        with patch('yfinance.download') as mock_download:
            # yf.download(...) returns a DataFrame, then code calls ['Close'] on it.
            # So we simulate that structure.
            # We create a MagicMock that returns our df when ['Close'] is accessed.
            mock_df_result = MagicMock()
            mock_df_result.__getitem__.side_effect = lambda key: df if key == 'Close' else MagicMock()
            mock_download.return_value = mock_df_result

            # Patch app_context.account_context (patching where it is defined since it is imported inside function)
            with patch('app_context.account_context') as mock_account_context:
                mock_account_context.get_account_or_default.return_value = "invest"

                # Patch state.chat_manager to avoid fallback errors if logic fails
                with patch('routes.market_routes.state') as mock_state:
                    mock_state.chat_manager.get_portfolio_history.return_value = []

                    # 4. Execute
                    result = await get_portfolio_history(days=2)

                    # 5. Verify
                    assert len(result) == 2

                    # Calculation check:
                    # Day 1:
                    # Cash: 1000
                    # AAPL: 10 * 150 = 1500
                    # LLOY.L: 100 * (4500 / 100) = 100 * 45 = 4500 (Currency conversion triggered > 500)
                    # Total: 1000 + 1500 + 4500 = 7000

                    point1 = result[0]
                    assert point1["timestamp"] == "2024-01-01T00:00:00"
                    assert point1["total_value"] == 7000.0
                    assert point1["cash_balance"] == 1000.0

                    # Day 2:
                    # Cash: 1000
                    # AAPL: 10 * 155 = 1550
                    # LLOY.L: 100 * (4600 / 100) = 100 * 46 = 4600
                    # Total: 1000 + 1550 + 4600 = 7150

                    point2 = result[1]
                    assert point2["timestamp"] == "2024-01-02T00:00:00"
                    assert point2["total_value"] == 7150.0

                    print("\nTest Passed! Calculations match expected values.")

if __name__ == "__main__":
    # Allow running directly
    asyncio.run(test_get_portfolio_history_vectorized())
