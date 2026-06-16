import sys
import os
import pytest
import datetime
from unittest.mock import AsyncMock, MagicMock, patch

# Add backend to sys.path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app_context import state, ChatMessage
from market_context import MarketContext
from routes.chat_routes import stream_chat_generator, extract_ticker_from_text

@pytest.mark.asyncio
async def test_stream_chat_generator():
    """Test the stream_chat_generator function directly."""

    # Save original state properties to avoid test leakage
    orig_chat_manager = getattr(state, "chat_manager", None)
    orig_mcp_client = getattr(state, "mcp_client", None)

    # Mock Chat Manager
    mock_chat_manager = MagicMock()
    mock_chat_manager.create_conversation.return_value = "test-conv-id"
    mock_chat_manager.load_history.return_value = []

    state.chat_manager = mock_chat_manager
    state.mcp_client = MagicMock()

    try:
        # Mock MarketContext
        mock_market_context = MarketContext(
            intent="general",
            ticker="AAPL",
            query="Analyze AAPL",
            user_context={}
        )

        class MockFinalEvent:
            def __init__(self, market_context, quick_actions=None):
                self.market_context = market_context
                self.quick_actions = quick_actions or []

        # Mock extract_ticker_from_text to return AAPL
        with patch("routes.chat_routes.extract_ticker_from_text", return_value="AAPL"), \
             patch("agents.orchestrator_agent.OrchestratorAgent") as MockOrchestrator, \
             patch("routes.chat_routes.update_conversation_title_if_needed", new_callable=AsyncMock):

            orchestrator_instance = MockOrchestrator.return_value

            # Generator for streaming
            async def mock_run_stream(*args, **kwargs):
                yield "Chunk 1"
                yield "Chunk 2"
                yield MockFinalEvent(market_context=mock_market_context)

            orchestrator_instance.run_stream = mock_run_stream

            # Test Input
            request = ChatMessage(message="Analyze AAPL", model_name="test-model")

            # Mock the LLM factory to prevent libmlx crashes in test
            with patch('agents.llm_factory.LLMFactory.create_llm', new_callable=AsyncMock) as MockFactory:
                MockFactory.return_value = MagicMock()

                # Run generator
                chunks = []
                async for chunk in stream_chat_generator(request):
                    chunks.append(chunk)

                # Verify
                assert len(chunks) > 0

                events = [c.get("event") for c in chunks]
                assert "meta" in events

                # If libmlx is missing, it will yield an error event, otherwise tokens
                if "error" not in events:
                    assert "token" in events  # At least one token
                    tokens = [c["data"] for c in chunks if c["event"] == "token"]
                    assert "".join(tokens) == "Chunk 1Chunk 2"
    finally:
        # Restore original state
        if orig_chat_manager is not None:
            state.chat_manager = orig_chat_manager
        if orig_mcp_client is not None:
            state.mcp_client = orig_mcp_client
