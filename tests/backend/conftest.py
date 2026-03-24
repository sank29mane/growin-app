import sys
import os
import importlib.util
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
import pytest

# Ensure project root is in path for absolute imports (from backend.xxx)
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if project_root not in sys.path:
    sys.path.append(project_root)

# --- Asyncio Scope Fix ---
@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for each test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()

# --- Python 3.13 Fixes ---
orig_find_spec = importlib.util.find_spec
def patched_find_spec(name, package=None):
    try:
        return orig_find_spec(name, package)
    except ValueError:
        return None
importlib.util.find_spec = patched_find_spec

# --- Helpers ---
def make_async(mock):
    """Helper to add awaitable capability to mocked objects, simulating AsyncMock."""
    if hasattr(mock, '__call__') and not isinstance(mock, AsyncMock):
        mock.side_effect = AsyncMock()
    return mock

# --- Global Resource Lifecycle Management ---
@pytest.fixture(scope="session", autouse=True)
async def cleanup_resources():
    """Ensure all background processes are killed after the test session."""
    yield
    
    print("\n🧹 Cleaning up test resources...")
    
    # 1. Stop Worker Service (MLX/TTM)
    try:
        from backend.utils.worker_client import get_worker_client
        client = get_worker_client()
        await client.stop()
        print("✅ Worker Service stopped")
    except Exception as e:
        print(f"⚠️ Failed to stop Worker Service: {e}")

    # 2. Stop MCP Clients
    try:
        from backend.app_context import state
        # Access internal _mcp_client to avoid re-triggering lazy init if it wasn't used
        if hasattr(state, '_mcp_client') and state._mcp_client is not None:
            await state._mcp_client._exit_stack.aclose()
            print("✅ MCP Sessions closed")
        
        # DO NOT close state.chat_manager here if it's shared across tests, 
        # as it closes the underlying sqlite connection.
    except Exception as e:
        print(f"⚠️ Failed to close MCP sessions: {e}")

@pytest.fixture(autouse=True)
def clear_cache():
    """Clear the global cache before every test."""
    try:
        from backend.cache_manager import cache
        cache.clear()
    except ImportError:
        pass
    yield

# --- Mock heavy dependencies ---
MOCK_MODULES = [
    'alpaca.data.historical',
    'alpaca.trading.client',
    'trading212.client',
    'yfinance',
    'docker'  # CRITICAL: Prevent Docker daemon connection attempts in CI
]

# Patch MultiMCPManager to prevent real server connections during tests
@pytest.fixture(autouse=True)
def mock_mcp_connection():
    with patch("backend.mcp_client.MultiMCPManager.connect_all") as mock_connect:
        # Create an async context manager mock
        mock_connect.return_value.__aenter__.return_value = MagicMock()
        yield mock_connect

for module in MOCK_MODULES:
    try:
        if module not in sys.modules:
            mock = MagicMock()
            if 'client' in module:
                mock.get_account = AsyncMock(return_value=MagicMock())
                mock.get_orders = AsyncMock(return_value=[])
            
            if module == 'docker':
                # Mock docker.from_env() and basic methods
                mock.from_env.return_value = MagicMock()
                mock.from_env.return_value.ping.return_value = True
            
            sys.modules[module] = mock
    except Exception:
        pass
