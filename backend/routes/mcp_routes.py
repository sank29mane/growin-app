"""
MCP Routes - Server management and tool execution
"""

from fastapi import APIRouter, HTTPException, BackgroundTasks
from app_context import state, T212ConfigRequest
import logging
import json

logger = logging.getLogger(__name__)
router = APIRouter()

# Security: Block potentially dangerous shell commands
BLOCKED_COMMANDS = {
    "bash", "sh", "zsh", "dash", "ksh", "csh", "tcsh",
    "powershell", "pwsh", "cmd", "cmd.exe",
    "nc", "netcat", "ncat"
}

def validate_mcp_config(command: str):
    """
    Validates MCP server configuration to prevent command injection.
    Ensures that known shell interpreters and dangerous tools are not used as commands.
    """
    if not command:
        return

    # Normalize command to handle paths (e.g. /bin/bash -> bash)
    cmd_name = command.replace("\\", "/").split("/")[-1].lower()

    if cmd_name in BLOCKED_COMMANDS:
        logger.warning(f"Blocked attempt to add forbidden MCP command: {cmd_name}")
        raise HTTPException(
            status_code=400,
            detail=f"Command '{cmd_name}' is not allowed for security reasons."
        )

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


@router.get("/mcp/servers")
async def get_mcp_servers_list():
    """Get MCP servers list with status"""
    return await get_mcp_status()


@router.post("/mcp/servers/add")
async def add_mcp_server(server_data: dict, background_tasks: BackgroundTasks):
    """Add new MCP server"""
    try:
        # Sentinel: Validate command to prevent shell injection
        validate_mcp_config(server_data.get("command"))

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
    except HTTPException:
        raise
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
