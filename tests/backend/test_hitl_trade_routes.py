import pytest
from httpx import AsyncClient
import uuid
from decimal import Decimal
from datetime import datetime
from unittest.mock import AsyncMock, patch

# Import the FastAPI app
from server import app
from app_context import state

@pytest.mark.asyncio
async def test_approve_trade_success():
    """Test successful trade approval and execution."""
    proposal_id = str(uuid.uuid4())
    proposal = {
        "proposal_id": proposal_id,
        "ticker": "TQQQ",
        "action": "BUY",
        "quantity": 10.5,
        "reasoning": "NPU High-Velocity Signal",
        "status": "PENDING",
        "timestamp": datetime.now().timestamp()
    }
    
    # Manually inject into state
    state.trade_proposals[proposal_id] = proposal
    
    # Mock MCP client call_tool
    with patch.object(state.mcp_client, "call_tool", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = {"orderId": "T212-123", "status": "PLACED"}
        
        async with AsyncClient(app=app, base_url="http://test") as ac:
            response = await ac.post(
                "/api/ai/trade/approve",
                json={"proposal_id": proposal_id, "decision": "APPROVED"}
            )
            
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert "TQQQ" in data["message"]
        
        # Verify state updated
        assert state.trade_proposals[proposal_id]["status"] == "APPROVED"
        assert "executed_at" in state.trade_proposals[proposal_id]
        
        # Verify tool called with correct args
        mock_call.assert_called_once_with(
            "place_market_order",
            {
                "ticker": "TQQQ",
                "action": "BUY",
                "quantity": 10.5
            }
        )

@pytest.mark.asyncio
async def test_approve_trade_not_found():
    """Test approval of non-existent proposal."""
    async with AsyncClient(app=app, base_url="http://test") as ac:
        response = await ac.post(
            "/api/ai/trade/approve",
            json={"proposal_id": "non-existent", "decision": "APPROVED"}
        )
    
    assert response.status_code == 404
    assert "not found" in response.json()["detail"]

@pytest.mark.asyncio
async def test_approve_trade_already_processed():
    """Test approval of already approved proposal."""
    proposal_id = str(uuid.uuid4())
    state.trade_proposals[proposal_id] = {
        "proposal_id": proposal_id,
        "ticker": "SQQQ",
        "action": "SELL",
        "quantity": 5.0,
        "status": "APPROVED"
    }
    
    async with AsyncClient(app=app, base_url="http://test") as ac:
        response = await ac.post(
            "/api/ai/trade/approve",
            json={"proposal_id": proposal_id, "decision": "APPROVED"}
        )
    
    assert response.status_code == 400
    assert "already APPROVED" in response.json()["detail"]

@pytest.mark.asyncio
async def test_reject_trade_success():
    """Test successful trade rejection."""
    proposal_id = str(uuid.uuid4())
    state.trade_proposals[proposal_id] = {
        "proposal_id": proposal_id,
        "ticker": "TQQQ",
        "action": "BUY",
        "quantity": 20.0,
        "status": "PENDING"
    }
    
    async with AsyncClient(app=app, base_url="http://test") as ac:
        response = await ac.post(
            "/api/ai/trade/reject",
            json={
                "proposal_id": proposal_id, 
                "decision": "REJECTED",
                "notes": "Too risky right now"
            }
        )
        
    assert response.status_code == 200
    assert response.json()["status"] == "rejected"
    
    # Verify state
    assert state.trade_proposals[proposal_id]["status"] == "REJECTED"
    assert state.trade_proposals[proposal_id]["rejection_notes"] == "Too risky right now"
    assert "rejected_at" in state.trade_proposals[proposal_id]
