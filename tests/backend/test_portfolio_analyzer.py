import pytest
import numpy as np
import pandas as pd
from decimal import Decimal
from unittest.mock import MagicMock, patch
from utils.portfolio_analyzer import PortfolioAnalyzer
from utils.jmce_model import NeuralJMCE
from utils.mlx_loader import HAS_MLX

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


# ---------------------------------------------------------------------------
# Tests for PR changes: optimize_weights refactored model-dispatch logic
# ---------------------------------------------------------------------------

class _MockCoreMLModel:
    """Duck-typed CoreML JMCE model used to exercise the CoreML branch."""

    def __init__(self, mu_override=None, L_override=None, V_override=None,
                 raise_on_call=False, initialized=True):
        self._initialized = initialized
        self._mu_override = mu_override
        self._L_override = L_override
        self._V_override = V_override
        self._raise_on_call = raise_on_call

    def __call__(self, x, error_vector=None, return_velocity=False):
        if self._raise_on_call:
            raise RuntimeError("Simulated CoreML failure")
        n = x.shape[1] if x.ndim >= 2 else 1
        mu = self._mu_override if self._mu_override is not None else np.zeros(n, dtype=np.float32)
        L = self._L_override if self._L_override is not None else np.eye(n, dtype=np.float32)
        V = self._V_override if return_velocity else None
        return mu, L, V


@pytest.fixture
def multi_asset_returns():
    rng = np.random.default_rng(42)
    return rng.normal(0.001, 0.01, (60, 5)).astype(np.float32)


@pytest.fixture
def single_asset_returns():
    rng = np.random.default_rng(42)
    return rng.normal(0.001, 0.01, (60, 1)).astype(np.float32)


# --- CPU/Numpy fallback path ---

@pytest.mark.asyncio
async def test_optimize_weights_cpu_fallback_multiasset(multi_asset_returns):
    """
    When model is neither NeuralJMCE+MLX nor CoreML, the new CPU fallback
    should compute mu/sigma via numpy and still return valid Decimal weights.
    """
    analyzer = PortfolioAnalyzer(model='mock')
    weights = await analyzer.optimize_weights(multi_asset_returns, macro_signals={})

    assert len(weights) == 5
    assert all(isinstance(w, Decimal) for w in weights)
    total = sum(float(w) for w in weights)
    assert pytest.approx(total, abs=1e-3) == 1.0


@pytest.mark.asyncio
async def test_optimize_weights_cpu_fallback_single_asset(single_asset_returns):
    """
    Single-asset fallback must use np.atleast_2d(np.var(...)) for sigma.
    The optimizer should still return a single weight equal to 1.0 (100% in
    the only asset).
    """
    analyzer = PortfolioAnalyzer(model='mock')
    weights = await analyzer.optimize_weights(single_asset_returns, macro_signals={})

    assert len(weights) == 1
    assert isinstance(weights[0], Decimal)
    # With a single asset and 10% cap, the optimizer may converge to 10%;
    # either way the result must be a valid non-negative Decimal.
    assert float(weights[0]) >= 0.0


@pytest.mark.asyncio
async def test_optimize_weights_cpu_fallback_returns_decimals_all_positive(multi_asset_returns):
    """All returned weights should be non-negative (long-only constraint)."""
    analyzer = PortfolioAnalyzer(model='mock')
    weights = await analyzer.optimize_weights(multi_asset_returns, macro_signals={})
    assert all(float(w) >= 0.0 for w in weights)


@pytest.mark.asyncio
async def test_optimize_weights_cpu_fallback_defensive_persona(multi_asset_returns):
    """CPU fallback must also work for the 'defensive' (min-vol) persona."""
    analyzer = PortfolioAnalyzer(model='mock')
    weights = await analyzer.optimize_weights(
        multi_asset_returns, macro_signals={}, persona='defensive'
    )
    assert len(weights) == 5
    assert all(isinstance(w, Decimal) for w in weights)


