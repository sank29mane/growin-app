import pytest
import pandas as pd
import numpy as np
import sys
import os

# Ensure backend is in path
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from trading212_mcp_server import normalize_ticker

# Mock the function until it is implemented
try:
    from trading212_mcp_server import _compute_indicators
except ImportError:
    _compute_indicators = None

class TestTickerNormalization:
    def test_basic_us_tickers(self):
        assert normalize_ticker("AAPL") == "AAPL"
        assert normalize_ticker("MSFT") == "MSFT"
        assert normalize_ticker("TSLA") == "TSLA"
        assert normalize_ticker("F") == "F"  # Single letter US

    def test_basic_uk_tickers(self):
        # Should append .L if not excluded
        assert normalize_ticker("VOD") == "VOD.L"
        assert normalize_ticker("RR") == "RR.L"

    def test_explicit_suffixes(self):
        assert normalize_ticker("VOD.L") == "VOD.L"
        assert normalize_ticker("AAPL.US") == "AAPL.US" # Should probably stay as is? Or stripped?
        # The current logic: if "." in ticker: return ticker

    def test_t212_suffixes(self):
        assert normalize_ticker("VOD_EQ") == "VOD.L" # _EQ implies UK if not _US
        assert normalize_ticker("AAPL_US_EQ") == "AAPL"

    def test_special_mappings(self):
        assert normalize_ticker("SSLNL") == "SSLN.L" # Should map to SSLN and then .L?
        # Mappings: "SSLNL": "SSLN". Then "SSLN" is likely UK -> "SSLN.L"
        assert normalize_ticker("LLOY1") == "LLOY.L"

    def test_leveraged_etps(self):
        assert normalize_ticker("3GLD") == "3GLD.L" # Mapped to 3GLD, likely UK
        # The current logic strips the trailing L for 3USL -> 3US.L
        # This asserts current behavior, even if arguably debatable.
        assert normalize_ticker("3USL") == "3US.L"

    def test_us_exclusions(self):
        # "IBM" is in US exclusions, so it should NOT get .L
        assert normalize_ticker("IBM") == "IBM"
        assert normalize_ticker("CAT") == "CAT"

class TestTechnicalIndicators:
    def test_compute_indicators_logic(self):
        # Skip if function doesn't exist yet (for initial run)
        if not _compute_indicators:
            pytest.skip("_compute_indicators not implemented yet")

        # Create sample data
        dates = pd.date_range(start='2023-01-01', periods=300, freq='D')
        close = np.linspace(100, 200, 300) # Steady uptrend
        # Add some noise for bands
        close += np.sin(np.linspace(0, 20, 300)) * 5

        df = pd.DataFrame({'Close': close}, index=dates)

        result = _compute_indicators(df)

        assert "SMA_50" in result.columns
        assert "SMA_200" in result.columns
        assert "RSI" in result.columns
        assert "MACD" in result.columns
        assert "BB_Upper" in result.columns

        # Verify values for steady uptrend
        # SMA 50 should be populated after index 49
        assert not pd.isna(result["SMA_50"].iloc[50])
        assert pd.isna(result["SMA_50"].iloc[10])

        # Verify MACD calculation
        # MACD = EMA(12) - EMA(26)
        # Verify columns exist
        assert "Signal_Line" in result.columns
