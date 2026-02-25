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
    
    # Mock Chat Manager
    mock_chat_manager = MagicMock()
    mock_chat_manager.create_conversation.return_value = "test-conv-id"
    # Ensure load_history returns proper list
    mock_chat_manager.load_history.return_value = []
    
    state.chat_manager = mock_chat_manager
    state.mcp_client = MagicMock()
    
    # Mock MarketContext
    mock_market_context = MarketContext(
        intent="general",
        ticker="AAPL",
        query="Analyze AAPL",
        user_context={}
    )
    
    # Mock extract_ticker_from_text to return AAPL
    with patch("routes.chat_routes.extract_ticker_from_text", return_value="AAPL"), \
         patch("agents.coordinator_agent.CoordinatorAgent") as MockCoord, \
         patch("agents.decision_agent.DecisionAgent") as MockDecider, \
         patch("routes.chat_routes.update_conversation_title_if_needed", new_callable=AsyncMock):
         
        coord_instance = MockCoord.return_value
        coord_instance.process_query = AsyncMock(return_value=mock_market_context)
        
        decider_instance = MockDecider.return_value
        
        # Generator for streaming
        async def mock_stream(*args, **kwargs):
            yield "Chunk 1"
            yield "Chunk 2"
        
        decider_instance.make_decision_stream = mock_stream
        
        # Test Input
        request = ChatMessage(message="Analyze AAPL", model_name="test-model")
        
        # Run generator
        chunks = []
        async for chunk in stream_chat_generator(request):
            chunks.append(chunk)
            
        # Verify
        assert len(chunks) > 0
        
        events = [c.get("event") for c in chunks]
        assert "meta" in events
        # In the new implementation, we yield telemetry events from specialists
        # but in this mock test, coordination might be too fast to catch them 
        # unless we mock the messenger interaction.
        # For now, let's just ensure we have tokens and meta.
        assert "token" in events  # At least one token
        
        tokens = [c["data"] for c in chunks if c["event"] == "token"]
        assert "".join(tokens) == "Chunk 1Chunk 2"
        
        # Verify save_message called
        assert mock_chat_manager.save_message.call_count >= 2 # Once for user, once for assistant
