"""Shared helpers for the model registry tests.

Everything here is synthetic: model ids are made up, key values are generated
per test, and no real endpoint is named. No file written by these helpers holds
a key value.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import socket
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import pytest

from model_registry import (
    CHAT_ROLES,
    ROLE_FORECASTER,
    ModelRegistry,
    load_registry_file,
    set_active_registry,
)

# Provider id -> URL prefix on the stub server.
STUB_PREFIXES = {"xai": "xai", "lmstudio": "lmstudio", "ollama": "ollama"}
XAI_KEY_ENV = "STUB_XAI_API_KEY_ENV"

# Which provider serves which role in the default fixture.
DEFAULT_ROLE_PROVIDER = {
    "coordinator": "lmstudio",
    "decision": "xai",
    "research": "ollama",
    "risk_critic": "xai",
    "math_codegen": "lmstudio",
}


def model_id_for(role: str) -> str:
    return f"stub-{role.replace('_', '-')}-model"


def registry_document(
    stub_url: str,
    *,
    roles: Optional[Iterable[str]] = None,
    role_provider: Optional[Dict[str, str]] = None,
    role_extra: Optional[Dict[str, Dict[str, Any]]] = None,
    include_forecaster: bool = True,
) -> Dict[str, Any]:
    """A valid registry document aimed at the stub server at ``stub_url``."""

    wanted = list(CHAT_ROLES if roles is None else roles)
    mapping = dict(DEFAULT_ROLE_PROVIDER)
    mapping.update(role_provider or {})
    document: Dict[str, Any] = {
        "schema_version": 1,
        "providers": {
            "xai": {
                "kind": "openai_compatible",
                "base_url": f"{stub_url}/xai/v1",
                "api_key_env": XAI_KEY_ENV,
                "timeout_s": 20,
            },
            "lmstudio": {
                "kind": "openai_compatible",
                "base_url": f"{stub_url}/lmstudio/v1",
                "api_key_env": None,
                "timeout_s": 20,
            },
            "ollama": {
                "kind": "openai_compatible",
                "base_url": f"{stub_url}/ollama/v1",
                "api_key_env": None,
                "timeout_s": 20,
            },
            "local_checkpoint": {"kind": "hf_local"},
        },
        "roles": {},
    }
    for role in wanted:
        entry: Dict[str, Any] = {"provider": mapping[role], "model": model_id_for(role)}
        entry.update((role_extra or {}).get(role, {}))
        document["roles"][role] = entry
    if include_forecaster:
        document["roles"][ROLE_FORECASTER] = {
            "provider": "local_checkpoint",
            "model": "stub-org/stub-forecaster",
            "revision": "stub-revision-1",
        }
    return copy.deepcopy(document)


def write_registry(directory: Path, document: Dict[str, Any]) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "models.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def make_registry(directory: Path, stub_url: str, **kwargs: Any) -> ModelRegistry:
    return load_registry_file(write_registry(directory, registry_document(stub_url, **kwargs)))


def activate(registry: Optional[ModelRegistry]) -> None:
    set_active_registry(registry)


# --- network guard -------------------------------------------------------------


class NetworkGuard:
    """Records every refused non-loopback connection attempt."""

    def __init__(self) -> None:
        self.blocked: List[str] = []


def _is_loopback(host: Any) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "ignore")
    if host in (None, "", "localhost"):
        return True
    try:
        return ipaddress.ip_address(str(host)).is_loopback
    except ValueError:
        return False


def install_network_guard(monkeypatch: pytest.MonkeyPatch) -> NetworkGuard:
    """Refuse DNS lookups and connections to anything but loopback."""

    guard = NetworkGuard()
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def _refuse(host: Any) -> None:
        guard.blocked.append(str(host))
        raise OSError(f"NETWORK_GUARD: refused non-loopback host {host!r}")

    def guarded_connect(self: socket.socket, address: Any) -> Any:
        if isinstance(address, tuple) and not _is_loopback(address[0]):
            _refuse(address[0])
        return real_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: Any) -> Any:
        if isinstance(address, tuple) and not _is_loopback(address[0]):
            _refuse(address[0])
        return real_connect_ex(self, address)

    def guarded_getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        if not _is_loopback(host):
            _refuse(host)
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    return guard
