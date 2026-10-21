"""Strict loader for relay.json (OD-5).

The file names the expected egress IP, the expected broker user id and the
Secret Manager names of the two Breeze secrets, nothing else. Anything
unexpected is refused: a missing or unknown key, an unfilled placeholder, a
wrong type, a URL off the transport allowlist, limits above the broker's own,
duplicate keys, a file readable by others.

ConfigError carries a fixed code and a key name, never a value.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit

from .transport import DEFAULT_ALLOWED_HOSTS

USERID_RE = re.compile(r"^[A-Za-z0-9]{3,32}$")
PLACEHOLDER_RE = re.compile(r"^<[A-Z0-9_]+>$")
SECRET_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,255}$")
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,40}$")

DEFAULT_ECHO_URL = "https://api.ipify.org"
DEFAULT_MASTER_URL = "https://directlink.icicidirect.com/NewSecurityMaster/SecurityMaster.zip"
MAX_PER_MINUTE = 100
MAX_PER_DAY = 5000

REQUIRED = ("expected_egress_ip", "expected_userid", "secret_names")
OPTIONAL = (
    "egress_echo_url",
    "security_master_url",
    "per_minute",
    "per_day",
    "security_master_per_hour",
)
SECRET_KEYS = ("app_key", "secret_key")


class ConfigError(Exception):
    """code is a fixed word; key is the config key name, never a value."""

    def __init__(self, code: str, key: str | None = None) -> None:
        self.code = code
        self.key = key
        super().__init__(f"{code}:{key}" if key else code)


def _safe_key(key: object) -> str:
    """A key name from the file is echoed only when it is plainly a name."""
    return key if isinstance(key, str) and _SAFE_KEY_RE.fullmatch(key) else "?"


@dataclass(frozen=True)
class RelayConfig:
    expected_egress_ip: str
    expected_userid: str = field(repr=False)
    secret_names: Mapping[str, str]
    egress_echo_url: str = DEFAULT_ECHO_URL
    security_master_url: str = DEFAULT_MASTER_URL
    per_minute: int = 80
    per_day: int = 4500
    security_master_per_hour: int = 6


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict:
    seen: dict[str, object] = {}
    for key, value in pairs:
        if key in seen:
            raise ConfigError("duplicate_key", _safe_key(key))
        seen[key] = value
    return seen


def _no_float(_text: str) -> object:
    raise ConfigError("bad_limit")


def _no_constant(_text: str) -> object:
    raise ConfigError("bad_json")


def _in_credentials_dir(path: Path) -> bool:
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not directory:
        return False
    try:
        path.resolve().relative_to(Path(directory).resolve())
    except ValueError:
        return False
    return True


def _check_mode(path: Path) -> None:
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        raise ConfigError("unreadable") from None
    # Looser than 0600 means any group or other bit, or an execute bit.
    if mode & 0o177 and not _in_credentials_dir(path):
        raise ConfigError("file_mode")


def _reject_unfilled(node: object, key: str) -> None:
    if isinstance(node, dict):
        for name, value in node.items():
            _reject_unfilled(value, f"{key}.{_safe_key(name)}" if key else _safe_key(name))
    elif isinstance(node, list):
        for value in node:
            _reject_unfilled(value, key)
    elif isinstance(node, str) and PLACEHOLDER_RE.fullmatch(node):
        raise ConfigError("placeholder", key)


def _url(raw: dict, key: str, default: str) -> str:
    value = raw.get(key, default)
    if not isinstance(value, str):
        raise ConfigError("bad_url", key)
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ConfigError("bad_url", key) from None
    rule = DEFAULT_ALLOWED_HOSTS.get((parts.hostname or "").lower())
    if (
        parts.scheme != "https"
        or "@" in parts.netloc
        or rule is None
        or rule.scheme != "https"
        or port not in (None, rule.port)
    ):
        raise ConfigError("bad_url", key)
    return value


def _limit(raw: dict, key: str, default: int, maximum: int | None) -> int:
    value = raw.get(key, default)
    if type(value) is not int or value < 1:
        raise ConfigError("bad_limit", key)
    if maximum is not None and value > maximum:
        raise ConfigError("limit_too_high", key)
    return value


def load_relay_config(path: Path) -> RelayConfig:
    path = Path(path)
    _check_mode(path)
    try:
        text = path.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise ConfigError("unreadable") from None
    try:
        raw = json.loads(
            text,
            object_pairs_hook=_reject_duplicates,
            parse_float=_no_float,
            parse_constant=_no_constant,
        )
    except ValueError:
        raise ConfigError("bad_json") from None
    except RecursionError:
        raise ConfigError("bad_json") from None
    if not isinstance(raw, dict):
        raise ConfigError("bad_json")

    for key in raw:
        if key not in REQUIRED and key not in OPTIONAL:
            raise ConfigError("unknown_key", _safe_key(key))
    for key in REQUIRED:
        if key not in raw:
            raise ConfigError("missing_key", key)
    names = raw["secret_names"]
    if not isinstance(names, dict):
        raise ConfigError("bad_secret_names", "secret_names")
    for key in names:
        if key not in SECRET_KEYS:
            raise ConfigError("unknown_key", f"secret_names.{_safe_key(key)}")
    for key in SECRET_KEYS:
        if key not in names:
            raise ConfigError("missing_key", f"secret_names.{key}")

    _reject_unfilled(raw, "")

    ip = raw["expected_egress_ip"]
    try:
        if not isinstance(ip, str):
            raise ValueError
        ipaddress.IPv4Address(ip)
    except ValueError:
        raise ConfigError("bad_ip", "expected_egress_ip") from None
    userid = raw["expected_userid"]
    if not isinstance(userid, str) or not USERID_RE.fullmatch(userid):
        raise ConfigError("bad_userid", "expected_userid")
    for key in SECRET_KEYS:
        name = names[key]
        if not isinstance(name, str) or not SECRET_NAME_RE.fullmatch(name):
            raise ConfigError("bad_secret_names", f"secret_names.{key}")

    return RelayConfig(
        expected_egress_ip=ip,
        expected_userid=userid,
        secret_names=MappingProxyType({key: names[key] for key in SECRET_KEYS}),
        egress_echo_url=_url(raw, "egress_echo_url", DEFAULT_ECHO_URL),
        security_master_url=_url(raw, "security_master_url", DEFAULT_MASTER_URL),
        per_minute=_limit(raw, "per_minute", 80, MAX_PER_MINUTE),
        per_day=_limit(raw, "per_day", 4500, MAX_PER_DAY),
        security_master_per_hour=_limit(raw, "security_master_per_hour", 6, None),
    )
