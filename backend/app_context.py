"""
Shared application state and models to avoid circular imports.
"""

from typing import Any, Dict, List, Optional
from pydantic import BaseModel
from chat_manager import ChatManager
from rag_manager import RAGManager
from mcp_client import Trading212MCPClient

import time





class ANEConfig(BaseModel):
    enabled: bool = False
    compute_units: str = "ALL"  # CPU_ONLY | CPU_GPU | ALL

class AppState:
    """Manages global application state with lazy initialization for heavy components"""
    def __init__(self):
        self._chat_manager = None
        self._rag_manager = None
        self._mcp_client = None
        self.lm_studio_client = None  # Lazy init to avoid startup blocking
        self.start_time = time.time()
        # On-device ANE configuration (default off; auto-detect on startup)
        self.ane_config = ANEConfig()
        # Phase 30: High-Velocity Trade Proposals (HITL)
        self.trade_proposals: Dict[str, Any] = {}

    @property
    def chat_manager(self) -> ChatManager:
        if self._chat_manager is None:
            self._chat_manager = ChatManager()
        return self._chat_manager

    @chat_manager.setter
    def chat_manager(self, value):
        self._chat_manager = value

    @property
    def rag_manager(self) -> RAGManager:
        if self._rag_manager is None:
            self._rag_manager = RAGManager()
        return self._rag_manager

    @property
    def mcp_client(self) -> Trading212MCPClient:
        if self._mcp_client is None:
            self._mcp_client = Trading212MCPClient()
        return self._mcp_client

    @mcp_client.setter
    def mcp_client(self, value):
        self._mcp_client = value

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
        acc_type = account_type.lower() if account_type else "invest"
        if acc_type not in ["invest", "isa"]:
            # 'all' is valid for querying but not for setting active viewing state in some contexts,
            # but usually we want to switch between specific accounts. 
            # If the UI allows "All", we should permit it, but typically T212 is one or the other.
            # Allowing "all" for flexibility if needed, but primary use is Invest/ISA.
            if acc_type != "all":
                raise ValueError(f"Invalid account type: {account_type}. Must be 'invest' or 'isa'.")
        self._active_account = acc_type
        
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
