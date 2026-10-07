"""Fail-closed loader for ``private/<workspace>/`` configuration.

The caller always passes ``private_dir``. This module reads no environment
variable and imports nothing from the execution package. The only venue facts
it reads come from the stdlib-only ``venue_registry``.

Every failure raises ``PrivateConfigError`` with a stable code and a field
name. No value read from a private file is ever placed in an error.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any

from pydantic import BaseModel, ValidationError
from venue_registry import known_venues, spec_for

from .errors import PrivateConfigError, schema_error_from_validation_error
from .schemas import (
    FileRef,
    IndiaExecution,
    IndiaLimits,
    IndiaStrategy,
    UkLimits,
    UkManifest,
    WorkspaceExecution,
)

SUPPORTED_WORKSPACES = frozenset({"uk", "india"})
WORKSPACE_CURRENCY = {"uk": "GBP", "india": "INR"}

MAX_CONFIG_BYTES = 65536
_HASH_CHUNK_BYTES = 1024 * 1024

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
    # Phase 66. ``execution`` is None when the workspace has no execution.json,
    # which means venue ``paper`` exactly as before. ``uk_limits`` is loaded
    # only for a practice venue.
    execution: WorkspaceExecution | None = None
    uk_limits: UkLimits | None = None
    # Phase 63 (P-15). Set only when ``require_india_execution`` was passed: the validated
    # India execution file (collar and slippage cap). It is the same object as ``execution``.
    india_execution: IndiaExecution | None = None

    @property
    def venue(self) -> str:
        return "paper" if self.execution is None else self.execution.venue

    def __repr__(self) -> str:
        return f"WorkspaceConfig(workspace={self.workspace!r}, fingerprint={self.fingerprint!r})"


# --- filesystem checks -------------------------------------------------------


def _check_stat(st: os.stat_result, *, directory: bool, field: str) -> None:
    """Apply type, owner and permission rules to an lstat or fstat result."""

    if stat.S_ISLNK(st.st_mode):
        raise PrivateConfigError("SYMLINK_REFUSED", field)
    if directory:
        if not stat.S_ISDIR(st.st_mode):
            raise PrivateConfigError("NOT_A_DIRECTORY", field)
    elif not stat.S_ISREG(st.st_mode):
        raise PrivateConfigError("NOT_A_REGULAR_FILE", field)
    if st.st_uid != os.getuid():
        raise PrivateConfigError("OWNER_MISMATCH", field)
    if st.st_mode & 0o077:
        raise PrivateConfigError("PERMISSIONS_TOO_OPEN", field)


def _lstat_checked(path: Path, *, directory: bool, field: str) -> os.stat_result:
    st = os.lstat(path)
    _check_stat(st, directory=directory, field=field)
    return st


def _open_regular_file(path: Path, field: str) -> int:
    """Open without following symlinks and re-check the opened descriptor."""

    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise PrivateConfigError("SYMLINK_REFUSED", field) from exc
        raise PrivateConfigError("FILE_MISSING", field) from exc
    try:
        _check_stat(os.fstat(fd), directory=False, field=field)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_config_file(path: Path, name: str) -> bytes:
    try:
        st = _lstat_checked(path, directory=False, field=name)
    except OSError as exc:
        raise PrivateConfigError("FILE_MISSING", name) from exc
    if st.st_size > MAX_CONFIG_BYTES:
        raise PrivateConfigError("FILE_TOO_LARGE", name)
    fd = _open_regular_file(path, name)
    try:
        data = b""
        while len(data) <= MAX_CONFIG_BYTES:
            chunk = os.read(fd, MAX_CONFIG_BYTES + 1 - len(data))
            if not chunk:
                break
            data += chunk
    finally:
        os.close(fd)
    if len(data) > MAX_CONFIG_BYTES:
        raise PrivateConfigError("FILE_TOO_LARGE", name)
    return data


# --- strict JSON parsing -----------------------------------------------------


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise PrivateConfigError("DUPLICATE_KEY", key)
        result[key] = value
    return result


def _refuse_float(_text: str) -> Any:
    raise PrivateConfigError("FLOAT_NOT_ALLOWED")


def _refuse_constant(_text: str) -> Any:
    raise PrivateConfigError("NON_FINITE_NOT_ALLOWED")


def _parse_object(raw: bytes, name: str) -> dict[str, Any]:
    # Decode and parse errors hold the file content (``.object`` and ``.doc``).
    # Each failure is raised after its except block so the new error has no
    # cause and no context that could reach that content.
    failure = ""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        failure = "NOT_UTF8"
    if failure:
        raise PrivateConfigError(failure, name)
    if not text.strip():
        raise PrivateConfigError("FILE_EMPTY", name)
    field = name
    try:
        data = json.loads(
            text,
            object_pairs_hook=_reject_duplicates,
            parse_float=_refuse_float,
            parse_constant=_refuse_constant,
        )
    except PrivateConfigError as exc:
        # Parse hooks do not know which file they ran for; name it when the
        # hook supplied no field of its own.
        failure, field = exc.code, exc.field or name
    except (ValueError, RecursionError):
        failure, field = "INVALID_JSON", name
    if failure:
        raise PrivateConfigError(failure, field)
    if not isinstance(data, dict):
        raise PrivateConfigError("NOT_AN_OBJECT", name)
    # JSON escapes can produce a lone surrogate (\uD800) from valid UTF-8.
    # Refuse it here so no later encode or path call fails outside
    # PrivateConfigError.
    try:
        json.dumps(data, ensure_ascii=False).encode("utf-8")
    except UnicodeEncodeError:
        failure = "INVALID_UNICODE"
    if failure:
        raise PrivateConfigError(failure, name)
    return data


# --- validation --------------------------------------------------------------


def _check_identity(data: dict[str, Any], workspace: str) -> None:
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
    # ValidationError text includes the rejected value. Raising after the
    # except block keeps it off __context__ as well as __cause__.
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        error = schema_error_from_validation_error(exc)
    raise error


def _check_bound_identity(parsed: Any, workspace: str) -> None:
    # The schema Literals already pin these. The explicit check keeps the
    # loader correct if a Literal is ever widened.
    if parsed.workspace != workspace:
        raise PrivateConfigError("WORKSPACE_MISMATCH", "workspace")
    currency = getattr(parsed, "currency", None)
    if currency is not None and currency != WORKSPACE_CURRENCY[workspace]:
        raise PrivateConfigError("CURRENCY_MISMATCH", "currency")


def _check_caps(limits: IndiaLimits | UkLimits) -> None:
    # Ordering only. No ceiling and no expected value: limits are operator
    # inputs and stay out of tracked code.
    zero = Decimal(0)
    if not limits.capital_cap > zero:
        raise PrivateConfigError("LIMIT_ORDER_INVALID", "capital_cap")
    if not zero < limits.per_position_cap <= limits.capital_cap:
        raise PrivateConfigError("LIMIT_ORDER_INVALID", "per_position_cap")


def _check_india_limits(limits: IndiaLimits) -> None:
    zero = Decimal(0)
    minus_one = Decimal(-1)
    _check_caps(limits)
    if not limits.drawdown_halt < zero:
        raise PrivateConfigError("LIMIT_ORDER_INVALID", "drawdown_halt")
    if not minus_one < limits.drawdown_flatten < limits.drawdown_halt:
        raise PrivateConfigError("LIMIT_ORDER_INVALID", "drawdown_flatten")
    if not minus_one < limits.position_stop < zero:
        raise PrivateConfigError("LIMIT_ORDER_INVALID", "position_stop")


def _canonical_params_hash(params: dict[str, Any]) -> str:
    encoded = json.dumps(
        params,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# --- research and holdout references -----------------------------------------


def _verify_ref(workspace_root: Path, ref: FileRef, field: str) -> None:
    raw_path = ref.path
    if not raw_path or "\x00" in raw_path or "\\" in raw_path:
        raise PrivateConfigError("REF_PATH_INVALID", field)
    posix = PurePosixPath(raw_path)
    if posix.is_absolute() or ".." in posix.parts:
        raise PrivateConfigError("REF_PATH_INVALID", field)
    candidate = workspace_root / os.path.normpath(raw_path)
    if candidate == workspace_root or workspace_root not in candidate.parents:
        raise PrivateConfigError("REF_PATH_INVALID", field)

    try:
        st = os.lstat(candidate)
    except FileNotFoundError as exc:
        raise PrivateConfigError("REF_MISSING", field) from exc
    except OSError as exc:
        raise PrivateConfigError("REF_PATH_INVALID", field) from exc
    if stat.S_ISLNK(st.st_mode):
        raise PrivateConfigError("SYMLINK_REFUSED", field)
    try:
        resolved = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise PrivateConfigError("REF_PATH_INVALID", field) from exc
    # A symlinked intermediate directory makes resolve() differ from the
    # normalised path, and is refused here.
    if resolved != candidate:
        raise PrivateConfigError("REF_PATH_INVALID", field)
    _check_stat(st, directory=False, field=field)

    fd = _open_regular_file(candidate, field)
    digest = hashlib.sha256()
    try:
        while True:
            chunk = os.read(fd, _HASH_CHUNK_BYTES)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(fd)
    if digest.hexdigest() != ref.sha256:
        raise PrivateConfigError("REF_HASH_MISMATCH", field)


def _verify_refs(workspace_dir: Path, strategy: IndiaStrategy) -> None:
    workspace_root = workspace_dir.resolve()
    for group, refs in (
        ("research_refs", strategy.research_refs),
        ("holdout_refs", strategy.holdout_refs),
    ):
        for index, ref in enumerate(refs):
            _verify_ref(workspace_root, ref, f"{group}.{index}")


# --- fingerprint and entry point ---------------------------------------------


def _fingerprint(raw_files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in sorted(raw_files):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(raw_files[name])
        digest.update(b"\0")
    return digest.hexdigest()


EXECUTION_FILE = "execution.json"
UK_LIMITS_FILE = "limits.json"


def _load_execution(
    workspace_dir: Path,
    workspace: str,
    raw_files: dict[str, bytes],
    *,
    require_india: bool = False,
) -> WorkspaceExecution | None:
    """Read execution.json. Absent means venue paper, as before, except for India authority.

    A present file that is invalid, names an unknown venue, or names a Trading
    212 venue in India is an error: it never falls back to paper.

    ``require_india`` is the Phase 63 execution start path (P-15): the file is then
    required for India (``EXECUTION_CONFIG_MISSING``) and validated as ``IndiaExecution``,
    which also carries the collar and the slippage cap. Every other read of an India
    workspace (the 62 research load) leaves it optional and its India values unchecked.
    """

    india_strict = require_india and workspace == "india"
    path = workspace_dir / EXECUTION_FILE
    if not os.path.lexists(path):
        if india_strict:
            raise PrivateConfigError("EXECUTION_CONFIG_MISSING", EXECUTION_FILE)
        return None
    raw = _read_config_file(path, EXECUTION_FILE)
    data = _parse_object(raw, EXECUTION_FILE)
    venue = data.get("venue")
    if not isinstance(venue, str) or venue not in known_venues():
        raise PrivateConfigError("VENUE_UNKNOWN", "venue")
    spec = spec_for(venue)
    if spec is not None:
        if workspace != spec.workspace:
            raise PrivateConfigError("VENUE_NOT_ALLOWED", "venue")
        for name in ("account_id", "currency"):
            if name not in data:
                raise PrivateConfigError("SCHEMA_INVALID", name)
    _check_identity(data, workspace)
    execution = _validate_model(IndiaExecution if india_strict else WorkspaceExecution, data)
    _check_bound_identity(execution, workspace)
    raw_files[EXECUTION_FILE] = raw
    return execution


def _load_uk_limits(workspace_dir: Path, raw_files: dict[str, bytes]) -> UkLimits:
    raw = _read_config_file(workspace_dir / UK_LIMITS_FILE, UK_LIMITS_FILE)
    data = _parse_object(raw, UK_LIMITS_FILE)
    _check_identity(data, "uk")
    limits = _validate_model(UkLimits, data)
    _check_bound_identity(limits, "uk")
    _check_caps(limits)
    raw_files[UK_LIMITS_FILE] = raw
    return limits


def load_workspace_config(
    private_dir: str | os.PathLike[str] | None,
    workspace: str,
    *,
    require_india_execution: bool = False,
) -> WorkspaceConfig:
    if workspace not in SUPPORTED_WORKSPACES:
        raise PrivateConfigError("UNSUPPORTED_WORKSPACE", "workspace")
    if private_dir is None:
        raise PrivateConfigError("PRIVATE_DIR_MISSING", "private_dir")
    private_path = Path(private_dir)
    if not os.path.lexists(private_path):
        raise PrivateConfigError("PRIVATE_DIR_MISSING", "private_dir")
    _lstat_checked(private_path, directory=True, field="private_dir")

    workspace_dir = private_path / workspace
    if not os.path.lexists(workspace_dir):
        raise PrivateConfigError("WORKSPACE_DIR_MISSING", workspace)
    _lstat_checked(workspace_dir, directory=True, field=workspace)

    raw_files: dict[str, bytes] = {}
    for name in _REQUIRED_FILES[workspace]:
        raw_files[name] = _read_config_file(workspace_dir / name, name)

    parsed_files: dict[str, dict[str, Any]] = {}
    for name, raw in raw_files.items():
        data = _parse_object(raw, name)
        _check_identity(data, workspace)
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
        _verify_refs(workspace_dir, strategy)
    else:
        manifest = _validate_model(UkManifest, parsed_files["manifest.json"])
        _check_bound_identity(manifest, workspace)

    execution = _load_execution(
        workspace_dir, workspace, raw_files, require_india=require_india_execution
    )
    uk_limits: UkLimits | None = None
    execution_spec = None if execution is None else spec_for(execution.venue)
    if execution_spec is not None and execution_spec.workspace == "uk":
        uk_limits = _load_uk_limits(workspace_dir, raw_files)

    return WorkspaceConfig(
        workspace=workspace,
        currency=WORKSPACE_CURRENCY[workspace],
        limits=limits,
        strategy=strategy,
        manifest=manifest,
        fingerprint=_fingerprint(raw_files),
        execution=execution,
        uk_limits=uk_limits,
        india_execution=execution if isinstance(execution, IndiaExecution) else None,
    )
