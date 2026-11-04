"""CAS class from session-dated NSE security-master snapshots (64-02 Task 2b, D-34).

The closing-auction class of a symbol is a daily flag (``ElgbltyClsgAuctnSsn``) in the NSE file
``NSE_CM_security_DDMMYYYY.csv.gz``. It moved between 28 and 29 Oct 2026, so one table or one open-ended
row is not freshness. This module keeps one operator-saved snapshot per session date, immutable and
content-addressed, derives a per-procedure index from it, and resolves a class only from the snapshot
dated exactly the session date.

Rules, all from D-34 and the operator's 2026-11-04 decision on the four defaults:

* No agent fetches NSE and none imports a real snapshot. The operator saves and imports the file. This
  module has no network code and its tests run under a network sentinel.
* No carry-forward, no nearest date, no default class. A missing, wrong-date or tampered snapshot, an
  unapproved procedure, a non-EQ series, an invalid or mismatched identity, an ambiguous symbol, an
  unknown flag value or conflicting flags is a ``CasUnresolved`` with exactly one sub-reason.
* The procedure (spec plus implementation) is approved once by the operator in the append-only log
  ``costs/schedules/nse_cas_procedure.json``. A version counts only with an approval entry and no
  revocation. The committed log has no entry, so nothing imports or resolves until the operator approves.
* A non-EQ series is ``series_unsupported`` and never borrows the EQ class. The active-row predicate is
  EQ, ``DelFlg`` N, ``ElgbltyNrmlMkt`` 1, ``SctyStsNrmlMkt`` 1 or 6. U-24 (does the file dated D apply to
  session D) is carried as the ``u24_status`` of the approved entry; ``verified`` is what gates QP-2b.
* Resolution reads the stored bytes and the stored index only. It never re-parses the raw file, so an
  approved newer procedure cannot change a recorded result.
* Decision paths (64-04 close, the session script, probe compare) call only ``resolve_broker``.
  ``resolve_symbol`` is for ``show``.

CLI: ``python -m pilot_data.cas_snapshots {procedure,import,reindex,show,check-broker,verify}``.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import inspect
import io
import json
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from costs import cas_class as _cas
from costs.cas_class import (
    CAS,
    NON_CAS,
    CasClassRow,
    CasUnresolved,
    IciciRef,
    ProcedureEntry,
    U24_UNVERIFIED,
    approved_entry,
    entry_sha256,
    load_procedure_log,
)
from market_data import models as _models

from . import security_master as _sm
from .core import PilotDataError, canonical_sha256, sha256_hex
from .nse_calendar import PinnedCalendar, load_pinned_calendar
from .store import PilotDataStore

INDEX_SCHEMA = "growin.pilot_data.cas_index/1"
FILE_NAME_RE = re.compile(r"NSE_CM_security_(\d{2})(\d{2})(\d{4})\.csv\.gz")
MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024

# The procedure spec. Its canonical sha256 is ``spec_sha256``. Every field here is part of what the
# operator approves; a change is a new procedure version.
SPEC: dict[str, Any] = {
    "schema": "growin.pilot_data.cas_snapshot_spec/1",
    "file_name_rule": {
        "pattern": "NSE_CM_security_DDMMYYYY.csv.gz",
        "file_date_rule": "file_date_is_session_date",
    },
    "format": "gzip, utf-8 (BOM allowed), comma csv, one header row, values stripped of surrounding whitespace",
    "columns": {
        "symbol": "TckrSymb",
        "series": "SctySrs",
        "isin": "ISIN",
        "flag": "ElgbltyClsgAuctnSsn",
        "del_flag": "DelFlg",
        "normal_market_eligibility": "ElgbltyNrmlMkt",
        "security_status_normal_market": "SctyStsNrmlMkt",
    },
    "flag_map": {"1": CAS, "0": NON_CAS, "anything_else": "unknown"},
    "active_row_predicate": {
        "series": "EQ",
        "del_flag": "N",
        "normal_market_eligibility": "1",
        "security_status_normal_market": ["1", "6"],
        "match": "exact string match after stripping surrounding whitespace; any other value makes the row inactive",
    },
    "aggregation_order": [
        "parse every row before anything is decided",
        "group rows by ISIN id",
        "relevant rows of a group are its active rows",
        "identity_invalid when the id fails market_data.models.is_valid_isin",
        "flag_unknown when any relevant row has an unknown flag, even beside a usable row",
        "flag_conflict when relevant rows disagree",
        "no_active_row when there are no relevant rows (resolves to identity_mismatch)",
        "otherwise the shared flag is the class",
    ],
    "report": [
        "id counts by sub-reason (groups, and groups with an active row), through the real validator",
        "class counts",
        "ISINs whose class changed since the previous stored session snapshot",
        "required entries that do not resolve, with their sub-reason",
        "ICICI mapped names whose ICICI symbol differs from the active NSE symbol",
        "session dates with more than one ICICI snapshot",
    ],
}

_ACTIVE = SPEC["active_row_predicate"]
_COLUMNS = SPEC["columns"]
_NO_ACTIVE_ROW = "no_active_row"
_GROUP_VERDICTS = ("identity_invalid", "flag_unknown", "flag_conflict", _NO_ACTIVE_ROW)

# Globals the ICICI read functions may touch without being part of the fingerprint: table creation and the
# imported types. Anything else they read must be fingerprinted, or ``test_cas_procedure_fingerprint_covers_
# validator_globals`` fails.
INERT_GLOBALS = frozenset({"ensure_master_tables", "PilotDataError", "SecurityMasterRow", "MasterSnapshotRef"})


class CasSnapshotError(PilotDataError):
    """A refused import, reindex or malformed call. ``code`` is stable and tests assert on it."""


# ---- procedure identity ------------------------------------------------------------------


def spec_sha256() -> str:
    return canonical_sha256(SPEC)


def _source_bytes(module: Any) -> bytes:
    return Path(inspect.getsourcefile(module) or "").read_bytes()


def impl_parts() -> dict[str, Any]:
    """Every input of ``impl_sha256``, by name. Read through the modules at call time so a patch is seen."""
    isin_shape = _models._ISIN_SHAPE
    return {
        "pilot_data/cas_snapshots.py": sha256_hex(_source_bytes(sys.modules[__name__])),
        "costs/cas_class.py": sha256_hex(_source_bytes(_cas)),
        "market_data.models.is_valid_isin": sha256_hex(inspect.getsource(_models.is_valid_isin).encode("utf-8")),
        "market_data.models._ISIN_SHAPE": {"pattern": isin_shape.pattern, "flags": int(isin_shape.flags)},
        "security_master.snapshot_on": sha256_hex(inspect.getsource(_sm.snapshot_on).encode("utf-8")),
        "security_master.snapshot_by_sha": sha256_hex(inspect.getsource(_sm.snapshot_by_sha).encode("utf-8")),
        "security_master.lookup_stock_code": sha256_hex(inspect.getsource(_sm.lookup_stock_code).encode("utf-8")),
        "security_master._row_from_db": sha256_hex(inspect.getsource(_sm._row_from_db).encode("utf-8")),
        "security_master._snapshot_from_db": sha256_hex(inspect.getsource(_sm._snapshot_from_db).encode("utf-8")),
        "security_master._ROW_COLUMNS": _sm._ROW_COLUMNS,
    }


def impl_sha256() -> str:
    return canonical_sha256(impl_parts())


def fingerprinted_functions() -> dict[str, Callable[..., Any]]:
    """The functions whose source is in the fingerprint, keyed by the part name; the walk test reads this."""
    return {
        "market_data.models.is_valid_isin": _models.is_valid_isin,
        "security_master.snapshot_on": _sm.snapshot_on,
        "security_master.snapshot_by_sha": _sm.snapshot_by_sha,
        "security_master.lookup_stock_code": _sm.lookup_stock_code,
        "security_master._row_from_db": _sm._row_from_db,
        "security_master._snapshot_from_db": _sm._snapshot_from_db,
    }


def running_identity() -> tuple[str, str]:
    return spec_sha256(), impl_sha256()


# ---- ICICI pin ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IciciPin:
    """Which ICICI master snapshot a resolution reads. Required; there is no default and no latest fallback."""

    kind: str
    snapshot_date: date | None = None
    snapshot_sha256: str | None = None
    row_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.kind == "select":
            if not isinstance(self.snapshot_date, date) or isinstance(self.snapshot_date, datetime):
                raise CasSnapshotError("icici_pin_invalid", "select pin needs a datetime.date")
            if self.snapshot_sha256 is not None or self.row_sha256 is not None:
                raise CasSnapshotError("icici_pin_invalid", "select pin carries no hashes")
        elif self.kind == "recorded":
            for value in (self.snapshot_sha256, self.row_sha256):
                if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                    raise CasSnapshotError("icici_pin_invalid", "recorded pin needs two sha256 values")
            if self.snapshot_date is not None:
                raise CasSnapshotError("icici_pin_invalid", "recorded pin takes its date from the session date")
        else:
            raise CasSnapshotError("icici_pin_invalid", f"unknown pin kind {self.kind!r}")

    @classmethod
    def select(cls, session_date: date) -> "IciciPin":
        """Close and the probe: the ICICI snapshot dated exactly ``session_date``; its sha256 becomes ``icici_ref``."""
        return cls("select", snapshot_date=session_date)

    @classmethod
    def recorded(cls, snapshot_sha256: str, row_sha256: str) -> "IciciPin":
        """Replay and acceptance: the snapshot and row recorded earlier, selected on (sha256, session date)."""
        return cls("recorded", snapshot_sha256=snapshot_sha256, row_sha256=row_sha256)


# ---- parsing and the index ---------------------------------------------------------------


@dataclass(frozen=True)
class SnapshotRow:
    symbol: str
    series: str
    isin: str
    flag: str
    del_flag: str
    eligibility: str
    status: str

    @property
    def active(self) -> bool:
        return (
            self.series == _ACTIVE["series"]
            and self.del_flag == _ACTIVE["del_flag"]
            and self.eligibility == _ACTIVE["normal_market_eligibility"]
            and self.status in _ACTIVE["security_status_normal_market"]
        )

    def evidence(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "series": self.series,
            "flag": self.flag,
            "del_flag": self.del_flag,
            "eligibility": self.eligibility,
            "status": self.status,
            "active": self.active,
        }


def file_date(name: str) -> date | None:
    """The date in ``NSE_CM_security_DDMMYYYY.csv.gz``, or None when the name does not follow the rule."""
    match = FILE_NAME_RE.fullmatch(name)
    if match is None:
        return None
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def parse_snapshot(raw: bytes) -> list[SnapshotRow]:
    """Decompress and parse every row. Anything malformed refuses the whole file; nothing is skipped."""
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as handle:
            data = handle.read(MAX_UNCOMPRESSED_BYTES + 1)
    except (OSError, EOFError) as exc:
        raise CasSnapshotError("snapshot_not_gzip", "the snapshot is not a readable gzip file") from exc
    if len(data) > MAX_UNCOMPRESSED_BYTES:
        raise CasSnapshotError("snapshot_too_large", "the snapshot expands beyond the size limit")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise CasSnapshotError("snapshot_not_utf8", "the snapshot is not valid UTF-8") from exc
    reader = csv.reader(io.StringIO(text))
    try:
        header = [name.strip() for name in next(reader)]
    except StopIteration as exc:
        raise CasSnapshotError("snapshot_empty", "the snapshot has no header") from exc
    positions: dict[str, int] = {}
    for key, column in _COLUMNS.items():
        found = [n for n, name in enumerate(header) if name == column]
        if len(found) != 1:
            raise CasSnapshotError("snapshot_columns", f"column {column} must appear exactly once in the header")
        positions[key] = found[0]
    rows: list[SnapshotRow] = []
    for line, fields in enumerate(reader, start=2):
        if not fields:
            continue
        if len(fields) != len(header):
            raise CasSnapshotError("snapshot_row_width", f"line {line} has {len(fields)} fields, expected {len(header)}")
        cell = {key: fields[position].strip() for key, position in positions.items()}
        rows.append(
            SnapshotRow(
                symbol=cell["symbol"], series=cell["series"], isin=cell["isin"], flag=cell["flag"],
                del_flag=cell["del_flag"], eligibility=cell["normal_market_eligibility"],
                status=cell["security_status_normal_market"],
            )
        )
    if not rows:
        raise CasSnapshotError("snapshot_empty", "the snapshot has no data rows")
    return rows


def _group_verdict(isin: str, rows: Sequence[SnapshotRow]) -> tuple[str | None, str | None]:
    """(class, sub-reason) for one ISIN group, in the order the spec fixes."""
    if not _models.is_valid_isin(isin):
        return None, "identity_invalid"
    relevant = [row for row in rows if row.active]
    flags = [SPEC["flag_map"].get(row.flag) for row in relevant]
    if any(flag is None for flag in flags):
        return None, "flag_unknown"
    if len(set(flags)) > 1:
        return None, "flag_conflict"
    if not relevant:
        return None, _NO_ACTIVE_ROW
    return flags[0], None


def build_index(
    rows: Sequence[SnapshotRow], *, session_date: date, source_file_name: str, raw_sha256: str, entry: ProcedureEntry
) -> dict[str, Any]:
    groups: dict[str, list[SnapshotRow]] = {}
    for row in rows:
        groups.setdefault(row.isin, []).append(row)
    isins: dict[str, Any] = {}
    symbols: dict[str, set[str]] = {}
    for isin, members in groups.items():
        cas_class, sub_reason = _group_verdict(isin, members)
        isins[isin] = {"cas_class": cas_class, "sub_reason": sub_reason, "rows": [row.evidence() for row in members]}
        for row in members:
            if row.active:
                symbols.setdefault(row.symbol, set()).add(isin)
    return {
        "schema": INDEX_SCHEMA,
        "session_date": session_date.isoformat(),
        "source_file_name": source_file_name,
        "raw_sha256": raw_sha256,
        "row_count": len(rows),
        "procedure": entry.as_dict(),
        "isins": isins,
        "symbols": {symbol: sorted(values) for symbol, values in symbols.items()},
    }


def index_bytes(index: Mapping[str, Any]) -> bytes:
    return json.dumps(index, sort_keys=True, separators=(",", ":")).encode("utf-8")


# ---- the store ---------------------------------------------------------------------------


def cas_dir(root: Path, session_date: date) -> Path:
    return Path(root) / "cas" / session_date.isoformat()


def _write_new(path: Path, data: bytes) -> bool:
    """Create ``path`` with ``data`` without ever replacing a file. True when written, False for identical bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(data)
    try:
        os.link(temporary, path)
        return True
    except FileExistsError:
        if path.read_bytes() == data:
            return False
        raise CasSnapshotError("replace_refused", f"{path.name} already exists with different bytes") from None
    finally:
        temporary.unlink(missing_ok=True)


