"""
Shared application state and models to avoid circular imports.
"""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel
import asyncio
from chat_manager import ChatManager
from rag_manager import RAGManager
from mcp_client import Trading212MCPClient

import time





class ANEConfig(BaseModel):
    enabled: bool = False
    compute_units: str = "ALL"  # CPU_ONLY | CPU_GPU | ALL

class AppState:
    """Manages global application state"""
    def __init__(self):
        self.chat_manager = ChatManager()
        self.rag_manager = RAGManager()
        self.mcp_client = Trading212MCPClient()
        self.start_time = time.time()
        # On-device ANE configuration (default off; auto-detect on startup)
        self.ane_config = ANEConfig()

class AccountContext:
    """
    Manages the active Trading212 account context (Invest vs ISA).
    This allows the backend to be stateful regarding which account is being viewed/acted upon.
    """
    def __init__(self):
        self._active_account: str = "invest" # Default to invest
        
    def get_active_account(self) -> str:
        return self._active_account

    def set_active_account(self, account_type: str):
        if account_type.lower() not in ["invest", "isa"]:
            # 'all' is valid for querying but not for setting active viewing state in some contexts,
            # but usually we want to switch between specific accounts. 
            # If the UI allows "All", we should permit it, but typically T212 is one or the other.
            # Allowing "all" for flexibility if needed, but primary use is Invest/ISA.
            if account_type.lower() != "all":
                raise ValueError(f"Invalid account type: {account_type}. Must be 'invest' or 'isa'.")
        self._active_account = account_type.lower()
        
    def get_account_or_default(self, requested_account: Optional[str]) -> str:
        """
        Returns the requested account if provided, otherwise returns the active account.
        Used by API endpoints to determine which account to target.
        """
        if requested_account:
            return requested_account.lower()
        return self._active_account

# Global state instances
state = AppState()
account_context = AccountContext()

# Request Models
class ChatMessage(BaseModel):
    message: str
    conversation_id: Optional[str] = None
    model_name: Optional[str] = "native-mlx"
    coordinator_model: Optional[str] = "granite-tiny"
    api_keys: Optional[Dict[str, str]] = None
    account_type: Optional[str] = None  # None = ask user interactively

class AnalyzeRequest(BaseModel):
    query: str
    model_name: str = "native-mlx"
    coordinator_model: Optional[str] = "granite-tiny"
    api_keys: Optional[Dict[str, str]] = None
    account_type: Optional[str] = None  # None = ask user interactively

class AgentResponse(BaseModel):
    messages: List[Dict[str, Any]]
    final_answer: str

class T212ConfigRequest(BaseModel):
    account_type: str
    invest_key: Optional[str] = None
    invest_secret: Optional[str] = None
    isa_key: Optional[str] = None
    isa_secret: Optional[str] = None
