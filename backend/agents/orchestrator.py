import asyncio
import logging
import os
from utils.error_handler import handle_error
from typing import List, Dict, Any, Optional, Tuple
from pydantic_ai import Agent, RunContext
from pydantic_ai.models.openai import OpenAIModel
from pydantic import BaseModel
from utils.hardware_guard import hardware_guard
from .swarm_utils import ContextBuffer, AgentResult, summarize_specialist_data

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
    
    def __init__(
        self, 
        model_name: str = "nemotron-3-30b-moe-jang-q4_k_m",
        reflex_timeout: float = 0.4,
        synthesis_timeout: float = 1.5
    ):
        self.model_name = model_name
        self.reflex_timeout = reflex_timeout
        self.synthesis_timeout = synthesis_timeout
        # Initialize OpenAI-compatible model for LM Studio
        lm_studio_url = os.getenv("LM_STUDIO_URL", "http://127.0.0.1:1234")
        self.model = OpenAIModel(
            model_name,
            base_url=f"{lm_studio_url}/v1",
            api_key="lmstudio-token"
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

    def _get_scaled_timeouts(self) -> Tuple[float, float]:
        """
        Inspect virtual memory to adjust timeouts dynamically under heavy load.
        """
        import psutil
        try:
            mem = psutil.virtual_memory()
            available_gb = mem.available / (1024**3)
            percent = mem.percent
            
            # If memory pressure is high (>85% or <6GB free RAM), scale timeouts by 1.5x
            if percent > 85.0 or available_gb < 6.0:
                logger.warning(
                    f"⚠️ High memory pressure detected ({percent}% used, {available_gb:.2f}GB free). "
                    "Scaling timeout thresholds by 1.5x."
                )
                return self.reflex_timeout * 1.5, self.synthesis_timeout * 1.5
        except Exception as e:

            handle_error(e, "Failed to read virtual memory for scaling timeouts", logger, raise_error=False)
            
        return self.reflex_timeout, self.synthesis_timeout

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
        scaled_reflex, scaled_synthesis = self._get_scaled_timeouts()
        logger.info(f"Calibration timeouts: Reflex={scaled_reflex}s, Synthesis={scaled_synthesis}s")

        # Ensure we have some fast data
        fast_sources = {"QuantEngine", "ForecastBridge", "QuantAgent"}
        all_results = await self.buffer.get_all()
        fast_data = [r for r in all_results if r.source in fast_sources]

        if not fast_data:
            if len(all_results) == 0:
                # Trigger default simulation in case nothing is pushed
                asyncio.create_task(self._simulate_fast_specialist())
            await self.buffer.wait_for_new(timeout=scaled_reflex)
            all_results = await self.buffer.get_all()
            fast_data = [r for r in all_results if r.source in fast_sources]

        # Stage 1: Reflex
        async with hardware_guard.heavy_inference():
            logger.info("⚡ Stage 1: Reflex inference starting")
            if not fast_data and all_results:
                # Fallback to whatever we have if no specific fast data found
                fast_data = [all_results[0]]

            # Apply summarization to preserve prefix caching
            fast_context = "\n".join([
                f"- {r.source}: {summarize_specialist_data(r).data} (Conviction: {r.conviction}/10)" 
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
            logger.info(f"⏳ Waiting up to {scaled_synthesis}s for slow specialist data...")
            self.buffer.new_data_event.clear()  # Clear stale events from fast data pushes
            await self.buffer.wait_for_new(timeout=scaled_synthesis)
            all_results = await self.buffer.get_all()
            slow_data = [r for r in all_results if r not in fast_data]

        if slow_data:
            logger.info("📥 Slow data received, triggering Stage 2: Synthesis Pivot")
            # Apply summarization to preserve prefix caching
            slow_context = "\n".join([
                f"- {r.source}: {summarize_specialist_data(r).data} (Conviction: {r.conviction}/10)" 
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