def _index_files(directory: Path, version: int) -> list[Path]:
    folder = directory / "index"
    pattern = re.compile(rf"v{version}-([0-9a-f]{{64}})\.json")
    return sorted(path for path in folder.glob("*.json") if pattern.fullmatch(path.name)) if folder.is_dir() else []


def _raw_files(directory: Path) -> list[Path]:
    folder = directory / "raw"
    pattern = re.compile(r"[0-9a-f]{64}\.csv\.gz")
    return sorted(path for path in folder.glob("*.csv.gz") if pattern.fullmatch(path.name)) if folder.is_dir() else []


def _require_running_procedure(version: int) -> ProcedureEntry:
    """Import and reindex: the version must be approved, unrevoked and equal to what is running."""
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise CasSnapshotError("procedure_version_required", "a procedure version is required, there is no default")
    try:
        log = load_procedure_log()
    except (ValueError, OSError) as exc:
        raise CasSnapshotError("procedure_file_invalid", "the procedure approval log does not load") from exc
    entry = approved_entry(log, version)
    if entry is None:
        raise CasSnapshotError("procedure_unapproved", f"procedure version {version} has no approval, or it is revoked")
    spec, impl = running_identity()
    if entry.spec_sha256 != spec or entry.impl_sha256 != impl:
        raise CasSnapshotError(
            "procedure_identity_differs", f"procedure version {version} approves a different spec or implementation"
        )
    return entry


