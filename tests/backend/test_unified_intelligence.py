import pytest
import numpy as np
from utils.financial_math import TechnicalIndicators
from utils.ticker_utils import TickerResolver
from utils.portfolio_analyzer import PortfolioAnalyzer

def test_ticker_resolver_python_logic():
    """Verify TickerResolver Python logic for UK tickers."""
    resolver = TickerResolver()
    # Explicitly test the Python normalization logic
    # BARC -> BARC.L
    assert resolver.normalize("BARC") == "BARC.L"

def test_math_library_numpy_path():
    """Verify TechnicalIndicators work via explicit NumPy path."""
    prices = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0]
    # SMA should work via NumPy
    sma = TechnicalIndicators.calculate_sma(prices, period=3, backend='numpy')
    assert len(sma) == len(prices)
    assert sma[-1] == pytest.approx(104.0)

def test_math_library_rsi_numpy():
    """Verify RSI NumPy implementation."""
    prices = [float(100 + i) for i in range(20)]
    rsi = TechnicalIndicators.calculate_rsi(prices, period=14, backend='numpy')
    assert len(rsi) == len(prices)
    assert 50.0 <= rsi[-1] <= 100.0

def test_portfolio_analyzer_edge_cases():
    """Verify PortfolioAnalyzer handles missing data gracefully."""
    # Empty prices
    returns = PortfolioAnalyzer.calculate_daily_returns([])
    assert len(returns) == 1
    assert returns[0] == 0.0
    
    # Single price
    returns = PortfolioAnalyzer.calculate_daily_returns([100.0])
    assert len(returns) == 1
    assert returns[0] == 0.0

def test_unified_math_auto_fallback():
    """Verify that 'auto' backend produces valid results."""
    prices = [float(100 + i) for i in range(10)]
    res_auto = TechnicalIndicators.calculate_sma(prices, period=5, backend='auto')
    assert len(res_auto) == 10
    assert not np.all(res_auto == 0)
