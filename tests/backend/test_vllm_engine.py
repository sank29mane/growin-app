import pytest
import asyncio
from unittest.mock import MagicMock, patch, AsyncMock
import sys
import os

# Add backend to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../backend')))

# Create mock classes before import
class MockBatchedEngine:
    def __init__(self, *args, **kwargs):
        self.start = AsyncMock()
        self.stop = AsyncMock()
        self.generate = AsyncMock()
        self.stream_generate = MagicMock()
        self._loaded = True

# We need to mock the entire module structure for vllm_mlx
mock_vllm_mlx = MagicMock()
mock_batched_mod = MagicMock()
mock_scheduler_mod = MagicMock()

sys.modules['vllm_mlx'] = mock_vllm_mlx
sys.modules['vllm_mlx.engine'] = MagicMock()
sys.modules['vllm_mlx.engine.batched'] = mock_batched_mod
sys.modules['vllm_mlx.scheduler'] = mock_scheduler_mod

# Assign mock classes to the mocked modules
mock_batched_mod.BatchedEngine = MockBatchedEngine
mock_scheduler_mod.SchedulerConfig = MagicMock

# Now import the engine
from vllm_engine import VLLMInferenceEngine

@pytest.mark.asyncio
async def test_vllm_engine_init():
    engine = VLLMInferenceEngine()
    assert engine.engine is None
    assert engine.current_model_path is None

@pytest.mark.asyncio
async def test_vllm_engine_load_model():
    engine = VLLMInferenceEngine()
    
    # Patch HAS_VLLM_MLX to True for testing
    with patch('vllm_engine.HAS_VLLM_MLX', True):
        success = await engine.load_model("test-model")
        
        assert success is True
        assert engine.current_model_path == "test-model"
        assert engine.engine is not None
        engine.engine.start.assert_called_once()

@pytest.mark.asyncio
async def test_vllm_engine_generate():
    engine = VLLMInferenceEngine()
    mock_instance = MockBatchedEngine()
    engine.engine = mock_instance
    
    mock_output = MagicMock()
    mock_output.text = "Generated text"
    mock_instance.generate.return_value = mock_output
    
    result = await engine.generate("Test prompt")
    
    assert result == "Generated text"
    mock_instance.generate.assert_called_once_with(
        prompt="Test prompt",
        max_tokens=512,
        temperature=0.7,
        top_p=0.9
    )

@pytest.mark.asyncio
async def test_vllm_engine_stream_generate():
    engine = VLLMInferenceEngine()
    mock_instance = MockBatchedEngine()
    engine.engine = mock_instance
    
    # Mock async iterator for stream_generate
    async def mock_stream_iter(*args, **kwargs):
        chunk1 = MagicMock()
        chunk1.new_text = "token"
        chunk1.completion_tokens = 1
        chunk1.finished = False
        yield chunk1
        
        chunk2 = MagicMock()
        chunk2.new_text = " done"
        chunk2.completion_tokens = 2
        chunk2.finished = True
        yield chunk2

    mock_instance.stream_generate.side_effect = mock_stream_iter
    
    chunks = []
    async for chunk in engine.stream_generate("Test prompt"):
        chunks.append(chunk)
        
    assert len(chunks) == 2
    assert chunks[0]["text"] == "token"
    assert chunks[1]["text"] == " done"
    assert chunks[1]["tokens"] == 2
    assert chunks[1]["finished"] is True

@pytest.mark.asyncio
async def test_vllm_engine_stop():
    engine = VLLMInferenceEngine()
    mock_instance = MockBatchedEngine()
    engine.engine = mock_instance
    engine.current_model_path = "some-model"
    
    await engine.stop()
    
    mock_instance.stop.assert_called_once()
    assert engine.engine is None
    assert engine.current_model_path is None