@dataclass(frozen=True)
class ImportOutcome:
    session_date: date
    procedure_version: int
    raw_sha256: str
    index_sha256: str
    raw_written: bool
    index_written: bool
    report: dict[str, Any]
    report_path: Path


def _calendar_says_session(calendar: PinnedCalendar | None, day: date) -> bool:
    if calendar is None or not calendar.covers(day):
        return False
    if calendar.is_special(day):
        return True
    return not (calendar.is_weekend(day) or calendar.is_holiday(day))


def import_snapshot(
    root: Path,
    file: Path,
    session_date: date,
    procedure_version: int,
    *,
    calendar: PinnedCalendar | None,
    required: Sequence[Mapping[str, str]] = (),
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> ImportOutcome:
    """Store one operator-saved snapshot and its index under an approved procedure version.

    Refused, storing nothing: no approved version; a file name whose date is not ``session_date``; a date that is not a
    session on the pinned calendar; a malformed file; different bytes for a stored date; a second index for a stored
    (date, version).
    """
    entry = _require_running_procedure(procedure_version)
    if not isinstance(session_date, date) or isinstance(session_date, datetime):
        raise CasSnapshotError("session_date_invalid", "session_date must be a datetime.date")
    source = Path(file)
    named = file_date(source.name)
    if named is None or named != session_date:
        raise CasSnapshotError("file_name_date_mismatch", "the file name must be NSE_CM_security_DDMMYYYY.csv.gz for the session date")
    if not _calendar_says_session(calendar, session_date):
        raise CasSnapshotError("not_a_session", "the session date is not a session on the pinned calendar")
    raw = source.read_bytes()
    rows = parse_snapshot(raw)
    raw_sha = sha256_hex(raw)
    index = build_index(rows, session_date=session_date, source_file_name=source.name, raw_sha256=raw_sha, entry=entry)
    payload = index_bytes(index)
    index_sha = sha256_hex(payload)
    directory = cas_dir(root, session_date)
    for existing in _raw_files(directory):
        if existing.name != f"{raw_sha}.csv.gz":
            raise CasSnapshotError("reimport_different_bytes_refused", "a different snapshot is already stored for this date")
    for existing in _index_files(directory, procedure_version):
        if existing.name != f"v{procedure_version}-{index_sha}.json":
            raise CasSnapshotError("index_replace_refused", "a different index is already stored for this date and version")
    raw_written = _write_new(directory / "raw" / f"{raw_sha}.csv.gz", raw)
    index_written = _write_new(directory / "index" / f"v{procedure_version}-{index_sha}.json", payload)
    log_line = {
        "imported_at_utc": clock().astimezone(timezone.utc).isoformat(),
        "procedure_version": procedure_version,
        "raw_sha256": raw_sha,
        "index_sha256": index_sha,
        "outcome": "stored" if (raw_written or index_written) else "noop",
    }
    with open(directory / "imports.jsonl", "a", encoding="utf-8") as handle:
        handle.write(json.dumps(log_line, sort_keys=True) + "\n")
    report = build_report(root, session_date, procedure_version, index, index_sha, required)
    report_path = directory / f"report-v{procedure_version}.json"
    report_path.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return ImportOutcome(session_date, procedure_version, raw_sha, index_sha, raw_written, index_written, report, report_path)


def reindex(
    root: Path,
    session_date: date,
    procedure_version: int,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> ImportOutcome:
    """Re-process the stored raw bytes under a newer approved version. The older index is never touched."""
    entry = _require_running_procedure(procedure_version)
    directory = cas_dir(root, session_date)
    stored = _raw_files(directory)
    if len(stored) != 1:
        raise CasSnapshotError("snapshot_missing", "exactly one stored raw snapshot is needed to reindex")
    raw = stored[0].read_bytes()
    raw_sha = sha256_hex(raw)
    if stored[0].name != f"{raw_sha}.csv.gz":
        raise CasSnapshotError("snapshot_hash_mismatch", "the stored raw bytes no longer match their hash")
    named = file_date(_source_name_of(directory) or "")
    if named != session_date:
        raise CasSnapshotError("file_name_date_mismatch", "the stored source file name does not carry the session date")
    index = build_index(
        parse_snapshot(raw), session_date=session_date, source_file_name=_source_name_of(directory) or "",
        raw_sha256=raw_sha, entry=entry,
    )
    payload = index_bytes(index)
    index_sha = sha256_hex(payload)
    for existing in _index_files(directory, procedure_version):
        if existing.name != f"v{procedure_version}-{index_sha}.json":
            raise CasSnapshotError("index_replace_refused", "a different index is already stored for this date and version")
    index_written = _write_new(directory / "index" / f"v{procedure_version}-{index_sha}.json", payload)
    with open(directory / "imports.jsonl", "a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "imported_at_utc": clock().astimezone(timezone.utc).isoformat(),
                    "procedure_version": procedure_version,
                    "raw_sha256": raw_sha,
                    "index_sha256": index_sha,
                    "outcome": "reindexed" if index_written else "noop",
                },
                sort_keys=True,
            )
            + "\n"
        )
    report = build_report(root, session_date, procedure_version, index, index_sha, ())
    report_path = directory / f"report-v{procedure_version}.json"
    report_path.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return ImportOutcome(session_date, procedure_version, raw_sha, index_sha, False, index_written, report, report_path)


