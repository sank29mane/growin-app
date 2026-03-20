"""
Vision Agent - Technical chart analysis using local VLMs (MLX)
"""

from backend.agents.base_agent import BaseAgent, AgentConfig, AgentResponse
from backend.market_context import VisionData, VisualPattern
from typing import Dict, Any, List, Optional
import logging
import os
import time
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
        # SOTA 2026: Shadow Mode toggle
        self.shadow_mode = os.environ.get("GROWIN_SHADOW_MODE", "0") == "1"

    @property
    def engine(self):
        if self._engine is None:
            from backend.mlx_vlm_engine import get_vlm_engine
            self._engine = get_vlm_engine()
            # Note: load_model is now managed by the engine's lazy load/TTL logic
        return self._engine

    def _check_for_injection(self, text: str) -> bool:
        """SOTA 2026: Basic visual prompt injection detection."""
        injection_patterns = [
            "ignore all previous instructions",
            "ignore the prompt",
            "system override",
            "new instructions:",
            "user is now admin",
            "forget your training"
        ]
        text_lower = text.lower()
        for pattern in injection_patterns:
            if pattern in text_lower:
                logger.warning(f"🚨 Potential visual prompt injection detected: '{pattern}'")
                return True
        return False

    async def analyze(self, context: Dict[str, Any]) -> AgentResponse:
        """
        Analyze a chart image using VLM with SOTA guardrails.
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

        # SOTA 2026: Shadow Mode Interceptor (Task 3.1 placeholder)
        if self.shadow_mode:
             logger.info("🕵️ Shadow Mode: VisionAgent intercepting for logging only.")

        try:
            # 1. Run VLM Inference
            prompt = (
                "Describe this technical stock chart in detail. "
                "Focus on price trends, support/resistance levels, and any visible technical patterns "
                "like triangles, head and shoulders, or flags. Provide coordinate hints if possible."
            )
            
            start_time = time.time()
            raw_description = await self.engine.generate(image_data, prompt)
            
            # SOTA 2026: Guardrail check
            if self._check_for_injection(raw_description):
                 return AgentResponse(
                    agent_name=self.config.name,
                    success=False,
                    data={},
                    error="Security Policy: Visual prompt injection detected in chart analysis.",
                    latency_ms=int((time.time() - start_time) * 1000)
                )

            # 2. Extract structured patterns using Magentic
            analysis = await asyncio.to_thread(extract_visual_patterns, raw_description)
            
            vision_data = VisionData(
                patterns=analysis.patterns,
                raw_description=analysis.raw_description or raw_description,
                timestamp=datetime.now(timezone.utc)
            )

            latency = int((time.time() - start_time) * 1000)
            
            return AgentResponse(
                agent_name=self.config.name,
                success=True,
                data=vision_data.model_dump(),
                latency_ms=latency
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
