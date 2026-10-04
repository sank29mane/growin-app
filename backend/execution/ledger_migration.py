"""Operator-confirmed, reversible pinning of a legacy ledger to one workspace.

A ledger file from schema v1 to v5 has no workspace pin, so ``ExecutionLedger``
refuses it (``LedgerUnpinned``). This module is the only way to pin one:

* ``inspect_ledger`` takes the ledger's writer lock, copies the file with
  SQLite's backup API (so the WAL is included), checks the copy, and reports
  every workspace tag it can find.
* ``apply_migration`` needs the operator's explicit workspace and the backup's
  hash prefix, refuses conflicting or unknown tags, and adds the pin in one
  ``BEGIN IMMEDIATE`` transaction that only adds structure.
* ``verify_migration`` proves every pre-existing byte survived.
* ``restore_from_backup`` moves the v6 files aside and rebuilds the original.

Ownership never comes from an environment variable, a path or a directory
name. This module reads no environment variable at all.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

from .ledger import (
    LEGACY_SCHEMA_VERSIONS,
    SCHEMA_VERSION,
    LedgerError,
    LedgerReader,
    _apply_base_schema,
    _install_identity,
    _read_identity,
    _require_sqlite_capabilities,
    _ro_uri,
)
from .models import Workspace

TOOL_VERSION = "1"
MIN_CONFIRMATION_HEX = 12
_VALID_WORKSPACES = {"uk", "india"}

# Columns that carry a workspace value directly.
_WORKSPACE_COLUMN_TABLES = (
    "approval_keys",
    "approval_challenges",
    "execution_approvals",
    "execution_admissions",
    "paper_budgets",
    "buying_power_reservations",
    "workspace_controls",
    "workspace_control_events",
    "paper_positions",
)

# (table, column, is_blob): JSON payloads searched recursively for a "workspace" key.
_JSON_PAYLOAD_COLUMNS = (
    ("execution_events", "payload_json", False),
    ("requote_intents", "candidate_json", False),
    ("requote_events", "payload_json", False),
    ("order_projection", "acknowledgment_json", False),
    ("dispatch_attempts", "acknowledgment_json", False),
    ("approval_challenges", "signed_payload", True),
)


class MigrationRefused(LedgerError):
    """A migration step was refused. ``code`` is stable and machine readable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _open_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(_ro_uri(path), uri=True)


def _content_digest(connection: sqlite3.Connection) -> str:
    return hashlib.sha256("\n".join(connection.iterdump()).encode("utf-8")).hexdigest()


