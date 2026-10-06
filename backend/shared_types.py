from pydantic import BaseModel
from typing import List, Optional, Dict

class ModelCapabilities(BaseModel):
    quantization: str
    sizeCategory: str
    isInstruct: bool
    recommendedRAM: str

class HFModel(BaseModel):
    id: str
    name: str
    downloads: int
    likes: int
    author: str
    tags: List[str]
    pipeline_tag: Optional[str] = None
    sizeOnDisk: float
    modelSize: str
    downloadTime: str
    capabilities: Optional[ModelCapabilities] = None
    isCompatible: bool = True

class MLXModel(BaseModel):
    repoId: str
    displayName: str
    description: str
    sizeGb: float
    quantization: str
    downloads: int
    likes: int
    isCached: bool
    combinedScore: float = 0.0

class ModelProvider(BaseModel):
    name: str
    models: List[str]
    description: Optional[str] = None
    endpoint: Optional[str] = None
    requires_api_key: Optional[bool] = False

class MCPServer(BaseModel):
    name: str
    type: str
    command: Optional[str] = None
    args: Optional[List[str]] = None
    env: Optional[Dict[str, str]] = None
    url: Optional[str] = None
    active: bool = False

class AvailableModelsResponse(BaseModel):
    providers: List[ModelProvider]
    default: Dict[str, str]

class MLXModelsResponse(BaseModel):
    models: List[MLXModel]

# Shared list of tools that must never run through the generic MCP route.
# The Trading 212 MCP server no longer defines any of them (66-02, D-05); the
# list stays as defence in depth so a re-added tool, or a tool name from another
# server, is still blocked by every consumer.
SENSITIVE_TOOLS = [
    "place_market_order",
    "place_limit_order",
    "place_stop_order",
    "place_stop_limit_order",
    "cancel_order",
    "update_pie",
    "create_investment_pie",
    "update_investment_pie",
    "delete_investment_pie",
    "switch_account",
]


# --- Trading 212 live/demo selection (66-02, D-10b) ---------------------------------
# One helper feeds the MCP server and /api/system/status, so the value the server
# acts on and the value the app is shown cannot drift apart. There is no default:
# only the exact strings "true" and "false" select an environment.

TRADING212_USE_DEMO_ENV = "TRADING212_USE_DEMO"


class Trading212EnvironmentError(ValueError):
    """TRADING212_USE_DEMO is not exactly ``true`` or ``false``."""


def resolve_trading212_environment(environ=None) -> str:
    """Return ``demo`` for ``true``, ``live`` for ``false``, ``unset`` when the
    variable is absent or empty, and ``invalid`` for any other value."""

    import os

    env = os.environ if environ is None else environ
    raw = env.get(TRADING212_USE_DEMO_ENV)
    if raw is None or raw == "":
        return "unset"
    if raw == "true":
        return "demo"
    if raw == "false":
        return "live"
    return "invalid"


def require_trading212_environment(environ=None) -> str:
    """Return ``demo`` or ``live``, or raise naming the variable."""

    resolved = resolve_trading212_environment(environ)
    if resolved in {"demo", "live"}:
        return resolved
    raise Trading212EnvironmentError(
        f"{TRADING212_USE_DEMO_ENV} must be exactly 'true' (demo) or 'false' (live); "
        f"it is {resolved}. No Trading 212 client was built."
    )
