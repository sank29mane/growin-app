"""Workspace scope for credentials that live in environment variables.

TRADING212_* and ALPACA_* names belong to the UK workspace. A process may use
them only when GROWIN_WORKSPACE is exactly ``uk``; an India process, and a
process whose GROWIN_WORKSPACE is unset or unknown, is refused. This module
imports nothing from ``execution`` so data and MCP modules can use it without
pulling in the ledger.

Reading GROWIN_WORKSPACE here only refuses a credential. It never decides which
ledger a process owns: only the ledger's own pin does that.
"""

from __future__ import annotations

import os
from typing import Mapping, Optional

WORKSPACE_ENV = "GROWIN_WORKSPACE"
UK_ONLY_CREDENTIAL_PREFIXES = ("TRADING212_", "ALPACA_")
_KNOWN_WORKSPACES = frozenset({"uk", "india"})


class CredentialScopeError(RuntimeError):
    """A UK-only credential was requested outside a UK process."""


def process_workspace() -> Optional[str]:
    """Return GROWIN_WORKSPACE when it is exactly ``uk`` or ``india``, else None.

    Read at call time with no default and no normalisation.
    """

    value = os.environ.get(WORKSPACE_ENV)
    return value if value in _KNOWN_WORKSPACES else None


def is_uk_only_credential(name: str) -> bool:
    return name.upper().startswith(UK_ONLY_CREDENTIAL_PREFIXES)


def uk_credential(name: str) -> Optional[str]:
    """Return a UK-only environment credential, or raise outside a UK process.

    ``ValueError`` when ``name`` is not a UK-only name. ``CredentialScopeError``
    unless the process workspace is ``uk``; its text names the variable and the
    workspace and never the value. Returns None when the variable is unset or
    blank.
    """

    if not is_uk_only_credential(name):
        raise ValueError(f"{name} is not a UK-only credential name")
    workspace = process_workspace()
    if workspace != "uk":
        raise CredentialScopeError(
            f"{name} is a UK-only credential; process workspace is {workspace or 'unset'}"
        )
    value = os.environ.get(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def scrub_uk_only_credentials(environment: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of ``environment`` without UK-only keys in a non-UK process.

    This also drops non-secret flags that share the prefix (for example
    TRADING212_USE_DEMO and ALPACA_USE_PAPER). That fails closed and is intended.
    """

    copy = dict(environment)
    if process_workspace() == "uk":
        return copy
    return {
        key: value
        for key, value in copy.items()
        if not is_uk_only_credential(key)
    }
