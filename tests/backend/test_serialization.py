import os
import sys
import numpy as np
import pytest
import joblib
from sklearn.mixture import GaussianMixture
from sklearn.preprocessing import StandardScaler

# Ensure backend is in the path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../backend')))
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from backend.coreml.gmm_loader import load_gmm_params

def test_load_gmm_params_success(tmp_path):
    """Test successful loading of valid GMM parameters from npz file."""
    # Prepare dummy parameters
    weights = np.array([0.4, 0.6])
    means = np.array([[1.0, 2.0], [3.0, 4.0]])
    precisions_cholesky = np.array([[[1.5, 0.0], [0.5, 1.2]], [[1.8, 0.0], [0.2, 1.1]]])
    scaler_mean = np.array([0.5, 1.5])
    scaler_var = np.array([0.25, 0.64])
    
    file_path = tmp_path / "test_gmm_params.npz"
    np.savez_compressed(
        file_path,
        weights=weights,
        means=means,
        precisions_cholesky=precisions_cholesky,
        scaler_mean=scaler_mean,
        scaler_var=scaler_var
    )
    
    # Load and verify
    params = load_gmm_params(str(file_path))
    
    assert np.allclose(params["weights"], weights)
    assert np.allclose(params["means"], means)
    assert np.allclose(params["precisions_cholesky"], precisions_cholesky)
    assert np.allclose(params["scaler_mean"], scaler_mean)
    assert np.allclose(params["scaler_var"], scaler_var)

def test_load_gmm_params_file_not_found():
    """Test error when the NPZ parameter file does not exist."""
    with pytest.raises(FileNotFoundError):
        load_gmm_params("non_existent_file_path_12345.npz")

def test_load_gmm_params_missing_keys(tmp_path):
    """Test error when required keys are missing in the NPZ file."""
    file_path = tmp_path / "incomplete_params.npz"
    np.savez_compressed(
        file_path,
        weights=np.array([0.5, 0.5]),
        means=np.array([[1.0], [2.0]])
        # missing precision_cholesky, scaler_mean, scaler_var
    )
    
    with pytest.raises(ValueError, match="Missing required parameter key"):
        load_gmm_params(str(file_path))

def test_load_gmm_params_nan_inf_detection(tmp_path):
    """Test detection of NaN or Infinity values in loaded parameters."""
    # Test NaN
    file_path_nan = tmp_path / "nan_params.npz"
    np.savez_compressed(
        file_path_nan,
        weights=np.array([0.5, np.nan]),
        means=np.array([[1.0, 2.0], [3.0, 4.0]]),
        precisions_cholesky=np.ones((2, 2, 2)),
        scaler_mean=np.array([0.5, 1.5]),
        scaler_var=np.array([0.2, 0.3])
    )
    with pytest.raises(ValueError, match="contains NaN values"):
        load_gmm_params(str(file_path_nan))
        
    # Test Inf
    file_path_inf = tmp_path / "inf_params.npz"
    np.savez_compressed(
        file_path_inf,
        weights=np.array([0.5, 0.5]),
        means=np.array([[1.0, np.inf], [3.0, 4.0]]),
        precisions_cholesky=np.ones((2, 2, 2)),
        scaler_mean=np.array([0.5, 1.5]),
        scaler_var=np.array([0.2, 0.3])
    )
    with pytest.raises(ValueError, match="contains infinity values"):
        load_gmm_params(str(file_path_inf))

def test_loaded_params_match_source_model():
    """Verify that loaded parameters from the actual output directory match the source model."""
    params_path = "models/gmm_regime_params.npz"
    joblib_path = "models/gmm_regime.joblib"
    
    if not os.path.exists(params_path) or not os.path.exists(joblib_path):
        pytest.skip("Models not yet generated on disk. Skipping match verification.")
        
    loaded_payload = joblib.load(joblib_path)
    model = loaded_payload["model"]
    scaler = loaded_payload["scaler"]
    
    loaded_params = load_gmm_params(params_path)
    
    assert np.allclose(loaded_params["weights"], model.weights_)
    assert np.allclose(loaded_params["means"], model.means_)
    assert np.allclose(loaded_params["precisions_cholesky"], model.precisions_cholesky_)
    assert np.allclose(loaded_params["scaler_mean"], scaler.mean_)
    assert np.allclose(loaded_params["scaler_var"], scaler.var_)
