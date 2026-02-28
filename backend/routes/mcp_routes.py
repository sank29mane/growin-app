"""
MCP Routes - Server management and tool execution
"""

from fastapi import APIRouter, HTTPException, BackgroundTasks
from app_context import state, T212ConfigRequest
from utils.mcp_validation import validate_mcp_config
import logging
import json
import os

logger = logging.getLogger(__name__)
router = APIRouter()

# Security: Block potentially dangerous shell commands
# BLOCKED_COMMANDS and validate_mcp_config are imported from utils.mcp_validation


@router.get("/mcp/status")
async def get_mcp_status():
    """
    Get connection status of all configured MCP servers.
    
    Returns status for Trading212, HuggingFace, and any other configured
    Model Context Protocol servers.
    
    Returns:
        Dict with list of servers and their connection statuses
    """
    try:
        sanitized_servers = state.chat_manager.get_mcp_servers(sanitize=True)

        for server in sanitized_servers:
            # Check connection status
            # MultiMCPManager stores sessions in a dict keyed by server name
            is_connected = server["name"] in state.mcp_client.sessions
            server["status"] = "connected" if is_connected else "disconnected"

        return {"servers": sanitized_servers}
    except Exception as e:
        logger.error(f"Error fetching MCP status: {e}", exc_info=True)
        # Sentinel: Generic error message
        raise HTTPException(status_code=500, detail="Internal Server Error")

@router.post("/mcp/trading212/config")
async def update_t212_config(request: T212ConfigRequest):
    """
    Update Trading212 API configuration and switch account type.
    
    Allows switching between Invest and ISA accounts and updating API keys.
    Configuration is persisted to database and account switch is executed via MCP.
    
    Args:
        request: T212ConfigRequest with account_type and optional API keys
        
    Returns:
        Success status with message
        
    Raises:
        HTTPException: If server not found or switch operation fails
    """
    try:
        # Update server config in DB
        cursor = state.chat_manager.conn.cursor()
        
        # Determine strictness of command
        # Ideally we fetch existing args/env and update them
        cursor.execute("SELECT env FROM mcp_servers WHERE name = 'Trading 212'")
        row = cursor.fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Trading 212 server not found")
            
        current_env = json.loads(row[0]) if row[0] else {}
        new_env = current_env.copy()
        
        # Update keys if provided
        if request.invest_key:
            new_env["TRADING212_API_KEY"] = request.invest_key
        if request.invest_secret:
            new_env["TRADING212_API_SECRET"] = request.invest_secret
        if request.isa_key:
            new_env["TRADING212_API_KEY_ISA"] = request.isa_key or ""
        if request.isa_secret:
            new_env["TRADING212_API_SECRET_ISA"] = request.isa_secret or ""
        new_env["TRADING212_USE_DEMO"] = "false"

        cursor.execute(
            """
            UPDATE mcp_servers
            SET env = ?
            WHERE name = 'Trading 212'
            """,
            (json.dumps(new_env),),
        )
        state.chat_manager.conn.commit()

        # Try switching account via the MCP tool
        tool_args = {"account_type": request.account_type}
        if request.account_type == "invest":
            tool_args["key"] = request.invest_key
            tool_args["secret"] = request.invest_secret
        else:
            tool_args["key"] = request.isa_key
            tool_args["secret"] = request.isa_secret


        if "Trading 212" in state.mcp_client.sessions:
            result = await state.mcp_client.call_tool("switch_account", tool_args)
            return {"status": "success", "message": str(result)}
        else:
            return {"status": "saved", "message": "Configuration saved, but MCP not connected"}
            
    except Exception as e:
        logger.error(f"Error updating T212 config: {e}", exc_info=True)
        # Sentinel: Generic error message
        raise HTTPException(status_code=500, detail="Internal Server Error")


from typing import Dict, Any, Optional
from pydantic import BaseModel
import uuid
import hmac
import hashlib
import time

# Secret for signing approval tokens (in production, use a secure env var)
APPROVAL_SECRET = os.getenv("TRADE_APPROVAL_SECRET", "sota-2026-secure-gate-9911")

class ToolCallRequest(BaseModel):
    server_name: str
    tool_name: str
    arguments: Dict[str, Any]
    approval_token: Optional[str] = None

