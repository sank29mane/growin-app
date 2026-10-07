import asyncio
import importlib.util
import os
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio

# --- Python 3.13 Fixes ---
orig_find_spec = importlib.util.util.find_spec if hasattr(importlib.util, 'util') else importlib.util.find_spec
def patched_find_spec(name, package=None):
    try:
        return orig_find_spec(name, package)
    except ValueError:
        return None
importlib.util.find_spec = patched_find_spec

# Ensure both supported import styles resolve. The repository contains legacy
# flat imports (``from execution import ...``) and package imports
# (``from backend.execution import ...``); pytest must expose both roots.
project_root = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)
backend_path = os.path.join(project_root, "backend")
for import_root in (project_root, backend_path):
    if import_root not in sys.path:
        sys.path.insert(0, import_root)

# --- Session-scoped event loop so async session fixtures work in STRICT mode ---
@pytest.fixture(scope="session")
def event_loop():
    """Session-scoped event loop for pytest-asyncio STRICT mode compatibility."""
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()

# --- Global Resource Lifecycle Management ---

@pytest_asyncio.fixture(scope="session", autouse=True)
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

def make_async(obj):
    """Recursively wrap all callable attributes of a MagicMock into AsyncMocks."""
    for name in dir(obj):
        if name.startswith("_"):
            continue
        attr = getattr(obj, name)
        if callable(attr) and not isinstance(attr, (AsyncMock, MagicMock)):
            setattr(obj, name, AsyncMock())

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


# --- Phase 58: private config fixture (append-only block) ---
import hashlib as _p58_hashlib
import json as _p58_json
from pathlib import Path as _P58Path


def _p58_write_json(path, payload):
    path.write_text(_p58_json.dumps(payload), encoding="utf-8")
    os.chmod(path, 0o600)


def _p58_canonical_hash(params):
    encoded = _p58_json.dumps(
        params, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return _p58_hashlib.sha256(encoded).hexdigest()


def build_private_config(root: _P58Path) -> _P58Path:
    """Create root/private with synthetic uk and india files; return root/private.

    Every value below is a test-only synthetic value. None is a real limit.
    Directories are 0700 and files 0600 because tmp_path inherits the umask.
    """

    private = root / "private"
    private.mkdir()
    os.chmod(private, 0o700)
    uk = private / "uk"
    india = private / "india"
    for directory in (uk, india, india / "research", india / "holdout"):
        directory.mkdir()
        os.chmod(directory, 0o700)

    # Test-only synthetic values.
    _p58_write_json(uk / "manifest.json", {"schema_version": 1, "workspace": "uk", "currency": "GBP"})
    _p58_write_json(
        india / "limits.json",
        {
            "schema_version": 1,
            "workspace": "india",
            "currency": "INR",
            "capital_cap": "1000.00",
            "per_position_cap": "250.00",
            "drawdown_halt": "-0.30",
            "drawdown_flatten": "-0.45",
            "position_stop": "-0.25",
        },
    )
    # Phase 63-04 (P-15): India execution authority needs this file. The slippage cap is the
    # operator's India value (25 bps, 2026-10-07); the collar is the D-09 2%. Both are
    # synthetic here like every other value in this directory.
    _p58_write_json(
        india / "execution.json",
        {
            "schema_version": 1,
            "workspace": "india",
            "venue": "paper",
            "fat_finger_collar": "0.02",
            "max_slippage_bps": "25",
        },
    )
    research = india / "research" / "fixture-research.json"
    holdout = india / "holdout" / "fixture-holdout.json"
    _p58_write_json(research, {"fixture": "synthetic research result"})
    _p58_write_json(holdout, {"fixture": "synthetic holdout result"})
    params = {"fixture_label": "synthetic", "fixture_window": 3}
    _p58_write_json(
        india / "strategy.json",
        {
            "schema_version": 1,
            "workspace": "india",
            "strategy_params_version": "test-fixture-1",
            "params": params,
            "params_sha256": _p58_canonical_hash(params),
            "research_refs": [
                {
                    "path": "research/fixture-research.json",
                    "sha256": _p58_hashlib.sha256(research.read_bytes()).hexdigest(),
                }
            ],
            "holdout_refs": [
                {
                    "path": "holdout/fixture-holdout.json",
                    "sha256": _p58_hashlib.sha256(holdout.read_bytes()).hexdigest(),
                }
            ],
        },
    )
    return private


@pytest.fixture
def private_config_dir(tmp_path):
    """A valid synthetic private/ directory. Reads no environment variable."""
    return build_private_config(tmp_path)
