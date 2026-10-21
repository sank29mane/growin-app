"""Dated NSE closing-auction (CAS) class per ISIN and session date.

The intraday fill model (``intraday_fills``) needs to know whether a security was in
closing-auction scope on a date, because its volume check band is pinned per class. The
class comes from an NSE publication the operator identifies and saves locally; no agent
fetches NSE. Each table version cites that publication, the sha256 of the operator-saved
file and the operator's written approval line, and lists one explicit row per ISIN.

Fail closed, like the holiday table in the gateway and ``ticks.py``:

* There is no default class. An ISIN with no row on a date raises ``CasClassUnavailable``;
  the simulator caller maps that to the INCONCLUSIVE code ``session_scope_unknown``.
* A version with no publication, sha256 or operator approval refuses to load, as does a
  row the operator marked unverified.
* An empty table (the committed state until the operator sources the rows) loads and
  resolves nothing.

The simulator never reads the table file: it receives the resolved ``CasClassRow``.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from .core import (
    InputError,
    ScheduleError,
    canonical_json,
    load_strict_json,
    sha256_hex,
)

CAS_SCHEMA = "growin.costs.cas_class/1"
CAS_TABLE_PATH = Path(__file__).parent / "schedules" / "nse_cas_class.json"
CAS = "cas"
NON_CAS = "non_cas"
CAS_CLASSES = (CAS, NON_CAS)
# The INCONCLUSIVE code a caller reports when this module has no row.
SCOPE_UNKNOWN_CODE = "session_scope_unknown"

_FILE_KEYS = ("schema", "source_id", "status", "versions")
_VERSION_KEYS = (
    "version",
    "revision",
    "effective_from",
    "effective_to",
    "publication_id",
    "publication_date",
    "source_sha256",
    "operator_approval",
    "rows",
)
_APPROVAL_KEYS = ("line", "date")
_ROW_KEYS = ("isin", "symbol", "class")
_ISIN = re.compile(r"[A-Z0-9]{12}", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)


class CasClassUnavailable(InputError):
    """No approved CAS-class row covers this ISIN on this date. Nothing defaults."""

    code = SCOPE_UNKNOWN_CODE


@dataclass(frozen=True)
class CasClassRow:
    """One resolved row plus the provenance the simulator records."""

    isin: str
    symbol: str
    cas_class: str
    session_date: date
    table_version: str
    table_revision: str
    version_hash: str
    publication_id: str
    publication_date: date
    source_sha256: str
    approval_date: date


@dataclass(frozen=True)
class CasClassVersion:
    version: str
    revision: str
    effective_from: date
    effective_to: date | None
    publication_id: str
    publication_date: date
    source_sha256: str
    approval_line: str
    approval_date: date
    classes: tuple[tuple[str, str, str], ...]  # (isin, symbol, class), file order
    version_hash: str

    def covers(self, session_date: date) -> bool:
        if session_date < self.effective_from:
            return False
        return self.effective_to is None or session_date <= self.effective_to


@dataclass(frozen=True)
class CasClassTable:
    schema: str
    source_id: str
    status: str
    versions: tuple[CasClassVersion, ...]


def _mapping(raw: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(raw, dict):
        raise ScheduleError(f"{path}: expected an object")
    return raw


def _exact(raw: Mapping[str, Any], keys: tuple[str, ...], path: str) -> None:
    for key in raw:
        if key not in keys:
            raise ScheduleError(f"{path}.{key}: unknown key")
    for key in keys:
        if key not in raw:
            raise ScheduleError(f"{path}.{key}: missing required key")


def _text(raw: Mapping[str, Any], key: str, path: str) -> str:
    value = raw[key]
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ScheduleError(f"{path}.{key}: expected a non-empty string")
    return value


def _date(raw: Mapping[str, Any], key: str, path: str) -> date:
    value = raw[key]
    if not isinstance(value, str):
        raise ScheduleError(f"{path}.{key}: expected an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ScheduleError(f"{path}.{key}: not an ISO date") from exc


def _parse_rows(raw: Any, path: str) -> tuple[tuple[str, str, str], ...]:
    if not isinstance(raw, list) or not raw:
        raise ScheduleError(f"{path}: expected a non-empty list of rows")
    rows: list[tuple[str, str, str]] = []
    seen: set[str] = set()
    for n, item in enumerate(raw):
        row_path = f"{path}[{n}]"
        row = _mapping(item, row_path)
        # ``status`` is the operator's per-row mark. Only an explicit "verified" is accepted;
        # anything else (UNVERIFIED included) refuses the whole version.
        if "status" in row:
            if row["status"] != "verified":
                raise ScheduleError(f"{row_path}.status: only 'verified' rows load, got {row['status']!r}")
            row = {key: value for key, value in row.items() if key != "status"}
        _exact(row, _ROW_KEYS, row_path)
        isin = _text(row, "isin", row_path)
        if not _ISIN.fullmatch(isin):
            raise ScheduleError(f"{row_path}.isin: expected 12 upper-case letters or digits")
        if isin in seen:
            raise ScheduleError(f"{row_path}.isin: duplicate ISIN {isin!r} in one version")
        seen.add(isin)
        cas_class = _text(row, "class", row_path)
        if cas_class not in CAS_CLASSES:
            raise ScheduleError(f"{row_path}.class: expected one of {CAS_CLASSES}, got {cas_class!r}")
        rows.append((isin, _text(row, "symbol", row_path), cas_class))
    return tuple(rows)


def _parse_version(raw: Any, index: int) -> CasClassVersion:
    path = f"versions[{index}]"
    top = _mapping(raw, path)
    _exact(top, _VERSION_KEYS, path)
    effective_from = _date(top, "effective_from", path)
    effective_to = None if top["effective_to"] is None else _date(top, "effective_to", path)
    if effective_to is not None and effective_to < effective_from:
        raise ScheduleError(f"{path}.effective_to: precedes effective_from")
    sha = _text(top, "source_sha256", path)
    if not _SHA256.fullmatch(sha):
        raise ScheduleError(f"{path}.source_sha256: expected 64 lowercase hex characters")
    approval = _mapping(top["operator_approval"], f"{path}.operator_approval")
    _exact(approval, _APPROVAL_KEYS, f"{path}.operator_approval")
    return CasClassVersion(
        version=_text(top, "version", path),
        revision=_text(top, "revision", path),
        effective_from=effective_from,
        effective_to=effective_to,
        publication_id=_text(top, "publication_id", path),
        publication_date=_date(top, "publication_date", path),
        source_sha256=sha,
        approval_line=_text(approval, "line", f"{path}.operator_approval"),
        approval_date=_date(approval, "date", f"{path}.operator_approval"),
        classes=_parse_rows(top["rows"], f"{path}.rows"),
        version_hash=sha256_hex(canonical_json(top)),
    )


def load_cas_table(path: Path) -> CasClassTable:
    """Load one CAS-class file. The path is required; ``resolve_cas_class`` uses the committed file."""
    source = Path(path)
    raw = _mapping(load_strict_json(source.read_text(encoding="utf-8"), ScheduleError, source.name), "$")
    _exact(raw, _FILE_KEYS, "$")
    if raw["schema"] != CAS_SCHEMA:
        raise ScheduleError(f"$.schema: expected {CAS_SCHEMA!r}")
    versions_raw = raw["versions"]
    # An empty list is the unpopulated state and is allowed: every lookup then fails closed.
    if not isinstance(versions_raw, list):
        raise ScheduleError("$.versions: expected a list")
    versions = sorted(
        (_parse_version(item, n) for n, item in enumerate(versions_raw)), key=lambda v: v.effective_from
    )
    ids = [version.version for version in versions]
    if len(set(ids)) != len(ids):
        raise ScheduleError("$.versions: duplicate version id")
    for earlier, later in zip(versions, versions[1:]):
        if earlier.effective_to is None:
            raise ScheduleError(f"$.versions: {earlier.version} is open-ended but is not the latest version")
        if earlier.effective_to >= later.effective_from:
            raise ScheduleError(f"$.versions: {earlier.version} overlaps {later.version}")
        if earlier.effective_to + timedelta(days=1) != later.effective_from:
            raise ScheduleError(f"$.versions: {earlier.version} and {later.version} are not contiguous")
    return CasClassTable(raw["schema"], _text(raw, "source_id", "$"), _text(raw, "status", "$"), tuple(versions))


@lru_cache(maxsize=None)
def committed_cas_table() -> CasClassTable:
    return load_cas_table(CAS_TABLE_PATH)


def resolve_cas_class(isin: str, session_date: date, *, table: CasClassTable | None = None) -> CasClassRow:
    """The operator-approved class of ``isin`` on ``session_date``; no row raises ``CasClassUnavailable``."""
    if not isinstance(isin, str) or not isin.strip():
        raise CasClassUnavailable("isin must be a non-empty string")
    if not isinstance(session_date, date) or isinstance(session_date, datetime):
        raise CasClassUnavailable(f"session_date must be a datetime.date, got {type(session_date).__name__}")
    used = committed_cas_table() if table is None else table
    for version in used.versions:
        if not version.covers(session_date):
            continue
        for row_isin, symbol, cas_class in version.classes:
            if row_isin == isin:
                return CasClassRow(
                    isin=row_isin,
                    symbol=symbol,
                    cas_class=cas_class,
                    session_date=session_date,
                    table_version=version.version,
                    table_revision=version.revision,
                    version_hash=version.version_hash,
                    publication_id=version.publication_id,
                    publication_date=version.publication_date,
                    source_sha256=version.source_sha256,
                    approval_date=version.approval_date,
                )
        break  # versions never overlap: the covering version has no row for this ISIN
    raise CasClassUnavailable(
        f"no approved CAS-class row for {isin!r} on {session_date.isoformat()} (there is no default class)"
    )
