import asyncio
import pytest
import time
import sys
from pathlib import Path
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, AsyncMock

# Add project root and backend folder to path
project_root = Path(__file__).parent.parent.parent.absolute()
sys.path.append(str(project_root))
sys.path.append(str(project_root / 'backend'))

from agents.orchestrator import SwarmOrchestrator, SwarmResponse
from agents.swarm_utils import ContextBuffer, AgentResult
from utils.hardware_guard import hardware_guard

# Mock Stream Result helper
class MockStreamResult:
    def __init__(self, text_chunks, messages=None):
        self.chunks = text_chunks
        self.messages = messages or ["reflex_mock_msg"]
        
    async def __aenter__(self):
        return self
        
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        pass
        
    async def stream_text(self):
        for chunk in self.chunks:
            yield chunk
            
    def new_messages(self):
        return self.messages

@pytest.mark.asyncio
async def test_reflex_latency():
    """
    Test Case 1: Reflex latency verification.
    Verify that the orchestrator starts streaming reflex response immediately
    once fast data is available, even if slow specialists are still running.
    """
    orch = SwarmOrchestrator()
    # Clear any default results
    orch.buffer = ContextBuffer()
    
    # Mock agent run_stream
    orch.agent.run_stream = MagicMock(side_effect=lambda prompt, **kwargs: MockStreamResult(["Reflex Token 1", "Reflex Token 2"]))
    
    # Push fast data
    await orch.buffer.push(AgentResult(
        source="QuantEngine",
        data={"signal": "BULLISH"},
        conviction=8
    ))
    
    # Push slow data with a delay of 0.3s in the background
    async def push_slow_later():
        await asyncio.sleep(0.3)
        await orch.buffer.push(AgentResult(
            source="SocialSentiment",
            data={"sentiment": "BEARISH"},
            conviction=7
        ))
    
    asyncio.create_task(push_slow_later())
    
    start_time = time.time()
    tokens = []
    
    async for chunk in orch.stream_swarm_run("test query"):
        tokens.append(chunk)
        # Check latency to the first actual token (ignoring Stage 1 header)
        if len(tokens) == 2:
            first_token_time = time.time() - start_time
            # First token must arrive very quickly (<200ms in mocked run, well below <1.5s limit)
            assert first_token_time < 0.2, f"First token latency too high: {first_token_time}s"
            
    # Verify both stages executed
    assert "=== STAGE 1: REFLEX ===\n" in tokens
    assert "\n=== STAGE 2: SYNTHESIS ===\n" in tokens

@pytest.mark.asyncio
async def test_pivot_logic():
    """
    Test Case 2: Pivot logic verification.
    Verify that the orchestrator performs a second synthesis call with message history
    when slow data contradicts or refines fast data.
    """
    orch = SwarmOrchestrator()
    orch.buffer = ContextBuffer()
    
    # Track prompt calls
    calls = []
    
    def mock_run_stream(prompt, message_history=None):
        calls.append((prompt, message_history))
        if message_history is None:
            return MockStreamResult(["Reflex: Bullish Buy signal."], ["reflex_message"])
        else:
            return MockStreamResult(["Synthesis: Wait, pivot to Sell!"], ["synthesis_message"])
            
    orch.agent.run_stream = MagicMock(side_effect=mock_run_stream)
    
    # Setup fast and slow data
    await orch.buffer.push(AgentResult(
        source="QuantEngine",
        data={"signal": "BULLISH"},
        conviction=8
    ))
    
    await orch.buffer.push(AgentResult(
        source="SocialSentiment",
        data={"sentiment": "BEARISH"},
        conviction=7
    ))
    
    response = await orch.execute_swarm_run("Should we trade Growin?")
    
    assert isinstance(response, SwarmResponse)
    assert "Bullish Buy" in response.reflex_conclusion
    assert "pivot to Sell" in response.synthesis_conclusion
    assert response.confidence_score == 0.75 # (8 + 7) / 20
    
    # Assert the correct history was passed to the second (Synthesis) call
    assert len(calls) == 2
    assert calls[0][1] is None  # Reflex call has no history
    assert calls[1][1] == ["reflex_message"]  # Synthesis call received Reflex history

@pytest.mark.asyncio
async def test_memory_guard_check():
    """
    Test Case 3: Memory guard verification.
    Verify that the heavy_inference GPU semaphore is acquired during Reflex,
    released while waiting for slow data, and re-acquired for Synthesis.
    """
    orch = SwarmOrchestrator()
    orch.buffer = ContextBuffer()
    
    # Spy on heavy_inference
    semaphore_events = []
    original_heavy = hardware_guard.heavy_inference
    
    @asynccontextmanager
    async def spy_heavy_inference():
        semaphore_events.append("acquire_start")
        async with original_heavy():
            semaphore_events.append("acquired")
            try:
                yield
            finally:
                semaphore_events.append("released")
                
    hardware_guard.heavy_inference = spy_heavy_inference
    
    orch.agent.run_stream = MagicMock(side_effect=lambda prompt, **kwargs: MockStreamResult(["Token"]))
    
    # Setup fast data
    await orch.buffer.push(AgentResult(
        source="QuantEngine",
        data={"signal": "BULLISH"},
        conviction=8
    ))
    
    # Push slow data after a short delay
    async def push_slow():
        await asyncio.sleep(0.1)
        await orch.buffer.push(AgentResult(
            source="SocialSentiment",
            data={"sentiment": "BEARISH"},
            conviction=7
        ))
        
    asyncio.create_task(push_slow())
    
    # Consume the stream
    async for _ in orch.stream_swarm_run("test"):
        pass
        
    # Verify semaphore event ordering
    # Reflex: acquire_start -> acquired -> released
    # Synthesis: acquire_start -> acquired -> released
    expected_sequence = [
        "acquire_start",
        "acquired",
        "released", # Released after Stage 1 finishes
        "acquire_start", # Re-acquired when slow data is ready
        "acquired",
        "released" # Released after Stage 2 finishes
    ]
    
    assert semaphore_events == expected_sequence, f"Semaphore sequence mismatch: {semaphore_events}"
    
    # Restore original method
    hardware_guard.heavy_inference = original_heavy
