"""
Vision Agent - Technical chart analysis using local VLMs (MLX)
"""

from .base_agent import BaseAgent, AgentConfig, AgentResponse
from market_context import VisionData, VisualPattern
from typing import Dict, Any, List, Optional
import logging
from pydantic import BaseModel, Field
from magentic import prompt as mag_prompt
from datetime import datetime, timezone
import asyncio

logger = logging.getLogger(__name__)

class VisionAnalysis(BaseModel):
    """Structured analysis from VLM"""
    patterns: List[VisualPattern]
    raw_description: str

@mag_prompt(
    "Analyze this technical chart description and extract structured patterns.\n"
    "Description: {description}\n"
    "Identify any technical patterns (e.g., Head and Shoulders, Double Top, Bull Flag, Support/Resistance).\n"
    "For each pattern, provide a confidence score (0-1) and a brief reasoning."
)
def extract_visual_patterns(description: str) -> VisionAnalysis:
    ...

class VisionAgent(BaseAgent):
    """
    Agent specialized in visual technical analysis using local VLMs.
    """
    
    def __init__(self, config: AgentConfig = None):
        if config is None:
            config = AgentConfig(
                name="VisionAgent",
                enabled=True,
                timeout=60.0, # Vision tasks take longer
                cache_ttl=300
            )
        super().__init__(config)
        self.model_name = "mlx-community/Qwen2.5-VL-7B-Instruct-4bit"
        self._engine = None

    @property
    def engine(self):
        if self._engine is None:
            from mlx_vlm_engine import get_vlm_engine
            self._engine = get_vlm_engine()
            if not self._engine.is_loaded():
                self._engine.load_model(self.model_name)
        return self._engine

    async def analyze(self, context: Dict[str, Any]) -> AgentResponse:
        """
        Analyze a chart image using VLM.

        Args:
            context: Dict containing:
                - image: image data (bytes or path)
                - ticker: optional ticker
        """
        image_data = context.get("image")
        if not image_data:
            return AgentResponse(
                agent_name=self.config.name,
                success=False,
                data={},
                error="No image data provided for VisionAgent",
                latency_ms=0
            )

        try:
            # 1. Run VLM Inference
            prompt = (
                "Describe this technical stock chart in detail. "
                "Focus on price trends, support/resistance levels, and any visible technical patterns "
                "like triangles, head and shoulders, or flags. Provide coordinate hints if possible."
            )
            
            # The engine already handles offloading to thread if it uses mlx_vlm directly in a blocking way
            raw_description = await self.engine.generate(image_data, prompt)
            
            # 2. Extract structured patterns using Magentic
            # We use Magentic to turn the raw text description into structured Pydantic models
            analysis = await asyncio.to_thread(extract_visual_patterns, raw_description)
            
            vision_data = VisionData(
                patterns=analysis.patterns,
                raw_description=analysis.raw_description or raw_description,
                timestamp=datetime.now(timezone.utc)
            )

            return AgentResponse(
                agent_name=self.config.name,
                success=True,
                data=vision_data.model_dump(),
                latency_ms=0
            )
            
        except Exception as e:
            logger.error(f"Vision analysis failed: {e}")
            return AgentResponse(
                agent_name=self.config.name,
                success=False,
                data={},
                error=str(e),
                latency_ms=0
            )
