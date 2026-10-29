import os
import sys
import time
import numpy as np
import pytest
from sklearn.mixture import GaussianMixture
from sklearn.preprocessing import StandardScaler

# Ensure backend is in the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../backend')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from backend.coreml.fast_gmm import fast_gmm_predict_proba

def test_fast_gmm_mathematical_parity_and_benchmark():
    """
    Test mathematical parity (under 1e-5 epsilon tolerance) between
    scikit-learn GMM predictions and Numba-optimized GMM predictor.
    The sub-10-microsecond budget lives in test_fast_gmm_microbenchmark.
    """
    # 1. Generate representative synthetic data
    np.random.seed(42)
    X = np.random.randn(200, 2)
    # Add offset to split into clusters
    X[:100] += 3.0
    X[100:] -= 3.0
    
    # Fit StandardScaler
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)
    
    # Fit GMM
    gmm = GaussianMixture(n_components=3, covariance_type='full', random_state=42)
    gmm.fit(X_scaled)
    
    # Extract model parameters
    weights = gmm.weights_
    means = gmm.means_
    precisions_cholesky = gmm.precisions_cholesky_
    scaler_mean = scaler.mean_
    scaler_var = scaler.var_
    
    # 2. Assert shape compatibility and mathematical parity
    # Let's test on 50 different raw points
    test_points = np.random.randn(50, 2) * 5.0
    
    # Compile Numba function by calling it once (warm-up)
    _ = fast_gmm_predict_proba(
        test_points[0], weights, means, precisions_cholesky, scaler_mean, scaler_var
    )
    
    # Check mathematical parity for all test points
    for x in test_points:
        # Numba prediction
        numba_prob = fast_gmm_predict_proba(
            x, weights, means, precisions_cholesky, scaler_mean, scaler_var
        )
        
        # Scikit-learn prediction (requires scaling first)
        x_scaled = scaler.transform(x.reshape(1, -1))
        sklearn_prob = gmm.predict_proba(x_scaled)[0]
        
        # Assert within 1e-5 tolerance
        np.testing.assert_allclose(numba_prob, sklearn_prob, rtol=1e-5, atol=1e-5)


@pytest.mark.performance
def test_fast_gmm_microbenchmark():
    """10,000 predictions must stay under 10 microseconds each, on a quiet core."""
    np.random.seed(42)
    X = np.random.randn(200, 2)
    X[:100] += 3.0
    X[100:] -= 3.0
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X)
    gmm = GaussianMixture(n_components=3, covariance_type="full", random_state=42)
    gmm.fit(X_scaled)
    test_points = np.array([[0.0, 0.0], [3.0, 3.0], [-3.0, -3.0], [1.0, -1.0]])
    for _ in range(10):
        fast_gmm_predict_proba(
            test_points[0], gmm.weights_, gmm.means_, gmm.precisions_cholesky_, scaler.mean_, scaler.var_
        )
    n_iterations = 10000
    start_time = time.perf_counter()
    for i in range(n_iterations):
        idx = i % len(test_points)
        fast_gmm_predict_proba(
            test_points[idx], gmm.weights_, gmm.means_, gmm.precisions_cholesky_, scaler.mean_, scaler.var_
        )
    avg_time_per_prediction_us = ((time.perf_counter() - start_time) / n_iterations) * 1e6
    print(f"\nAverage prediction time (Numba): {avg_time_per_prediction_us:.4f} microseconds")
    assert avg_time_per_prediction_us < 10.0, (
        f"Inference engine exceeded latency limit: {avg_time_per_prediction_us:.2f}us >= 10us"
    )