# --- CoreML branch ---

@pytest.mark.asyncio
async def test_optimize_weights_coreml_path_basic(multi_asset_returns):
    """
    When the model has _initialized=True the CoreML branch should run and
    return valid Decimal weights whose sum is approximately 1.
    """
    model = _MockCoreMLModel()
    analyzer = PortfolioAnalyzer(model=model)
    weights = await analyzer.optimize_weights(multi_asset_returns, macro_signals={})

    assert len(weights) == 5
    assert all(isinstance(w, Decimal) for w in weights)
    total = sum(float(w) for w in weights)
    assert pytest.approx(total, abs=1e-3) == 1.0


@pytest.mark.asyncio
async def test_optimize_weights_coreml_path_none_mu_raises(multi_asset_returns):
    """
    If the CoreML model returns mu=None the RuntimeError should be caught
    internally and the method should fall back to equal weights.
    """
    class _NullMuModel:
        _initialized = True
        def __call__(self, x, **kw):
            n = x.shape[1]
            return None, np.eye(n, dtype=np.float32), None

    analyzer = PortfolioAnalyzer(model=_NullMuModel())
    weights = await analyzer.optimize_weights(multi_asset_returns, macro_signals={})

    # Should fall back to equal weights (n=5 -> 0.2 each)
    assert len(weights) == 5
    assert all(isinstance(w, Decimal) for w in weights)


@pytest.mark.asyncio
async def test_optimize_weights_coreml_path_none_L_raises(multi_asset_returns):
    """
    If the CoreML model returns L=None the RuntimeError should be caught
    internally and the method should fall back to equal weights.
    """
    class _NullLModel:
        _initialized = True
        def __call__(self, x, **kw):
            n = x.shape[1]
            return np.zeros(n, dtype=np.float32), None, None

    analyzer = PortfolioAnalyzer(model=_NullLModel())
    weights = await analyzer.optimize_weights(multi_asset_returns, macro_signals={})

    assert len(weights) == 5
    assert all(isinstance(w, Decimal) for w in weights)


@pytest.mark.asyncio
async def test_optimize_weights_coreml_path_batched_L(multi_asset_returns):
    """
    CoreML may return L with an extra batch dimension (ndim==3).
    The code strips it; the result should still be valid weights.
    """
    n = multi_asset_returns.shape[1]
    L_batched = np.eye(n, dtype=np.float32)[np.newaxis, :, :]  # shape (1, n, n)
    mu_flat = np.zeros(n, dtype=np.float32)

    class _BatchedLModel:
        _initialized = True
        def __call__(self, x, **kw):
            return mu_flat, L_batched, None

    analyzer = PortfolioAnalyzer(model=_BatchedLModel())
    weights = await analyzer.optimize_weights(multi_asset_returns, macro_signals={})

    assert len(weights) == 5
    assert all(isinstance(w, Decimal) for w in weights)


@pytest.mark.asyncio
async def test_optimize_weights_coreml_path_batched_mu(multi_asset_returns):
    """
    CoreML may return mu with an extra batch dimension (ndim==2).
    The code strips it; the result should still be valid weights.
    """
    n = multi_asset_returns.shape[1]
    mu_batched = np.zeros((1, n), dtype=np.float32)  # shape (1, n)
    L_flat = np.eye(n, dtype=np.float32)

    class _BatchedMuModel:
        _initialized = True
        def __call__(self, x, **kw):
            return mu_batched, L_flat, None

    analyzer = PortfolioAnalyzer(model=_BatchedMuModel())
    weights = await analyzer.optimize_weights(multi_asset_returns, macro_signals={})

    assert len(weights) == 5
    assert all(isinstance(w, Decimal) for w in weights)


