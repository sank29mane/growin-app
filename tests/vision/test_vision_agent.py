import pytest
from unittest.mock import MagicMock, patch, AsyncMock
try:
    import mlx.core as mx
except ImportError:
    mx = None

import pytest
if mx is None:
    pytest.skip("MLX not available, skipping VLM tests", allow_module_level=True)

from agents.vision_agent import VisionAgent, VisionAnalysis
from market_context import VisualPattern, VisionData
from datetime import datetime, timezone

@pytest.fixture
def vision_agent():
    return VisionAgent()

@pytest.mark.asyncio
async def test_vision_agent_no_image(vision_agent):
    """Test VisionAgent returns failure if no image is provided"""
    response = await vision_agent.analyze({"ticker": "AAPL"})
    assert response.success is False
    assert "No image data provided" in response.error

@pytest.mark.asyncio
async def test_vision_agent_success(vision_agent):
    """Test VisionAgent successfully analyzes an image and structures the output"""
    mock_description = "A chart showing a bull flag pattern."
    mock_analysis = VisionAnalysis(
        patterns=[
            VisualPattern(name="Bull Flag", confidence=0.9, reasoning="Clear consolidation after a sharp rise.")
        ],
        raw_description="A chart showing a bull flag pattern."
    )

    with patch("mlx_vlm_engine.get_vlm_engine") as mock_get_engine:
        mock_engine = mock_get_engine.return_value
        mock_engine.is_loaded.return_value = True
        mock_engine.generate = AsyncMock(return_value=mock_description)
        
        # We need to patch the magentic function which is called via asyncio.to_thread
        with patch("agents.vision_agent.extract_visual_patterns", return_value=mock_analysis):
            response = await vision_agent.analyze({"image": b"fake_image_bytes", "ticker": "AAPL"})
            
            assert response.success is True
            assert "patterns" in response.data
            assert len(response.data["patterns"]) == 1
            assert response.data["patterns"][0]["name"] == "Bull Flag"
            assert response.data["raw_description"] == mock_description

@pytest.mark.asyncio
async def test_vision_agent_exception(vision_agent):
    """Test VisionAgent handles exceptions gracefully"""
    with patch("mlx_vlm_engine.get_vlm_engine") as mock_get_engine:
        mock_engine = mock_get_engine.return_value
        mock_engine.is_loaded.return_value = True
        mock_engine.generate = AsyncMock(side_effect=Exception("VLM Engine Error"))
        
        response = await vision_agent.analyze({"image": b"fake_image_bytes"})
        
        assert response.success is False
        assert "VLM Engine Error" in response.error
