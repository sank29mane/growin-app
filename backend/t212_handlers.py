"""
Trading 212 Handlers - SOTA 2026 MAS Integration
Provides optimized web handlers for Trading 212 data sync and portfolio visibility.
"""

from fastapi import APIRouter, HTTPException, Depends
from typing import List, Dict, Any
import logging
import asyncio
from market_context import PortfolioPosition
from data_models import T212AccountInfo
from utils.http_client import agent_http_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/t212", tags=["trading212"])

@router.get("/account", response_model=T212AccountInfo)
async def get_account_summary():
    """
    Fetch aggregated account summary from Trading 212 via pooled connection.
    """
    try:
        # Pooled request to Trading 212 internal/mock aggregator
        response = await agent_http_client.client.get("http://127.0.0.1:8001/t212/account", timeout=5.0)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Failed to fetch T212 account: {e}")
        raise HTTPException(status_code=502, detail="Trading 212 Gateway Timeout")

@router.get("/positions", response_model=List[PortfolioPosition])
async def get_positions():
    """
    Fetch all active positions from Trading 212.
    """
    try:
        response = await agent_http_client.client.get("http://127.0.0.1:8001/t212/positions", timeout=5.0)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Failed to fetch T212 positions: {e}")
        raise HTTPException(status_code=502, detail="Portfolio Sync Failure")

@router.post("/sync")
async def trigger_sync():
    """
    Manually trigger a full portfolio re-sync.
    """
    try:
        response = await agent_http_client.client.post("http://127.0.0.1:8001/t212/sync", timeout=10.0)
        response.raise_for_status()
        return {"status": "success", "message": "Portfolio sync initiated"}
    except Exception as e:
        logger.error(f"Manual sync failed: {e}")
        raise HTTPException(status_code=500, detail="Sync Command Rejected")
