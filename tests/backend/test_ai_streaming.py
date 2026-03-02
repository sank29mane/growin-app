import pytest
import json
import asyncio
from fastapi.testclient import TestClient
from server import app

client = TestClient(app)

def test_strategy_stream_headers():
    """Verify SSE headers for SOTA AG-UI streaming."""
    with client.stream("GET", "/api/ai/strategy/stream?session_id=test-session") as response:
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["Content-Type"]
        assert response.headers["Cache-Control"] == "no-cache"
        assert response.headers["X-Accel-Buffering"] == "no"

@pytest.mark.asyncio
async def test_strategy_stream_events():
    """Verify sequence of events in the strategy stream."""
    from unittest.mock import patch, AsyncMock, MagicMock
    from datetime import datetime
    
    from routes.ai_routes import strategy_event_generator
    
    # We'll patch OrchestratorAgent and the messenger to return controlled events
    with patch("agents.orchestrator_agent.OrchestratorAgent") as MockOrch, \
         patch("agents.messenger.get_messenger") as MockGetMessenger:
        
        mock_messenger = MagicMock()
        MockGetMessenger.return_value = mock_messenger
        
        # We simulate the messenger behavior: when run() is called, 
        # it will trigger the handler provided to subscribe_to_trace
        handlers = []
        def mock_subscribe(cid, handler):
            handlers.append(handler)
            
        mock_messenger.subscribe_to_trace.side_effect = mock_subscribe
        
        # Mock the orch run to trigger handlers
        async def mock_run(**kwargs):
            # Simulate messages being sent on the bus
            class MockMsg:
                def __init__(self, subject, sender, payload):
                    self.subject = subject
                    self.sender = sender
                    self.payload = payload
                    self.timestamp = datetime.now()
            
            for h in handlers:
                await h(MockMsg("intent_classified", "OrchestratorAgent", {}))
                await h(MockMsg("agent_started", "QuantAgent", {}))
            
            return {"content": "Success", "context": MagicMock()}
            
        mock_orch_instance = MockOrch.return_value
        mock_orch_instance.run.side_effect = mock_run
        
        events = []
        # Increase timeout or ensure we don't block
        # the generator now yields an initial status_update before the task starts, so we need to collect at least 3 events to see the reasoning_step
        async for event in strategy_event_generator("test-session", "AAPL"):
            events.append(event)
            if len(events) >= 3:
                break
                
        assert any(e["event"] == "status_update" for e in events)
        assert any(e["event"] == "reasoning_step" for e in events)

def test_strategy_not_found():
    """Verify 404 for invalid strategy IDs."""
    response = client.get("/api/ai/strategy/invalid-id")
    assert response.status_code == 404

def test_strategy_challenge_invalid():
    """Verify 404 for challenging invalid strategy."""
    response = client.post("/api/ai/strategy/invalid-id/challenge?challenge=test")
    assert response.status_code == 404