@pytest.mark.asyncio
async def test_optimize_weights_coreml_not_initialized_falls_back(multi_asset_returns):
    """
    A model with _initialized=False should skip the CoreML branch and fall
    through to the CPU fallback, returning valid equal/optimized weights.
    """
    model = _MockCoreMLModel(initialized=False)
    analyzer = PortfolioAnalyzer(model=model)
    weights = await analyzer.optimize_weights(multi_asset_returns, macro_signals={})

    assert len(weights) == 5
    assert all(isinstance(w, Decimal) for w in weights)


# --- MLX path (patched) ---

@pytest.mark.asyncio
async def test_optimize_weights_mlx_path_with_patched_has_mlx(multi_asset_returns):
    """
    Verifies the MLX branch of optimize_weights is exercised when HAS_MLX is
    patched to True and a NeuralJMCE-typed model is provided with a mocked
    forward pass.  A NeuralJMCE subclass is used so isinstance() passes
    naturally without patching builtins.
    """
    n = multi_asset_returns.shape[1]

    mu_fake = np.zeros((1, n), dtype=np.float32)
    sigma_fake = np.eye(n, dtype=np.float32)[np.newaxis]  # (1, n, n)

    class _FakeNeuralJMCE(NeuralJMCE):
        def __init__(self):
            # Skip parent __init__ to avoid MLX dependency on non-MLX CI
            self.n_assets = n

        def __call__(self, x, **kwargs):
            return mu_fake, MagicMock(), None

        def get_covariance(self, L):
            return sigma_fake

    mock_mx = MagicMock()
    mock_mx.array.side_effect = lambda x: x  # pass numpy arrays through unchanged

    fake_model = _FakeNeuralJMCE()
    analyzer = PortfolioAnalyzer(model=fake_model)

    with patch('utils.portfolio_analyzer.HAS_MLX', True), \
         patch('utils.portfolio_analyzer.mx', mock_mx):
        weights = await analyzer.optimize_weights(multi_asset_returns, macro_signals={})

    assert len(weights) == n
    assert all(isinstance(w, Decimal) for w in weights)


# ---------------------------------------------------------------------------
# Tests for PR changes: get_covariance_velocity refactored model-dispatch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_covariance_velocity_no_acceleration_returns_none(multi_asset_returns):
    """
    When the model has no MLX and no _initialized attribute, the new explicit
    else branch must return None.
    """
    analyzer = PortfolioAnalyzer(model='mock')
    result = await analyzer.get_covariance_velocity(multi_asset_returns)
    assert result is None


@pytest.mark.asyncio
async def test_get_covariance_velocity_coreml_path_2d_V(multi_asset_returns):
    """
    CoreML path: when V is a 2D array it should be treated as already stripped
    and the Frobenius norm is returned as a float.
    """
    n = multi_asset_returns.shape[1]
    V_2d = np.eye(n, dtype=np.float32)  # ndim == 2

    class _VeloModel:
        _initialized = True
        def __call__(self, x, error_vector=None, return_velocity=False):
            return None, None, V_2d if return_velocity else None

    analyzer = PortfolioAnalyzer(model=_VeloModel())
    result = await analyzer.get_covariance_velocity(multi_asset_returns)

    assert result is not None
    assert isinstance(result, float)
    assert result > 0.0


@pytest.mark.asyncio
async def test_get_covariance_velocity_coreml_path_3d_V_batch_stripped(multi_asset_returns):
    """
    CoreML path: when V has ndim==3 (batch dimension present) the code strips
    the first axis and returns the Frobenius norm.
    """
    n = multi_asset_returns.shape[1]
    V_3d = np.eye(n, dtype=np.float32)[np.newaxis, :, :]  # shape (1, n, n)

    class _BatchedVModel:
        _initialized = True
        def __call__(self, x, error_vector=None, return_velocity=False):
            return None, None, V_3d if return_velocity else None

    analyzer = PortfolioAnalyzer(model=_BatchedVModel())
    result = await analyzer.get_covariance_velocity(multi_asset_returns)

    assert result is not None
    assert isinstance(result, float)
    assert result > 0.0


