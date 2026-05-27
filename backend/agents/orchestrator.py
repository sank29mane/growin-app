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

from typing import AsyncGenerator

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
            system_prompt=(
                "You are the Swarm Orchestrator for the Growin App. "
                "Your goal is to coordinate multiple specialist agents to extract profit. "
                "Provide a clear, precise, and actionable reasoning conclusion based on the provided specialist data."
            )
        )
        self.buffer = ContextBuffer()

    async def test_tool(self, ctx: RunContext) -> str:
        """A simple test tool to verify delegation logic."""
        return "Specialist tool call successful."

    async def _simulate_fast_specialist(self):
        await asyncio.sleep(0.05)
        await self.buffer.push(AgentResult(
            source="QuantEngine",
            data={"signal": "BULLISH", "confidence": 0.85},
            conviction=8
        ))

    async def stream_swarm_run(self, query: str) -> AsyncGenerator[str, None]:
        """
        Streams a 2-stage reasoning process:
        Stage 1: Reflex - triggered immediately when fast specialist data is available.
        Stage 2: Synthesis - triggered when slow specialist data is available, refining or pivoting.
        """
        # Ensure we have some fast data (wait up to 500ms for fast specialists)
        fast_sources = {"QuantEngine", "ForecastBridge", "QuantAgent"}
        all_results = await self.buffer.get_all()
        fast_data = [r for r in all_results if r.source in fast_sources]

        if not fast_data:
            if len(all_results) == 0:
                # Trigger default simulation in case nothing is pushed
                asyncio.create_task(self._simulate_fast_specialist())
            await self.buffer.wait_for_new(timeout=0.5)
            all_results = await self.buffer.get_all()
            fast_data = [r for r in all_results if r.source in fast_sources]

        # Stage 1: Reflex
        async with hardware_guard.heavy_inference():
            logger.info("⚡ Stage 1: Reflex inference starting")
            if not fast_data and all_results:
                # Fallback to whatever we have if no specific fast data found
                fast_data = [all_results[0]]

            fast_context = "\n".join([
                f"- {r.source}: {r.data} (Conviction: {r.conviction}/10)" 
                for r in fast_data
            ])
            
            prompt = (
                f"User Query: {query}\n\n"
                f"Immediate Specialist Data:\n{fast_context}\n\n"
                "Please provide a FAST Reflex conclusion based on this immediate data."
            )
            
            yield "=== STAGE 1: REFLEX ===\n"
            
            history = []
            async with self.agent.run_stream(prompt) as reflex_result:
                async for token in reflex_result.stream_text():
                    yield token
                # Fetch messages to continue the conversation
                history = reflex_result.new_messages()

        # Stage 2: Synthesis - release GPU lock while waiting for slow data
        all_results = await self.buffer.get_all()
        slow_data = [r for r in all_results if r not in fast_data]

        if not slow_data:
            logger.info("⏳ Waiting up to 2.0s for slow specialist data...")
            self.buffer.new_data_event.clear()  # Clear stale events from fast data pushes
            await self.buffer.wait_for_new(timeout=2.0)
            all_results = await self.buffer.get_all()
            slow_data = [r for r in all_results if r not in fast_data]

        if slow_data:
            logger.info("📥 Slow data received, triggering Stage 2: Synthesis Pivot")
            slow_context = "\n".join([
                f"- {r.source}: {r.data} (Conviction: {r.conviction}/10)" 
                for r in slow_data
            ])
            
            synthesis_prompt = (
                f"New Late-Arriving Specialist Data:\n{slow_context}\n\n"
                "Please review the new data. If it contradicts or refines your initial finding, "
                "explicitly pivot and provide a revised 'Synthesis' conclusion. "
                "If it confirms your finding, consolidate the final strategy."
            )
            
            # Re-acquire GPU lock for the synthesis stage
            async with hardware_guard.heavy_inference():
                yield "\n=== STAGE 2: SYNTHESIS ===\n"
                async with self.agent.run_stream(synthesis_prompt, message_history=history) as synthesis_result:
                    async for token in synthesis_result.stream_text():
                        yield token
        else:
            logger.info("⚠️ No slow data received within timeout, skipping Stage 2.")

    async def execute_swarm_run(self, query: str) -> SwarmResponse:
        """
        Executes a full swarm inference cycle and returns the final structured SwarmResponse.
        """
        reflex_parts = []
        synthesis_parts = []
        is_synthesis = False
        
        async for chunk in self.stream_swarm_run(query):
            if chunk == "\n=== STAGE 2: SYNTHESIS ===\n":
                is_synthesis = True
                continue
            if chunk == "=== STAGE 1: REFLEX ===\n":
                continue
            
            if is_synthesis:
                synthesis_parts.append(chunk)
            else:
                reflex_parts.append(chunk)
                
        reflex_conclusion = "".join(reflex_parts).strip()
        synthesis_conclusion = "".join(synthesis_parts).strip() if synthesis_parts else None
        
        # Calculate confidence score dynamically based on slow/fast data
        confidence = 0.85
        all_data = await self.buffer.get_all()
        if all_data:
            confidence = sum(r.conviction for r in all_data) / (len(all_data) * 10)
            
        return SwarmResponse(
            reflex_conclusion=reflex_conclusion,
            synthesis_conclusion=synthesis_conclusion,
            confidence_score=confidence
        )

async def get_orchestrator() -> SwarmOrchestrator:
    return SwarmOrchestrator()
