from fastapi import APIRouter, WebSocket, WebSocketDisconnect
import asyncio
import time
import logging
from backend.app_context import state

router = APIRouter()
logger = logging.getLogger("alpha_stream")

# Maintain a list of active websocket connections
active_connections: list[WebSocket] = []

@router.websocket("/stream")
async def alpha_stream(websocket: WebSocket):
    """
    WebSocket endpoint representing the Control & Data Backbone for Alpha streaming.
    Streams Portfolio vs FTSE 100 benchmark metrics at approx 2Hz.
    """
    await websocket.accept()
    active_connections.append(websocket)
    logger.info("Alpha stream client connected.")
    
    try:
        while True:
            # Mock / Placeholders for real state engine integration.
            # In the full implementation, this pulls from QuantEngine or RLState.
            portfolio_val = 10500.25 # Mocked
            benchmark_val = 8200.50  # Mocked FTSE 100
            
            # Simple alpha calculation example
            portfolio_return = (portfolio_val - 10000.0) / 10000.0
            benchmark_return = (benchmark_val - 8000.0) / 8000.0
            alpha = portfolio_return - benchmark_return
            
            payload = {
                "portfolio_val": portfolio_val,
                "benchmark_val": benchmark_val,
                "alpha": alpha,
                "regime": "bull",
                "conviction": 0.98,
                "timestamp": time.time()
            }
            
            await websocket.send_json(payload)
            await asyncio.sleep(0.5)  # Yield every 500ms (2Hz)
            
    except WebSocketDisconnect:
        logger.info("Alpha stream client disconnected (normal).")
    except Exception as e:
        logger.error(f"Alpha stream error: {e}")
    finally:
        if websocket in active_connections:
            active_connections.remove(websocket)

@router.websocket("/training-stream")
async def training_stream(websocket: WebSocket):
    """
    WebSocket endpoint for real-time RL training metrics.
    Broadcasts loss, entropy, reward, and stability_score.
    """
    await websocket.accept()
    logger.info("Training stream client connected.")
    
    try:
        while True:
            # Wait for metrics to arrive in the queue
            try:
                # Use a short timeout to allow checking for disconnects even if no training is happening
                metrics = await asyncio.wait_for(state.training_metrics_queue.get(), timeout=1.0)
                await websocket.send_json(metrics)
                state.training_metrics_queue.task_done()
            except asyncio.TimeoutError:
                # Connection heartbeat/check
                # Check if client is still connected by sending a ping or just waiting
                continue
                
    except WebSocketDisconnect:
        logger.info("Training stream client disconnected.")
    except Exception as e:
        logger.error(f"Training stream error: {e}")
