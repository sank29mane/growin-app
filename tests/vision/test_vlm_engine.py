"""Tests for MLX VLM Inference Engine."""
import sys
from unittest.mock import MagicMock, patch

# Mock mlx_vlm before it's imported in the engine
mock_mlx_vlm = MagicMock()
sys.modules["mlx_vlm"] = mock_mlx_vlm
sys.modules["mlx_vlm.utils"] = MagicMock()

import pytest
from PIL import Image
from backend.mlx_vlm_engine import MLXVLMInferenceEngine

@pytest.fixture
def vlm_engine():
    """Fixture for MLX VLM Inference Engine."""
    return MLXVLMInferenceEngine()

def test_vlm_engine_initial_state(vlm_engine):
    """Test initial engine state."""
    assert not vlm_engine.is_loaded()
    assert vlm_engine.model is None
    assert vlm_engine.processor is None

def test_vlm_engine_load_model(vlm_engine):
    """Test model loading logic."""
    mock_model = MagicMock()
    mock_processor = MagicMock()
    mock_mlx_vlm.load.return_value = (mock_model, mock_processor)
    
    # Need to mock parameters for warmup
    mock_model.parameters.return_value = []
    
    success = vlm_engine.load_model("test-model")
    
    assert success
    assert vlm_engine.is_loaded()
    assert vlm_engine.current_model_path == "test-model"
    mock_mlx_vlm.load.assert_called_with("test-model")

@pytest.mark.asyncio
async def test_vlm_engine_generate(vlm_engine):
    """Test text generation from image and prompt."""
    vlm_engine.model = MagicMock()
    vlm_engine.processor = MagicMock()
    mock_mlx_vlm.generate.return_value = "This is a test chart."
    
    test_image = Image.new("RGB", (100, 100), color="blue")
    prompt = "Describe this image."
    
    response = await vlm_engine.generate(test_image, prompt)
    
    assert response == "This is a test chart."
    mock_mlx_vlm.generate.assert_called_once()
    
    # Verify call arguments (blocking call wrapped in thread)
    args, kwargs = mock_mlx_vlm.generate.call_args
    assert kwargs["prompt"] == prompt
    assert kwargs["image"] == [test_image]

def test_vlm_engine_unload(vlm_engine):
    """Test unloading logic."""
    vlm_engine.model = MagicMock()
    vlm_engine.processor = MagicMock()
    vlm_engine.current_model_path = "some-model"
    
    vlm_engine.unload()
    
    assert not vlm_engine.is_loaded()
    assert vlm_engine.model is None
    assert vlm_engine.processor is None
    assert vlm_engine.current_model_path is None
