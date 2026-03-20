"""
Status Routes - System health and agent monitoring
"""

from fastapi import APIRouter, HTTPException
from status_manager import status_manager
from agents.messenger import get_messenger
import time

router = APIRouter()

@router.get("/api/system/status")
async def get_system_status():
    """Returns detailed status of all agents, environment settings, and system metrics."""
    import os
    return {
        "system": status_manager.get_system_info(),
        "agents": status_manager.get_all_statuses(),
        "environment": {
            "trading212": "demo" if os.getenv("TRADING212_USE_DEMO", "true").lower() == "true" else "live",
            "alpaca": "paper" if os.getenv("ALPACA_USE_PAPER", "true").lower() == "true" else "live",
        },
        "timestamp": time.time()
    }

@router.get("/api/telemetry/trace/{request_id}")
async def get_request_trace(request_id: str):
    """
    Returns the full reasoning trace for a specific request ID.
    Used for the Reasoning Trace UI.
    """
    messenger = get_messenger()
    history = messenger.get_history(request_id)
    
    if not history:
        raise HTTPException(status_code=404, detail="Trace not found")
        
    # Process history into a structured trace
    trace = []
    for msg in history:
        trace.append({
            "sender": msg.sender,
            "subject": msg.subject,
            "payload": msg.payload,
            "timestamp": msg.timestamp.isoformat()
        })
        
    return {
        "request_id": request_id,
        "trace": trace,
        "count": len(trace)
    }
