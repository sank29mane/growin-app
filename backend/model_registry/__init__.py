"""Model role registry: one config file, one OpenAI-compatible provider.

``private/models.json`` maps each runtime role to a provider and a model. No
code in the backend picks a model by name.
"""

from .errors import (
    ModelRegistryError,
    ModelRegistryUnavailable,
    ModelRoleMissing,
    ProviderError,
)
from .loader import (
    REGISTRY_FILENAME,
    ModelRegistry,
    ResolvedRole,
    active_registry_error,
    active_registry_or_none,
    get_active_registry,
    load_registry,
    load_registry_file,
    resolve_role,
    set_active_registry,
)
from .schemas import (
    CHAT_ROLES,
    ROLE_COORDINATOR,
    ROLE_DECISION,
    ROLE_FORECASTER,
    ROLE_MATH_CODEGEN,
    ROLE_NAMES,
    ROLE_RESEARCH,
    ROLE_RISK_CRITIC,
)

__all__ = [
    "CHAT_ROLES",
    "REGISTRY_FILENAME",
    "ROLE_COORDINATOR",
    "ROLE_DECISION",
    "ROLE_FORECASTER",
    "ROLE_MATH_CODEGEN",
    "ROLE_NAMES",
    "ROLE_RESEARCH",
    "ROLE_RISK_CRITIC",
    "ModelRegistry",
    "ModelRegistryError",
    "ModelRegistryUnavailable",
    "ModelRoleMissing",
    "ProviderError",
    "ResolvedRole",
    "active_registry_error",
    "active_registry_or_none",
    "get_active_registry",
    "load_registry",
    "load_registry_file",
    "resolve_role",
    "set_active_registry",
]
