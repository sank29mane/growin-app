import threading
import time
from typing import Dict, Optional
from datetime import datetime

class StatusManager:
    """
    Thread-safe registry for real-time status updates of agents and system components.
    Used by the Intelligent Console to show what the AI is doing "right now".
    """
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super(StatusManager, cls).__new__(cls)
                cls._instance.statuses = {
                    "coordinator": {"status": "ready", "detail": "Idle", "model": "Granite 4.0 Tiny", "timestamp": datetime.now().isoformat()},
                    "decision_agent": {"status": "ready", "detail": "Idle", "model": "LFM 2.5B (Native)", "timestamp": datetime.now().isoformat()},
                    "quant_agent": {"status": "ready", "detail": "Monitoring markets", "model": "TA-Lib", "timestamp": datetime.now().isoformat()},
                    "portfolio_agent": {"status": "ready", "detail": "Synced", "model": "Trading212 API", "timestamp": datetime.now().isoformat()},
                    "forecasting_agent": {"status": "ready", "detail": "Models loaded", "model": "TTM-R2", "timestamp": datetime.now().isoformat()},
                    "research_agent": {"status": "ready", "detail": "Idle", "model": "NewsAPI", "timestamp": datetime.now().isoformat()},
                    "social_agent": {"status": "ready", "detail": "Monitoring feeds", "model": "Tavily + VADER", "timestamp": datetime.now().isoformat()},
                    "whale_agent": {"status": "ready", "detail": "Monitoring trades", "model": "Alpaca Trades", "timestamp": datetime.now().isoformat()},
                    "lmstudio": {"status": "ready", "detail": "Idle", "model": "LM Studio v1", "timestamp": datetime.now().isoformat()},
                }
            return cls._instance

    def set_status(self, agent: str, status: str, detail: Optional[str] = None, model: Optional[str] = None):
        """Update the status of an agent or create it if it doesn't exist."""
        with self._lock:
            existing = self.statuses.get(agent, {})
            update_data = {
                "status": status,
                "detail": detail or existing.get("detail", "Idle"),
                "timestamp": datetime.now().isoformat()
            }
            if model:
                update_data["model"] = model
            elif "model" not in existing:
                update_data["model"] = "Unknown"
            
            if agent in self.statuses:
                self.statuses[agent].update(update_data)
            else:
                self.statuses[agent] = update_data

    def get_all_statuses(self) -> Dict:
        """Get all agent statuses."""
        with self._lock:
            return self.statuses.copy()

    def get_system_info(self) -> Dict:
        """Get system-level metrics."""
        try:
            import psutil
            import os
            from app_context import state
            
            process = psutil.Process(os.getpid())
            
            # Check MCP Status
            mcp_connected = False
            mcp_servers = 0
            if hasattr(state, "mcp_client") and state.mcp_client:
                mcp_connected = True
                # Simple check if servers are available
                # Use getattr to be safe if sessions doesn't exist yet
                mcp_servers = len(getattr(state.mcp_client, "sessions", [])) 

            return {
                "uptime": time.time() - process.create_time(),
                "memory_mb": process.memory_info().rss / 1024 / 1024,
                "active_threads": threading.active_count(),
                "mcp": {
                    "connected": mcp_connected,
                    "servers_count": mcp_servers
                },
                "status": "Healthy"
            }
        except Exception as e:
            print(f"Error collecting system status: {e}")
            return {
                "uptime": 0,
                "memory_mb": 0,
                "active_threads": 0,
                "mcp": {"connected": False, "servers_count": 0},
                "status": "Error"
            }

# Global singleton
status_manager = StatusManager()