def _table_names(connection: sqlite3.Connection) -> list[str]:
    return [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def _checked_ledger_path(ledger_path: os.PathLike[str] | str) -> Path:
    path = Path(ledger_path)
    if path.is_symlink():
        raise MigrationRefused("SYMLINK_REFUSED", "ledger path must not be a symbolic link")
    if not path.exists():
        raise MigrationRefused("LEDGER_MISSING", f"no ledger at {path}")
    return path


@contextmanager
def _writer_lock(ledger_path: Path) -> Iterator[None]:
    """Take the same advisory lock ExecutionLedger uses, so no backend is running."""

    lock_path = ledger_path.with_name(f"{ledger_path.name}.lock")
    if lock_path.is_symlink():
        raise MigrationRefused("SYMLINK_REFUSED", "ledger lock path must not be a symbolic link")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    fd = os.open(lock_path, flags, 0o600)
    os.chmod(lock_path, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            raise MigrationRefused(
                "WRITER_ACTIVE", f"an execution writer is already active for {ledger_path}"
            ) from None
        raise
    try:
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _classify_legacy(connection: sqlite3.Connection) -> int:
    """Return the legacy user_version, or refuse a file that is not a legacy ledger."""

    identity = _read_identity(connection)
    if identity.kind == "pinned":
        raise MigrationRefused(
            "ALREADY_PINNED",
            f"ledger is already pinned to {identity.workspace.value if identity.workspace else '?'}",
        )
    if identity.kind != "unpinned" or identity.user_version not in LEGACY_SCHEMA_VERSIONS:
        raise MigrationRefused(
            "UNSUPPORTED_VERSION",
            f"ledger is {identity.kind} at schema {identity.user_version}; "
            "only v1 to v5 ledgers can be pinned",
        )
    return identity.user_version


def _integrity(connection: sqlite3.Connection) -> tuple[str, list[list[Any]]]:
    rows = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
    status = "ok" if rows == ["ok"] else "; ".join(rows)
    fk_rows = [list(row) for row in connection.execute("PRAGMA foreign_key_check")]
    return status, fk_rows


def _walk_workspace_values(node: Any, found: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "workspace":
                found.append(value if isinstance(value, str) else json.dumps(value, sort_keys=True))
            _walk_workspace_values(value, found)
    elif isinstance(node, list):
        for item in node:
            _walk_workspace_values(item, found)


def _scan_tags(connection: sqlite3.Connection) -> dict[str, Any]:
    """Collect every workspace value in the file, by location, with counts."""

    tables = set(_table_names(connection))
    tags: dict[str, dict[str, int]] = {}
    unparseable: list[str] = []
    absent: list[str] = []

    def add(location: str, value: str) -> None:
        bucket = tags.setdefault(location, {})
        bucket[value] = bucket.get(value, 0) + 1

    if "order_intents" in tables:
        for rowid, raw in connection.execute("SELECT rowid, canonical_json FROM order_intents"):
            location = "order_intents.canonical_json"
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError):
                unparseable.append(f"{location}#rowid={rowid}")
                continue
            value = parsed.get("workspace") if isinstance(parsed, dict) else None
            if not isinstance(value, str):
                unparseable.append(f"{location}#rowid={rowid}")
                continue
            add(location, value)
    else:
        absent.append("order_intents")

    for table in _WORKSPACE_COLUMN_TABLES:
        if table not in tables:
            absent.append(table)
            continue
        for value, count in connection.execute(
            f"SELECT workspace, COUNT(*) FROM {table} GROUP BY workspace"
        ):
            label = value if isinstance(value, str) else json.dumps(value)
            tags.setdefault(f"{table}.workspace", {})[label] = int(count)

    for table, column, is_blob in _JSON_PAYLOAD_COLUMNS:
        if table not in tables:
            if table not in absent:
                absent.append(table)
            continue
        columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            continue
        location = f"{table}.{column}"
        for rowid, raw in connection.execute(
            f"SELECT rowid, {column} FROM {table} WHERE {column} IS NOT NULL"
        ):
            try:
                text = bytes(raw).decode("utf-8") if is_blob else str(raw)
                parsed = json.loads(text)
            except (TypeError, ValueError):
                unparseable.append(f"{location}#rowid={rowid}")
                continue
            found: list[str] = []
            _walk_workspace_values(parsed, found)
            for value in found:
                add(location, value)

    distinct = sorted({value for bucket in tags.values() for value in bucket})
    if unparseable:
        decision = "blocked-unparseable"
    elif any(value not in _VALID_WORKSPACES for value in distinct):
        decision = "blocked-unknown"
    elif len(distinct) > 1:
        decision = "blocked-conflict"
    elif len(distinct) == 1:
        decision = "confirm-one"
    else:
        decision = "confirm-any"
    return {
        "workspace_tags": tags,
        "distinct_workspaces": distinct,
        "unparseable": unparseable,
        "absent_tables": sorted(absent),
        "decision_required": decision,
    }


def _load_manifest(manifest_path: os.PathLike[str] | str) -> dict[str, Any]:
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise MigrationRefused("STALE_INSPECTION", f"manifest cannot be read: {exc}") from None
    required = {"backup_path", "backup_sha256", "source_path", "source_user_version", "content_digest"}
    if not isinstance(manifest, dict) or not required <= set(manifest):
        raise MigrationRefused("STALE_INSPECTION", "manifest is missing required keys")
    return manifest


def _write_private_json(path: Path, payload: Mapping[str, Any], *, exclusive: bool) -> None:
    flags = os.O_WRONLY | os.O_CREAT
    flags |= os.O_EXCL if exclusive else os.O_TRUNC
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.chmod(path, 0o600)


def _verified_backup(manifest: Mapping[str, Any]) -> Path:
    backup = Path(str(manifest["backup_path"]))
    if not backup.exists():
        raise MigrationRefused("BACKUP_HASH_MISMATCH", f"backup {backup} is missing")
    if _sha256_file(backup) != manifest["backup_sha256"]:
        raise MigrationRefused("BACKUP_HASH_MISMATCH", "backup file hash differs from the manifest")
    return backup


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


def inspect_ledger(
    ledger_path: os.PathLike[str] | str,
    backup_dir: os.PathLike[str] | str,
    *,
    repo_root: os.PathLike[str] | str,
) -> dict[str, Any]:
    """Back up a legacy ledger, check the copy, and report every workspace tag.

    Never opens the source read-write. Holds the writer lock throughout.
    """

    ledger = _checked_ledger_path(ledger_path)
    resolved_backup_dir = Path(backup_dir).resolve()
    repo = Path(repo_root).resolve()
    if resolved_backup_dir == repo or repo in resolved_backup_dir.parents:
        raise MigrationRefused(
            "BACKUP_INSIDE_REPO", "backups hold orders and must stay outside the repository"
        )

    with _writer_lock(ledger):
        source = _open_ro(ledger)
        try:
            _require_sqlite_capabilities(source)
            _classify_legacy(source)
        finally:
            source.close()

        if not resolved_backup_dir.exists():
            resolved_backup_dir.mkdir(mode=0o700, parents=True)
            os.chmod(resolved_backup_dir, 0o700)
        backup_path = resolved_backup_dir / (
            f"{ledger.resolve().parent.name}-{_utc_stamp()}-pre-v6.sqlite3"
        )
        try:
            fd = os.open(backup_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise MigrationRefused("BACKUP_EXISTS", f"{backup_path} already exists") from None
        os.close(fd)

        source = _open_ro(ledger)
        destination = sqlite3.connect(backup_path)
        try:
            source.backup(destination)
        finally:
            destination.close()
            source.close()
        os.chmod(backup_path, 0o600)

        backup = _open_ro(backup_path)
        try:
            integrity, fk_rows = _integrity(backup)
            if integrity != "ok":
                raise MigrationRefused(
                    "INTEGRITY_FAILED", f"backup {backup_path} failed integrity_check: {integrity}"
                )
            if fk_rows:
                raise MigrationRefused(
                    "FOREIGN_KEY_FAILED",
                    f"backup {backup_path} has {len(fk_rows)} foreign key violation(s)",
                )
            user_version = _classify_legacy(backup)
            tables = _table_names(backup)
            row_counts = {
                table: int(backup.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in tables
            }
            key_ids: list[dict[str, str]] = []
            if "approval_keys" in tables:
                key_ids = [
                    {"workspace": str(row[0]), "key_id": str(row[1])}
                    for row in backup.execute(
                        "SELECT workspace, key_id FROM approval_keys ORDER BY workspace, key_id"
                    )
                ]
            scan = _scan_tags(backup)
            content_digest = _content_digest(backup)
        finally:
            backup.close()

        manifest: dict[str, Any] = {
            "tool_version": TOOL_VERSION,
            "source_path": str(ledger.resolve()),
            "backup_path": str(backup_path),
            "backup_sha256": _sha256_file(backup_path),
            "created_at": _utc_iso(),
            "source_user_version": user_version,
            "row_counts": row_counts,
            "content_digest": content_digest,
            "integrity_check": integrity,
            "foreign_key_check": fk_rows,
            "approval_key_ids": key_ids,
            **scan,
        }
        _write_private_json(Path(f"{backup_path}.manifest.json"), manifest, exclusive=True)
        manifest["manifest_path"] = f"{backup_path}.manifest.json"
        return manifest


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def apply_migration(
    ledger_path: os.PathLike[str] | str,
    manifest_path: os.PathLike[str] | str,
    *,
    confirm_workspace: Optional[str],
    confirm_backup_sha256_prefix: Optional[str],
) -> dict[str, Any]:
    """Pin a legacy ledger to ``confirm_workspace``. Additive DDL only."""

    if confirm_workspace not in _VALID_WORKSPACES:
        raise MigrationRefused(
            "CONFIRMATION_REQUIRED", "--confirm-workspace must be 'uk' or 'india'"
        )
    prefix = (confirm_backup_sha256_prefix or "").strip().lower()
    if len(prefix) < MIN_CONFIRMATION_HEX or any(c not in "0123456789abcdef" for c in prefix):
        raise MigrationRefused(
            "CONFIRMATION_REQUIRED",
            f"confirm the backup with at least {MIN_CONFIRMATION_HEX} hex characters of its sha256",
        )

    ledger = _checked_ledger_path(ledger_path)
    manifest = _load_manifest(manifest_path)
    if Path(str(manifest["source_path"])) != ledger.resolve():
        raise MigrationRefused("STALE_INSPECTION", "manifest was made for a different ledger file")
    backup = _verified_backup(manifest)
    if not str(manifest["backup_sha256"]).startswith(prefix):
        raise MigrationRefused("CONFIRMATION_MISMATCH", "backup hash confirmation does not match")

    backup_connection = _open_ro(backup)
    try:
        integrity, _fk = _integrity(backup_connection)
        if integrity != "ok":
            raise MigrationRefused("INTEGRITY_FAILED", f"backup failed integrity_check: {integrity}")
        scan = _scan_tags(backup_connection)
        backup_version = _classify_legacy(backup_connection)
        backup_digest = _content_digest(backup_connection)
    finally:
        backup_connection.close()
    # The staleness baseline comes from the hash-checked backup. The manifest's
    # copies are not covered by the confirmed sha256, so they only have to agree.
    if (
        manifest["source_user_version"] != backup_version
        or manifest["content_digest"] != backup_digest
    ):
        raise MigrationRefused("STALE_INSPECTION", "manifest does not match the backup")
    # Decide from the backup itself, not from the manifest's say-so.
    if scan["decision_required"] != manifest.get("decision_required"):
        raise MigrationRefused("STALE_INSPECTION", "manifest decision does not match the backup")
    decision = scan["decision_required"]
    if decision == "blocked-conflict":
        raise MigrationRefused(
            "CONFLICTING_TAGS", f"conflicting workspace tags {scan['distinct_workspaces']}"
        )
    if decision == "blocked-unknown":
        raise MigrationRefused(
            "UNKNOWN_TAG", f"unknown workspace tag in {scan['distinct_workspaces']}"
        )
    if decision == "blocked-unparseable":
        raise MigrationRefused(
            "UNPARSEABLE_PAYLOAD", f"unparseable payloads: {scan['unparseable'][:5]}"
        )
    if decision == "confirm-one" and scan["distinct_workspaces"] != [confirm_workspace]:
        raise MigrationRefused(
            "CONFIRMATION_MISMATCH",
            f"ledger content is tagged {scan['distinct_workspaces']}, not {confirm_workspace}",
        )

    with _writer_lock(ledger):
        connection = sqlite3.connect(ledger, isolation_level=None, timeout=5.0)
        try:
            _require_sqlite_capabilities(connection)
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            try:
                identity = _read_identity(connection)
                if (
                    identity.kind != "unpinned"
                    or identity.user_version != backup_version
                    or _content_digest(connection) != backup_digest
                ):
                    raise MigrationRefused(
                        "STALE_INSPECTION", "the ledger changed since it was inspected"
                    )
                _apply_base_schema(connection, identity.user_version)
                _install_identity(
                    connection,
                    Workspace(confirm_workspace),
                    "operator-confirmed-migration",
                    _utc_iso(),
                )
                # Check the pin against the rows on this connection before the
                # commit, so a mixed file is never left pinned.
                distinct = {
                    row[0]
                    for row in connection.execute("SELECT DISTINCT workspace FROM order_intents")
                }
                if not distinct <= {confirm_workspace}:
                    raise MigrationRefused(
                        "CONFLICTING_TAGS",
                        f"order_intents carry {sorted(map(str, distinct))}, pin is {confirm_workspace}",
                    )
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            connection.execute("COMMIT")
        finally:
            connection.close()

        manifest["confirmed_workspace"] = confirm_workspace
        manifest["applied_at"] = _utc_iso()
        try:
            report = _verify(ledger, manifest)
        except MigrationRefused as exc:
            # Still under the writer lock: no backend can open the pinned file
            # before it is rebuilt from the backup.
            restored = _restore_locked(ledger, manifest, backup)
            raise MigrationRefused(
                "VERIFY_FAILED",
                f"{exc.message}; ledger restored from the backup, v6 files kept at {restored['aside']}",
            ) from None
        _write_private_json(Path(manifest_path), manifest, exclusive=False)
    report["status"] = "applied"
    return report


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def verify_migration(
    ledger_path: os.PathLike[str] | str, manifest_path: os.PathLike[str] | str
) -> dict[str, Any]:
    """Prove the pinned ledger still holds every byte the backup held.

    Meant to run right after ``apply``: row counts must equal the backup's, so
    it will report VERIFY_FAILED once the ledger has taken new orders.
    """

    return _verify(_checked_ledger_path(ledger_path), _load_manifest(manifest_path))


def _verify(ledger: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    def fail(check: str, detail: str) -> MigrationRefused:
        return MigrationRefused("VERIFY_FAILED", f"{check}: {detail}")

    backup_path = _verified_backup(manifest)
    checks: list[str] = []
    migrated = _open_ro(ledger)
    original = _open_ro(backup_path)
    try:
        identity = _read_identity(migrated)
        if identity.kind != "pinned" or identity.workspace is None:
            raise fail("pin", f"ledger is {identity.kind}, expected pinned")
        pin = identity.workspace.value
        confirmed = manifest.get("confirmed_workspace")
        if confirmed is not None and confirmed != pin:
            raise fail("pin", f"pinned to {pin} but the confirmation was {confirmed}")
        distinct_tags = manifest.get("distinct_workspaces") or []
        if confirmed is None and len(distinct_tags) == 1 and distinct_tags[0] != pin:
            raise fail("pin", f"pinned to {pin} but every tag in the backup says {distinct_tags[0]}")
        checks.append("pin")

        distinct = {row[0] for row in migrated.execute("SELECT DISTINCT workspace FROM order_intents")}
        if not distinct <= {pin}:
            raise fail("order_intents.workspace", f"found {sorted(map(str, distinct))}, pin is {pin}")
        checks.append("order_intents.workspace")

        for proposal_id, snapshot, digest in migrated.execute(
            "SELECT proposal_id, canonical_json, intent_hash FROM order_intents"
        ):
            if hashlib.sha256(str(snapshot).encode("utf-8")).hexdigest() != digest:
                raise fail("intent_hash", f"hash of canonical_json differs for {proposal_id}")
        checks.append("intent_hash")

        for table in _table_names(original):
            columns = [str(row[1]) for row in original.execute(f"PRAGMA table_info({table})")]
            column_list = ", ".join(f'"{name}"' for name in columns)
            sql = f"SELECT {column_list} FROM {table} ORDER BY rowid"
            before = original.execute(sql).fetchall()
            try:
                after = migrated.execute(sql).fetchall()
            except sqlite3.Error as exc:
                raise fail(f"{table} rows", f"cannot read migrated table: {exc}") from None
            if len(before) != len(after):
                raise fail(
                    f"{table} row count", f"backup has {len(before)}, ledger has {len(after)}"
                )
            if before != after:
                raise fail(f"{table} bytes", "a pre-existing row differs from the backup")
        checks.append("byte-identity")
        checks.append("row-counts")

        integrity, _fk = _integrity(migrated)
        if integrity != "ok":
            raise fail("integrity_check", integrity)
        checks.append("integrity_check")

        version = int(migrated.execute("PRAGMA user_version").fetchone()[0])
        if version != SCHEMA_VERSION:
            raise fail("user_version", f"{version}, expected {SCHEMA_VERSION}")
        checks.append("user_version")
    finally:
        original.close()
        migrated.close()

    try:
        with LedgerReader(ledger, workspace=pin) as reader:
            events = reader.list_events()
    except LedgerError as exc:
        raise fail("LedgerReader", str(exc)) from None
    checks.append("LedgerReader")
    return {
        "status": "verified",
        "workspace": pin,
        "user_version": version,
        "event_count": len(events),
        "checks": checks,
    }


# ---------------------------------------------------------------------------
# restore
# ---------------------------------------------------------------------------


def restore_from_backup(
    ledger_path: os.PathLike[str] | str,
    manifest_path: os.PathLike[str] | str,
    *,
    confirm_restore: bool,
) -> dict[str, Any]:
    """Move the v6 files aside (never delete) and rebuild the verified backup."""

    if not confirm_restore:
        raise MigrationRefused(
            "RESTORE_CONFIRMATION_REQUIRED", "restore needs --confirm-restore"
        )
    ledger = Path(ledger_path)
    if ledger.is_symlink():
        raise MigrationRefused("SYMLINK_REFUSED", "ledger path must not be a symbolic link")
    manifest = _load_manifest(manifest_path)
    if Path(str(manifest["source_path"])) != ledger.resolve():
        raise MigrationRefused("STALE_INSPECTION", "manifest was made for a different ledger file")
    backup = _verified_backup(manifest)

    with _writer_lock(ledger):
        return _restore_locked(ledger, manifest, backup)


def _restore_locked(ledger: Path, manifest: Mapping[str, Any], backup: Path) -> dict[str, Any]:
    """Rebuild the ledger from the backup. The caller holds the writer lock."""

    stamp = _utc_stamp()
    aside: list[str] = []
    for suffix in ("", "-wal", "-shm"):
        current = ledger.with_name(f"{ledger.name}{suffix}")
        if current.exists():
            target = current.with_name(f"{current.name}.v6-aside-{stamp}")
            if target.exists():
                raise MigrationRefused("BACKUP_EXISTS", f"{target} already exists")
            os.rename(current, target)
            aside.append(str(target))

    fd = os.open(ledger, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    source = _open_ro(backup)
    destination = sqlite3.connect(ledger)
    try:
        source.backup(destination)
    finally:
        destination.close()
        source.close()
    os.chmod(ledger, 0o600)

    check = _open_ro(ledger)
    try:
        version = int(check.execute("PRAGMA user_version").fetchone()[0])
        digest = _content_digest(check)
    finally:
        check.close()
    if version != manifest["source_user_version"] or digest != manifest["content_digest"]:
        raise MigrationRefused(
            "VERIFY_FAILED",
            f"restored ledger does not match the backup; v6 files kept at {aside}",
        )
    return {
        "status": "restored",
        "user_version": version,
        "content_digest": digest,
        "aside": aside,
    }


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def show_ledger(
    ledger_path: os.PathLike[str] | str,
    workspace: Workspace | str,
    proposal_id: Optional[str] = None,
) -> dict[str, Any]:
    """Summarise a pinned ledger through ``LedgerReader``: no lock, no private config.

    Raises ``WorkspaceMismatch`` or ``LedgerUnpinned`` (both ``LedgerError``).
    Orders are listed through their events: every registered intent has an
    INTENT_CREATED event.
    """

    with LedgerReader(ledger_path, workspace=workspace) as reader:
        pin = _open_ro(Path(ledger_path))
        try:
            row = pin.execute(
                "SELECT workspace, pinned_at, pinned_by FROM ledger_identity WHERE singleton = 1"
            ).fetchone()
        finally:
            pin.close()
        events = reader.list_events(proposal_id)
        ids = [proposal_id] if proposal_id else sorted({event.proposal_id for event in events})
        orders = []
        for current in ids:
            order = reader.get_order(current)
            if order is None:
                continue
            orders.append(
                {
                    "proposal_id": order.proposal_id,
                    "state": order.state,
                    "intent_hash": order.intent_hash,
                    "created_at": order.created_at,
                    "updated_at": order.updated_at,
                    "event_count": sum(1 for event in events if event.proposal_id == current),
                }
            )
    return {
        "status": "ok",
        "pin": {"workspace": row[0], "pinned_at": row[1], "pinned_by": row[2]},
        "order_count": len(orders),
        "orders": orders,
    }


__all__ = [
    "MigrationRefused",
    "apply_migration",
    "inspect_ledger",
    "restore_from_backup",
    "show_ledger",
    "verify_migration",
]
