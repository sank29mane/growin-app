"""NSE daily price bands with effective dates, provenance and an explicit coverage report.

Output contract for Phase 62: load the latest band coverage report whose period contains the
required evaluation period and refuse to run when the report is missing or `phase62_blocked`
is true. Phase 62 passes BandUnavailable to Phase 60 for every unknown (ISIN, session); Phase 60
then records NO_ASSUMED_FILL, which is unsupported simulation data, not a market miss.

Rules enforced here (review decision D15):
- a band file is stored append-only with URL, fetch time, sha256 and file date;
- a changes file applies only forward from a validated full-list baseline, never without one;
- fixed bands and "No Band" are explicit categories; anything else is unknown with a reason;
- the ISIN for a band on session s comes from that session's primary bhavcopy row;
- band_on never reads a file effective after the session, and never substitutes today's bands.

Bands are percent categories only. Rupee price limits are Phase 62's job (from the previous
raw close and Phase 60's tick rules).
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import tempfile
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Callable, Literal, Mapping

import httpx
from pydantic import BaseModel, ConfigDict

from .bhavcopy import primary_bars_on
from .core import (
    CaveatedResult,
    Caveat,
    PilotDataError,
    SourceDescriptor,
    canonical_sha256,
    dec_str,
    parse_decimal,
    standard_caveats,
    utc_naive,
    utc_now,
)
from .lineage import lineage_for_target
from .models import QuarantineRecord
from .nse_http import NoFile, NseHttp, build_default_client
from .nse_ingest import free_bytes
from .sessions import day_status, ensure_fetch_log, sessions_between
from .store import PilotDataStore
from .targets import TargetUniverseResult, latest_target_universe

SEC_LIST_URL = "https://nsearchives.nseindia.com/content/equities/sec_list.csv"
SEC_LIST_DATED_URL = "https://nsearchives.nseindia.com/content/equities/sec_list_{ddmmyyyy}.csv"
BAND_CHANGES_URL = "https://nsearchives.nseindia.com/content/equities/eq_band_changes_{ddmmyyyy}.csv"
SEC_LIST_HEADER = ("Symbol", "Series", "Security Name", "Band", "Remarks")
BAND_CHANGES_HEADER = ("Sr. No", "Symbol", "Series", "Security Name", "From", "To")
FIXED_BAND_PERCENTS = (Decimal("2"), Decimal("5"), Decimal("10"), Decimal("20"), Decimal("40"))
NO_BAND_TEXT = "No Band"
BandCategory = Literal["fixed", "no_band"]
BandStatus = Literal["fixed", "no_band", "unknown"]
Rule = Literal["next_session_after_file_date", "file_date"]
MAX_LISTED_DATES = 50

PRICE_BAND_UNSUPPORTED_CAVEAT = Caveat(
    code="PRICE_BAND_UNSUPPORTED",
    text=(
        "Sessions without an effective NSE price band are unsupported simulation data, not market misses. "
        "No fill is assumed on them, and today's bands are never used for a historical date."
    ),
)

FILES_DDL = (
    "CREATE TABLE IF NOT EXISTS price_band_files("
    "source_sha256 VARCHAR NOT NULL, file_kind VARCHAR NOT NULL, file_date DATE NOT NULL, url VARCHAR NOT NULL, "
    "fetched_at_utc TIMESTAMP NOT NULL, row_count BIGINT NOT NULL, row_sha256 VARCHAR NOT NULL, "
    "PRIMARY KEY(source_sha256, file_kind, file_date))"
)
LIST_ROWS_DDL = (
    "CREATE TABLE IF NOT EXISTS price_band_list_rows("
    "source_sha256 VARCHAR NOT NULL, file_date DATE NOT NULL, nse_symbol VARCHAR NOT NULL, series VARCHAR NOT NULL, "
    "category VARCHAR NOT NULL, percent DECIMAL(6,2), remarks VARCHAR NOT NULL, raw_band VARCHAR NOT NULL, "
    "row_sha256 VARCHAR NOT NULL, PRIMARY KEY(source_sha256, nse_symbol, series))"
)
CHANGE_ROWS_DDL = (
    "CREATE TABLE IF NOT EXISTS price_band_change_rows("
    "source_sha256 VARCHAR NOT NULL, file_date DATE NOT NULL, serial BIGINT NOT NULL, nse_symbol VARCHAR NOT NULL, "
    "series VARCHAR NOT NULL, from_category VARCHAR NOT NULL, from_percent DECIMAL(6,2), "
    "to_category VARCHAR NOT NULL, to_percent DECIMAL(6,2), row_sha256 VARCHAR NOT NULL, "
    "PRIMARY KEY(source_sha256, row_sha256))"
)
FETCH_LOG_DDL = (
    "CREATE TABLE IF NOT EXISTS price_band_fetch_log("
    "attempt_id VARCHAR PRIMARY KEY, file_date DATE NOT NULL, file_kind VARCHAR NOT NULL, outcome VARCHAR NOT NULL, "
    "http_status INTEGER, error_code VARCHAR, source_sha256 VARCHAR, url VARCHAR NOT NULL, "
    "attempted_at_utc TIMESTAMP NOT NULL, row_sha256 VARCHAR NOT NULL)"
)
CONVENTIONS_DDL = (
    "CREATE TABLE IF NOT EXISTS price_band_conventions("
    "recorded_at_utc TIMESTAMP PRIMARY KEY, list_rule VARCHAR NOT NULL, changes_rule VARCHAR NOT NULL, "
    "basis VARCHAR NOT NULL, evidence_sha256 VARCHAR NOT NULL, row_sha256 VARCHAR NOT NULL, "
    "source_sha256 VARCHAR NOT NULL)"
)
ARCHIVE_DEPTH_DDL = (
    "CREATE TABLE IF NOT EXISTS price_band_archive_depth("
    "probed_at_utc TIMESTAMP PRIMARY KEY, earliest_ingested_list DATE, earliest_ingested_changes DATE, "
    "no_file_dates_json VARCHAR NOT NULL, failed_dates_json VARCHAR NOT NULL, row_sha256 VARCHAR NOT NULL, "
    "source_sha256 VARCHAR NOT NULL)"
)
COVERAGE_DDL = (
    "CREATE TABLE IF NOT EXISTS band_coverage_reports("
    "report_sha256 VARCHAR PRIMARY KEY, workspace VARCHAR NOT NULL, period_start DATE NOT NULL, "
    "period_end DATE NOT NULL, phase62_blocked BOOLEAN NOT NULL, payload_json VARCHAR NOT NULL, "
    "built_at_utc TIMESTAMP NOT NULL, row_sha256 VARCHAR NOT NULL, source_sha256 VARCHAR NOT NULL)"
)


def ensure_band_tables(store: PilotDataStore) -> None:
    store.ensure_table("price_band_files", FILES_DDL, key_columns=("source_sha256", "file_kind", "file_date"))
    store.ensure_table("price_band_list_rows", LIST_ROWS_DDL, key_columns=("source_sha256", "nse_symbol", "series"))
    store.ensure_table("price_band_change_rows", CHANGE_ROWS_DDL, key_columns=("source_sha256", "row_sha256"))
    store.ensure_table("price_band_fetch_log", FETCH_LOG_DDL, key_columns=("attempt_id",))
    store.ensure_table("price_band_conventions", CONVENTIONS_DDL, key_columns=("recorded_at_utc",))
    store.ensure_table("price_band_archive_depth", ARCHIVE_DEPTH_DDL, key_columns=("probed_at_utc",))
    store.ensure_table("band_coverage_reports", COVERAGE_DDL, key_columns=("report_sha256",))
    ensure_fetch_log(store)


# --------------------------------------------------------------------------- parsing
class BandListRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    nse_symbol: str
    series: str
    security_name: str
    category: BandCategory
    percent: Decimal | None
    remarks: str
    raw_band: str

    def row_sha256(self) -> str:
        return canonical_sha256(
            {"symbol": self.nse_symbol, "series": self.series, "category": self.category,
             "percent": None if self.percent is None else dec_str(self.percent), "remarks": self.remarks,
             "raw_band": self.raw_band}
        )


class BandChangeRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    serial: int
    nse_symbol: str
    series: str
    security_name: str
    from_category: BandCategory
    from_percent: Decimal | None
    to_category: BandCategory
    to_percent: Decimal | None
    raw_from: str
    raw_to: str

    def row_sha256(self) -> str:
        return canonical_sha256(
            {"serial": self.serial, "symbol": self.nse_symbol, "series": self.series,
             "from": [self.from_category, None if self.from_percent is None else dec_str(self.from_percent)],
             "to": [self.to_category, None if self.to_percent is None else dec_str(self.to_percent)]}
        )


def parse_band_value(text: str) -> tuple[BandCategory, Decimal | None]:
    cleaned = text.strip()
    if cleaned == NO_BAND_TEXT:
        return "no_band", None
    try:
        percent = parse_decimal(cleaned, field="band")
    except PilotDataError as exc:
        raise PilotDataError("band_value_unrecognized", "band value is neither a fixed percent nor No Band") from exc
    if percent not in FIXED_BAND_PERCENTS:
        raise PilotDataError("band_value_unrecognized", f"band percent {cleaned!r} is not a known fixed band")
    return "fixed", percent


def _rows_of(content: bytes, header: tuple[str, ...], code: str) -> list[list[str]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise PilotDataError(code, "band file is not valid UTF-8") from exc
    reader = csv.reader(io.StringIO(text))
    try:
        first = tuple(cell.strip() for cell in next(reader))
    except StopIteration as exc:
        raise PilotDataError(code, "band file is empty") from exc
    if first != header:
        raise PilotDataError(code, "band file header differs from the verified header")
    rows = []
    for raw in reader:
        if not raw:
            continue
        if len(raw) != len(header):
            raise PilotDataError(code, "band file row has the wrong field count")
        rows.append([cell.strip() for cell in raw])
    return rows


def parse_sec_list(content: bytes) -> tuple[BandListRow, ...]:
    rows = _rows_of(content, SEC_LIST_HEADER, "band_list_schema_mismatch")
    if not rows:
        raise PilotDataError("band_list_empty", "band list holds no rows")
    parsed: list[BandListRow] = []
    seen: set[tuple[str, str]] = set()
    for symbol, series, name, band, remarks in rows:
        key = (symbol, series)
        if key in seen:
            raise PilotDataError("band_list_duplicate", "band list repeats a symbol and series")
        seen.add(key)
        category, percent = parse_band_value(band)
        parsed.append(
            BandListRow(nse_symbol=symbol, series=series, security_name=name, category=category, percent=percent,
                        remarks=remarks, raw_band=band)
        )
    return tuple(parsed)


def parse_band_changes(content: bytes) -> tuple[BandChangeRow, ...]:
    rows = _rows_of(content, BAND_CHANGES_HEADER, "band_changes_schema_mismatch")
    parsed: list[BandChangeRow] = []
    for serial, symbol, series, name, old, new in rows:
        if not serial.isdigit():
            raise PilotDataError("band_changes_schema_mismatch", "serial is not an integer")
        from_category, from_percent = parse_band_value(old)
        to_category, to_percent = parse_band_value(new)
        parsed.append(
            BandChangeRow(serial=int(serial), nse_symbol=symbol, series=series, security_name=name,
                          from_category=from_category, from_percent=from_percent, to_category=to_category,
                          to_percent=to_percent, raw_from=old, raw_to=new)
        )
    return tuple(parsed)


# --------------------------------------------------------------------------- ingest
@dataclass(frozen=True)
class BandFileRef:
    source_sha256: str
    file_kind: str
    file_date: date
    row_count: int


def _check_workspace(store: PilotDataStore, workspace: str) -> None:
    if workspace != store.workspace:
        raise PilotDataError("workspace_mismatch", "workspace differs from the store workspace")


def _file_row(ref_hash: str, kind: str, file_date: date, descriptor: SourceDescriptor, count: int) -> dict[str, object]:
    return {
        "source_sha256": ref_hash, "file_kind": kind, "file_date": file_date, "url": descriptor.locator,
        "fetched_at_utc": utc_naive(descriptor.fetched_at), "row_count": count,
        "row_sha256": canonical_sha256({"kind": kind, "date": file_date.isoformat(), "rows": count}),
    }


def ingest_band_list(
    store: PilotDataStore, descriptor: SourceDescriptor, content: bytes, *, file_date: date, workspace: str
) -> BandFileRef:
    _check_workspace(store, workspace)
    ensure_band_tables(store)
    ref = store.register_source(descriptor, content)
    rows = parse_sec_list(content)
    store.append_rows(
        "price_band_list_rows",
        [
            {"source_sha256": ref.source_sha256, "file_date": file_date, "nse_symbol": row.nse_symbol,
             "series": row.series, "category": row.category, "percent": row.percent, "remarks": row.remarks,
             "raw_band": row.raw_band, "row_sha256": row.row_sha256()}
            for row in rows
        ],
        check="price_band_list_rows",
    )
    store.append_rows("price_band_files", [_file_row(ref.source_sha256, "list", file_date, descriptor, len(rows))],
                      check="price_band_files")
    return BandFileRef(ref.source_sha256, "list", file_date, len(rows))


def ingest_band_changes(
    store: PilotDataStore, descriptor: SourceDescriptor, content: bytes, *, file_date: date, workspace: str
) -> BandFileRef:
    _check_workspace(store, workspace)
    ensure_band_tables(store)
    ref = store.register_source(descriptor, content)
    rows = parse_band_changes(content)
    store.append_rows(
        "price_band_change_rows",
        [
            {"source_sha256": ref.source_sha256, "file_date": file_date, "serial": row.serial,
             "nse_symbol": row.nse_symbol, "series": row.series, "from_category": row.from_category,
             "from_percent": row.from_percent, "to_category": row.to_category, "to_percent": row.to_percent,
             "row_sha256": row.row_sha256()}
            for row in rows
        ],
        check="price_band_change_rows",
    )
    store.append_rows("price_band_files", [_file_row(ref.source_sha256, "changes", file_date, descriptor, len(rows))],
                      check="price_band_files")
    return BandFileRef(ref.source_sha256, "changes", file_date, len(rows))


def _log(store: PilotDataStore, *, file_date: date, kind: str, outcome: str, status: int | None, code: str | None,
         source: str | None, url: str) -> None:
    now = utc_naive(datetime.now(timezone.utc))
    attempt = uuid.uuid4().hex
    store.append_rows(
        "price_band_fetch_log",
        [{"attempt_id": attempt, "file_date": file_date, "file_kind": kind, "outcome": outcome, "http_status": status,
          "error_code": code, "source_sha256": source, "url": url, "attempted_at_utc": now,
          "row_sha256": canonical_sha256({"attempt": attempt, "kind": kind, "outcome": outcome})}],
        check="price_band_fetch_log",
    )


@dataclass(frozen=True)
class BandFetchOutcome:
    file_date: date
    list_outcome: str
    changes_outcome: str


def _ddmmyyyy(day: date) -> str:
    return f"{day.day:02d}{day.month:02d}{day.year}"


def fetch_band_files(store: PilotDataStore, http: NseHttp, file_date: date, *, workspace: str) -> BandFetchOutcome:
    _check_workspace(store, workspace)
    ensure_band_tables(store)
    outcomes: dict[str, str] = {}
    for kind, template, ingest in (
        ("list", SEC_LIST_DATED_URL, ingest_band_list), ("changes", BAND_CHANGES_URL, ingest_band_changes)
    ):
        url = template.format(ddmmyyyy=_ddmmyyyy(file_date))
        try:
            fetched = http.fetch(url, expect="csv")
        except PilotDataError as exc:
            _log(store, file_date=file_date, kind=kind, outcome="failed", status=None, code=exc.code, source=None,
                 url=url)
            outcomes[kind] = "failed"
            continue
        if isinstance(fetched, NoFile):
            _log(store, file_date=file_date, kind=kind, outcome="no_file", status=404, code=None, source=None, url=url)
            outcomes[kind] = "no_file"
            continue
        descriptor = fetched.descriptor("price_band_list" if kind == "list" else "price_band_changes", file_date)
        try:
            ref = ingest(store, descriptor, fetched.content, file_date=file_date, workspace=workspace)
        except PilotDataError as exc:
            _log(store, file_date=file_date, kind=kind, outcome="failed", status=fetched.status, code=exc.code,
                 source=None, url=url)
            outcomes[kind] = "failed"
            continue
        _log(store, file_date=file_date, kind=kind, outcome="ingested", status=fetched.status, code=None,
             source=ref.source_sha256, url=url)
        outcomes[kind] = "ingested"
    return BandFetchOutcome(file_date, outcomes["list"], outcomes["changes"])


# --------------------------------------------------------------------------- convention
class BandConvention(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    list_rule: Rule
    changes_rule: Rule
    basis: Literal["operator_confirmed", "test_fixture"]
    evidence_sha256: str
    recorded_at_utc: datetime


def record_band_convention(store: PilotDataStore, convention: BandConvention, *, workspace: str) -> None:
    _check_workspace(store, workspace)
    ensure_band_tables(store)
    store.append_rows(
        "price_band_conventions",
        [
            {"recorded_at_utc": utc_naive(convention.recorded_at_utc), "list_rule": convention.list_rule,
             "changes_rule": convention.changes_rule, "basis": convention.basis,
             "evidence_sha256": convention.evidence_sha256,
             "row_sha256": canonical_sha256(convention.model_dump(mode="json")),
             "source_sha256": convention.evidence_sha256}
        ],
        check="price_band_convention",
    )


def current_band_convention(store: PilotDataStore) -> BandConvention | None:
    ensure_band_tables(store)
    found = store.query(
        "SELECT list_rule, changes_rule, basis, evidence_sha256, recorded_at_utc FROM price_band_conventions "
        "ORDER BY recorded_at_utc DESC LIMIT 1"
    )
    if not found:
        return None
    list_rule, changes_rule, basis, evidence, recorded = found[0]
    return BandConvention(list_rule=list_rule, changes_rule=changes_rule, basis=basis, evidence_sha256=evidence,
                          recorded_at_utc=recorded.replace(tzinfo=timezone.utc))


# --------------------------------------------------------------------------- effective dates and band_on
class BandObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    isin: str
    session: date
    status: BandStatus
    percent: Decimal | None
    nse_symbol: str | None
    series: str | None
    source_kind: Literal["list", "changes_chain"] | None
    source_sha256s: tuple[str, ...]
    reason: str | None


@dataclass(frozen=True)
class _Basis:
    kind: Literal["list", "changes_chain"] | None
    reason: str | None
    sources: tuple[str, ...] = ()  # list source first, then the changes sources in order


class BandResolver:
    """Resolves bands for sessions. It only reads files effective on or before the session asked for."""

    def __init__(self, store: PilotDataStore, *, record_quarantines: bool = True) -> None:
        ensure_band_tables(store)
        self.store = store
        self.convention = current_band_convention(store)
        self._record = record_quarantines and not store.read_only
        self._lists_by_eff: dict[date, list[tuple[date, str]]] = {}
        self._changes_by_eff: dict[date, list[tuple[date, str]]] = {}
        self._basis_cache: dict[date, _Basis] = {}
        self._bars_cache: dict[date, dict[str, list[tuple[str, str]]]] = {}
        self._list_cache: OrderedDict[str, dict[tuple[str, str], tuple[str, Decimal | None]]] = OrderedDict()
        self._change_cache: OrderedDict[str, dict[tuple[str, str], list[tuple]]] = OrderedDict()
        self._recorded: set[tuple[str, date]] = set()
        if self.convention is not None:
            for kind, file_date, source in store.query(
                "SELECT file_kind, file_date, source_sha256 FROM price_band_files ORDER BY file_date, source_sha256"
            ):
                rule = self.convention.list_rule if kind == "list" else self.convention.changes_rule
                effective = self._effective(file_date, rule)
                if effective is None:
                    continue  # no verifiable calendar after this file: it is not effective for any checked session
                target = self._lists_by_eff if kind == "list" else self._changes_by_eff
                target.setdefault(effective, []).append((file_date, source))

    def _effective(self, file_date: date, rule: Rule) -> date | None:
        if rule == "file_date":
            return file_date
        day = file_date
        for _ in range(15):
            day += timedelta(days=1)
            status = day_status(self.store, day)
            if status == "session":
                return day
            if status not in ("holiday", "weekend_no_session"):
                return None
        return None

    # -- cached row loaders
    def _list_rows(self, source: str) -> dict[tuple[str, str], tuple[str, Decimal | None]]:
        if source not in self._list_cache:
            rows = self.store.query(
                "SELECT nse_symbol, series, category, percent FROM price_band_list_rows WHERE source_sha256 = ?",
                [source],
            )
            self._list_cache[source] = {(r[0], r[1]): (r[2], None if r[3] is None else Decimal(r[3])) for r in rows}
            while len(self._list_cache) > 6:
                self._list_cache.popitem(last=False)
        self._list_cache.move_to_end(source)
        return self._list_cache[source]

    def _change_rows(self, source: str) -> dict[tuple[str, str], list[tuple]]:
        if source not in self._change_cache:
            rows = self.store.query(
                "SELECT nse_symbol, series, from_category, from_percent, to_category, to_percent, serial "
                "FROM price_band_change_rows WHERE source_sha256 = ? ORDER BY serial", [source]
            )
            keyed: dict[tuple[str, str], list[tuple]] = {}
            for symbol, series, fc, fp, tc, tp, _ in rows:
                keyed.setdefault((symbol, series), []).append(
                    ((fc, None if fp is None else Decimal(fp)), (tc, None if tp is None else Decimal(tp)))
                )
            self._change_cache[source] = keyed
            while len(self._change_cache) > 8:
                self._change_cache.popitem(last=False)
        self._change_cache.move_to_end(source)
        return self._change_cache[source]

    def _previous_session(self, session: date) -> date | None:
        day = session
        for _ in range(15):
            day -= timedelta(days=1)
            status = day_status(self.store, day)
            if status == "session":
                return day
            if status not in ("holiday", "weekend_no_session"):
                return None
        return None

    def _quarantine(self, reason: str, session: date, evidence: tuple[str, ...], *, isin: str | None = None,
                    symbol: str | None = None, series: str | None = None, detail: dict[str, str] | None = None) -> None:
        marker = (reason + (symbol or "") + (series or ""), session)
        if not self._record or marker in self._recorded:
            return
        self._recorded.add(marker)
        self.store.record_quarantine([
            QuarantineRecord(workspace="india", check="price_band", reason_code=reason, scope="raw", isin=isin,
                             nse_symbol=symbol, series=series, date_from=session, date_to=session,
                             detail=detail or {}, evidence_sha256s=evidence)
        ])

    # -- session-level basis
    def basis(self, session: date) -> _Basis:
        cached = self._basis_cache.get(session)
        if cached is not None:
            return cached
        result = self._compute_basis(session)
        self._basis_cache[session] = result
        return result

    def _compute_basis(self, session: date) -> _Basis:
        if self.convention is None:
            return _Basis(None, "band_convention_unverified")
        direct = self._lists_by_eff.get(session)
        if direct:
            sources = sorted({source for _, source in direct})
            if len(sources) > 1:
                return _Basis(None, "band_list_ambiguous", tuple(sources))
            conflict = self._crosscheck_conflict(session, sources[0])
            if conflict:
                return _Basis(None, "band_crosscheck_conflict", conflict)
            return _Basis("list", None, (sources[0],))
        earlier = [eff for eff in self._lists_by_eff if eff < session]
        if not earlier:
            return _Basis(None, "band_no_baseline")
        baseline_day = max(earlier)
        baseline_sources = sorted({source for _, source in self._lists_by_eff[baseline_day]})
        if len(baseline_sources) > 1:
            return _Basis(None, "band_list_ambiguous", tuple(baseline_sources))
        chain: list[str] = []
        for day in sessions_between(self.store, baseline_day, session):
            if day == baseline_day:
                continue
            files = sorted({source for _, source in self._changes_by_eff.get(day, [])})
            if len(files) != 1:
                return _Basis(None, "band_chain_gap")
            chain.append(files[0])
        return _Basis("changes_chain", None, (baseline_sources[0], *chain))

    def _crosscheck_conflict(self, session: date, source: str) -> tuple[str, ...] | None:
        """Consecutive lists plus the changes file for the later session must agree with each other."""
        previous = self._previous_session(session)
        if previous is None:
            return None
        before = sorted({s for _, s in self._lists_by_eff.get(previous, [])})
        changes = sorted({s for _, s in self._changes_by_eff.get(session, [])})
        if len(before) != 1 or len(changes) != 1:
            return None  # the cross-check needs both lists and the changes file to exist
        old_rows, new_rows = self._list_rows(before[0]), self._list_rows(source)
        change_rows = self._change_rows(changes[0])
        disagree = False
        for key in set(old_rows) & set(new_rows):
            if old_rows[key] != new_rows[key]:
                moves = change_rows.get(key)
                if not moves or moves[0][0] != old_rows[key] or moves[-1][1] != new_rows[key]:
                    disagree = True
        for key, moves in change_rows.items():
            if key in old_rows and key in new_rows and (moves[0][0] != old_rows[key] or moves[-1][1] != new_rows[key]):
                disagree = True
        if not disagree:
            return None
        evidence = (before[0], source, changes[0])
        self._quarantine("band_crosscheck_conflict", session, evidence)
        return evidence

    # -- per-target observation
    def _bars_on(self, session: date) -> dict[str, list[tuple[str, str]]]:
        if session not in self._bars_cache:
            by_isin: dict[str, list[tuple[str, str]]] = {}
            for bar in primary_bars_on(self.store, session):
                if bar.isin is not None:
                    pair = (bar.nse_symbol, bar.series)
                    if pair not in by_isin.setdefault(bar.isin, []):
                        by_isin[bar.isin].append(pair)
            self._bars_cache.clear()
            self._bars_cache[session] = by_isin
        return self._bars_cache[session]

    def has_bar(self, isin: str, session: date) -> bool:
        return isin in self._bars_on(session)

    def observe(self, isin: str, session: date) -> BandObservation:
        def unknown(reason: str, symbol=None, series=None, sources=()) -> BandObservation:
            return BandObservation(isin=isin, session=session, status="unknown", percent=None, nse_symbol=symbol,
                                   series=series, source_kind=None, source_sha256s=tuple(sources), reason=reason)

        if self.convention is None:
            return unknown("band_convention_unverified")
        pairs = self._bars_on(session).get(isin, [])
        if not pairs:
            return unknown("no_bar_on_session")
        if len(pairs) > 1:
            return unknown("band_isin_ambiguous")
        symbol, series = pairs[0]
        basis = self.basis(session)
        if basis.kind is None:
            return unknown(basis.reason or "band_unsupported", symbol, series, basis.sources)
        key = (symbol, series)
        baseline = self._list_rows(basis.sources[0]).get(key)
        moves = [self._change_rows(source).get(key, []) for source in basis.sources[1:]]
        if baseline is None:
            if any(moves):
                self._quarantine("band_change_from_mismatch", session, basis.sources, isin=isin, symbol=symbol,
                                 series=series, detail={"problem": "change row for a symbol absent from the baseline"})
                return unknown("band_change_from_mismatch", symbol, series, basis.sources)
            return unknown("not_in_band_list", symbol, series, basis.sources)
        current = baseline
        for group in moves:
            for old, new in group:
                if old != current:
                    self._quarantine("band_change_from_mismatch", session, basis.sources, isin=isin, symbol=symbol,
                                     series=series, detail={"expected_from": f"{current[0]}:{current[1]}",
                                                            "actual_from": f"{old[0]}:{old[1]}"})
                    return unknown("band_change_from_mismatch", symbol, series, basis.sources)
                current = new
        return BandObservation(
            isin=isin, session=session, status=current[0], percent=current[1], nse_symbol=symbol, series=series,
            source_kind=basis.kind, source_sha256s=basis.sources, reason=None,
        )


def band_on(store: PilotDataStore, *, isin: str, session: date, workspace: str) -> BandObservation:
    _check_workspace(store, workspace)
    return BandResolver(store).observe(isin, session)


# --------------------------------------------------------------------------- convention evidence
class ConventionCheck(CaveatedResult):
    pairs_examined: int
    explained_by_same_date: int
    explained_by_previous_date: int
    explained_by_both: int
    explained_by_neither: int
    missing_changes_files: int
    rows: tuple[dict[str, str], ...]
    evidence_sha256: str


def check_band_convention(store: PilotDataStore, *, start: date, end: date) -> ConventionCheck:
    """Read-only comparison table: which changes file explains the difference between consecutive lists."""
    ensure_band_tables(store)
    lists = store.query(
        "SELECT file_date, source_sha256 FROM price_band_files WHERE file_kind = 'list' AND file_date >= ? "
        "AND file_date <= ? ORDER BY file_date", [start, end]
    )
    changes = {
        row[0]: row[1]
        for row in store.query("SELECT file_date, source_sha256 FROM price_band_files WHERE file_kind = 'changes'")
    }
    resolver = BandResolver(store, record_quarantines=False)

    def explains(source: str | None, moves: dict[tuple[str, str], tuple]) -> bool | None:
        if source is None:
            return None
        rows = resolver._change_rows(source)
        return all(key in rows and rows[key][-1][0] == old and rows[key][-1][1] == new
                   for key, (old, new) in moves.items())

    counts = {"same": 0, "previous": 0, "both": 0, "neither": 0, "missing": 0}
    table: list[dict[str, str]] = []
    for (prev_date, prev_source), (date_q, source_q) in zip(lists, lists[1:]):
        old_rows, new_rows = resolver._list_rows(prev_source), resolver._list_rows(source_q)
        moves = {k: (old_rows[k], new_rows[k]) for k in set(old_rows) & set(new_rows) if old_rows[k] != new_rows[k]}
        if not moves:
            continue
        previous = explains(changes.get(prev_date), moves)
        same = explains(changes.get(date_q), moves)
        if previous is None and same is None:
            counts["missing"] += 1
        elif previous and same:
            counts["both"] += 1
        elif previous:
            counts["previous"] += 1
        elif same:
            counts["same"] += 1
        else:
            counts["neither"] += 1
        if len(table) < 25:
            table.append({"older_list": prev_date.isoformat(), "newer_list": date_q.isoformat(),
                          "differences": str(len(moves)), "explained_by_older_date_changes": str(previous),
                          "explained_by_newer_date_changes": str(same)})
    evidence = canonical_sha256({"counts": counts, "pairs": [list(t.values()) for t in table],
                                 "start": start.isoformat(), "end": end.isoformat()})
    return ConventionCheck(
        workspace="india", caveats=standard_caveats(), pairs_examined=sum(counts.values()),
        explained_by_same_date=counts["same"], explained_by_previous_date=counts["previous"],
        explained_by_both=counts["both"], explained_by_neither=counts["neither"],
        missing_changes_files=counts["missing"], rows=tuple(table), evidence_sha256=evidence,
    )


# --------------------------------------------------------------------------- archive depth
class ArchiveDepth(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    probed_at: datetime
    earliest_ingested_list: date | None
    earliest_ingested_changes: date | None
    no_file_dates: tuple[date, ...]
    failed_dates: tuple[date, ...]


def record_archive_depth(
    store: PilotDataStore, *, probed: Mapping[date, Literal["ingested", "no_file", "failed"]], workspace: str
) -> ArchiveDepth:
    _check_workspace(store, workspace)
    ensure_band_tables(store)
    earliest_list = min((d for d, outcome in probed.items() if outcome == "ingested"), default=None)
    changes = store.query("SELECT min(file_date) FROM price_band_files WHERE file_kind = 'changes'")[0][0]
    no_file = sorted(d for d, outcome in probed.items() if outcome == "no_file")
    failed = sorted(d for d, outcome in probed.items() if outcome == "failed")
    now = datetime.now(timezone.utc)
    depth = ArchiveDepth(probed_at=now, earliest_ingested_list=earliest_list, earliest_ingested_changes=changes,
                         no_file_dates=tuple(no_file), failed_dates=tuple(failed))
    digest = canonical_sha256(
        {"list": earliest_list.isoformat() if earliest_list else None, "changes": changes.isoformat() if changes else None,
         "no_file": [d.isoformat() for d in no_file], "failed": [d.isoformat() for d in failed],
         "probed_at": now.isoformat()}
    )
    store.append_rows(
        "price_band_archive_depth",
        [{"probed_at_utc": utc_naive(now), "earliest_ingested_list": earliest_list,
          "earliest_ingested_changes": changes, "no_file_dates_json": json.dumps([d.isoformat() for d in no_file]),
          "failed_dates_json": json.dumps([d.isoformat() for d in failed]), "row_sha256": digest,
          "source_sha256": digest}],
        check="price_band_archive_depth",
    )
    return depth


def latest_archive_depth(store: PilotDataStore) -> ArchiveDepth | None:
    ensure_band_tables(store)
    found = store.query(
        "SELECT probed_at_utc, earliest_ingested_list, earliest_ingested_changes, no_file_dates_json, "
        "failed_dates_json FROM price_band_archive_depth ORDER BY probed_at_utc DESC LIMIT 1"
    )
    if not found:
        return None
    probed, earliest, changes, no_file, failed = found[0]
    return ArchiveDepth(
        probed_at=probed.replace(tzinfo=timezone.utc), earliest_ingested_list=earliest,
        earliest_ingested_changes=changes, no_file_dates=tuple(date.fromisoformat(d) for d in json.loads(no_file)),
        failed_dates=tuple(date.fromisoformat(d) for d in json.loads(failed)),
    )


# --------------------------------------------------------------------------- coverage
class BandCoverageReport(CaveatedResult):
    period_start: date
    period_end: date
    sessions: int
    sessions_by_status: dict[str, int]
    unsupported_sessions: tuple[date, ...]
    targets_checked: int
    target_unknown_counts: dict[str, int]
    fixed_count: int
    no_band_count: int
    unknown_count: int
    unknown_by_reason: dict[str, int]
    convention: BandConvention | None
    archive_depth: ArchiveDepth | None
    phase62_blocked: bool
    blocked_reasons: tuple[str, ...]
    report_sha256: str


def _listed(days: list[date]) -> str:
    return ",".join(d.isoformat() for d in days[:MAX_LISTED_DATES])


def build_band_coverage(
    store: PilotDataStore, *, start: date, end: date, targets: TargetUniverseResult, workspace: str
) -> BandCoverageReport:
    _check_workspace(store, workspace)
    ensure_band_tables(store)
    sessions = sessions_between(store, start, end)
    resolver = BandResolver(store)
    lineages = []
    for member in targets.members:
        try:
            lineages.append((member, lineage_for_target(store, member, as_of=targets.as_of, workspace="india")))
        except PilotDataError as exc:
            if exc.code != "lineage_anchor_not_observed":
                raise
    by_status: dict[str, int] = {}
    session_reasons: dict[str, list[date]] = {}
    unsupported: list[date] = []
    fixed = no_band = unknown = 0
    unknown_by_reason: dict[str, int] = {}
    unknown_sessions: dict[str, list[date]] = {}
    target_unknown: dict[str, int] = {}
    for session in sessions:
        basis = resolver.basis(session)
        if basis.kind is None:
            unsupported.append(session)
            by_status["unsupported"] = by_status.get("unsupported", 0) + 1
            session_reasons.setdefault(basis.reason or "band_unsupported", []).append(session)
        else:
            by_status[basis.kind] = by_status.get(basis.kind, 0) + 1
        for member, lineage in lineages:
            isin = lineage.isin_on(session)
            if isin is None or not resolver.has_bar(isin, session):
                continue
            observation = resolver.observe(isin, session)
            if observation.status == "fixed":
                fixed += 1
            elif observation.status == "no_band":
                no_band += 1
            else:
                unknown += 1
                reason = observation.reason or "unknown"
                unknown_by_reason[reason] = unknown_by_reason.get(reason, 0) + 1
                unknown_sessions.setdefault(reason, []).append(session)
                target_unknown[member.stock_code] = target_unknown.get(member.stock_code, 0) + 1
    blocked: list[str] = []
    for reason, days in sorted(session_reasons.items()):
        blocked.append(f"{reason}: {len(days)} sessions, first {MAX_LISTED_DATES}: {_listed(days)}")
    for reason, days in sorted(unknown_sessions.items()):
        if reason in session_reasons and reason == "band_convention_unverified":
            continue
        blocked.append(f"target-sessions unknown ({reason}): {unknown_by_reason[reason]}, first {MAX_LISTED_DATES} "
                       f"sessions: {_listed(sorted(set(days)))}")
    convention = resolver.convention
    if convention is None and not any(r.startswith("band_convention_unverified") for r in blocked):
        blocked.append("band_convention_unverified")
    caveats = standard_caveats(PRICE_BAND_UNSUPPORTED_CAVEAT)
    fields = dict(
        period_start=start, period_end=end, sessions=len(sessions), sessions_by_status=dict(sorted(by_status.items())),
        unsupported_sessions=tuple(unsupported), targets_checked=len(lineages),
        target_unknown_counts=dict(sorted(target_unknown.items())), fixed_count=fixed, no_band_count=no_band,
        unknown_count=unknown, unknown_by_reason=dict(sorted(unknown_by_reason.items())), convention=convention,
        archive_depth=latest_archive_depth(store), phase62_blocked=bool(blocked), blocked_reasons=tuple(blocked),
    )
    digest_payload = {
        key: (value.model_dump(mode="json") if isinstance(value, BaseModel)
              else [d.isoformat() for d in value] if key == "unsupported_sessions"
              else value.isoformat() if isinstance(value, date) else list(value) if isinstance(value, tuple) else value)
        for key, value in fields.items()
    }
    digest = canonical_sha256({"fields": digest_payload, "targets": targets.target_sha256,
                               "caveats": [c.code for c in caveats]})
    report = BandCoverageReport(workspace="india", caveats=caveats, report_sha256=digest, **fields)
    store.append_rows(
        "band_coverage_reports",
        [{"report_sha256": digest, "workspace": store.workspace, "period_start": start, "period_end": end,
          "phase62_blocked": report.phase62_blocked,
          "payload_json": json.dumps(report.model_dump(mode="json"), sort_keys=True),
          "built_at_utc": utc_naive(datetime.now(timezone.utc)), "row_sha256": digest,
          "source_sha256": targets.target_sha256}],
        check="band_coverage_report",
    )
    return report


# --------------------------------------------------------------------------- CLI
def _caveat_payload() -> list[dict[str, str]]:
    return [caveat.model_dump() for caveat in standard_caveats(PRICE_BAND_UNSUPPORTED_CAVEAT)]


def _final_dates(store: PilotDataStore, start: date, end: date) -> set[date]:
    """Dates whose list and changes attempts both ended in ingested or no_file (so a resume skips them)."""
    latest: dict[tuple[date, str], str] = {}
    for file_date, kind, outcome in store.query(
        "SELECT file_date, file_kind, outcome FROM price_band_fetch_log WHERE file_date >= ? AND file_date <= ? "
        "ORDER BY attempted_at_utc, rowid", [start, end]
    ):
        latest[(file_date, kind)] = outcome
    return {
        d for d in {key[0] for key in latest}
        if all(latest.get((d, kind)) in ("ingested", "no_file") for kind in ("list", "changes"))
    }


def ingest_range(
    store: PilotDataStore, http: NseHttp, start: date, end: date, *, workspace: str, min_free_bytes: int,
    disk_free: Callable[[Path], int] = free_bytes,
) -> dict[str, int]:
    _check_workspace(store, workspace)
    ensure_band_tables(store)
    final = _final_dates(store, start, end)
    counts = {"fetched": 0, "skipped_final": 0, "skipped_not_session": 0, "failed": 0}
    day = start
    while day <= end:
        if day in final:
            counts["skipped_final"] += 1
        elif day_status(store, day) != "session":
            counts["skipped_not_session"] += 1
        else:
            free = disk_free(store.root)
            if free < min_free_bytes:
                raise PilotDataError("disk_floor_reached", f"{free} bytes free is below the {min_free_bytes} byte floor")
            outcome = fetch_band_files(store, http, day, workspace=workspace)
            counts["fetched"] += 1
            if "failed" in (outcome.list_outcome, outcome.changes_outcome):
                counts["failed"] += 1
        day += timedelta(days=1)
    return counts


def _probe_map(store: PilotDataStore) -> dict[date, Literal["ingested", "no_file", "failed"]]:
    latest: dict[date, str] = {}
    for file_date, outcome in store.query(
        "SELECT file_date, outcome FROM price_band_fetch_log WHERE file_kind = 'list' ORDER BY attempted_at_utc, rowid"
    ):
        latest[file_date] = outcome
    return latest  # type: ignore[return-value]


def write_coverage_report(root: Path, report: BandCoverageReport) -> Path:
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / (
        f"band-coverage-{report.period_start.isoformat()}-{report.period_end.isoformat()}-{report.report_sha256[:12]}.json"
    )
    if path.exists():
        return path
    dumped = report.model_dump(mode="json")
    ordered = {"caveats": dumped.pop("caveats"), **dumped}
    fd, tmp = tempfile.mkstemp(dir=reports, prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(ordered, indent=2))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    os.chmod(path, 0o444)
    return path


def main(argv: list[str] | None = None, *, client: httpx.Client | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pilot_data.price_bands", description="NSE price band data and coverage.")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(name: str) -> argparse.ArgumentParser:
        child = sub.add_parser(name)
        child.add_argument("--root", required=True, type=Path)
        child.add_argument("--workspace", required=True, choices=["india"])
        return child

    ingest = common("ingest-range")
    ingest.add_argument("--start", required=True, type=date.fromisoformat)
    ingest.add_argument("--end", required=True, type=date.fromisoformat)
    ingest.add_argument("--min-free-gib", type=float, default=3.0)
    ingest.add_argument("--min-interval-seconds", type=float, default=1.0)
    common("archive-depth")
    check = common("check-convention")
    check.add_argument("--start", required=True, type=date.fromisoformat)
    check.add_argument("--end", required=True, type=date.fromisoformat)
    record = common("record-convention")
    record.add_argument("--list-rule", required=True, choices=["next_session_after_file_date", "file_date"])
    record.add_argument("--changes-rule", required=True, choices=["next_session_after_file_date", "file_date"])
    record.add_argument("--evidence-sha256", required=True)
    cover = common("coverage")
    cover.add_argument("--start", required=True, type=date.fromisoformat)
    cover.add_argument("--end", required=True, type=date.fromisoformat)
    args = parser.parse_args(argv)
    owns_client = client is None and args.command == "ingest-range"
    http_client = client or (build_default_client() if owns_client else None)
    exit_code = 0
    try:
        with PilotDataStore(args.root, workspace=args.workspace) as store:
            ensure_band_tables(store)
            if args.command == "ingest-range":
                http = NseHttp(http_client, min_interval_seconds=args.min_interval_seconds)
                payload = ingest_range(store, http, args.start, args.end, workspace=args.workspace,
                                       min_free_bytes=int(args.min_free_gib * 1024**3))
                exit_code = 2 if payload["failed"] else 0
            elif args.command == "archive-depth":
                payload = record_archive_depth(store, probed=_probe_map(store), workspace=args.workspace).model_dump(
                    mode="json")
            elif args.command == "check-convention":
                payload = check_band_convention(store, start=args.start, end=args.end).model_dump(mode="json")
            elif args.command == "record-convention":
                record_band_convention(
                    store,
                    BandConvention(list_rule=args.list_rule, changes_rule=args.changes_rule, basis="operator_confirmed",
                                   evidence_sha256=args.evidence_sha256, recorded_at_utc=utc_now()),
                    workspace=args.workspace,
                )
                payload = {"recorded": True, "list_rule": args.list_rule, "changes_rule": args.changes_rule}
            else:
                targets = latest_target_universe(store, workspace=args.workspace)
                if targets is None:
                    raise PilotDataError("target_universe_missing", "build the target universe first")
                report = build_band_coverage(store, start=args.start, end=args.end, targets=targets,
                                             workspace=args.workspace)
                path = write_coverage_report(args.root, report)
                payload = {"report_path": str(path), "report_sha256": report.report_sha256,
                           "phase62_blocked": report.phase62_blocked, "blocked_reasons": list(report.blocked_reasons),
                           "unknown_count": report.unknown_count, "fixed_count": report.fixed_count,
                           "no_band_count": report.no_band_count,
                           "archive_depth": report.archive_depth.model_dump(mode="json") if report.archive_depth else None}
                exit_code = 3 if report.phase62_blocked else 0
    except PilotDataError as exc:
        print(json.dumps({"caveats": _caveat_payload(), "error_code": exc.code, "error": str(exc)}, indent=2))
        return 2
    finally:
        if owns_client and http_client is not None:
            http_client.close()
    print(json.dumps({"caveats": _caveat_payload(), "result": payload}, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
