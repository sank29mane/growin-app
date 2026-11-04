"""Fail-closed loader for the gitignored ``private/<workspace>/`` directory."""

from .errors import PrivateConfigError
from .loader import (
    SUPPORTED_WORKSPACES,
    WORKSPACE_CURRENCY,
    WorkspaceConfig,
    load_shadow_forbidden_literals,
    load_shadow_runtime_config,
    load_workspace_config,
)
from .schemas import (
    FileRef,
    IndiaExecution,
    IndiaLimits,
    IndiaStrategy,
    ShadowRuntime,
    UkLimits,
    UkManifest,
    WorkspaceExecution,
)

__all__ = [
    "SUPPORTED_WORKSPACES",
    "WORKSPACE_CURRENCY",
    "FileRef",
    "IndiaExecution",
    "IndiaLimits",
    "IndiaStrategy",
    "PrivateConfigError",
    "ShadowRuntime",
    "UkLimits",
    "UkManifest",
    "WorkspaceExecution",
    "WorkspaceConfig",
    "load_shadow_forbidden_literals",
    "load_shadow_runtime_config",
    "load_workspace_config",
]
