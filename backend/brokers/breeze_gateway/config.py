"""Strict loader for the Mac gateway config (~/.config/growin/gateway.json).

The file holds names only: the GCP project, zone and instance, the daily
identity's email, and two tunnel numbers. It holds no secret. Every value ends
up in a gcloud argv, so each one is checked against a fixed pattern and a value
that could start a flag is refused. A ConfigError names the section or key and
never a value.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

PROJECT_RE = re.compile(r"[a-z][-a-z0-9]{4,28}[a-z0-9]")
ZONE_RE = re.compile(r"[a-z]+-[a-z]+[0-9]+-[a-z]")
INSTANCE_RE = re.compile(r"[a-z]([-a-z0-9]{0,61}[a-z0-9])?")
ACCOUNT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._%+-]*@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PLACEHOLDER_RE = re.compile(r"<[^>]*>")

SSH_PORT = 22
MAX_CONFIG_BYTES = 16 * 1024
READY_TIMEOUT_MAX_S = 300
LISTENER_WINDOW_MAX_S = 120


class ConfigError(Exception):
    """A rejected config. `code` is a fixed word and `key` a section or key name."""

    def __init__(self, code: str, key: str) -> None:
        self.code = code
        self.key = key
        super().__init__(f"{code} ({key})")


@dataclass(frozen=True)
class GcpSection:
    project: str
    zone: str
    instance: str
    daily_account: str


@dataclass(frozen=True)
class TunnelSection:
    relay_port: int
    ready_timeout_s: int


@dataclass(frozen=True)
class ListenerSection:
    window_s: int


@dataclass(frozen=True)
class GatewayConfig:
    gcp: GcpSection
    tunnel: TunnelSection
    listener: ListenerSection | None = None


def default_config_path() -> Path:
    return Path.home() / ".config" / "growin" / "gateway.json"


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: dict[str, Any] = {}
    for name, value in pairs:
        if name in seen:
            raise ConfigError("duplicate_key", "file")
        seen[name] = value
    return seen


def _section(data: Mapping[str, Any], name: str, keys: tuple[str, ...], *, optional: tuple[str, ...] = ()) -> Mapping[str, Any]:
    section = data.get(name)
    if not isinstance(section, dict):
        raise ConfigError("missing_section", name)
    if set(section) - set(keys):
        raise ConfigError("unknown_key", name)
    for key in keys:
        if key not in section and key not in optional:
            raise ConfigError("missing_key", f"{name}.{key}")
    return section


def _text(section: Mapping[str, Any], path: str, key: str, pattern: re.Pattern[str]) -> str:
    value = section[key]
    where = f"{path}.{key}"
    if not isinstance(value, str):
        raise ConfigError("bad_type", where)
    if PLACEHOLDER_RE.search(value):
        raise ConfigError("placeholder", where)
    if not pattern.fullmatch(value):
        raise ConfigError("bad_value", where)
    return value


def _number(section: Mapping[str, Any], path: str, key: str, low: int, high: int) -> int:
    value = section[key]
    where = f"{path}.{key}"
    if not isinstance(value, int) or isinstance(value, bool):
        raise ConfigError("bad_type", where)
    if not low <= value <= high:
        raise ConfigError("bad_value", where)
    return value


def parse_gateway_config(data: object) -> GatewayConfig:
    if not isinstance(data, dict):
        raise ConfigError("bad_type", "file")
    if set(data) - {"gcp", "tunnel", "listener"}:
        raise ConfigError("unknown_key", "file")

    gcp = _section(data, "gcp", ("project", "zone", "instance", "daily_account"))
    tunnel = _section(data, "tunnel", ("relay_port", "ready_timeout_s"))
    relay_port = _number(tunnel, "tunnel", "relay_port", 1, 65535)
    if relay_port == SSH_PORT:
        raise ConfigError("bad_value", "tunnel.relay_port")

    listener: ListenerSection | None = None
    if "listener" in data:
        section = _section(data, "listener", ("window_s",))
        listener = ListenerSection(window_s=_number(section, "listener", "window_s", 1, LISTENER_WINDOW_MAX_S))

    return GatewayConfig(
        gcp=GcpSection(
            project=_text(gcp, "gcp", "project", PROJECT_RE),
            zone=_text(gcp, "gcp", "zone", ZONE_RE),
            instance=_text(gcp, "gcp", "instance", INSTANCE_RE),
            daily_account=_text(gcp, "gcp", "daily_account", ACCOUNT_RE),
        ),
        tunnel=TunnelSection(
            relay_port=relay_port,
            ready_timeout_s=_number(tunnel, "tunnel", "ready_timeout_s", 1, READY_TIMEOUT_MAX_S),
        ),
        listener=listener,
    )


def load_gateway_config(path: Path | None = None) -> GatewayConfig:
    """Read and validate the file. Mode must be 0600 or 0400 and the owner must be the caller."""
    target = path if path is not None else default_config_path()
    try:
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        raise ConfigError("unreadable", "file") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ConfigError("unreadable", "file")
        if stat.S_IMODE(info.st_mode) not in (0o600, 0o400):
            raise ConfigError("bad_mode", "file")
        if info.st_uid != os.geteuid():
            raise ConfigError("bad_owner", "file")
        raw = os.read(fd, MAX_CONFIG_BYTES + 1)
    except OSError:
        raise ConfigError("unreadable", "file") from None
    finally:
        os.close(fd)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigError("too_large", "file")
    try:
        data = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except ConfigError:
        raise
    except ValueError:
        raise ConfigError("bad_json", "file") from None
    return parse_gateway_config(data)
