"""Fail-closed loader for the gitignored ``private/<workspace>/`` directory."""

from .errors import PrivateConfigError
from .loader import (
    SUPPORTED_WORKSPACES,
    WORKSPACE_CURRENCY,
    WorkspaceConfig,
    load_workspace_config,
)
from .schemas import FileRef, IndiaLimits, IndiaStrategy, UkManifest

__all__ = [
    "SUPPORTED_WORKSPACES",
    "WORKSPACE_CURRENCY",
    "FileRef",
    "IndiaLimits",
    "IndiaStrategy",
    "PrivateConfigError",
    "UkManifest",
    "WorkspaceConfig",
    "load_workspace_config",
]
