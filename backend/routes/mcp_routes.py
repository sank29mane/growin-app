"""
MCP Routes - Server management and tool execution
"""

from fastapi import APIRouter, HTTPException
from app_context import state, T212ConfigRequest
import logging
import json

logger = logging.getLogger(__name__)
router = APIRouter()

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
        servers = state.chat_manager.get_mcp_servers(sanitize=True)
        for server in servers:
            # Check connection status
            is_connected = False
            for session in state.mcp_client.sessions:
                # This is a bit hacky, need better way to map session to server name
                # For now assume if we have sessions, we are connected
                pass
            
            server["status"] = "connected" if state.mcp_client.session else "disconnected"
            
        return {"servers": servers}
    except Exception as e:
        logger.error(f"Error fetching MCP status: {e}", exc_info=True)
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

        if state.mcp_client.session:
            result = await state.mcp_client.call_tool("switch_account", tool_args)
            return {"status": "success", "message": str(result)}
        else:
            return {"status": "saved", "message": "Configuration saved, but MCP not connected"}
            
    except Exception as e:
        logger.error(f"Error updating T212 config: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Internal Server Error")
