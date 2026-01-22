"""
Growin Backend Server - Main Application Entry Point
"""

from datetime import datetime

from app_logging import setup_logging

# Import shared state and models from app_context (single source of truth)
from app_context import state, ChatMessage, AnalyzeRequest, AgentResponse, T212ConfigRequest

# Initialize Logging
logger = setup_logging("growin_server")

# --------------------------------------------------------------------------- #
# Load Environment Variables FIRST (before any imports that need them)
# --------------------------------------------------------------------------- #

from dotenv import load_dotenv
load_dotenv()  # Critical: Load .env before importing modules that need env vars

# Set macOS library paths for TA-Lib and other dependencies
import os
if os.name == 'posix':  # macOS/Linux
    os.environ.setdefault('DYLD_LIBRARY_PATH', '/opt/homebrew/lib:/usr/local/lib')
    os.environ.setdefault('DYLD_FALLBACK_LIBRARY_PATH', '/opt/homebrew/lib:/usr/local/lib')

# ANE auto-detection and config (macOS Apple Silicon)
try:
    from utils.ane_detection import detect_ane_available
    _ane_available = detect_ane_available()
except Exception as e:
    logger.debug(f"ANE detection failed (safe to ignore on non-Apple Silicon): {e}")
    _ane_available = False

# Environment override (optional)
import os
_env_use_ane = str(os.getenv('USE_ANE', '')).lower() in {'1','true','yes'}
_ane_enabled = _ane_available or _env_use_ane

# Apply to AppState (guarded if available)
try:
    state.ane_config.enabled = bool(_ane_enabled)
    state.ane_config.compute_units = 'ALL' if _ane_enabled else 'CPU_ONLY'
except Exception:
    pass

# Local-safe reference for environments without the module


# --------------------------------------------------------------------------- #
# FastAPI App Initialization
# --------------------------------------------------------------------------- #

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import os

# Routes will be imported later to avoid circular dependencies


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Lifecycle events - MCP connection and model initialization"""
    # Startup
    logger.info("Starting Growin Server...")
    # ANE auto-detection: enable on Apple Silicon by default (but gated by user flag)

    
    # 1. Ensure default MCP servers are configured
    default_servers = [
        {
            "name": "Trading 212",
            "type": "stdio",
            "command": "python",
            "args": ["trading212_mcp_server.py"],
            "env": {},
            "url": None,
        },
        {
            "name": "HuggingFace",
            "type": "stdio",
            "command": "python",
            "args": ["huggingface_mcp_server.py"],
            "env": {"HF_TOKEN": os.getenv("HF_TOKEN", "")},
            "url": None,
        },
    ]

    for server in default_servers:
        try:
            existing = state.chat_manager.get_mcp_servers()
            if not any(s["name"] == server["name"] for s in existing):
                state.chat_manager.add_mcp_server(
                    server["name"],
                    server["type"],
                    server["command"],
                    server["args"],
                    server["env"],
                    server["url"],
                )
                logger.info(f"Added default MCP server: {server['name']}")
        except Exception as e:
            logger.error(f"Failed to add MCP server {server['name']}: {e}")

    # 2. Fetch configured MCP servers
    try:
        servers = state.chat_manager.get_mcp_servers(active_only=True)
    except Exception as e:
        logger.error(f"Failed to fetch MCP servers: {e}")
        servers = []

    # 3. Connect to MCP servers with graceful degradation
    try:
        async with state.mcp_client.connect_all(servers):
            logger.info(f"✅ Connected to {len(state.mcp_client.sessions)} MCP servers")
            
            # 4. Initialize TTM-R2 Model (async, non-blocking)
            try:
                from forecaster import get_forecaster
                get_forecaster()
                logger.info("✅ Forecaster initialized")
            except Exception as e:
                logger.warning(f"Forecaster initialization warning: {e}")

            logger.info("🚀 Growin Server ready!")
            yield
    except Exception as e:
        logger.error(f"MCP connection error: {e}")
        logger.warning("⚠️  Server starting without MCP connections")
        yield
    finally:
        logger.info("Shutting down...")
        state.chat_manager.close()


app = FastAPI(title="Growin API", version="2.0.0", lifespan=lifespan)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --------------------------------------------------------------------------- #
# Route Registration
# --------------------------------------------------------------------------- #

# Include specialized routers
from routes import chat_routes, agent_routes, market_routes, mcp_routes, additional_routes, status_routes

app.include_router(chat_routes.router)
app.include_router(agent_routes.router)
app.include_router(market_routes.router)
app.include_router(mcp_routes.router)
app.include_router(status_routes.router) # Detailed health & agent status
app.include_router(additional_routes.router)  # Stub endpoints for Mac app compatibility

# --------------------------------------------------------------------------- #
# Root Endpoints
# --------------------------------------------------------------------------- #

@app.get("/")
async def root():
    """
    Root endpoint returning API status and version information.
    """
    return {
        "status": "online",
        "service": "Growin Financial AI",
        "version": "2.0.0",
        "timestamp": datetime.now().isoformat()
    }

@app.get("/health")
async def health_check():
    """
    Health check endpoint verifying database connectivity.
    """
    return {
        "status": "healthy",
        "database": "connected" if state.chat_manager.conn else "error"
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8002, reload=True)
