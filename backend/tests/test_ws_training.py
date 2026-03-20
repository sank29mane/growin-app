import pytest
from fastapi.testclient import TestClient
from backend.server import app
from backend.app_context import state
import time
import asyncio

def test_training_stream_ws():
    """Test that training-stream WebSocket receives pushed metrics."""
    # Ensure the queue is clear
    while not state.training_metrics_queue.empty():
        state.training_metrics_queue.get_nowait()
        
    client = TestClient(app)
    
    # Mock metrics to push
    mock_metrics = {
        "loss": 0.5,
        "entropy": 0.1,
        "mean_reward": 1.2,
        "stability_score": 0.95,
        "timestamp": time.time(),
        "epoch": 10
    }
    
    # Push metrics to state queue
    state.training_metrics_queue.put_nowait(mock_metrics)
    
    with client.websocket_connect("/api/alpha/training-stream") as websocket:
        data = websocket.receive_json()
        assert data["loss"] == 0.5
        assert data["stability_score"] == 0.95
        assert "entropy" in data
        assert "mean_reward" in data
        assert "timestamp" in data
        assert "epoch" in data

def test_training_stream_disconnect():
    """Test that the training stream handles disconnects gracefully."""
    client = TestClient(app)
    
    with client.websocket_connect("/api/alpha/training-stream") as websocket:
        websocket.close()
    
    # If we reached here without server crash, it's graceful.
    assert True

@pytest.mark.asyncio
async def test_ppo_agent_push_to_queue():
    """Test that PPOAgent actually pushes to the queue provided in constructor."""
    import mlx.core as mx
    from backend.agents.ppo_agent import PPOAgent
    
    queue = asyncio.Queue()
    agent = PPOAgent(n_assets=5, state_dim=16, metrics_queue=queue)
    
    # Fill buffer
    for _ in range(10):
        state_arr = mx.random.normal((1, 16))
        action, log_prob, value = agent.select_action(state_arr)
        agent.buffer.add(state_arr, action, log_prob, 0.1, value.item(), 1.0)
    
    next_state = mx.random.normal((1, 16))
    _, _, next_value = agent.select_action(next_state)
    
    # Train
    metrics = agent.train_on_batch(mx.squeeze(next_value), batch_size=5, epochs=1)
    
    # Check queue
    assert not queue.empty()
    queued_metrics = queue.get_nowait()
    assert queued_metrics["loss"] == metrics["loss"]
    assert "stability_score" in queued_metrics
