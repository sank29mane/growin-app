"""Workspace-pinned, append-only DuckDB store with content-addressed blobs."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence

import duckdb

from .core import PilotDataError, SourceDescriptor, sha256_hex, utc_naive
from .models import QuarantineRecord

SCHEMA_VERSION = 1
DB_FILE_NAME = "pilot_india.duckdb"

_TABLE_RE = re.compile(r"^[a-z_]+$")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_CHECK_RE = re.compile(r"^[a-z_]+$")

_CORE_DDL = (
    "CREATE TABLE IF NOT EXISTS pilot_store_identity("
    "workspace VARCHAR NOT NULL, created_at_utc TIMESTAMP NOT NULL, schema_version INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS source_files("
    "source_sha256 VARCHAR PRIMARY KEY, source VARCHAR NOT NULL, kind VARCHAR NOT NULL, "
    "locator VARCHAR NOT NULL, first_fetched_at_utc TIMESTAMP NOT NULL, byte_size BIGINT NOT NULL, "
    "for_date DATE)",
    "CREATE TABLE IF NOT EXISTS fetch_events("
    "source_sha256 VARCHAR NOT NULL, fetched_at_utc TIMESTAMP NOT NULL, locator VARCHAR NOT NULL)",
    "CREATE TABLE IF NOT EXISTS quarantine_records("
    "quarantine_id VARCHAR PRIMARY KEY, workspace VARCHAR NOT NULL, check_name VARCHAR NOT NULL, "
    "reason_code VARCHAR NOT NULL, scope VARCHAR NOT NULL, isin VARCHAR, nse_symbol VARCHAR, "
    "stock_code VARCHAR, series VARCHAR, date_from DATE, date_to DATE, detail_json VARCHAR NOT NULL, "
    "evidence_json VARCHAR NOT NULL, run_id VARCHAR, recorded_at_utc TIMESTAMP NOT NULL)",
)
_CORE_TABLES = ("pilot_store_identity", "source_files", "fetch_events", "quarantine_records")


@dataclass(frozen=True)
class SourceRef:
    source_sha256: str
    byte_size: int
    new_blob: bool


@dataclass(frozen=True)
class AppendOutcome:
    inserted: int
    identical: int
    conflicts: int


class PilotDataStore:
    """One DuckDB file plus a blob directory, pinned to a single workspace."""

    def __init__(self, root: Path, *, workspace: Literal["india"], read_only: bool = False) -> None:
        if workspace != "india":
            raise PilotDataError("workspace_invalid", "only the india workspace is supported")
        self.workspace: str = workspace
        self.root: Path = Path(root)
        self.read_only: bool = read_only
        self._lock = threading.RLock()
        self._tables: dict[str, tuple[str, ...]] = {}
        db_path = self.root / DB_FILE_NAME
        if read_only:
            if not db_path.exists():
                raise PilotDataError("store_missing", "read-only open needs an existing store")
        else:
            self.root.mkdir(parents=True, exist_ok=True)
        self._con = duckdb.connect(str(db_path), read_only=read_only)
        try:
            self._open_core(read_only)
        except Exception:
            self._con.close()
            raise

    # ------------------------------------------------------------------ lifecycle
    def _open_core(self, read_only: bool) -> None:
        if not read_only:
            for ddl in _CORE_DDL:
                self._con.execute(ddl)
        rows = self._con.execute("SELECT workspace, schema_version FROM pilot_store_identity").fetchall()
        if not rows:
            if read_only:
                raise PilotDataError("store_identity_missing", "store has no identity row")
            self._con.execute(
                "INSERT INTO pilot_store_identity VALUES (?, ?, ?)",
                [self.workspace, utc_naive(datetime.now(timezone.utc)), SCHEMA_VERSION],
            )
        elif len(rows) > 1:
            raise PilotDataError("store_identity_corrupt", "store has more than one identity row")
        elif rows[0][0] != self.workspace:
            raise PilotDataError("store_workspace_mismatch", "store belongs to a different workspace")
        for name in _CORE_TABLES:
            self._tables[name] = ()

    def close(self) -> None:
        with self._lock:
            self._con.close()

    def __enter__(self) -> "PilotDataStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _require_writable(self) -> None:
        if self.read_only:
            raise PilotDataError("store_read_only", "store was opened read-only")

    # ------------------------------------------------------------------ reads
    def query(self, sql: str, params: Sequence[object] = ()) -> list[tuple]:
        head = sql.lstrip().upper()
        if not (head.startswith("SELECT") or head.startswith("WITH")):
            raise PilotDataError("store_query_not_select", "query() accepts SELECT statements only")
        with self._lock:
            return self._con.execute(sql, list(params)).fetchall()

    def table_exists(self, name: str) -> bool:
        rows = self.query(
            "SELECT count(*) FROM information_schema.tables WHERE table_name = ? AND table_schema = 'main'",
            [name],
        )
        return bool(rows[0][0])

    def columns(self, table: str) -> tuple[str, ...]:
        self._check_table_name(table)
        with self._lock:
            info = self._con.execute(f"PRAGMA table_info('{table}')").fetchall()
        return tuple(row[1] for row in info)

    @staticmethod
    def _check_table_name(name: str) -> None:
        if not _TABLE_RE.match(name):
            raise PilotDataError("table_name_invalid", "table names must match ^[a-z_]+$")

    # ------------------------------------------------------------------ tables
    def ensure_table(self, name: str, ddl: str, *, key_columns: tuple[str, ...]) -> None:
        self._check_table_name(name)
        prefix = f"CREATE TABLE IF NOT EXISTS {name}("
        if not ddl.startswith(prefix):
            raise PilotDataError("ddl_invalid", "ddl must start with CREATE TABLE IF NOT EXISTS <name>(")
        with self._lock:
            if self.read_only:
                if not self.table_exists(name):
                    raise PilotDataError("store_read_only", "store was opened read-only")
            else:
                self._con.execute(ddl)
            columns = self.columns(name)
            missing = [col for col in key_columns if col not in columns]
            if missing:
                raise PilotDataError("ddl_invalid", f"key columns missing from {name}: {missing}")
            self._tables[name] = tuple(key_columns)

    # ------------------------------------------------------------------ sources
    def _blob_path(self, digest: str) -> Path:
        return self.root / "blobs" / "sha256" / digest[:2] / digest

    def register_source(self, descriptor: SourceDescriptor, content: bytes) -> SourceRef:
        self._require_writable()
        digest = sha256_hex(content)
        path = self._blob_path(digest)
        with self._lock:
            new_blob = False
            if path.exists():
                if sha256_hex(path.read_bytes()) != digest:
                    raise PilotDataError("blob_integrity", "stored blob no longer matches its hash")
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(content)
                        handle.flush()
                        os.fsync(handle.fileno())
                    os.replace(tmp_name, path)
                except BaseException:
                    if os.path.exists(tmp_name):
                        os.unlink(tmp_name)
                    raise
                os.chmod(path, 0o444)
                new_blob = True
            fetched = utc_naive(descriptor.fetched_at)
            present = self._con.execute(
                "SELECT count(*) FROM source_files WHERE source_sha256 = ?", [digest]
            ).fetchone()[0]
            if not present:
                self._con.execute(
                    "INSERT INTO source_files VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        digest,
                        descriptor.source,
                        descriptor.kind,
                        descriptor.locator,
                        fetched,
                        len(content),
                        descriptor.for_date,
                    ],
                )
            self._con.execute(
                "INSERT INTO fetch_events VALUES (?, ?, ?)", [digest, fetched, descriptor.locator]
            )
        return SourceRef(source_sha256=digest, byte_size=len(content), new_blob=new_blob)

    def read_blob(self, source_sha256: str) -> bytes:
        if not isinstance(source_sha256, str) or not _HASH_RE.match(source_sha256):
            raise PilotDataError("source_hash_invalid", "source hash must be 64 lowercase hex characters")
        path = self._blob_path(source_sha256)
        if not path.exists():
            raise PilotDataError("blob_missing", "no blob stored for that hash")
        data = path.read_bytes()
        if sha256_hex(data) != source_sha256:
            raise PilotDataError("blob_integrity", "stored blob no longer matches its hash")
        return data

    # ------------------------------------------------------------------ quarantine
    def record_quarantine(self, records: Sequence[QuarantineRecord]) -> int:
        self._require_writable()
        inserted = 0
        with self._lock:
            for record in records:
                if record.workspace != self.workspace:
                    raise PilotDataError("workspace_mismatch", "quarantine record workspace differs from the store")
                inserted += self._insert_quarantine(record)
        return inserted

    def _insert_quarantine(self, record: QuarantineRecord) -> int:
        qid = record.quarantine_id
        present = self._con.execute(
            "SELECT count(*) FROM quarantine_records WHERE quarantine_id = ?", [qid]
        ).fetchone()[0]
        if present:
            return 0
        self._con.execute(
            "INSERT INTO quarantine_records VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                qid,
                record.workspace,
                record.check,
                record.reason_code,
                record.scope,
                record.isin,
                record.nse_symbol,
                record.stock_code,
                record.series,
                record.date_from,
                record.date_to,
                json.dumps(record.detail, sort_keys=True),
                json.dumps(list(record.evidence_sha256s)),
                record.run_id,
                utc_naive(datetime.now(timezone.utc)),
            ],
        )
        return 1

    # ------------------------------------------------------------------ append-only rows
    def append_rows(self, table: str, rows: Sequence[Mapping[str, object]], *, check: str) -> AppendOutcome:
        self._require_writable()
        self._check_table_name(table)
        if not _CHECK_RE.match(check):
            raise PilotDataError("check_invalid", "check label must match ^[a-z_]+$")
        with self._lock:
            key_columns = self._tables.get(table)
            if not key_columns:
                raise PilotDataError("table_not_registered", f"table {table} was not registered with ensure_table")
            if not rows:
                return AppendOutcome(0, 0, 0)
            columns = self.columns(table)
            for column in ("row_sha256", "source_sha256"):
                if column not in columns:
                    raise PilotDataError("table_not_appendable", f"table {table} has no {column} column")
            prepared = self._prepare_rows(table, rows, columns, key_columns)
            unique_rows, batch_conflicts = prepared
            return self._append_prepared(table, columns, key_columns, unique_rows, batch_conflicts, check)

    @staticmethod
    def _prepare_rows(
        table: str,
        rows: Sequence[Mapping[str, object]],
        columns: tuple[str, ...],
        key_columns: tuple[str, ...],
    ) -> tuple[list[dict[str, Any]], list[tuple[dict[str, Any], dict[str, Any]]]]:
        groups: dict[tuple, list[dict[str, Any]]] = {}
        order: list[tuple] = []
        for raw in rows:
            row = dict(raw)
            unknown = set(row) - set(columns)
            if unknown:
                raise PilotDataError("append_column_unknown", f"{table}: unknown columns {sorted(unknown)}")
            for needed in ("row_sha256", "source_sha256"):
                if not isinstance(row.get(needed), str) or not row[needed]:
                    raise PilotDataError("append_row_invalid", f"{table}: every row needs {needed}")
            for key in key_columns:
                if row.get(key) is None:
                    raise PilotDataError("append_row_invalid", f"{table}: key column {key} is missing")
            key = tuple(row[col] for col in key_columns)
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(row)
        unique: list[dict[str, Any]] = []
        conflicts: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for key in order:
            group = groups[key]
            distinct = {r["row_sha256"] for r in group}
            if len(distinct) == 1:
                unique.append(group[0])
            else:
                conflicts.append((group[0], next(r for r in group if r["row_sha256"] != group[0]["row_sha256"])))
        return unique, conflicts

    def _append_prepared(
        self,
        table: str,
        columns: tuple[str, ...],
        key_columns: tuple[str, ...],
        unique_rows: list[dict[str, Any]],
        batch_conflicts: list[tuple[dict[str, Any], dict[str, Any]]],
        check: str,
    ) -> AppendOutcome:
        stage = f"_stage_{table}"
        col_list = ", ".join(columns)
        join_on = " AND ".join(f"t.{col} = s.{col}" for col in key_columns)
        con = self._con
        conflict_records: list[QuarantineRecord] = []
        inserted = identical = 0
        con.execute("BEGIN TRANSACTION")
        try:
            con.execute(f"DROP TABLE IF EXISTS {stage}")
            con.execute(f"CREATE TEMP TABLE {stage} AS SELECT * FROM {table} LIMIT 0")
            if unique_rows:
                placeholders = ", ".join("?" for _ in columns)
                con.executemany(
                    f"INSERT INTO {stage} ({col_list}) VALUES ({placeholders})",
                    [[row.get(col) for col in columns] for row in unique_rows],
                )
                key_select = ", ".join(f"s.{col}" for col in key_columns)
                conflict_rows = con.execute(
                    f"SELECT {key_select}, t.row_sha256, s.row_sha256, t.source_sha256, s.source_sha256 "
                    f"FROM {stage} s JOIN {table} t ON {join_on} WHERE t.row_sha256 <> s.row_sha256"
                ).fetchall()
                for found in conflict_rows:
                    n = len(key_columns)
                    key_values = dict(zip(key_columns, found[:n]))
                    old_row, new_row, old_src, new_src = found[n : n + 4]
                    conflict_records.append(
                        self._conflict_record(table, check, key_values, (old_src, new_src), (old_row, new_row))
                    )
                identical = con.execute(
                    f"SELECT count(*) FROM {stage} s JOIN {table} t ON {join_on} WHERE t.row_sha256 = s.row_sha256"
                ).fetchone()[0]
                inserted = con.execute(
                    f"SELECT count(*) FROM {stage} s WHERE NOT EXISTS (SELECT 1 FROM {table} t WHERE {join_on})"
                ).fetchone()[0]
                con.execute(
                    f"INSERT INTO {table} ({col_list}) SELECT {col_list} FROM {stage} s "
                    f"WHERE NOT EXISTS (SELECT 1 FROM {table} t WHERE {join_on})"
                )
            for first, second in batch_conflicts:
                key_values = {col: first[col] for col in key_columns}
                conflict_records.append(
                    self._conflict_record(
                        table,
                        check,
                        key_values,
                        (first["source_sha256"], second["source_sha256"]),
                        (first["row_sha256"], second["row_sha256"]),
                    )
                )
            con.execute(f"DROP TABLE IF EXISTS {stage}")
            for record in conflict_records:
                self._insert_quarantine(record)
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
        return AppendOutcome(inserted=inserted, identical=identical, conflicts=len(conflict_records))

    def _conflict_record(
        self,
        table: str,
        check: str,
        key_values: Mapping[str, object],
        sources: tuple[str, str],
        row_hashes: tuple[str, str],
    ) -> QuarantineRecord:
        detail = {"table": table, "caller_check": check, "row_sha256_existing": str(row_hashes[0]),
                  "row_sha256_new": str(row_hashes[1])}
        for name, value in key_values.items():
            detail[f"key_{name}"] = value.isoformat() if isinstance(value, (date, datetime)) else str(value)

        def pick(name: str) -> str | None:
            value = key_values.get(name)
            return value if isinstance(value, str) else None

        trade_date = key_values.get("trade_date")
        day = trade_date if isinstance(trade_date, date) and not isinstance(trade_date, datetime) else None
        return QuarantineRecord(
            workspace="india",
            check="append_conflict",
            reason_code="conflicting_reingest",
            scope="raw",
            isin=pick("isin"),
            nse_symbol=pick("nse_symbol"),
            stock_code=pick("stock_code"),
            series=pick("series"),
            date_from=day,
            date_to=day,
            detail=detail,
            evidence_sha256s=tuple(str(s) for s in sources),
        )
