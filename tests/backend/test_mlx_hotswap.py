import sys
import os
import pytest
import tempfile
import numpy as np
import mlx.core as mx
import mlx.nn as nn

# Ensure root and backend are in sys.path
root_path = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if root_path not in sys.path:
    sys.path.insert(0, root_path)
backend_path = os.path.join(root_path, "backend")
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)

from backend.mlx_engine import MLXInferenceEngine

class MockMLXModel(nn.Module):
    """Mock MLX model for testing in-memory parameter updates."""
    def __init__(self):
        super().__init__()
        self.w1 = mx.array([1.0, 2.0])
        self.w2 = mx.array([3.0, 4.0])

    def update(self, params):
        if 'w1' in params:
            self.w1 = params['w1']
        if 'w2' in params:
            self.w2 = params['w2']

def test_switch_adapter_success():
    engine = MLXInferenceEngine()
    engine.model = MockMLXModel()
    
    # Save dummy weights to a temporary directory
    with tempfile.TemporaryDirectory() as temp_dir:
        weights_path = os.path.join(temp_dir, 'adapters.safetensors')
        mx.save_safetensors(weights_path, {
            'w1': mx.array([10.0, 20.0]),
            'w2': mx.array([30.0, 40.0])
        })
        
        # Verify initial state
        assert np.allclose(np.array(engine.model.w1), np.array([1.0, 2.0]))
        assert np.allclose(np.array(engine.model.w2), np.array([3.0, 4.0]))
        
        # Switch adapters
        success = engine.switch_adapter(temp_dir)
        assert success is True
        
        # Verify state was updated in-memory
        assert np.allclose(np.array(engine.model.w1), np.array([10.0, 20.0]))
        assert np.allclose(np.array(engine.model.w2), np.array([30.0, 40.0]))

def test_switch_adapter_no_model():
    engine = MLXInferenceEngine()
    engine.model = None  # No model loaded
    
    with tempfile.TemporaryDirectory() as temp_dir:
        success = engine.switch_adapter(temp_dir)
        assert success is False

def test_switch_adapter_missing_directory():
    engine = MLXInferenceEngine()
    engine.model = MockMLXModel()
    
    # Non-existent path
    success = engine.switch_adapter('/path/does/not/exist')
    assert success is False

def test_switch_adapter_missing_weights_file():
    engine = MLXInferenceEngine()
    engine.model = MockMLXModel()
    
    # Empty directory (no adapters.safetensors or adapters.npz)
    with tempfile.TemporaryDirectory() as temp_dir:
        success = engine.switch_adapter(temp_dir)
        assert success is False
