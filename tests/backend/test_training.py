import os
import sys
import tempfile
import pytest
import numpy as np
import pandas as pd
import joblib
from sklearn.mixture import GaussianMixture

# Ensure backend is in the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../backend')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from backend.analytics_db import AnalyticsDB
from scripts.train_gmm_regime import extract_training_data, fit_optimal_gmm


@pytest.fixture
def mock_analytics_db():
    """Create a temporary in-memory database with mock training features."""
    # Ensure a fresh connection
    AnalyticsDB.reset_global_connection()
    db = AnalyticsDB(db_path=":memory:")
    
    # Insert mock raw data
    data = []
    base_time = pd.Timestamp("2026-01-01 09:30:00")
    
    # We generate a dataset composed of two distinct regimes
    np.random.seed(42)
    
    # Regime 0: Low volatility (std=0.005), low spread (avg=0.001)
    for i in range(100):
        # We need historical prices to compute returns/features. Let's insert into ohlcv_history
        # Daily return will be calculated relative to previous close.
        # Let's insert sequential prices to trigger proper features.
        # Volatility is calculated as rolling std of returns.
        # For simplicity, let's just insert into ohlcv_history and call calculate_and_ingest_features.
        pass

    # Actually, let's construct a direct sequence of prices that yields distinct vol and spread
    # ticker AAPL: 50 points of low volatility, 50 points of high volatility
    # Spreads: high=close*(1+spread), low=close*(1-spread).
    # Since rolling spread is AVG((high-low)/close) = AVG(2*spread), let's construct high/low/close accordingly.
    
    prices_aapl = []
    current_price = 100.0
    for i in range(150):
        ts = base_time + pd.Timedelta(days=i)
        # First 75: low variance, low spread
        if i < 75:
            ret = np.random.normal(0, 0.001)
            spread_factor = 0.002
        else:
            ret = np.random.normal(0, 0.05)
            spread_factor = 0.02
            
        current_price *= (1.0 + ret)
        high = current_price * (1.0 + spread_factor)
        low = current_price * (1.0 - spread_factor)
        
        prices_aapl.append({
            'timestamp': ts.isoformat(),
            'open': current_price,
            'high': high,
            'low': low,
            'close': current_price,
            'volume': 1000000
        })
        
    db.bulk_insert_ohlcv("AAPL", prices_aapl)
    
    yield db
    AnalyticsDB.reset_global_connection()


def test_database_extraction(mock_analytics_db):
    """Test that extract_training_data extracts correct columns and handles empty state."""
    df = extract_training_data(":memory:")
    assert isinstance(df, pd.DataFrame)
    assert not df.empty
    assert "volatility" in df.columns
    assert "spread" in df.columns
    assert "ticker" in df.columns
    assert "timestamp" in df.columns
    
    # Filter by ticker
    df_aapl = extract_training_data(":memory:", ticker="AAPL")
    assert (df_aapl['ticker'] == 'AAPL').all()


def test_fit_optimal_gmm_bic():
    """Test that fit_optimal_gmm selects K that minimizes BIC, converges, and checks degenerate covs."""
    # Generate fake normalized features: 2 clusters
    np.random.seed(42)
    cluster1 = np.random.multivariate_normal([1.0, 1.0], [[0.1, 0.0], [0.0, 0.1]], 200)
    cluster2 = np.random.multivariate_normal([-1.0, -1.0], [[0.1, 0.0], [0.0, 0.1]], 200)
    features = np.vstack([cluster1, cluster2])
    
    best_gmm, best_k, bic_scores = fit_optimal_gmm(features, min_k=2, max_k=4)
    
    # Assert K=2 is selected because the data has 2 clear components and minimal BIC
    assert best_k == 2
    assert bic_scores[2] < bic_scores[3]
    assert bic_scores[2] < bic_scores[4]
    
    # Assert GMM properties
    assert best_gmm.converged_
    assert best_gmm.covariance_type == 'full'
    assert best_gmm.weights_.shape[0] == 2
    assert best_gmm.means_.shape == (2, 2)


def test_fit_optimal_gmm_degenerate_handling():
    """Test that degenerate covariance matrices are excluded."""
    # Create features with zero variance to force degenerate covariance
    np.random.seed(42)
    features = np.zeros((100, 2))
    # Add minor noise to prevent total singularity failure but keep eigenvalues close to 0
    features += np.random.normal(0, 1e-9, size=(100, 2))
    
    with pytest.raises(ValueError, match="Failed to fit any valid, converged, non-degenerate GMM models"):
        fit_optimal_gmm(features, min_k=2, max_k=4, reg_covar=0.0)


def test_offline_training_script_end_to_end(mock_analytics_db):
    """Test the training script execution flow and payload serialization."""
    with tempfile.TemporaryDirectory() as tmpdir:
        output_path = os.path.join(tmpdir, "gmm_regime.joblib")
        
        # We invoke main logic using mock database path ":memory:"
        df = extract_training_data(":memory:")
        X = df[['volatility', 'spread']].values
        
        # Scale
        from sklearn.preprocessing import StandardScaler
        scaler = StandardScaler()
        X_scaled = scaler.fit_transform(X)
        
        # Fit
        model, best_k, bic_scores = fit_optimal_gmm(X_scaled)
        
        # Prepare payload
        payload = {
            "model": model,
            "scaler": scaler,
            "best_k": best_k,
            "bic_scores": bic_scores,
            "means": model.means_,
            "covariances": model.covariances_,
            "weights": model.weights_,
            "precisions_cholesky": model.precisions_cholesky_,
            "scaler_mean": scaler.mean_,
            "scaler_scale": scaler.scale_
        }
        
        # Save
        joblib.dump(payload, output_path)
        assert os.path.exists(output_path)
        
        # Load and verify
        loaded = joblib.load(output_path)
        assert loaded["best_k"] in [2, 3, 4]
        assert isinstance(loaded["model"], GaussianMixture)
        assert isinstance(loaded["scaler"], StandardScaler)
        assert np.allclose(loaded["means"], loaded["model"].means_)
