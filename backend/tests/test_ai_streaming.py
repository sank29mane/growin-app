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
    from routes.ai_routes import strategy_event_generator
    
    events = []
    async for event in strategy_event_generator("test-session", "AAPL"):
        events.append(event)
        if len(events) >= 5: # Get the first 5 events
            break
            
    assert any(e["event"] == "status_update" for e in events)
    assert any(e["event"] == "reasoning_step" for e in events)
    
    # Check specific SOTA data structure
    reasoning_events = [e for e in events if e["event"] == "reasoning_step"]
    for e in reasoning_events:
        data = json.loads(e["data"])
        assert "agent" in data
        assert "status" in data
        assert "step" in data
        assert "timestamp" in data

def test_strategy_not_found():
    """Verify 404 for invalid strategy IDs."""
    response = client.get("/api/ai/strategy/invalid-id")
    assert response.status_code == 404

def test_strategy_challenge_invalid():
    """Verify 404 for challenging invalid strategy."""
    response = client.post("/api/ai/strategy/invalid-id/challenge?challenge=test")
    assert response.status_code == 404