def verify_approval_token(token: str, tool_name: str, arguments: Dict[str, Any]) -> bool:
    """Verify that the token matches the tool and arguments (Simplified HMAC)."""
    try:
        # Expected format: timestamp:signature
        parts = token.split(":")
        if len(parts) != 2:
            return False
            
        timestamp, signature = parts
        # Check expiry (5 minutes)
        if time.time() - float(timestamp) > 300:
            logger.warning("Approval token expired")
            return False
            
        # Recreate signature
        msg = f"{timestamp}:{tool_name}:{json.dumps(arguments, sort_keys=True)}"
        expected = hmac.new(
            APPROVAL_SECRET.encode(),
            msg.encode(),
            hashlib.sha256
        ).hexdigest()
        
        return hmac.compare_digest(expected, signature)
    except Exception as e:
        logger.error(f"Token verification error: {e}")
        return False

@router.post("/mcp/tool/call")
async def call_mcp_tool(request: ToolCallRequest):
    """
    Execute an MCP tool with SOTA 2026 Governance Gates.
    Intersects sensitive actions (trades) to enforce HITL.
    """
    sensitive_tools = [
        "place_market_order", "place_limit_order", 
        "place_stop_order", "place_stop_limit_order"
    ]
    
    # 1. Check Governance Gate
    if request.tool_name in sensitive_tools:
        if not request.approval_token:
            logger.warning(f"BLOCKED: {request.tool_name} requires approval_token (HITL Gate)")
            raise HTTPException(
                status_code=403, 
                detail={
                    "error": "APPROVAL_REQUIRED",
                    "message": f"Human-in-the-Loop approval required for {request.tool_name}",
                    "tool": request.tool_name,
                    "arguments": request.arguments
                }
            )
        
        if not verify_approval_token(request.approval_token, request.tool_name, request.arguments):
            logger.error(f"BLOCKED: Invalid or expired approval_token for {request.tool_name}")
            raise HTTPException(status_code=401, detail="Invalid trade approval token")

    # 2. Execute Tool
    try:
        if request.server_name not in state.mcp_client.sessions:
            # Try to connect if not connected
            servers = state.chat_manager.get_mcp_servers()
            server_config = next((s for s in servers if s['name'] == request.server_name), None)
            if server_config:
                await state.mcp_client.connect_server(server_config)
            else:
                raise HTTPException(status_code=404, detail=f"Server {request.server_name} not found")

        # Explicitly route to the correct session
        session = state.mcp_client.sessions.get(request.server_name)
        if not session:
             raise HTTPException(status_code=503, detail=f"Server {request.server_name} not available")
             
        # call_tool on MultiMCPManager handles routing if tool name is unique, 
        # but here we use the specific session for safety
        from mcp.types import TextContent
        result = await session.call_tool(request.tool_name, request.arguments)
        
        return {"status": "success", "result": result.content}
    except Exception as e:
        logger.error(f"Tool execution failed: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/mcp/servers/add")
async def add_mcp_server(server_data: dict, background_tasks: BackgroundTasks):
    """Add new MCP server"""
    try:
        # Sentinel: Validate command to prevent injection
        if server_data.get("type") == "stdio":
            validate_mcp_config(server_data.get("command"), server_data.get("args", []))

        state.chat_manager.add_mcp_server(
            name=server_data.get("name"),
            type=server_data.get("type"),
            command=server_data.get("command"),
            args=server_data.get("args", []),
            env=server_data.get("env", {}),
            url=server_data.get("url")
        )
        
        # Add background task to connect
        background_tasks.add_task(state.mcp_client.connect_server, {
            "name": server_data.get("name"),
            "type": server_data.get("type"),
            "command": server_data.get("command"),
            "args": server_data.get("args", []),
            "env": server_data.get("env", {}),
            "url": server_data.get("url")
        })
        
        return {"status": "success"}
    except ValueError as e:
        # Client error for validation failure
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to add MCP server: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal Server Error")


@router.delete("/mcp/servers/{server_name}")
async def delete_mcp_server(server_name: str):
    """Delete MCP server"""
    try:
        # Check if server exists first
        servers = state.chat_manager.get_mcp_servers()
        if not any(s['name'] == server_name for s in servers):
            raise HTTPException(status_code=404, detail="Server not found")
            
        state.chat_manager.delete_mcp_server(server_name)
        return {"status": "success", "message": f"Server {server_name} deleted"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Failed to delete MCP server {server_name}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal Server Error")
