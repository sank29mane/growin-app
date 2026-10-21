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

import secrets

import pytest

from model_stub_server import ModelStubServer
from model_registry.schemas import TEMPERATURE_REQUIRED_ROLES
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
    base_urls: Optional[Dict[str, str]] = None,
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
        if role in TEMPERATURE_REQUIRED_ROLES:
            entry["temperature"] = 0.0
        entry.update((role_extra or {}).get(role, {}))
        document["roles"][role] = entry
    if include_forecaster:
        document["roles"][ROLE_FORECASTER] = {
            "provider": "local_checkpoint",
            "model": "stub-org/stub-forecaster",
            "revision": "stub-revision-1",
        }
    for provider_id, url in (base_urls or {}).items():
        document["providers"][provider_id]["base_url"] = url
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


# --- fixtures (import into a test module to use) -----------------------------------


# Arguments the stub returns when magentic forces a structured-output tool
# (``run_magentic`` only; no production call site uses it any more).
STUB_TOOL_ARGUMENTS = {
    "return_list_of_toolcall": {"value": []},
}

# What the stub answers to a ``response_format`` json_schema request, by schema name.
STUB_STRUCTURED_REPLIES = {
    "ToolCallList": {"tool_calls": []},
    "RiskAssessment": {
        "status": "APPROVED",
        "confidence_score": 0.5,
        "risk_assessment": "stub",
        "compliance_notes": "stub",
        "recommendation_adjustment": "none",
        "debate_refutation": "stub",
        "requires_hitl": False,
    },
    "NewsDataQueryParams": {"q": "stub query"},
}


@pytest.fixture
def stub():
    server = ModelStubServer(
        tool_arguments=dict(STUB_TOOL_ARGUMENTS), structured=dict(STUB_STRUCTURED_REPLIES)
    ).start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def risk_workspace(tmp_path, monkeypatch):
    """The India workspace config, loaded by the real loader, as the loaded app state.

    The critic reads its limits and currency from ``state.workspace_config``.
    Values are the synthetic ones ``build_private_config`` writes.
    """

    from app_context import state
    from conftest import build_private_config
    from private_config import load_workspace_config

    (tmp_path / "ws").mkdir()
    config = load_workspace_config(build_private_config(tmp_path / "ws"), "india")
    monkeypatch.setattr(state, "workspace_config", config)
    return config


def uk_practice_config(root: Path, capital_cap: str, per_position_cap: str):
    """A UK practice ``WorkspaceConfig`` (GBP, limits.json) built by the real loader."""

    from conftest import build_private_config
    from private_config import load_workspace_config
    from venue_seam_testkit import SYNTH_LIMITS, write_practice_files

    root.mkdir(parents=True, exist_ok=True)
    private = build_private_config(root)
    write_practice_files(
        private,
        limits={**SYNTH_LIMITS, "capital_cap": capital_cap, "per_position_cap": per_position_cap},
    )
    return load_workspace_config(private, "uk")


@pytest.fixture
def key_env(monkeypatch):
    """A generated key value in the env var the xai provider names."""

    value = "stubkey-" + secrets.token_hex(12)
    monkeypatch.setenv(XAI_KEY_ENV, value)
    return value


@pytest.fixture
def offline_registry(tmp_path, monkeypatch):
    """An active registry whose endpoints nothing listens on.

    For unit tests that replace the LLM call itself: the role still resolves,
    and any request that escaped the mocks would fail on the closed port.
    """

    monkeypatch.setenv(XAI_KEY_ENV, "stubkey-" + secrets.token_hex(12))
    registry = make_registry(tmp_path / "private", "http://127.0.0.1:9")
    activate(registry)
    try:
        yield registry
    finally:
        set_active_registry(None)


@pytest.fixture
def private_dir_registry(tmp_path, monkeypatch):
    """A private root holding a valid models.json, found through GROWIN_PRIVATE_DIR.

    The app lifespan loads the registry from this directory, so tests that use
    ``with TestClient(app)`` exercise the real startup path.
    """

    monkeypatch.setenv(XAI_KEY_ENV, "stubkey-" + secrets.token_hex(12))
    private_dir = tmp_path / "private_root"
    write_registry(private_dir, registry_document("http://127.0.0.1:9"))
    monkeypatch.setenv("GROWIN_PRIVATE_DIR", str(private_dir))
    try:
        yield private_dir
    finally:
        set_active_registry(None)


def fake_replies(*texts: str):
    """A stand-in role chat model: ``ainvoke`` returns each text in turn."""

    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    llm = MagicMock()
    llm.ainvoke = AsyncMock(side_effect=[SimpleNamespace(content=t) for t in texts])
    return llm


@pytest.fixture
def network_guard(monkeypatch):
    return install_network_guard(monkeypatch)


@pytest.fixture
def registry_factory(tmp_path, stub, key_env):
    """Build a registry aimed at the stub and make it the active registry."""

    def build(**kwargs: Any) -> ModelRegistry:
        registry = make_registry(tmp_path / "private", stub.url, **kwargs)
        activate(registry)
        return registry

    try:
        yield build
    finally:
        set_active_registry(None)


# A loopback port nothing listens on: connections are refused at once.
DEAD_URL = "http://127.0.0.1:9"


def dead_provider_urls(*provider_ids: str) -> Dict[str, str]:
    """``base_urls`` override that points the given providers at a closed port."""

    return {pid: f"{DEAD_URL}/{pid}/v1" for pid in provider_ids}