def _source_name_of(directory: Path) -> str | None:
    """The source file name recorded in any stored index of this date (every index records the same one)."""
    folder = directory / "index"
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            name = json.loads(path.read_bytes()).get("source_file_name")
        except ValueError:
            continue
        if isinstance(name, str):
            return name
    return None


# ---- the exception and change report -----------------------------------------------------


def _previous_index(root: Path, session_date: date, version: int) -> dict[str, Any] | None:
    cas_root = Path(root) / "cas"
    candidates: list[date] = []
    for child in cas_root.iterdir() if cas_root.is_dir() else []:
        try:
            day = date.fromisoformat(child.name)
        except ValueError:
            continue
        if day < session_date and _index_files(child, version):
            candidates.append(day)
    for day in sorted(candidates, reverse=True):
        files = _index_files(cas_dir(root, day), version)
        if len(files) == 1 and sha256_hex(files[0].read_bytes()) == files[0].name[len(f"v{version}-") : -len(".json")]:
            try:
                return json.loads(files[0].read_bytes())
            except ValueError:
                return None
    return None


def _icici_report(root: Path, session_date: date, index: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"master": "icici_master_missing", "disagreements": [], "multi_snapshot_dates": []}
    try:
        store = PilotDataStore(Path(root), workspace="india", read_only=True)
    except PilotDataError:
        return out
    with store:
        try:
            snapshot = _sm.snapshot_on(store, session_date)
            dated = store.query(
                "SELECT snapshot_date FROM security_master_snapshots GROUP BY snapshot_date HAVING count(*) > 1 "
                "ORDER BY snapshot_date"
            )
        except PilotDataError:
            return out
        out["multi_snapshot_dates"] = [row[0].isoformat() for row in dated]
        if snapshot is None:
            return out
        out["master"] = snapshot.snapshot_sha256
        for row in _sm.master_rows(store, snapshot):
            if row.series != "EQ" or row.token == 0:
                continue
            group = index["isins"].get(row.isin)
            if group is None:
                continue
            active = {entry["symbol"] for entry in group["rows"] if entry["active"]}
            if active and row.nse_symbol not in active:
                out["disagreements"].append(
                    {"stock_code": row.stock_code, "icici_symbol": row.nse_symbol, "isin": row.isin,
                     "nse_active_symbols": sorted(active)}
                )
    return out


