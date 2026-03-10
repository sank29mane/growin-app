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
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

@pytest.fixture(autouse=True)
def clear_cache():
    """Clear the global cache before every test."""
    try:
        from cache_manager import cache
        cache.clear()
    except ImportError:
        pass
    yield

# --- Async Helper ---
def make_async(obj):
    """Wrap a mock to be awaitable."""
    if not isinstance(obj, (AsyncMock, MagicMock)):
        return obj
    
    async def side_effect(*args, **kwargs):
        return obj.return_value
    
    obj.side_effect = side_effect
    return obj

# Mock heavy dependencies
MOCK_MODULES = [
    'alpaca.data.historical',
    'alpaca.trading.client',
    'trading212.client',
    'yfinance'
]

for module in MOCK_MODULES:
    try:
        if module not in sys.modules:
            mock = MagicMock()
            if 'client' in module:
                mock.get_account = AsyncMock(return_value=MagicMock())
                mock.get_orders = AsyncMock(return_value=[])
            
            make_async(mock)
            make_async(mock.return_value)
            sys.modules[module] = mock
    except Exception:
        pass
