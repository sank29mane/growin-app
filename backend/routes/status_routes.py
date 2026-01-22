"""
Status Routes - System health and agent monitoring
"""

from fastapi import APIRouter
from status_manager import status_manager
import time

router = APIRouter()

@router.get("/api/system/status")
async def get_system_status():
    """Returns detailed status of all agents and system metrics."""
    return {
        "system": status_manager.get_system_info(),
        "agents": status_manager.get_all_statuses(),
        "timestamp": time.time()
    }
