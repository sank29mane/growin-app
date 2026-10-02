"""Fail-closed loader for ``private/<workspace>/`` configuration.

The caller always passes ``private_dir``. This module reads no environment
variable and imports nothing from the execution package.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from .errors import PrivateConfigError, schema_error_from_validation_error
from .schemas import IndiaLimits, IndiaStrategy, UkManifest

SUPPORTED_WORKSPACES = frozenset({"uk", "india"})
WORKSPACE_CURRENCY = {"uk": "GBP", "india": "INR"}

_REQUIRED_FILES = {
    "india": ("limits.json", "strategy.json"),
    "uk": ("manifest.json",),
}


@dataclass(frozen=True, repr=False)
class WorkspaceConfig:
    workspace: str
    currency: str
    limits: IndiaLimits | None
    strategy: IndiaStrategy | None
    manifest: UkManifest | None
    fingerprint: str

    def __repr__(self) -> str:
        return f"WorkspaceConfig(workspace={self.workspace!r}, fingerprint={self.fingerprint!r})"


def _canonical_params_hash(params: dict[str, Any]) -> str:
    encoded = json.dumps(
        params,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _read_file(path: Path, name: str) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise PrivateConfigError("FILE_MISSING", name) from exc


def _parse_object(raw: bytes, name: str) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PrivateConfigError("NOT_UTF8", name) from exc
    if not text.strip():
        raise PrivateConfigError("FILE_EMPTY", name)
    try:
        data = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise PrivateConfigError("INVALID_JSON", name) from exc
    if not isinstance(data, dict):
        raise PrivateConfigError("NOT_AN_OBJECT", name)
    return data


def _check_identity(data: dict[str, Any], workspace: str, name: str) -> None:
    """Refuse a file that names another workspace or the wrong currency.

    This runs before schema validation so that an India file copied into
    ``private/uk/`` reports WORKSPACE_MISMATCH instead of a generic schema
    failure. Only the field name is reported, never the value.
    """

    inner_workspace = data.get("workspace")
    if isinstance(inner_workspace, str) and inner_workspace != workspace:
        raise PrivateConfigError("WORKSPACE_MISMATCH", "workspace")
    inner_currency = data.get("currency")
    if isinstance(inner_currency, str) and inner_currency != WORKSPACE_CURRENCY[workspace]:
        raise PrivateConfigError("CURRENCY_MISMATCH", "currency")


def _validate_model(model: type[BaseModel], data: dict[str, Any]) -> Any:
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise schema_error_from_validation_error(exc) from None


def _check_bound_identity(parsed: Any, workspace: str) -> None:
    if parsed.workspace != workspace:
        raise PrivateConfigError("WORKSPACE_MISMATCH", "workspace")
    currency = getattr(parsed, "currency", None)
    if currency is not None and currency != WORKSPACE_CURRENCY[workspace]:
        raise PrivateConfigError("CURRENCY_MISMATCH", "currency")


def _check_india_limits(limits: IndiaLimits) -> None:
    zero = Decimal(0)
    minus_one = Decimal(-1)
    if not limits.capital_cap > zero:
        raise PrivateConfigError("LIMIT_ORDER_INVALID", "capital_cap")
    if not zero < limits.per_position_cap <= limits.capital_cap:
        raise PrivateConfigError("LIMIT_ORDER_INVALID", "per_position_cap")
    if not limits.drawdown_halt < zero:
        raise PrivateConfigError("LIMIT_ORDER_INVALID", "drawdown_halt")
    if not minus_one < limits.drawdown_flatten < limits.drawdown_halt:
        raise PrivateConfigError("LIMIT_ORDER_INVALID", "drawdown_flatten")
    if not minus_one < limits.position_stop < zero:
        raise PrivateConfigError("LIMIT_ORDER_INVALID", "position_stop")


def _fingerprint(raw_files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in sorted(raw_files):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(raw_files[name])
        digest.update(b"\0")
    return digest.hexdigest()


def load_workspace_config(
    private_dir: str | os.PathLike[str] | None, workspace: str
) -> WorkspaceConfig:
    if workspace not in SUPPORTED_WORKSPACES:
        raise PrivateConfigError("UNSUPPORTED_WORKSPACE", "workspace")
    if private_dir is None or not Path(private_dir).is_dir():
        raise PrivateConfigError("PRIVATE_DIR_MISSING", "private_dir")
    workspace_dir = Path(private_dir) / workspace
    if not workspace_dir.is_dir():
        raise PrivateConfigError("WORKSPACE_DIR_MISSING", workspace)

    raw_files: dict[str, bytes] = {}
    for name in _REQUIRED_FILES[workspace]:
        raw_files[name] = _read_file(workspace_dir / name, name)

    parsed_files: dict[str, dict[str, Any]] = {}
    for name, raw in raw_files.items():
        data = _parse_object(raw, name)
        _check_identity(data, workspace, name)
        parsed_files[name] = data

    limits: IndiaLimits | None = None
    strategy: IndiaStrategy | None = None
    manifest: UkManifest | None = None
    if workspace == "india":
        limits = _validate_model(IndiaLimits, parsed_files["limits.json"])
        strategy = _validate_model(IndiaStrategy, parsed_files["strategy.json"])
        _check_bound_identity(limits, workspace)
        _check_bound_identity(strategy, workspace)
        _check_india_limits(limits)
        if _canonical_params_hash(strategy.params) != strategy.params_sha256:
            raise PrivateConfigError("PARAMS_HASH_MISMATCH", "params_sha256")
    else:
        manifest = _validate_model(UkManifest, parsed_files["manifest.json"])
        _check_bound_identity(manifest, workspace)

    return WorkspaceConfig(
        workspace=workspace,
        currency=WORKSPACE_CURRENCY[workspace],
        limits=limits,
        strategy=strategy,
        manifest=manifest,
        fingerprint=_fingerprint(raw_files),
    )
