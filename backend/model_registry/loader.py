"""Fail-closed loader for ``private/models.json``.

The caller passes ``private_dir``. This module reads no environment variable
to find the file. Every failure raises ``ModelRegistryError`` with a stable
code and a field path. No value read from the file is placed in an error.

A role absent from the file is not a load error: the registry loads and
``resolve(role)`` raises ``ModelRoleMissing`` for that role only, so one
missing role never takes the others down (open question 5, default).
"""

from __future__ import annotations

import errno
import hashlib
import ipaddress
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit

from pydantic import ValidationError

from .errors import (
    ModelRegistryError,
    ModelRegistryUnavailable,
    ModelRoleMissing,
    schema_error_from_validation_error,
)
from .schemas import (
    API_KEY_ENV_PATTERN,
    CHAT_ROLE_KINDS,
    FORECASTER_ROLE_KINDS,
    INLINE_KEY_FIELDS,
    KIND_HF_LOCAL,
    KIND_OPENAI_COMPATIBLE,
    ROLE_FORECASTER,
    ROLE_NAMES,
    SCHEMA_VERSION,
    PROVIDER_ID_PATTERN,
    ProviderConfig,
    RegistryFile,
    RoleConfig,
)

REGISTRY_FILENAME = "models.json"
MAX_REGISTRY_BYTES = 65536

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_PLAIN_HTTP_SCHEMES = frozenset({"http"})
_API_KEY_ENV_RE = re.compile(API_KEY_ENV_PATTERN)
_PROVIDER_ID_RE = re.compile(PROVIDER_ID_PATTERN)
_CONTROL_OR_SPACE = re.compile(r"[\x00-\x20\x7f]")