def build_report(
    root: Path, session_date: date, version: int, index: Mapping[str, Any], index_sha: str,
    required: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    id_counts: dict[str, dict[str, int]] = {}
    class_counts = {CAS: 0, NON_CAS: 0}
    for group in index["isins"].values():
        has_active = any(row["active"] for row in group["rows"])
        if group["sub_reason"] is not None:
            slot = id_counts.setdefault(group["sub_reason"], {"groups": 0, "with_active_rows": 0})
            slot["groups"] += 1
            slot["with_active_rows"] += 1 if has_active else 0
        elif group["cas_class"] is not None:
            class_counts[group["cas_class"]] += 1
    previous = _previous_index(root, session_date, version)
    changed: list[dict[str, Any]] = []
    if previous is not None:
        for isin, group in sorted(index["isins"].items()):
            before = previous["isins"].get(isin)
            if before is not None and before["cas_class"] != group["cas_class"] and (
                before["cas_class"] is not None or group["cas_class"] is not None
            ):
                changed.append({"isin": isin, "from": before["cas_class"], "to": group["cas_class"]})
    unresolved: list[dict[str, Any]] = []
    for item in required:
        verdict = resolve_broker(root, session_date, version, item["stock_code"], IciciPin.select(session_date))
        if isinstance(verdict, CasUnresolved):
            unresolved.append({**dict(item), "sub_reason": verdict.sub_reason, "detail": verdict.detail})
        elif (verdict.isin, verdict.nse_symbol) != (item["isin"], item["nse_symbol"]):
            unresolved.append({**dict(item), "sub_reason": "identity_mismatch", "detail": "required_row_disagrees"})
    return {
        "session_date": session_date.isoformat(),
        "procedure_version": version,
        "raw_sha256": index["raw_sha256"],
        "index_sha256": index_sha,
        "id_counts": id_counts,
        "class_counts": class_counts,
        "previous_snapshot_compared": previous is not None and previous["session_date"],
        "class_changes": changed,
        "required_total": len(required),
        "required_unresolved": unresolved,
        "icici": _icici_report(root, session_date, index),
    }


# ---- resolution --------------------------------------------------------------------------


@dataclass(frozen=True)
class _CasState:
    missing: str | None = None
    wrong_date: str | None = None
    hash_bad: str | None = None
    raw_sha: str | None = None
    index_sha: str | None = None
    index: dict[str, Any] | None = None


def _read_cas(root: Path, session_date: date, version: int) -> _CasState:
    directory = cas_dir(root, session_date)
    raws = _raw_files(directory)
    if not raws:
        return _CasState(missing="no_snapshot_for_date")
    indexes = _index_files(directory, version)
    if not indexes:
        return _CasState(missing="index_missing_for_procedure")
    hash_bad: str | None = None
    if len(raws) != 1:
        hash_bad = "more_than_one_raw_snapshot_for_date"
    if len(indexes) != 1 and hash_bad is None:
        hash_bad = "more_than_one_index_for_version"
    raw_path, index_path = raws[0], indexes[0]
    try:
        raw_data, index_data = raw_path.read_bytes(), index_path.read_bytes()
    except OSError:
        return _CasState(missing="stored_file_unreadable")
    raw_sha, index_sha = sha256_hex(raw_data), sha256_hex(index_data)
    try:
        parsed = json.loads(index_data)
        if not isinstance(parsed, dict):
            raise ValueError("index is not an object")
    except ValueError:
        return _CasState(hash_bad="index_unreadable", raw_sha=raw_sha, index_sha=index_sha)
    named = file_date(str(parsed.get("source_file_name", "")))
    if parsed.get("session_date") != session_date.isoformat() or named != session_date:
        return _CasState(
            wrong_date="stored_entry_is_for_another_date", raw_sha=raw_sha, index_sha=index_sha, index=parsed
        )
    if hash_bad is None and raw_path.name != f"{raw_sha}.csv.gz":
        hash_bad = "raw_bytes_do_not_match_their_hash"
    if hash_bad is None and index_path.name != f"v{version}-{index_sha}.json":
        hash_bad = "index_bytes_do_not_match_their_hash"
    if hash_bad is None and parsed.get("raw_sha256") != raw_sha:
        hash_bad = "index_names_a_different_raw_snapshot"
    return _CasState(hash_bad=hash_bad, raw_sha=raw_sha, index_sha=index_sha, index=parsed)


@dataclass(frozen=True)
class _IciciState:
    missing: str | None = None
    wrong_date: str | None = None
    hash_bad: str | None = None
    snapshot: _sm.MasterSnapshotRef | None = None
    row: Any = None
    row_sha: str | None = None
    ambiguous: bool = False


def _read_icici(root: Path, session_date: date, stock_code: str, pin: IciciPin) -> _IciciState:
    if pin.kind == "select" and pin.snapshot_date != session_date:
        return _IciciState(wrong_date="icici_pin_date_mismatch")
    try:
        store = PilotDataStore(Path(root), workspace="india", read_only=True)
    except PilotDataError as exc:
        if exc.code in ("store_missing", "store_identity_missing"):
            return _IciciState(missing="icici_master_missing")
        return _IciciState(hash_bad=f"icici_store_{exc.code}")
    with store:
        try:
            if pin.kind == "select":
                snapshot = _sm.snapshot_on(store, session_date)
            else:
                assert pin.snapshot_sha256 is not None
                snapshot = _sm.snapshot_by_sha(store, pin.snapshot_sha256, session_date)
        except PilotDataError as exc:
            if exc.code == "store_read_only":
                return _IciciState(missing="icici_master_missing")
            return _IciciState(hash_bad=f"icici_store_{exc.code}")
        if snapshot is None:
            return _IciciState(missing="icici_master_missing")
        try:
            store.read_blob(snapshot.snapshot_sha256)
        except PilotDataError as exc:
            return _IciciState(snapshot=snapshot, hash_bad=f"icici_blob_{exc.code}")
        try:
            row = _sm.lookup_stock_code(store, snapshot, stock_code)
        except PilotDataError:
            return _IciciState(snapshot=snapshot, ambiguous=True)
        if row is None:
            return _IciciState(snapshot=snapshot)
        stored = store.query(
            "SELECT row_sha256 FROM security_master_rows WHERE snapshot_sha256 = ? AND stock_code = ? AND series = ?",
            [snapshot.snapshot_sha256, row.stock_code, row.series],
        )
        recomputed = row.row_sha256()
        if len(stored) != 1 or stored[0][0] != recomputed:
            return _IciciState(snapshot=snapshot, hash_bad="icici_row_does_not_match_its_hash")
        if pin.kind == "recorded" and pin.row_sha256 != recomputed:
            return _IciciState(snapshot=snapshot, hash_bad="icici_row_differs_from_recorded")
        return _IciciState(snapshot=snapshot, row=row, row_sha=recomputed)


def _unresolved(
    sub_reason: str, detail: str, session_date: date, version: int, cas: _CasState | None = None,
    entry: ProcedureEntry | None = None, stock_code: str | None = None, icici: _IciciState | None = None,
) -> CasUnresolved:
    ref = None
    if icici is not None and icici.snapshot is not None and icici.row_sha is not None:
        ref = IciciRef(icici.snapshot.snapshot_date, icici.snapshot.snapshot_sha256, icici.row_sha)
    return CasUnresolved(
        sub_reason=sub_reason,
        detail=detail,
        session_date=session_date,
        procedure_version=version,
        snapshot_sha256=None if cas is None else cas.raw_sha,
        index_sha256=None if cas is None else cas.index_sha,
        spec_sha256=None if entry is None else entry.spec_sha256,
        impl_sha256=None if entry is None else entry.impl_sha256,
        u24_status=None if entry is None else entry.u24_status,
        u24_evidence_ref=None if entry is None else entry.u24_evidence_ref,
        stock_code=stock_code,
        icici_ref=ref,
    )


def _check_procedure(version: int, cas: _CasState) -> tuple[ProcedureEntry | None, str | None]:
    """(entry, detail). A detail means procedure_unapproved."""
    try:
        log = load_procedure_log()
    except (ValueError, OSError):
        return None, "procedure_file_invalid"
    entry = approved_entry(log, version)
    if entry is None:
        return None, "procedure_version_revoked" if version in log.revoked else "procedure_version_absent"
    assert cas.index is not None
    if cas.index.get("procedure") != entry.as_dict():
        return None, "index_identity_differs_from_entry"
    if impl_sha256() != entry.impl_sha256:
        return None, "running_implementation_differs_from_entry"
    return entry, None


def _check_inputs(session_date: date, version: int) -> None:
    if not isinstance(session_date, date) or isinstance(session_date, datetime):
        raise CasSnapshotError("session_date_invalid", "session_date must be a datetime.date")
    if isinstance(version, bool) or not isinstance(version, int):
        raise CasSnapshotError("procedure_version_required", "a procedure version is required, there is no default")


def _group_row(
    index: Mapping[str, Any], isin: str, symbol: str
) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
    group = index["isins"].get(isin)
    if group is None:
        return None, None
    for evidence in group["rows"]:
        if evidence["active"] and evidence["symbol"] == symbol:
            return group, evidence
    return group, None


def _group_result(
    group: Mapping[str, Any], *, isin: str, nse_symbol: str, series: str, session_date: date, version: int,
    cas: _CasState, entry: ProcedureEntry, stock_code: str | None, icici: _IciciState | None,
) -> CasClassRow | CasUnresolved:
    if group["sub_reason"] in ("flag_unknown", "flag_conflict"):
        return _unresolved(group["sub_reason"], f"{group['sub_reason']}_for_isin", session_date, version, cas, entry, stock_code, icici)
    if group["sub_reason"] == "identity_invalid":
        return _unresolved("identity_invalid", "isin_fails_validator", session_date, version, cas, entry, stock_code, icici)
    if group["cas_class"] is None:
        return _unresolved("identity_mismatch", _NO_ACTIVE_ROW, session_date, version, cas, entry, stock_code, icici)
    assert cas.raw_sha is not None and cas.index_sha is not None
    ref = None
    if icici is not None and icici.snapshot is not None and icici.row_sha is not None:
        ref = IciciRef(icici.snapshot.snapshot_date, icici.snapshot.snapshot_sha256, icici.row_sha)
    return CasClassRow(
        isin=isin, nse_symbol=nse_symbol, series=series, cas_class=group["cas_class"], snapshot_date=session_date,
        snapshot_sha256=cas.raw_sha, index_sha256=cas.index_sha, procedure_version=version,
        spec_sha256=entry.spec_sha256, impl_sha256=entry.impl_sha256, u24_status=entry.u24_status,
        u24_evidence_ref=entry.u24_evidence_ref, stock_code=stock_code, icici_ref=ref,
    )


def resolve_broker(
    root: Path, session_date: date, procedure_version: int, stock_code: str, icici_pin: IciciPin
) -> CasClassRow | CasUnresolved:
    """Class of a Breeze ``stock_code`` on ``session_date``, or a typed verdict with one sub-reason.

    The code maps through one ICICI master snapshot fixed by ``icici_pin`` and is then cross-checked against the
    same-date NSE snapshot's active EQ row (ISIN, symbol, series), so the check cannot be skipped. Sub-reasons are
    decided in this order, first match wins: snapshot_missing, snapshot_wrong_date, snapshot_hash_mismatch,
    procedure_unapproved, series_unsupported, identity_invalid, identity_mismatch, identity_ambiguous, flag_unknown,
    flag_conflict.
    """
    _check_inputs(session_date, procedure_version)
    if not isinstance(icici_pin, IciciPin):
        raise CasSnapshotError("icici_pin_required", "an IciciPin is required, there is no default")
    if not isinstance(stock_code, str) or not stock_code.strip():
        raise CasSnapshotError("stock_code_invalid", "stock_code must be a non-empty string")
    version = procedure_version
    cas = _read_cas(root, session_date, version)
    icici = _read_icici(root, session_date, stock_code, icici_pin)

    def stop(reason: str, detail: str, entry: ProcedureEntry | None = None) -> CasUnresolved:
        return _unresolved(reason, detail, session_date, version, cas, entry, stock_code, icici)

    if cas.missing is not None:
        return stop("snapshot_missing", cas.missing)
    if icici.missing is not None:
        return stop("snapshot_missing", icici.missing)
    if cas.wrong_date is not None:
        return stop("snapshot_wrong_date", cas.wrong_date)
    if icici.wrong_date is not None:
        return stop("snapshot_wrong_date", icici.wrong_date)
    if cas.hash_bad is not None:
        return stop("snapshot_hash_mismatch", cas.hash_bad)
    if icici.hash_bad is not None:
        return stop("snapshot_hash_mismatch", icici.hash_bad)
    entry, why = _check_procedure(version, cas)
    if why is not None:
        return stop("procedure_unapproved", why)
    assert entry is not None and cas.index is not None
    if icici.ambiguous:
        return stop("identity_ambiguous", "stock_code_ambiguous", entry)
    if icici.row is None:
        return stop("identity_mismatch", "icici_unmapped", entry)
    row = icici.row
    if row.series != "EQ":
        return stop("series_unsupported", f"icici_series_{row.series}", entry)
    if not _models.is_valid_isin(row.isin):
        return stop("identity_invalid", "icici_isin_fails_validator", entry)
    group, active_row = _group_row(cas.index, row.isin, row.nse_symbol)
    if group is not None and group["sub_reason"] == "identity_invalid":
        return stop("identity_invalid", "isin_fails_validator", entry)
    if group is None or active_row is None:
        return stop("identity_mismatch", _NO_ACTIVE_ROW if group is not None else "isin_not_in_snapshot", entry)
    if len(cas.index["symbols"].get(row.nse_symbol, ())) > 1:
        return stop("identity_ambiguous", "symbol_carries_more_than_one_isin", entry)
    return _group_result(
        group, isin=row.isin, nse_symbol=row.nse_symbol, series=row.series, session_date=session_date, version=version,
        cas=cas, entry=entry, stock_code=stock_code, icici=icici,
    )


def resolve_symbol(root: Path, session_date: date, procedure_version: int, nse_symbol: str) -> CasClassRow | CasUnresolved:
    """Display only (``show``). Decision paths use ``resolve_broker``."""
    _check_inputs(session_date, procedure_version)
    if not isinstance(nse_symbol, str) or not nse_symbol.strip():
        raise CasSnapshotError("symbol_invalid", "nse_symbol must be a non-empty string")
    version = procedure_version
    cas = _read_cas(root, session_date, version)
    if cas.missing is not None:
        return _unresolved("snapshot_missing", cas.missing, session_date, version, cas)
    if cas.wrong_date is not None:
        return _unresolved("snapshot_wrong_date", cas.wrong_date, session_date, version, cas)
    if cas.hash_bad is not None:
        return _unresolved("snapshot_hash_mismatch", cas.hash_bad, session_date, version, cas)
    entry, why = _check_procedure(version, cas)
    if why is not None:
        return _unresolved("procedure_unapproved", why, session_date, version, cas)
    assert entry is not None and cas.index is not None
    isins = cas.index["symbols"].get(nse_symbol, [])
    if not isins:
        return _unresolved("identity_mismatch", "symbol_has_no_active_row", session_date, version, cas, entry)
    if len(isins) > 1:
        return _unresolved("identity_ambiguous", "symbol_carries_more_than_one_isin", session_date, version, cas, entry)
    isin = isins[0]
    return _group_result(
        cas.index["isins"][isin], isin=isin, nse_symbol=nse_symbol, series="EQ", session_date=session_date,
        version=version, cas=cas, entry=entry, stock_code=None, icici=None,
    )


def verify_stored(root: Path, session_date: date, procedure_version: int) -> list[str]:
    """Re-hash the stored raw bytes and index. Problems are returned as short codes; empty means intact."""
    state = _read_cas(root, session_date, procedure_version)
    return [part for part in (state.missing, state.wrong_date, state.hash_bad) if part]


def describe(verdict: CasClassRow | CasUnresolved) -> str:
    """One text form of a verdict for ``check-broker`` and ``probe-intraday compare``, so the two always agree."""
    if isinstance(verdict, CasUnresolved):
        return f"session_scope_unknown {verdict.sub_reason} {verdict.detail}"
    pin = (
        "none" if verdict.icici_ref is None
        else f"{verdict.icici_ref.snapshot_date.isoformat()}:{verdict.icici_ref.snapshot_sha256}:{verdict.icici_ref.row_sha256}"
    )
    return (
        f"class {verdict.cas_class} nse_symbol {verdict.nse_symbol} isin {verdict.isin} series {verdict.series} "
        f"snapshot_date {verdict.snapshot_date.isoformat()} snapshot_sha256 {verdict.snapshot_sha256} "
        f"index_sha256 {verdict.index_sha256} procedure_version {verdict.procedure_version} "
        f"spec_sha256 {verdict.spec_sha256} impl_sha256 {verdict.impl_sha256} u24_status {verdict.u24_status} "
        f"icici_pin {pin}"
    )


# ---- command line ------------------------------------------------------------------------


def _day(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD") from None


def _required(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        wanted = ["stock_code", "isin", "nse_symbol", "series"]
        if reader.fieldnames is None or [name.strip() for name in reader.fieldnames] != wanted:
            raise CasSnapshotError("required_columns", f"--required needs the header {','.join(wanted)}")
        return [{key: (value or "").strip() for key, value in row.items()} for row in reader]


def procedure_entry_text() -> str:
    """The approval entry the operator adds by hand: running hashes, the next version, the chain link."""
    log = load_procedure_log()
    spec, impl = running_identity()
    version = max((a.procedure_version for a in log.approvals), default=0) + 1
    previous = entry_sha256(log.raw_entries[-1]) if log.raw_entries else None
    entry = {
        "procedure_version": version,
        "spec_sha256": spec,
        "impl_sha256": impl,
        "u24_status": U24_UNVERIFIED,
        "u24_evidence_ref": None,
        "operator_approval": {"line": "<the operator's written approval line>", "date": "<YYYY-MM-DD>"},
        "prev_sha256": previous,
    }
    return json.dumps(entry, indent=2)


def main(argv: Sequence[str] | None = None, *, out: Any = None) -> int:
    stream = out if out is not None else sys.stdout

    def say(line: str) -> None:
        print(line, file=stream)

    parser = argparse.ArgumentParser(prog="python -m pilot_data.cas_snapshots")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("procedure", help="print the running procedure identity and the approval entry to add")
    imp = sub.add_parser("import", help="store one operator-saved snapshot under an approved procedure version")
    imp.add_argument("--root", required=True, type=Path)
    imp.add_argument("--file", required=True, type=Path)
    imp.add_argument("--session-date", required=True, type=_day)
    imp.add_argument("--procedure-version", required=True, type=int)
    imp.add_argument("--calendar", required=True, type=Path, help="the operator-pinned NSE calendar file")
    imp.add_argument("--calendar-sha256", required=True)
    imp.add_argument("--required", type=Path, help="csv: stock_code,isin,nse_symbol,series")
    rx = sub.add_parser("reindex", help="process the stored bytes under another approved procedure version")
    rx.add_argument("--root", required=True, type=Path)
    rx.add_argument("--session-date", required=True, type=_day)
    rx.add_argument("--procedure-version", required=True, type=int)
    show = sub.add_parser("show", help="display the class of NSE symbols (not a decision path)")
    show.add_argument("--root", required=True, type=Path)
    show.add_argument("--date", required=True, type=_day)
    show.add_argument("--procedure-version", required=True, type=int)
    show.add_argument("--symbols", nargs="+", required=True)
    chk = sub.add_parser("check-broker", help="resolve Breeze stock codes the way close and the probe do")
    chk.add_argument("--root", required=True, type=Path)
    chk.add_argument("--date", required=True, type=_day)
    chk.add_argument("--procedure-version", required=True, type=int)
    chk.add_argument("--stock-codes", nargs="+", required=True)
    ver = sub.add_parser("verify", help="re-hash the stored raw bytes and index of one date and version")
    ver.add_argument("--root", required=True, type=Path)
    ver.add_argument("--date", required=True, type=_day)
    ver.add_argument("--procedure-version", required=True, type=int)
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else 2
    try:
        if args.command == "procedure":
            spec, impl = running_identity()
            say(f"spec_sha256 {spec}")
            say(f"impl_sha256 {impl}")
            for name, part in sorted(impl_parts().items()):
                say(f"impl_part {name} {json.dumps(part, sort_keys=True)}")
            say("approval_entry_to_add (append to entries in costs/schedules/nse_cas_procedure.json):")
            say(procedure_entry_text())
        elif args.command == "import":
            calendar = load_pinned_calendar(args.calendar, expected_sha256=args.calendar_sha256)
            required = _required(args.required) if args.required else []
            result = import_snapshot(
                args.root, args.file, args.session_date, args.procedure_version, calendar=calendar, required=required
            )
            say(f"import date {result.session_date.isoformat()} procedure_version {result.procedure_version} "
                f"raw_sha256 {result.raw_sha256} index_sha256 {result.index_sha256} "
                f"raw_written {result.raw_written} index_written {result.index_written}")
            say(json.dumps(result.report, sort_keys=True, indent=2))
        elif args.command == "reindex":
            result = reindex(args.root, args.session_date, args.procedure_version)
            say(f"reindex date {result.session_date.isoformat()} procedure_version {result.procedure_version} "
                f"raw_sha256 {result.raw_sha256} index_sha256 {result.index_sha256} index_written {result.index_written}")
            say(json.dumps(result.report, sort_keys=True, indent=2))
        elif args.command == "show":
            for symbol in args.symbols:
                say(f"symbol {symbol} {describe(resolve_symbol(args.root, args.date, args.procedure_version, symbol))}")
        elif args.command == "check-broker":
            for code in args.stock_codes:
                verdict = resolve_broker(args.root, args.date, args.procedure_version, code, IciciPin.select(args.date))
                say(f"stock_code {code} {describe(verdict)}")
        elif args.command == "verify":
            problems = verify_stored(args.root, args.date, args.procedure_version)
            if problems:
                say(f"verify date {args.date.isoformat()} procedure_version {args.procedure_version} FAILED {' '.join(problems)}")
                return 1
            say(f"verify date {args.date.isoformat()} procedure_version {args.procedure_version} ok")
    except PilotDataError as exc:
        say(f"refused {exc.code}")
        return 1
    except (ValueError, OSError) as exc:
        say(f"refused {type(exc).__name__}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
