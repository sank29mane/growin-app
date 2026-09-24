"""
MCP Routes - Server management and tool execution
"""

from fastapi import APIRouter, HTTPException, BackgroundTasks
from pydantic import BaseModel
from app_context import state, T212ConfigRequest
from utils.mcp_validation import validate_mcp_config
from shared_types import SENSITIVE_TOOLS
import logging
import os
from typing import Any, Dict, Optional

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
    Credentials are used only for the current local MCP session and are never
    persisted to the chat database.
    
    Args:
        request: T212ConfigRequest with account_type and optional API keys
        
    Returns:
        Success status with message
        
    Raises:
        HTTPException: If server not found or switch operation fails
    """
    try:
        # Forward only the selected account's credentials to the already-local
        # MCP session. Nothing is written to SQLite or process environment.
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
        raise HTTPException(
            status_code=503,
            detail="Trading 212 MCP is not connected; credentials were not stored",
        )
    except HTTPException:
        raise
    except Exception:
        logger.error("Error updating T212 config", exc_info=True)
        # Sentinel: Generic error message
        raise HTTPException(status_code=500, detail="Internal Server Error")


class ToolCallRequest(BaseModel):
    server_name: str
    tool_name: str
    arguments: Dict[str, Any]
    approval_token: Optional[str] = None

@router.post("/mcp/tool/call")
async def call_mcp_tool(request: ToolCallRequest):
    """
    Execute a read-only MCP tool.

    Sensitive broker mutations remain unavailable here even when callers send a
    legacy approval token. They must pass through the typed execution boundary.
    """
    if request.tool_name in SENSITIVE_TOOLS:
        logger.warning("Blocked sensitive tool on generic MCP route: %s", request.tool_name)
        raise HTTPException(
            status_code=503,
            detail="Sensitive broker tools are disabled on the generic MCP route",
        )

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
        result = await session.call_tool(request.tool_name, request.arguments)
        
        return {"status": "success", "result": result.content}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Tool execution failed: {e}")
        raise HTTPException(status_code=500, detail="Internal Server Error")


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
        
        # Add background task to connect (Skip in test to prevent hangs)
        if not os.environ.get("PYTEST_CURRENT_TEST"):
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
