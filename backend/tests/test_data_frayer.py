
import pytest
import pandas as pd
from unittest.mock import patch
import asyncio
import sys
import os

# Add backend to path
sys.path.append(os.path.join(os.getcwd(), 'backend'))

from utils.data_frayer import MarketDataFrayer

@pytest.mark.asyncio
async def test_fetch_yfinance_fallback_correctness():
    # 1. Setup Data
    # Create a small DataFrame with specific values to verify logic
    rows = 3
    dates = pd.date_range(start='2023-01-01', periods=rows, freq='1D', tz='America/New_York')

    # Expected timestamps (milliseconds)
    expected_ts = [int(ts.timestamp() * 1000) for ts in dates]

    data = {
        'Open': [100.0, 101.0, 102.0],
        'High': [105.0, 106.0, 107.0],
        'Low': [95.0, 96.0, 97.0],
        'Close': [102.0, 103.0, 104.0],
        'Volume': [1000, 2000, 3000]
    }
    df = pd.DataFrame(data, index=dates)

    # 2. Setup Frayer
    frayer = MarketDataFrayer()

    # 3. Mock yfinance
    # Import yfinance to ensure it's loaded and we can patch it
    with patch('yfinance.Ticker') as MockTicker:
        mock_instance = MockTicker.return_value
        mock_instance.history.return_value = df

        # 4. Run method
        result = await frayer._fetch_yfinance_fallback("AAPL", 10, "1Day")

        # 5. Assertions
        assert "bars" in result
        bars = result["bars"]
        assert len(bars) == rows

        for i, bar in enumerate(bars):
            assert bar["t"] == expected_ts[i], f"Timestamp mismatch at index {i}: expected {expected_ts[i]}, got {bar['t']}"
            assert bar["o"] == data['Open'][i]
            assert bar["h"] == data['High'][i]
            assert bar["l"] == data['Low'][i]
            assert bar["c"] == data['Close'][i]
            assert bar["v"] == data['Volume'][i]

            # Check types
            assert isinstance(bar["t"], int)
            assert isinstance(bar["o"], float)
            assert isinstance(bar["v"], int)
    print("Test Passed!")

if __name__ == "__main__":
    asyncio.run(test_fetch_yfinance_fallback_correctness())
