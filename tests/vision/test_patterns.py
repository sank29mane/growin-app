import pytest
import os
from PIL import Image
from agents.vision_agent import VisionAgent, VisionAnalysis
from market_context import VisualPattern
from utils.image_proc import prepare_vlm_image
from unittest.mock import patch, AsyncMock

@pytest.fixture
def dummy_chart_path(tmp_path):
    img_path = tmp_path / "dummy_chart.png"
    img = Image.new('RGB', (800, 600), color=(255, 255, 255))
    img.save(img_path)
    return str(img_path)

@pytest.mark.asyncio
async def test_head_and_shoulders_detection(dummy_chart_path):
    """
    Test VisionAgent can identify a Head and Shoulders pattern.
    Note: In a real environment, we'd use a real screenshot. 
    Here we verify the logic flow and structured extraction.
    """
    agent = VisionAgent()
    
    mock_description = "The chart shows a clear Head and Shoulders pattern with the left shoulder at 150, head at 170, and right shoulder at 155."
    mock_analysis = VisionAnalysis(
        patterns=[
            VisualPattern(name="Head and Shoulders", confidence=0.85, reasoning="Classic three-peak structure with a higher central peak.")
        ],
        raw_description=mock_description
    )

    with patch("mlx_vlm_engine.get_vlm_engine") as mock_get_engine:
        mock_engine = mock_get_engine.return_value
        mock_engine.is_loaded.return_value = True
        mock_engine.generate = AsyncMock(return_value=mock_description)
        
        with patch("agents.vision_agent.extract_visual_patterns", return_value=mock_analysis):
            response = await agent.analyze({"image": dummy_chart_path, "ticker": "SPY"})
            
            assert response.success is True
            patterns = response.data["patterns"]
            assert any(p["name"] == "Head and Shoulders" for p in patterns)
            assert patterns[0]["confidence"] >= 0.8
