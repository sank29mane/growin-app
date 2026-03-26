import os
import asyncio
import logging
from contextlib import asynccontextmanager, AsyncExitStack
from typing import Optional, Dict, List, Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.sse import sse_client

from resilience import get_circuit_breaker, CircuitBreakerOpenError

logger = logging.getLogger(__name__)


class MultiMCPManager:
    """
    Manages multiple MCP server connections dynamically.
    
    Features:
    - Auto-reconnect tracking for failed servers
    - Circuit breaker to prevent cascade failures
    - Timeout handling for tool calls
    - Graceful degradation when servers are unavailable
    """
    
    def __init__(self):
        self.sessions: Dict[str, ClientSession] = {}
        self._exit_stack = AsyncExitStack()
        self.primary_session_name = "Trading 212"
        self._server_configs: Dict[str, Dict] = {}  # Store configs for reconnection
        self._failed_servers: Dict[str, float] = {}  # Track failed servers and last attempt time
        self._reconnect_delay = 30.0  # Seconds before retry for failed servers

    @property
    def session(self) -> Optional[ClientSession]:
        """Compatibility property for single-session code"""
        return self.sessions.get(self.primary_session_name)

    @asynccontextmanager
    async def connect_all(self, server_configs: List[Dict]):
        """Connect to all configured servers with graceful handling"""
        try:
            for config in server_configs:
                self._server_configs[config["name"]] = config
                await self.connect_server(config)
            yield self
        finally:
            await self._exit_stack.aclose()
            self.sessions.clear()

    async def connect_server(self, config: Dict) -> bool:
        """
        Connect to a single MCP server.
        
        Returns:
            True if connection succeeded, False otherwise
        """
        name = config["name"]
        server_type = config["type"]
        
        # Check if this server recently failed
        if name in self._failed_servers:
            import time
            if time.time() - self._failed_servers[name] < self._reconnect_delay:
                logger.debug(f"Skipping {name} - recently failed, waiting for reconnect delay")
                return False
        
        try:
            if server_type == "stdio":
                # Handle relative paths for default server
                command = config["command"]
                args = list(config["args"] or [])  # Make a copy
                
                # Fix for default T212 server relative path
                if name == "Trading 212" and args and "trading212_mcp_server.py" in args[0]:
                    current_dir = os.path.dirname(os.path.abspath(__file__))
                    args[0] = os.path.join(current_dir, "trading212_mcp_server.py")
                
                # Filter out empty environment variables to allow .env fallbacks
                custom_env = {k: v for k, v in (config.get("env") or {}).items() if v and str(v).strip()}
                
                server_params = StdioServerParameters(
                    command=command,
                    args=args,
                    env={**os.environ.copy(), **custom_env}
                )
                
                read, write = await self._exit_stack.enter_async_context(stdio_client(server_params))
                session = await self._exit_stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self.sessions[name] = session
                
                # Clear from failed list on success
                self._failed_servers.pop(name, None)
                logger.info(f"Connected to MCP Server (stdio): {name}")
                return True
                
            elif server_type == "sse":
                url = config["url"]
                read, write = await self._exit_stack.enter_async_context(sse_client(url))
                session = await self._exit_stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self.sessions[name] = session
                
                # Clear from failed list on success
                self._failed_servers.pop(name, None)
                logger.info(f"Connected to MCP Server (SSE): {name}")
                return True
                
        except Exception as e:
            import time
            self._failed_servers[name] = time.time()
            logger.error(f"Failed to connect to MCP server {name}: {e}")
            return False
            
    async def disconnect_server(self, name: str):
        """Disconnect and remove a single MCP server"""
        if name in self.sessions:
            del self.sessions[name]
            logger.info(f"Disconnected and removed session for {name}")

    async def list_tools(self) -> List[Any]:
        """Aggregate tools from all active sessions"""
        all_tools = []
        for name, session in self.sessions.items():
            try:
                tools_result = await asyncio.wait_for(
                    session.list_tools(),
                    timeout=10.0
                )
                all_tools.extend(tools_result.tools)
            except asyncio.TimeoutError:
                logger.warning(f"Timeout listing tools for {name}")
            except Exception as e:
                logger.error(f"Error listing tools for {name}: {e}")
        return all_tools

    async def call_tool(self, name: str, arguments: dict, timeout: float = 30.0) -> Any:
        """
        Find and call a tool on the appropriate server.
        
        Features:
        - Timeout handling (default 30s)
        - Circuit breaker per server
        - Fallback to next available server
        
        Args:
            name: Tool name to call
            arguments: Tool arguments
            timeout: Maximum time to wait for response
            
        Returns:
            Tool result
            
        Raises:
            RuntimeError: If tool not found on any server
        """
        errors = []
        
        for server_name, session in self.sessions.items():
            cb = get_circuit_breaker(f"mcp_{server_name}", failure_threshold=3, recovery_timeout=30.0)
            
            # Check circuit breaker state
            if not cb.allow_request():
                logger.debug(f"Circuit breaker OPEN for {server_name}, skipping")
                continue
            
            try:
                result = await asyncio.wait_for(
                    session.call_tool(name, arguments),
                    timeout=timeout
                )
                cb.record_success()
                return result
                
            except asyncio.TimeoutError:
                error = f"Timeout calling {name} on {server_name}"
                logger.warning(error)
                cb.record_failure(TimeoutError(error))
                errors.append(error)
                
            except CircuitBreakerOpenError:
                errors.append(f"Circuit breaker open for {server_name}")
                
            except Exception as e:
                error = f"Error calling {name} on {server_name}: {e}"
                logger.warning(error)
                cb.record_failure(e)
                errors.append(error)
        
        # All servers failed
        error_summary = "; ".join(errors) if errors else "No active MCP servers"
        raise RuntimeError(f"Tool {name} failed: {error_summary}")

    def get_status(self) -> Dict[str, Any]:
        """Get status of all MCP connections"""
        return {
            "connected_servers": list(self.sessions.keys()),
            "failed_servers": list(self._failed_servers.keys()),
            "total_sessions": len(self.sessions),
        }


# Compatibility alias
Trading212MCPClient = MultiMCPManager
