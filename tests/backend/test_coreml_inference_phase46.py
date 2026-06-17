import sys
import os
import pytest
import numpy as np

# Ensure root and backend are in sys.path
root_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if root_path not in sys.path:
    sys.path.insert(0, root_path)
backend_path = os.path.join(root_path, "backend")
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)

from backend.coreml_inference import CoreMLRunner

def test_coreml_runner_init():
    runner = CoreMLRunner()
    # Should initialize without throwing exceptions even if model_path is None
    assert runner.model_path is None
    assert runner.available is False

def test_coreml_model_load_and_prediction():
    model_path = os.path.join(root_path, "models", "coreml", "NeuralJMCE.mlpackage")
    if not os.path.exists(model_path):
        pytest.skip(f"CoreML model not found at {model_path}, skipping integration test.")

    runner = CoreMLRunner()
    loaded = runner.load(model_path)
    if not loaded:
        pytest.skip("Failed to load CoreML model. CoreML runtime might not be functional in this environment.")

    assert runner.available is True
    assert runner.model_path == model_path

    # Construct input matching the shape: (1, 78, 50) with dataType FLOAT16
    # In python/coremltools, we pass float16 numpy array or standard float32 numpy array which gets cast
    dummy_returns = np.random.normal(loc=0.0, scale=0.01, size=(1, 78, 50)).astype(np.float32)

    features = {
        "returns": dummy_returns
    }

    # Run prediction
    try:
        prediction = runner.predict(features)
    except Exception as e:
        pytest.fail(f"CoreML prediction failed: {e}")

    # Verify prediction outputs
    assert isinstance(prediction, dict)
    assert "mu" in prediction
    assert "cholesky" in prediction
    assert "velocity" in prediction

    # Verify shapes
    assert prediction["mu"].shape == (1, 50)
    assert prediction["cholesky"].shape == (1, 1275)
    assert prediction["velocity"].shape == (1, 1275)

    # Verify types/content
    assert not np.isnan(prediction["mu"]).any()
    assert not np.isnan(prediction["cholesky"]).any()
    assert not np.isnan(prediction["velocity"]).any()

def test_coreml_calculate_indicators_fallback():
    # If model is not loaded, it should return an error dictionary
    runner = CoreMLRunner()
    res = runner.calculate_indicators([100.0, 101.0, 102.0])
    assert "error" in res
