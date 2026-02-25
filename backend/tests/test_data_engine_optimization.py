import pytest
import pandas as pd
import numpy as np
from unittest.mock import MagicMock, patch, call
from datetime import datetime, timedelta, timezone
from decimal import Decimal

# Import the module to test
# We need to make sure backend path is in sys.path or we run pytest from backend/
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from data_engine import AlpacaClient, PriceData

@pytest.mark.asyncio
async def test_get_historical_bars_yfinance_fallback_optimization():
    """
    Test that the optimized yfinance fallback in AlpacaClient works correctly.
    Verifies:
    1. It correctly falls back to yfinance (mocked).
    2. It correctly normalizes UK stocks (divides by 100).
    3. It correctly formats timestamps (milliseconds).
    4. It returns the expected list of dicts.
    """

    # Mock yfinance
    with patch('yfinance.Ticker') as MockTicker:
        # Setup mock data
        dates = pd.date_range(start='2023-01-01', periods=5, freq='D')

        # Create a DataFrame similar to what yfinance returns
        # UK Stock prices in Pence (GBX)
        mock_data = pd.DataFrame({
            'Open': [1000.0, 1010.0, 1020.0, 1030.0, 1040.0],
            'High': [1050.0, 1060.0, 1070.0, 1080.0, 1090.0],
            'Low': [950.0, 960.0, 970.0, 980.0, 990.0],
            'Close': [1025.0, 1035.0, 1045.0, 1055.0, 1065.0],
            'Volume': [100, 200, 300, 400, 500],
            'Dividends': [0, 0, 0, 0, 0],
            'Stock Splits': [0, 0, 0, 0, 0]
        }, index=dates)

        # Configure the mock
        mock_ticker_instance = MagicMock()
        mock_ticker_instance.history.return_value = mock_data
        MockTicker.return_value = mock_ticker_instance

        # Initialize client (will be in offline mode if env vars are not set, which is expected)
        # We patch logger to suppress warnings, but we want to see errors if it fails
        with patch('data_engine.logger') as mock_logger:
            client = AlpacaClient()
            # Force data_client to be None to ensure fallback
            client.data_client = None

            # 1. Test UK Stock (should be divided by 100)
            ticker = "TEST.L"
            result = await client.get_historical_bars(ticker, timeframe="1Day", limit=5)

            if result is None:
                print("\nLogger calls during failure:")
                for call in mock_logger.mock_calls:
                    print(call)

            assert result is not None
            assert result['ticker'] == ticker
            bars = result['bars']
            assert len(bars) == 5

            # Verify first bar
            first_bar = bars[0]
            # 1000.0 GBX -> 10.00 GBP
            assert first_bar['open'] == Decimal('10.00')
            assert first_bar['high'] == Decimal('10.50')
            assert first_bar['low'] == Decimal('9.50')
            assert first_bar['close'] == Decimal('10.25')
            assert first_bar['volume'] == 100

            # Verify timestamp
            # 2023-01-01 00:00:00 UTC -> 1672531200000 ms
            expected_ts = int(pd.Timestamp("2023-01-01", tz="UTC").timestamp() * 1000)
            
            # Now PriceData includes 't' (int ms) as well as 'timestamp' (ISO string)
            assert first_bar['t'] == expected_ts
            assert "2023-01-01" in first_bar['timestamp']


            # 2. Test US Stock (should NOT be divided)
            ticker_us = "AAPL"
            # Return same data
            mock_ticker_instance.history.return_value = mock_data.copy()

            result_us = await client.get_historical_bars(ticker_us, timeframe="1Day", limit=5)

            assert result_us is not None
            bars_us = result_us['bars']
            first_bar_us = bars_us[0]

            # 1000.0 USD -> 1000.0 USD
            assert first_bar_us['open'] == Decimal('1000.00')
            assert first_bar_us['close'] == Decimal('1025.00')

            # 3. Test TZ-Aware Index
            dates_tz = pd.date_range(start='2023-01-01', periods=5, freq='D', tz='America/New_York')
            mock_data_tz = mock_data.copy()
            mock_data_tz.index = dates_tz
            mock_ticker_instance.history.return_value = mock_data_tz

            result_tz = await client.get_historical_bars(ticker_us, timeframe="1Day", limit=5)
            assert result_tz is not None
            # 2023-01-01 00:00:00 EST -> 2023-01-01 05:00:00 UTC
            # 1672531200 + 5*3600 = 1672549200
            expected_ts_tz = 1672549200000
            # Verified: Robust timezone handling in data_engine.py now correctly converts to UTC
            assert result_tz['bars'][0]['t'] == expected_ts_tz
            assert "2023-01-01T05:00:00" in result_tz['bars'][0]['timestamp']