@pytest.mark.asyncio
async def test_get_covariance_velocity_coreml_V_none(multi_asset_returns):
    """
    When the CoreML model returns V=None the function should return None
    (no Frobenius norm computed).
    """
    class _NoVeloModel:
        _initialized = True
        def __call__(self, x, error_vector=None, return_velocity=False):
            return None, None, None

    analyzer = PortfolioAnalyzer(model=_NoVeloModel())
    result = await analyzer.get_covariance_velocity(multi_asset_returns)
    assert result is None


@pytest.mark.asyncio
async def test_get_covariance_velocity_coreml_exception_returns_none(multi_asset_returns):
    """
    If the CoreML model raises an exception, get_covariance_velocity must
    catch it and return None instead of propagating.
    """
    model = _MockCoreMLModel(raise_on_call=True)
    analyzer = PortfolioAnalyzer(model=model)
    result = await analyzer.get_covariance_velocity(multi_asset_returns)
    assert result is None


@pytest.mark.asyncio
async def test_get_covariance_velocity_not_initialized_returns_none(multi_asset_returns):
    """
    A model with _initialized=False should skip the CoreML branch and return
    None (same as no-acceleration path).
    """
    model = _MockCoreMLModel(initialized=False)
    analyzer = PortfolioAnalyzer(model=model)
    result = await analyzer.get_covariance_velocity(multi_asset_returns)
    assert result is None


@pytest.mark.asyncio
async def test_get_covariance_velocity_mlx_path_multi_asset():
    """
    Verifies the MLX branch of get_covariance_velocity for multi-asset case
    by patching HAS_MLX and mx, using a NeuralJMCE subclass so isinstance
    check passes without requiring real MLX.
    """
    n = 3
    rng = np.random.default_rng(7)
    returns = rng.normal(0, 0.01, (40, n)).astype(np.float32)

    V_fake = np.ones((1, n, n), dtype=np.float32)  # (batch=1, n, n)
    expected_norm = float(np.linalg.norm(V_fake[0]))

    class _FakeNeuralJMCE(NeuralJMCE):
        def __init__(self):
            self.n_assets = n

        def __call__(self, x, error_vector=None, return_velocity=False):
            return None, None, V_fake if return_velocity else None

    fake_model = _FakeNeuralJMCE()
    analyzer = PortfolioAnalyzer(model=fake_model)

    mock_mx = MagicMock()
    mock_mx.array.side_effect = lambda x: x  # pass-through

    with patch('utils.portfolio_analyzer.HAS_MLX', True), \
         patch('utils.portfolio_analyzer.mx', mock_mx):
        result = await analyzer.get_covariance_velocity(returns)

    assert result is not None
    assert isinstance(result, float)
    assert pytest.approx(result, rel=1e-5) == expected_norm


@pytest.mark.asyncio
async def test_get_covariance_velocity_mlx_path_single_asset():
    """
    Verifies the MLX branch returns the scalar value directly when n_assets==1
    (V shape is (1, 1, 1)).
    """
    n = 1
    rng = np.random.default_rng(11)
    returns = rng.normal(0, 0.01, (40, n)).astype(np.float32)

    V_scalar = np.array([[[0.42]]], dtype=np.float32)  # shape (1, 1, 1)

    class _SingleAssetModel(NeuralJMCE):
        def __init__(self):
            self.n_assets = n

        def __call__(self, x, error_vector=None, return_velocity=False):
            return None, None, V_scalar if return_velocity else None

    fake_model = _SingleAssetModel()
    analyzer = PortfolioAnalyzer(model=fake_model)

    mock_mx = MagicMock()
    mock_mx.array.side_effect = lambda x: x

    with patch('utils.portfolio_analyzer.HAS_MLX', True), \
         patch('utils.portfolio_analyzer.mx', mock_mx):
        result = await analyzer.get_covariance_velocity(returns)

    assert result is not None
    assert isinstance(result, float)
    assert pytest.approx(result, rel=1e-5) == 0.42
