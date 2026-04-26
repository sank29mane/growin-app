import pytest
from unittest.mock import MagicMock, patch, AsyncMock
import asyncio
from decimal import Decimal
import pandas as pd
import sys
import os

# Ensure backend is in path
sys.path.append(os.path.join(os.getcwd(), 'backend'))

from data_engine import AlpacaClient

@pytest.fixture
def mock_logger():
    with patch('data_engine.logger') as mock:
        yield mock


@pytest.mark.asyncio
async def test_get_historical_bars_yfinance_fallback(mock_logger):
    """Test US stock: Alpaca (Primary) fails, yfinance (Fallback) succeeds"""
    # Mock yfinance
    with patch('yfinance.Ticker') as MockTicker:
        # Setup mock data
        dates = pd.date_range(start='2023-01-01', periods=5, freq='D')
        mock_data = pd.DataFrame({
            'Open': [1000.0, 1010.0, 1020.0, 1030.0, 1040.0],
            'High': [1050.0, 1060.0, 1070.0, 1080.0, 1090.0],
            'Low': [950.0, 960.0, 970.0, 980.0, 990.0],
            'Close': [1025.0, 1035.0, 1045.0, 1055.0, 1065.0],
            'Volume': [100, 200, 300, 400, 500],
            'Dividends': [0, 0, 0, 0, 0],
            'Stock Splits': [0, 0, 0, 0, 0]
        }, index=dates)

        mock_ticker_instance = MagicMock()
        mock_ticker_instance.history.return_value = mock_data
        MockTicker.return_value = mock_ticker_instance

        client = AlpacaClient()
        # Ensure we are not using the global mock from conftest
        client.data_client = None

        ticker = "TEST.L"
        result = await client.get_historical_bars(ticker, timeframe="1Day", limit=5)

        assert result is not None
        assert result['ticker'] == ticker
        bars = result['bars']
        assert len(bars) == 5
        # 1000.0 GBX -> 10.00 GBP
        assert bars[0]['open'] == Decimal('10.00')


@pytest.mark.asyncio
async def test_get_batch_bars(mock_logger):
    """Test batch bar fetching with mocked internal method"""
    with patch('data_engine.get_finnhub_client') as mock_finnhub:

        mock_fh_instance = MagicMock()
        mock_finnhub.return_value = mock_fh_instance
        async def fh_side_effect(ticker, timeframe, limit):
            return {"ticker": ticker, "bars": [], "source": "finnhub"}
        mock_fh_instance.get_historical_bars.side_effect = fh_side_effect

        client = AlpacaClient()
        client.data_client = None

        with patch.object(client, '_fetch_batch_from_yfinance') as mock_yf:
            def yf_side_effect(tickers, timeframe, limit):
                return {t: {"ticker": t, "bars": [], "source": "yfinance"} for t in tickers}
            mock_yf.side_effect = yf_side_effect

            tickers = ["AAPL", "LLOY.L"]
            results = await client.get_batch_bars(tickers)

            assert len(results) == 2
            assert mock_fh_instance.get_historical_bars.call_count == 1
            assert mock_yf.call_count == 1


@pytest.mark.asyncio
async def test_get_portfolio_positions(mock_logger):
    """Test position fetching with both mock fallback and real client mock"""
    # 1. Test Mock Fallback (when trading_client is None)
    client = AlpacaClient()
    client.trading_client = None

    positions = await client.get_portfolio_positions()

    assert len(positions) == 1
    assert positions[0]['symbol'] == "AAPL"
    assert isinstance(positions[0]['qty'], Decimal)

    # 2. Test with Trading Client Mock
    mock_trading_client = MagicMock()
    client.trading_client = mock_trading_client

    # Create a mock position object
    class MockPosition:
        def __init__(self):
            self.symbol = "TSLA"
            self.qty = "5"
            self.market_value = "1000.0"
            self.cost_basis = "900.0"
            self.unrealized_pl = "100.0"
            self.unrealized_plpc = "0.11"
            self.current_price = "200.0"
            self.lastday_price = "190.0"
            self.change_today = "10.0"
            self.side = "long"
            self.avg_entry_price = "180.0"

    mock_positions_list = [MockPosition()]

    # Explicitly mock the to_thread behavior for this test
    with patch('asyncio.to_thread', new_callable=AsyncMock) as mock_to_thread:
        mock_to_thread.return_value = mock_positions_list
        positions = await client.get_portfolio_positions()

        assert len(positions) == 1
        assert positions[0]['symbol'] == "TSLA"
        assert positions[0]['qty'] == Decimal("5")
        assert positions[0]['market_value'] == Decimal("1000.0")
