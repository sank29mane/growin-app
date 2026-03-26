import pytest
import numpy as np
import pandas as pd
from backend.utils.portfolio_analyzer import PortfolioAnalyzer

@pytest.fixture
def price_history():
    # 1% daily return trend
    return [100.0 * (1.01**i) for i in range(30)]

@pytest.fixture
def benchmark_history():
    # 0.5% daily return trend
    return [100.0 * (1.005**i) for i in range(30)]

def test_daily_returns(price_history):
    returns = PortfolioAnalyzer.calculate_daily_returns(price_history, method='log')
    assert len(returns) == len(price_history) - 1
    # Log return of 1.01 should be approx 0.00995
    np.testing.assert_allclose(returns[0], np.log(1.01), rtol=1e-5)

def test_volatility(price_history):
    returns = PortfolioAnalyzer.calculate_daily_returns(price_history)
    vol = PortfolioAnalyzer.calculate_volatility(returns, annualize=True)
    # With perfect 1% returns, volatility should be effectively 0
    assert vol == pytest.approx(0.0, abs=1e-10)

def test_sharpe_ratio():
    # High return, zero vol -> positive Sharpe
    returns = np.array([0.01] * 252) # 1% daily
    sharpe = PortfolioAnalyzer.calculate_sharpe_ratio(returns, risk_free_rate=0.0)
    # Annual return = 0.01 * 252 = 2.52. Vol = 0. Wait, if vol=0, Sharpe is handled
    assert sharpe == 0.0 # Handled in code to avoid div by zero

    # Add some noise
    returns = np.random.normal(0.001, 0.01, 252)
    sharpe = PortfolioAnalyzer.calculate_sharpe_ratio(returns, risk_free_rate=0.0)
    assert isinstance(sharpe, float)

def test_beta(price_history, benchmark_history):
    returns = PortfolioAnalyzer.calculate_daily_returns(price_history)
    bench_returns = PortfolioAnalyzer.calculate_daily_returns(benchmark_history)

    beta = PortfolioAnalyzer.calculate_beta(returns, bench_returns)
    # Price history grows twice as fast as benchmark (approx)
    # Beta measures sensitivity
    assert beta > 0

def test_analyze_performance(price_history, benchmark_history):
    analyzer = PortfolioAnalyzer(model='mock')
    report = analyzer.analyze_performance(price_history, benchmark_history)

    assert "volatility" in report
    assert "sharpe_ratio" in report
    assert "beta" in report
    assert report["daily_returns_mean"] > 0

def test_generate_backcast_history():
    positions = [
        {"ticker": "AAPL", "qty": 10},
        {"ticker": "MSFT", "qty": 5, "entry_date": "2024-01-10"}
    ]

    # Mock market data
    dates = pd.date_range(start="2024-01-01", periods=20, freq="D")
    market_data = {
        "AAPL": [{"t": d.value // 10**6, "c": 150.0 + i} for i, d in enumerate(dates)],
        "MSFT": [{"t": d.value // 10**6, "c": 300.0 + i} for i, d in enumerate(dates)]
    }

    history = PortfolioAnalyzer.generate_backcast_history(positions, market_data)

    assert len(history) == 20
    assert "total_value" in history.columns

    # Before 2024-01-10, MSFT should be 0.0
    # Entry date 2024-01-10 is the 10th element (0-indexed 9)
    # Day 1: AAPL (150*10) + MSFT (0) = 1500
    assert history.iloc[0]["total_value"] == 1500.0

    # Day 11 (index 10): AAPL (160*10) + MSFT (310*5) = 1600 + 1550 = 3150
    assert history.iloc[10]["total_value"] == 3150.0