@dataclass(frozen=True, repr=False)
class ResolvedRole:
    """One role bound to its provider settings. Holds no key value."""

    role: str
    provider_id: str
    kind: str
    base_url: Optional[str]
    api_key_env: Optional[str]
    timeout_s: float
    model: str
    revision: Optional[str]
    temperature: Optional[float]
    max_tokens: Optional[int]
    top_p: Optional[float]
    image_prefix: Optional[str]
    compact_prompt: bool

    def __repr__(self) -> str:
        return (
            f"ResolvedRole(role={self.role!r}, provider={self.provider_id!r}, "
            f"kind={self.kind!r}, model={self.model!r})"
        )

    def api_key(self, environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
        """The key value from the environment, read at call time, or None."""

        if self.api_key_env is None:
            return None
        source = os.environ if environ is None else environ
        return source.get(self.api_key_env) or None


@dataclass(frozen=True, repr=False)
class ModelRegistry:
    """A validated registry. ``fingerprint`` is the sha256 of the raw bytes."""

    providers: Mapping[str, ProviderConfig]
    roles: Mapping[str, RoleConfig]
    fingerprint: str

    def __repr__(self) -> str:
        return f"ModelRegistry(roles={sorted(self.roles)!r}, fingerprint={self.fingerprint!r})"

    def has_role(self, role: str) -> bool:
        return role in self.roles

    def resolve(self, role: str) -> ResolvedRole:
        config = self.roles.get(role)
        if config is None:
            raise ModelRoleMissing(role)
        provider = self.providers[config.provider]
        return ResolvedRole(
            role=role,
            provider_id=config.provider,
            kind=provider.kind,
            base_url=provider.base_url,
            api_key_env=provider.api_key_env,
            timeout_s=provider.timeout_s,
            model=config.model,
            revision=config.revision,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            top_p=config.top_p,
            image_prefix=config.image_prefix,
            compact_prompt=config.compact_prompt,
        )

    def require(self, *roles: str) -> None:
        """Raise ``ModelRoleMissing`` for the first role that is not configured."""

        for role in roles:
            if role not in self.roles:
                raise ModelRoleMissing(role)

    def describe_roles(self, environ: Optional[Mapping[str, str]] = None) -> dict[str, Any]:
        """Non-secret role summary for the app. No URL, env name or key value.

        ``key_configured`` is null when the provider takes no key, otherwise
        whether the named environment variable is set.
        """

        items = []
        for name in ROLE_NAMES:
            if name not in self.roles:
                continue
            resolved = self.resolve(name)
            key_configured: Optional[bool] = None
            if resolved.api_key_env is not None:
                key_configured = resolved.api_key(environ) is not None
            items.append(
                {
                    "role": name,
                    "provider": resolved.provider_id,
                    "kind": resolved.kind,
                    "model": resolved.model,
                    "key_configured": key_configured,
                }
            )
        missing = [name for name in ROLE_NAMES if name not in self.roles]
        return {"roles": items, "missing_roles": missing}


# --- filesystem ---------------------------------------------------------------


def _read_registry_bytes(path: Path) -> bytes:
    field = REGISTRY_FILENAME
    try:
        st = os.lstat(path)
    except OSError as exc:
        raise ModelRegistryError("FILE_MISSING", field) from exc
    if stat.S_ISLNK(st.st_mode):
        raise ModelRegistryError("SYMLINK_REFUSED", field)
    if not stat.S_ISREG(st.st_mode):
        raise ModelRegistryError("NOT_A_REGULAR_FILE", field)
    if st.st_size > MAX_REGISTRY_BYTES:
        raise ModelRegistryError("FILE_TOO_LARGE", field)

    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ModelRegistryError("SYMLINK_REFUSED", field) from exc
        raise ModelRegistryError("FILE_MISSING", field) from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ModelRegistryError("NOT_A_REGULAR_FILE", field)
        data = b""
        while len(data) <= MAX_REGISTRY_BYTES:
            chunk = os.read(fd, MAX_REGISTRY_BYTES + 1 - len(data))
            if not chunk:
                break
            data += chunk
    finally:
        os.close(fd)
    if len(data) > MAX_REGISTRY_BYTES:
        raise ModelRegistryError("FILE_TOO_LARGE", field)
    return data


# --- strict JSON --------------------------------------------------------------


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ModelRegistryError("DUPLICATE_KEY", key)
        result[key] = value
    return result


def _parse_object(raw: bytes) -> dict[str, Any]:
    # Decode and parse errors hold the file content. Each failure is raised
    # after its except block so the new error has no cause or context.
    failure = ""
    field = REGISTRY_FILENAME
    text = ""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        failure = "NOT_UTF8"
    if failure:
        raise ModelRegistryError(failure, field)
    data: Any = None
    try:
        data = json.loads(text, object_pairs_hook=_reject_duplicates)
    except ModelRegistryError as exc:
        failure, field = exc.code, exc.field or field
    except (ValueError, RecursionError):
        failure = "INVALID_JSON"
    if failure:
        raise ModelRegistryError(failure, field)
    if not isinstance(data, dict):
        raise ModelRegistryError("NOT_AN_OBJECT", field)
    return data


# --- validation ---------------------------------------------------------------


def _is_loopback_host(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check_base_url(provider_id: str, config: ProviderConfig) -> None:
    field = f"providers.{provider_id}.base_url"
    url = config.base_url
    if url is None:
        raise ModelRegistryError("FIELD_INVALID", field)
    if _CONTROL_OR_SPACE.search(url) or "?" in url or "#" in url:
        raise ModelRegistryError("BASE_URL_INVALID", field)
    try:
        parts = urlsplit(url)
        host = parts.hostname
        _ = parts.port
    except ValueError as exc:
        raise ModelRegistryError("BASE_URL_INVALID", field) from exc
    if parts.scheme not in _ALLOWED_SCHEMES or not host:
        raise ModelRegistryError("BASE_URL_INVALID", field)
    # userinfo: any "@" in the authority, even with an empty user or password.
    if "@" in parts.netloc:
        raise ModelRegistryError("BASE_URL_INVALID", field)
    if (
        config.api_key_env is not None
        and parts.scheme in _PLAIN_HTTP_SCHEMES
        and not _is_loopback_host(host)
    ):
        raise ModelRegistryError("KEY_OVER_HTTP", field)


def _check_provider(provider_id: str, config: ProviderConfig) -> None:
    if _PROVIDER_ID_RE.match(provider_id) is None:
        raise ModelRegistryError("FIELD_INVALID", f"providers.{provider_id}")
    env_name = config.api_key_env
    if env_name is not None and _API_KEY_ENV_RE.match(env_name) is None:
        raise ModelRegistryError("API_KEY_ENV_INVALID", f"providers.{provider_id}.api_key_env")
    if config.kind in FORECASTER_ROLE_KINDS:
        if config.base_url is not None:
            raise ModelRegistryError("FIELD_INVALID", f"providers.{provider_id}.base_url")
        if env_name is not None:
            raise ModelRegistryError("FIELD_INVALID", f"providers.{provider_id}.api_key_env")
    else:
        _check_base_url(provider_id, config)


def _check_role(
    name: str, role: RoleConfig, providers: Mapping[str, ProviderConfig]
) -> None:
    provider = providers.get(role.provider)
    if provider is None:
        raise ModelRegistryError("PROVIDER_UNDEFINED", f"roles.{name}.provider")
    allowed = FORECASTER_ROLE_KINDS if name in (ROLE_FORECASTER,) else CHAT_ROLE_KINDS
    if provider.kind not in allowed:
        raise ModelRegistryError("KIND_ROLE_MISMATCH", f"roles.{name}.provider")


def _precheck(data: dict[str, Any]) -> None:
    """Checks that must name a precise code before pydantic sees the file."""

    version = data.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        raise ModelRegistryError("SCHEMA_VERSION", "schema_version")
    providers = data.get("providers")
    if isinstance(providers, dict):
        for provider_id, raw in providers.items():
            if isinstance(raw, dict):
                for key in raw:
                    if str(key).lower() in INLINE_KEY_FIELDS:
                        raise ModelRegistryError("INLINE_API_KEY", f"providers.{provider_id}")
    roles = data.get("roles")
    if isinstance(roles, dict):
        for name in roles:
            if name not in ROLE_NAMES:
                raise ModelRegistryError("UNKNOWN_ROLE", f"roles.{name}")


def _validate(data: dict[str, Any]) -> RegistryFile:
    _precheck(data)
    # ValidationError text includes the rejected value. Raising after the
    # except block keeps it off __context__ as well as __cause__.
    error: Optional[ModelRegistryError] = None
    parsed: Optional[RegistryFile] = None
    try:
        parsed = RegistryFile.model_validate(data)
    except ValidationError as exc:
        error = schema_error_from_validation_error(exc)
    if error is not None:
        raise error
    assert parsed is not None
    for provider_id, provider in parsed.providers.items():
        _check_provider(provider_id, provider)
    for name, role in parsed.roles.items():
        _check_role(name, role, parsed.providers)
    return parsed


# --- entry points -------------------------------------------------------------


def load_registry_file(path: str | os.PathLike[str]) -> ModelRegistry:
    raw = _read_registry_bytes(Path(path))
    parsed = _validate(_parse_object(raw))
    return ModelRegistry(
        providers=dict(parsed.providers),
        roles=dict(parsed.roles),
        fingerprint=hashlib.sha256(raw).hexdigest(),
    )


def load_registry(private_dir: str | os.PathLike[str] | None) -> ModelRegistry:
    """Load ``<private_dir>/models.json``. The caller resolves ``private_dir``."""

    if private_dir is None:
        raise ModelRegistryError("PRIVATE_DIR_MISSING", "private_dir")
    return load_registry_file(Path(private_dir) / REGISTRY_FILENAME)


# --- process-wide holder ------------------------------------------------------
#
# One registry is loaded at startup and held here. Call sites that must not
# import ``app_context`` (worker client, agents) read it through
# ``get_active_registry``. ``AppState.model_registry`` proxies to this holder.

_active: Optional[ModelRegistry] = None
_active_error: Optional[str] = None


def set_active_registry(
    registry: Optional[ModelRegistry], error_code: Optional[str] = None
) -> None:
    global _active, _active_error
    _active = registry
    _active_error = None if registry is not None else error_code


def active_registry_or_none() -> Optional[ModelRegistry]:
    return _active


def active_registry_error() -> Optional[str]:
    return _active_error


def get_active_registry() -> ModelRegistry:
    if _active is None:
        raise ModelRegistryUnavailable(_active_error or "")
    return _active


def resolve_role(role: str) -> ResolvedRole:
    return get_active_registry().resolve(role)