@pytest.mark.asyncio
async def test_get_batch_bars_optimization():
    """Test get_batch_bars logic (splitting Alpaca vs UK, and batching)."""
    from cache_manager import cache
    cache.clear()
    
    with patch('data_engine.AlpacaClient.get_historical_bars') as mock_single_fetch:
        # Mock single fetch to return specific data based on ticker
        async def side_effect(ticker, timeframe, limit):
            return {"ticker": ticker, "bars": [], "source": "single"}
        mock_single_fetch.side_effect = side_effect
        
        # 1. Test Offline Mode (No Data Client)
        client = AlpacaClient()
        client.data_client = None # Ensure offline
        
        tickers = ["AAPL", "LLOY.L"]
        results = await client.get_batch_bars(tickers)
        
        # Should call single fetch for both
        assert len(results) == 2
        assert mock_single_fetch.call_count == 2
        
        # 2. Test Online Mode (With Data Client)
        mock_data_client = MagicMock()
        client.data_client = mock_data_client
        
        # Mock the batch response
        # We need to mimic the Alpaca SDK response object structure
        mock_response = MagicMock()
        
        # Create mock bar objects
        mock_bar = MagicMock()
        mock_bar.timestamp = datetime.now(timezone.utc)
        mock_bar.open = 100.0
        mock_bar.high = 101.0
        mock_bar.low = 99.0
        mock_bar.close = 100.0
        mock_bar.volume = 1000
        
        mock_response.data = {
            "AAPL": [mock_bar]
             # TSLA missing from response to test partial failure handling
        }
        
        # Mock the to_thread call which executes the SDK method
        # We need to patch asyncio.to_thread because it's used in the method
        # It must return an awaitable
        async def async_return(*args, **kwargs):
            return mock_response

        with patch('asyncio.to_thread', side_effect=async_return) as mock_to_thread:
             
             mock_single_fetch.reset_mock()
             
             tickers = ["AAPL", "TSLA", "LLOY.L"]
             results = await client.get_batch_bars(tickers)
             
             # Logic:
             # AAPL -> Batch -> Success
             # TSLA -> Batch -> Missing -> Fallback -> Single Fetch
             # LLOY.L -> UK -> Fallback -> Single Fetch
             
             # Verify we got results
             assert "AAPL" in results
             assert "TSLA" in results
             assert "LLOY.L" in results
             
             # Verify Single Fetch Calls (should be called for TSLA and LLOY.L)
             # Note: logic calls gather(*tasks)
             assert mock_single_fetch.call_count == 2
             
             tickers_called = [c[0][0] for c in mock_single_fetch.call_args_list]
             assert "TSLA" in tickers_called
             assert "LLOY.L" in tickers_called
             assert "AAPL" not in tickers_called


if __name__ == "__main__":
    # Manually run the test function if executed as script
    import asyncio
    try:
        asyncio.run(test_get_historical_bars_yfinance_fallback_optimization())
        asyncio.run(test_get_batch_bars_optimization())
        print("Test passed!")
    except AssertionError as e:
        print(f"Test failed: {e}")
        import traceback
        traceback.print_exc()
    except Exception as e:
        print(f"An error occurred: {e}")
        import traceback
        traceback.print_exc()