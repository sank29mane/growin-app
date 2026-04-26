import asyncio
import logging
import os
from typing import List, Dict, Any, Optional
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIModel
from pydantic import BaseModel
from utils.hardware_guard import hardware_guard
from .swarm_utils import ContextBuffer, AgentResult

logger = logging.getLogger(__name__)

class SwarmResponse(BaseModel):
    """
    Final output from the Decision Swarm.
    """
    reflex_conclusion: str
    synthesis_conclusion: Optional[str] = None
    confidence_score: float

class SwarmOrchestrator:
    """
    SOTA 2026 Swarm Orchestrator.
    Manages specialist agent delegation and 2-stage 'Progressive Synthesis' 
    using pydantic-ai.
    """
    
    def __init__(self, model_name: str = "nemotron-3-30b-moe-jang-q4_k_m"):
        self.model_name = model_name
        # Initialize OpenAI-compatible model for vMLX
        self.model = OpenAIModel(
            model_name,
            base_url=os.getenv("VMLX_BASE_URL", "http://127.0.0.1:8000/v1"),
            api_key="vmlx-local-token"
        )
        self.agent = Agent(
            self.model,
            result_type=SwarmResponse,
            system_prompt=(
                "You are the Swarm Orchestrator for the Growin App. "
                "Your goal is to coordinate multiple specialist agents to extract profit. "
                "You must first provide a FAST reflex conclusion based on immediate data, "
                "and then REVISE it as more detailed context arrives in the synthesis buffer."
            )
        )
        self.buffer = ContextBuffer()

    async def test_tool(self, ctx: RunContext) -> str:
        """A simple test tool to verify delegation logic."""
        return "Specialist tool call successful."

    async def execute_swarm_run(self, query: str):
        """
        Executes a full swarm inference cycle with hardware guarding.
        """
        # Ensure we don't thrash the M4 Pro GPU (28GB VRAM limit)
        async with hardware_guard.heavy_inference():
            logger.info(f"🚀 Initializing Swarm Reasoning for: {query}")
            
            # This is where the 2-stage logic will eventually reside (Wave 1)
            # For now, just a scaffold run
            try:
                result = await self.agent.run(query)
                return result.data
            except Exception as e:
                logger.error(f"Swarm execution failed: {e}")
                raise

async def get_orchestrator() -> SwarmOrchestrator:
    return SwarmOrchestrator()
