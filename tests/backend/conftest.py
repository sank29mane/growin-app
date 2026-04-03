import sys
import os
import importlib.util
from unittest.mock import MagicMock, AsyncMock, patch
import pytest

# --- Python 3.13 Fixes ---
orig_find_spec = importlib.util.find_spec
def patched_find_spec(name, package=None):
    try:
        return orig_find_spec(name, package)
    except ValueError:
        return None
importlib.util.find_spec = patched_find_spec

# Ensure backend is in path
backend_path = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "backend")
if backend_path not in sys.path:
    sys.path.insert(0, backend_path)

# --- Global Resource Lifecycle Management ---
@pytest.fixture(scope="session", autouse=True)
async def cleanup_resources():
    """Ensure all background processes are killed after the test session."""
    yield
    
    print("\n🧹 Cleaning up test resources...")
    
    # 1. Stop Worker Service (MLX/TTM)
    try:
        from utils.worker_client import get_worker_client
        client = get_worker_client()
        await client.stop()
        print("✅ Worker Service stopped")
    except Exception as e:
        print(f"⚠️ Failed to stop Worker Service: {e}")

    # 2. Stop MCP Clients
    try:
        from app_context import state
        # Access internal _mcp_client to avoid re-triggering lazy init if it wasn't used
        if hasattr(state, '_mcp_client') and state._mcp_client is not None:
            await state._mcp_client._exit_stack.aclose()
            print("✅ MCP Sessions closed")
    except Exception as e:
        print(f"⚠️ Failed to close MCP sessions: {e}")

@pytest.fixture(autouse=True)
def clear_cache():
    """Clear the global cache before every test."""
    try:
        from cache_manager import cache
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
            
            make_async(mock)
            make_async(mock.return_value)
            sys.modules[module] = mock
    except Exception:
        pass
