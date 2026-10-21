"""Probe, relay security master refresh, and the paced, capped, resumable Breeze backfill.

Every Breeze byte arrives through Phase 61's RelayClient (contract growin-relay/1); this module
has no transport code, takes no relay URL and never reads a credential. Rules it keeps:

* each relay payload is re-validated (contract, sha256, fetch time, request echo) before it is stored;
* a ledger row is written before every Breeze call, and the daily cap counts those rows;
* a request that returns 1000 rows was capped upstream (the cap keeps the newest rows), so it is
  never stored as a complete window;
* prices are stored raw, exactly as Breeze sent them; corporate actions are applied later, locally;
* an empty answer is retried once, then flagged and quarantined, never read as zero volume;
* anything unrecognised stops the run.

Tables are append-only. The only file written outside the store is the run lock.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

from brokers.breeze_gateway import UpstreamPayload
from pydantic import BaseModel, ConfigDict, model_validator

from .breeze_bars import ingest_breeze_response
from .core import (
    BREEZE_RAW_UNVERIFIED_CAVEAT,
    IST,
    CaveatedResult,
    PilotDataError,
    canonical_sha256,
    standard_caveats,
    utc_naive,
    utc_now,
)
from .models import DailyBarConvention, QuarantineRecord
from .relay_contract import (
    API_DAILY_BARS,
    API_SECURITY_MASTER,
    BREEZE_ROW_CAP,
    KIND_DAILY_BARS,
    KIND_SECURITY_MASTER,
    STOCK_CODE_RE,
    RelaySource,
    classify_gateway_error,
    gate_budget,
    gate_health,
    open_relay_source,
    validate_payload,
)
from .security_master import MasterIngestOutcome, ingest_security_master, require_fresh_snapshot
from .sessions import sessions_between
from .store import PilotDataStore
from .targets import TargetMember, TargetUniverseResult, latest_target_universe

PACE_SECONDS = 1.2  # 50 calls a minute, below the relay's 80
DAILY_CALL_CAP = 3500  # per IST day, below the relay's 4,500
MAX_CONSECUTIVE_FAILURES = 3
PAD_DAYS = 3
# Calendar days in one request. About 0.7 trading days per calendar day, so this stays far
# below the 1000-row cap that Breeze applies (and that keeps the newest rows).
MAX_WINDOW_CALENDAR_DAYS = 1000
PROBE_STOCK_CODE = "RELIND"
PROBE_ETF_CODE = "NIFBEE"
PROBE_RECENT_DAYS = 30
PROBE_DEPTH_TOLERANCE_DAYS = 30
PROBE_MIN_COVERAGE_PERCENT = 99
DEFAULT_WINDOW_YEARS = 5

Strategy = Literal["two_windows", "yearly"]
WindowStatus = Literal["pending", "done", "empty_flagged", "failed"]
RunStatus = Literal[
    "complete",
    "completed_with_failed_windows",
    "daily_cap_reached",
    "stopped_fail_closed",
    "stopped_consecutive_failures",
    "incomplete",
]
TERMINAL_STATUSES = frozenset({"done", "empty_flagged"})
_STOCK_CODE = re.compile(STOCK_CODE_RE)

PROBE_DDL = (
    "CREATE TABLE IF NOT EXISTS breeze_probe_records("
    "probe_sha256 VARCHAR PRIMARY KEY, workspace VARCHAR NOT NULL, probed_at_utc TIMESTAMP NOT NULL, "
    "conclusive BOOLEAN NOT NULL, payload_json VARCHAR NOT NULL, source_sha256 VARCHAR, "
    "row_sha256 VARCHAR NOT NULL)"
)
PLANS_DDL = (
    "CREATE TABLE IF NOT EXISTS backfill_plans("
    "plan_sha256 VARCHAR PRIMARY KEY, workspace VARCHAR NOT NULL, payload_json VARCHAR NOT NULL, "
    "created_at_utc TIMESTAMP NOT NULL, source_sha256 VARCHAR, row_sha256 VARCHAR NOT NULL)"
)
EVENTS_DDL = (
    "CREATE TABLE IF NOT EXISTS backfill_events("
    "event_id VARCHAR PRIMARY KEY, plan_sha256 VARCHAR NOT NULL, stock_code VARCHAR NOT NULL, "
    "window_from DATE NOT NULL, window_to DATE NOT NULL, status VARCHAR NOT NULL, attempt INTEGER NOT NULL, "
    "error_code VARCHAR, source_sha256 VARCHAR, at_utc TIMESTAMP NOT NULL, row_sha256 VARCHAR NOT NULL)"
)
LEDGER_DDL = (
    "CREATE TABLE IF NOT EXISTS relay_call_ledger("
    "call_id VARCHAR PRIMARY KEY, workspace VARCHAR NOT NULL, ist_date DATE NOT NULL, "
    "reserved_at_utc TIMESTAMP NOT NULL, endpoint VARCHAR NOT NULL, stock_code VARCHAR, window_from DATE, "
    "window_to DATE, source_sha256 VARCHAR, row_sha256 VARCHAR NOT NULL)"
)
OUTCOMES_DDL = (
    "CREATE TABLE IF NOT EXISTS relay_call_outcomes("
    "call_id VARCHAR PRIMARY KEY, outcome VARCHAR NOT NULL, error_code VARCHAR, source_sha256 VARCHAR, "
    "finished_at_utc TIMESTAMP NOT NULL, row_sha256 VARCHAR NOT NULL)"
)
PROVENANCE_DDL = (
    "CREATE TABLE IF NOT EXISTS relay_provenance("
    "source_sha256 VARCHAR NOT NULL, request_id VARCHAR NOT NULL, contract VARCHAR NOT NULL, "
    "route VARCHAR NOT NULL, query_json VARCHAR NOT NULL, upstream_api VARCHAR NOT NULL, "
    "upstream_request_json VARCHAR NOT NULL, upstream_interval VARCHAR, breeze_status INTEGER, "
    "empty BOOLEAN NOT NULL, upstream_last_modified VARCHAR, fetched_at_utc TIMESTAMP NOT NULL, "
    "row_sha256 VARCHAR NOT NULL, PRIMARY KEY(source_sha256, request_id))"
)


def ensure_backfill_tables(store: PilotDataStore) -> None:
    store.ensure_table("breeze_probe_records", PROBE_DDL, key_columns=("probe_sha256",))
    store.ensure_table("backfill_plans", PLANS_DDL, key_columns=("plan_sha256",))
    store.ensure_table("backfill_events", EVENTS_DDL, key_columns=("event_id",))
    store.ensure_table("relay_call_ledger", LEDGER_DDL, key_columns=("call_id",))
    store.ensure_table("relay_call_outcomes", OUTCOMES_DDL, key_columns=("call_id",))
    store.ensure_table(
        "relay_provenance", PROVENANCE_DDL, key_columns=("source_sha256", "request_id")
    )


def _check_workspace(store: PilotDataStore, workspace: str) -> None:
    if workspace != store.workspace:
        raise PilotDataError("workspace_mismatch", "workspace differs from the store workspace")


def _append_one(store: PilotDataStore, table: str, row: Mapping[str, object], *, check: str) -> None:
    outcome = store.append_rows(table, [row], check=check)
    if outcome.conflicts:
        raise PilotDataError("backfill_row_conflict", f"{table} already holds a different row for this key")


def _iso(value: date | datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def add_years(day: date, years: int) -> date:
    try:
        return day.replace(year=day.year + years)
    except ValueError:  # 29 February
        return day.replace(year=day.year + years, day=28)


# --------------------------------------------------------------------------- call ledger


class _CallLedger:
    """Reserve a ledger row before each relay call, pace calls, and count the day's historical calls."""

    def __init__(
        self,
        store: PilotDataStore,
        *,
        workspace: str,
        clock: Callable[[], datetime],
        monotonic: Callable[[], float],
        sleeper: Callable[[float], None],
        pace_seconds: float,
        daily_call_cap: int,
    ) -> None:
        self._store = store
        self._workspace = workspace
        self._clock = clock
        self._monotonic = monotonic
        self._sleeper = sleeper
        self._pace = pace_seconds
        self._cap = daily_call_cap
        self._last_start: float | None = None

    def used_today(self, ist_date: date) -> int:
        rows = self._store.query(
            "SELECT count(*) FROM relay_call_ledger WHERE workspace = ? AND ist_date = ? "
            "AND endpoint = 'historical'",
            [self._workspace, ist_date],
        )
        return int(rows[0][0])

    def reserve(
        self,
        *,
        endpoint: Literal["historical", "security_master"],
        ist_date: date,
        stock_code: str | None = None,
        window_from: date | None = None,
        window_to: date | None = None,
    ) -> str:
        if self._clock().astimezone(IST).date() != ist_date:
            raise PilotDataError("relay_clock_mismatch", "the clock's IST date differs from the run date")
        if endpoint == "historical" and self.used_today(ist_date) >= self._cap:
            raise PilotDataError("daily_cap_reached", "the local daily Breeze call cap is reached")
        total = self._store.query(
            "SELECT count(*) FROM relay_call_ledger WHERE workspace = ? AND ist_date = ?",
            [self._workspace, ist_date],
        )
        call_id = f"{ist_date.isoformat()}-{int(total[0][0]) + 1:06d}"
        identity = {
            "call_id": call_id, "workspace": self._workspace, "ist_date": ist_date.isoformat(),
            "endpoint": endpoint, "stock_code": stock_code, "window_from": _iso(window_from),
            "window_to": _iso(window_to),
        }
        row = {
            "call_id": call_id, "workspace": self._workspace, "ist_date": ist_date,
            "reserved_at_utc": utc_naive(self._clock()), "endpoint": endpoint, "stock_code": stock_code,
            "window_from": window_from, "window_to": window_to, "source_sha256": None,
            "row_sha256": canonical_sha256(identity),
        }
        outcome = self._store.append_rows("relay_call_ledger", [row], check="relay_call_ledger")
        if outcome.inserted != 1:
            raise PilotDataError("ledger_write_failed", "the call reservation was not written")
        return call_id

    def pace(self) -> None:
        now = self._monotonic()
        if self._last_start is not None:
            wait = self._pace - (now - self._last_start)
            if wait > 0:
                self._sleeper(wait)
                now = self._monotonic()
        self._last_start = now

    def finish(self, call_id: str, outcome: str, error_code: str | None, source_sha256: str | None) -> None:
        row = {
            "call_id": call_id, "outcome": outcome, "error_code": error_code, "source_sha256": source_sha256,
            "finished_at_utc": utc_naive(self._clock()),
            "row_sha256": canonical_sha256(
                {"call_id": call_id, "outcome": outcome, "error_code": error_code, "source_sha256": source_sha256}
            ),
        }
        _append_one(self._store, "relay_call_outcomes", row, check="relay_call_outcome")


def _record_provenance(store: PilotDataStore, payload: UpstreamPayload) -> None:
    """One row per relay response, from UpstreamPayload.provenance() and its own fields."""
    provenance = payload.provenance()
    content = {
        "source_sha256": payload.sha256,
        "request_id": payload.request_id,
        "contract": provenance["contract"],
        "route": provenance["route"],
        "query_json": json.dumps(provenance["query"], sort_keys=True),
        "upstream_api": provenance["upstream_api"],
        "upstream_request_json": json.dumps(provenance["upstream_request"], sort_keys=True),
        "upstream_interval": payload.upstream_interval,
        "breeze_status": payload.breeze_status,
        "empty": bool(payload.empty),
        "upstream_last_modified": payload.upstream_last_modified,
        "fetched_at_utc": provenance["fetched_at_utc"],
    }
    row = {**content, "fetched_at_utc": utc_naive(payload.fetched_at_utc), "row_sha256": canonical_sha256(content)}
    _append_one(store, "relay_provenance", row, check="relay_provenance")


def _relay_failure(exc: BaseException) -> PilotDataError:
    _, code, _ = classify_gateway_error(exc)
    return PilotDataError(code, "relay call failed")


# --------------------------------------------------------------------------- probe


class BreezeProbeRecord(BaseModel):
    """What the probe learned: the daily bar convention, and which window strategy is safe."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace: Literal["india"]
    probed_at_utc: datetime
    window_start: date
    window_end: date
    recent_start: date
    recent_end: date
    conclusive: bool
    strategy: Strategy
    reasons: tuple[str, ...]
    truncation_detected: bool
    depth_limited: bool
    allowed_times: tuple[str, ...]
    full_rows: int
    earliest_date: date | None
    full_source_sha256: str
    recent_source_sha256s: tuple[str, str]

    def probe_sha256(self) -> str:
        content = self.model_dump(mode="json")
        content.pop("probed_at_utc")
        return canonical_sha256(content)

    def convention(self) -> DailyBarConvention:
        if not self.conclusive or len(self.allowed_times) != 1:
            raise PilotDataError("probe_inconclusive", "an inconclusive probe gives no bar convention")
        return DailyBarConvention(
            allowed_times=self.allowed_times, source=f"probe:{self.probe_sha256()[:16]}"
        )


_HARD_REASON_EXEMPT = frozenset({"truncation_detected", "depth_limited"})


def _probe_rows(body: bytes) -> list[tuple[date, str]] | None:
    try:
        envelope = json.loads(body, parse_float=Decimal)
        rows = envelope["Success"]
        parsed = []
        for row in rows:
            stamp = datetime.strptime(row["datetime"], "%Y-%m-%d %H:%M:%S")
            parsed.append((stamp.date(), stamp.strftime("%H:%M:%S")))
    except (ValueError, KeyError, TypeError, AttributeError):
        return None
    return parsed


def _coverage_short(expected: list[date], present: set[date]) -> bool:
    if not expected:
        return False
    found = sum(1 for day in expected if day in present)
    return found * 100 < len(expected) * PROBE_MIN_COVERAGE_PERCENT


def _fetch_for_probe(
    store: PilotDataStore,
    source: RelaySource,
    ledger: _CallLedger,
    *,
    stock_code: str,
    start: date,
    end: date,
    today_ist: date,
) -> UpstreamPayload:
    call_id = ledger.reserve(
        endpoint="historical", ist_date=today_ist, stock_code=stock_code, window_from=start, window_to=end
    )
    ledger.pace()
    try:
        payload = source.daily_bars(stock_code, start, end)
    except Exception as exc:
        failure = _relay_failure(exc)
        ledger.finish(call_id, "error", failure.code, None)
        raise failure from exc
    query = {"stock_code": stock_code, "from": start.isoformat(), "to": end.isoformat(), "exchange": "NSE"}
    try:
        descriptor = validate_payload(
            payload, expected_api=API_DAILY_BARS, kind=KIND_DAILY_BARS, expected_query=query
        )
    except PilotDataError as exc:
        ledger.finish(call_id, "rejected", exc.code, None)
        raise
    if payload.empty:
        ledger.finish(call_id, "empty", "breeze_empty_response", payload.sha256)
        raise PilotDataError("breeze_empty_response", "the probe call came back empty")
    ledger.finish(call_id, "ok", None, payload.sha256)
    store.register_source(descriptor, payload.body)
    _record_provenance(store, payload)
    return payload


def run_probe(
    store: PilotDataStore,
    source: RelaySource,
    *,
    window_start: date,
    window_end: date,
    today_ist: date,
    workspace: Literal["india"],
    clock: Callable[[], datetime] = utc_now,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    pace_seconds: float = PACE_SECONDS,
    daily_call_cap: int = DAILY_CALL_CAP,
) -> BreezeProbeRecord:
    """Three paced, ledgered calls: RELIND over the window, RELIND and NIFBEE over the last 30 days."""
    _check_workspace(store, workspace)
    if window_end < window_start:
        raise PilotDataError("window_invalid", "window_end is before window_start")
    ensure_backfill_tables(store)
    ledger = _CallLedger(
        store, workspace=workspace, clock=clock, monotonic=monotonic, sleeper=sleeper,
        pace_seconds=pace_seconds, daily_call_cap=daily_call_cap,
    )
    recent_end = today_ist - timedelta(days=1)  # today's session is pending in the calendar until NSE publishes
    recent_start = today_ist - timedelta(days=PROBE_RECENT_DAYS)
    full = _fetch_for_probe(
        store, source, ledger, stock_code=PROBE_STOCK_CODE, start=window_start, end=window_end, today_ist=today_ist
    )
    recent_stock = _fetch_for_probe(
        store, source, ledger, stock_code=PROBE_STOCK_CODE, start=recent_start, end=recent_end, today_ist=today_ist
    )
    recent_etf = _fetch_for_probe(
        store, source, ledger, stock_code=PROBE_ETF_CODE, start=recent_start, end=recent_end, today_ist=today_ist
    )

    reasons: set[str] = set()
    times: set[str] = set()
    truncated = depth_limited = False
    earliest: date | None = None
    full_rows = 0
    full_parsed = _probe_rows(full.body)
    if full_parsed is None or not full_parsed:
        reasons.add("probe_response_unparseable")
    else:
        full_rows = len(full_parsed)
        times |= {stamp for _, stamp in full_parsed}
        dates = {day for day, _ in full_parsed}
        earliest = min(dates)
        truncated = full_rows >= BREEZE_ROW_CAP
        if truncated:
            reasons.add("truncation_detected")
        elif (earliest - window_start).days > PROBE_DEPTH_TOLERANCE_DAYS:
            depth_limited = True
            reasons.add("depth_limited")
        sessions = sessions_between(store, window_start, window_end)
        if dates - set(sessions):
            reasons.add("bar_on_non_session")
        first_expected = earliest if (truncated or depth_limited) else window_start
        if _coverage_short([day for day in sessions if day >= first_expected], dates):
            reasons.add("coverage_below_99_percent")
    recent_sessions = sessions_between(store, recent_start, recent_end)
    for payload in (recent_stock, recent_etf):
        parsed = _probe_rows(payload.body)
        if parsed is None or not parsed:
            reasons.add("probe_response_unparseable")
            continue
        times |= {stamp for _, stamp in parsed}
        dates = {day for day, _ in parsed}
        if dates - set(recent_sessions):
            reasons.add("bar_on_non_session")
        if _coverage_short(list(recent_sessions), dates):
            reasons.add("recent_sessions_missing")
    if len(times) > 1:
        reasons.add("multiple_times_of_day")

    conclusive = not (reasons - _HARD_REASON_EXEMPT) and len(times) == 1
    record = BreezeProbeRecord(
        workspace=workspace,
        probed_at_utc=clock(),
        window_start=window_start,
        window_end=window_end,
        recent_start=recent_start,
        recent_end=recent_end,
        conclusive=conclusive,
        strategy="two_windows" if conclusive and not (truncated or depth_limited) else "yearly",
        reasons=tuple(sorted(reasons)),
        truncation_detected=truncated,
        depth_limited=depth_limited,
        allowed_times=tuple(sorted(times)),
        full_rows=full_rows,
        earliest_date=earliest,
        full_source_sha256=full.sha256,
        recent_source_sha256s=(recent_stock.sha256, recent_etf.sha256),
    )
    digest = record.probe_sha256()
    _append_one(
        store,
        "breeze_probe_records",
        {
            "probe_sha256": digest, "workspace": workspace, "probed_at_utc": utc_naive(record.probed_at_utc),
            "conclusive": conclusive, "payload_json": json.dumps(record.model_dump(mode="json"), sort_keys=True),
            "source_sha256": full.sha256, "row_sha256": digest,
        },
        check="breeze_probe",
    )
    if conclusive and not (truncated or depth_limited):
        # A capped or depth-limited body would claim coverage of days it lost, so only a whole answer is ingested.
        descriptor = validate_payload(full, expected_api=API_DAILY_BARS, kind=KIND_DAILY_BARS)
        ingest_breeze_response(
            store, descriptor, full.body, stock_code=PROBE_STOCK_CODE, requested_from=window_start,
            requested_to=window_end, convention=record.convention(),
        )
    return record


def latest_conclusive_probe(store: PilotDataStore) -> BreezeProbeRecord | None:
    ensure_backfill_tables(store)
    rows = store.query(
        "SELECT probe_sha256, payload_json FROM breeze_probe_records WHERE conclusive "
        "ORDER BY probed_at_utc DESC, rowid DESC LIMIT 1"
    )
    if not rows:
        return None
    record = BreezeProbeRecord.model_validate(json.loads(rows[0][1]))
    if record.probe_sha256() != rows[0][0] or not record.conclusive:
        raise PilotDataError("probe_record_corrupt", "a stored probe no longer matches its hash")
    return record


# --------------------------------------------------------------------------- security master


def refresh_security_master(
    store: PilotDataStore,
    source: RelaySource,
    *,
    workspace: Literal["india"],
    clock: Callable[[], datetime] = utc_now,
) -> MasterIngestOutcome:
    """Fetch the master through the relay, re-validate it, and ingest it as a relay_fetch snapshot."""
    _check_workspace(store, workspace)
    ensure_backfill_tables(store)
    ledger = _CallLedger(
        store, workspace=workspace, clock=clock, monotonic=time.monotonic, sleeper=time.sleep,
        pace_seconds=PACE_SECONDS, daily_call_cap=DAILY_CALL_CAP,
    )
    ist_date = clock().astimezone(IST).date()
    call_id = ledger.reserve(endpoint="security_master", ist_date=ist_date)
    try:
        payload = source.security_master()
    except Exception as exc:
        failure = _relay_failure(exc)
        ledger.finish(call_id, "error", failure.code, None)
        raise failure from exc
    try:
        descriptor = validate_payload(payload, expected_api=API_SECURITY_MASTER, kind=KIND_SECURITY_MASTER)
    except PilotDataError as exc:
        ledger.finish(call_id, "rejected", exc.code, None)
        raise
    ledger.finish(call_id, "ok", None, payload.sha256)
    store.register_source(descriptor, payload.body)
    _record_provenance(store, payload)
    return ingest_security_master(
        store, descriptor, payload.body, snapshot_date=payload.fetched_at_utc.astimezone(IST).date(),
        snapshot_date_basis="relay_fetch",
    )


# --------------------------------------------------------------------------- plan


def plan_windows(
    window_start: date, window_end: date, strategy: Strategy, *, pad_days: int = PAD_DAYS
) -> tuple[tuple[date, date], ...]:
    """Split [window_start, window_end] and pad each piece by pad_days on both sides.

    Padding is wider than needed on purpose: rows are filtered by parsed trading date later, and the
    overlap is harmless because identical bars from two responses count once.
    """
    if window_end < window_start:
        raise PilotDataError("window_invalid", "window_end is before window_start")
    if not isinstance(pad_days, int) or isinstance(pad_days, bool) or pad_days < 0:
        raise PilotDataError("window_invalid", "pad_days must be a non-negative integer")
    pieces: list[tuple[date, date]]
    if strategy == "two_windows":
        first_days = ((window_end - window_start).days + 1) // 2
        if first_days < 1:
            pieces = [(window_start, window_end)]
        else:
            mid = window_start + timedelta(days=first_days - 1)
            pieces = [(window_start, mid), (mid + timedelta(days=1), window_end)]
    elif strategy == "yearly":
        starts = [window_start]
        while add_years(window_start, len(starts)) < window_end:
            starts.append(add_years(window_start, len(starts)))
        pieces = [
            (start, starts[index + 1] - timedelta(days=1) if index + 1 < len(starts) else window_end)
            for index, start in enumerate(starts)
        ]
    else:
        raise PilotDataError("strategy_invalid", "strategy must be two_windows or yearly")
    padded = tuple(
        (start - timedelta(days=pad_days), end + timedelta(days=pad_days)) for start, end in pieces
    )
    for start, end in padded:
        if (end - start).days + 1 > MAX_WINDOW_CALENDAR_DAYS:
            raise PilotDataError(
                "window_too_large", f"a request would span more than {MAX_WINDOW_CALENDAR_DAYS} calendar days"
            )
    return padded


class BackfillPlan(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace: Literal["india"]
    window_start: date
    window_end: date
    strategy: Strategy
    pad_days: int
    windows: tuple[tuple[date, date], ...]
    stock_codes: tuple[str, ...]
    probe_sha256: str
    targets_sha256: str | None
    plan_sha256: str

    @staticmethod
    def _digest(content: dict) -> str:
        return canonical_sha256({key: value for key, value in content.items() if key != "plan_sha256"})

    @model_validator(mode="after")
    def _hash_matches(self) -> "BackfillPlan":
        if self._digest(self.model_dump(mode="json")) != self.plan_sha256:
            raise ValueError("plan_sha256 does not match the plan content")
        return self

    @classmethod
    def create(
        cls,
        *,
        workspace: Literal["india"],
        window_start: date,
        window_end: date,
        strategy: Strategy,
        pad_days: int,
        windows: tuple[tuple[date, date], ...],
        stock_codes: tuple[str, ...],
        probe_sha256: str,
        targets_sha256: str | None,
    ) -> "BackfillPlan":
        content = {
            "workspace": workspace, "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(), "strategy": strategy, "pad_days": pad_days,
            "windows": [[a.isoformat(), b.isoformat()] for a, b in windows], "stock_codes": list(stock_codes),
            "probe_sha256": probe_sha256, "targets_sha256": targets_sha256,
        }
        return cls(**{**content, "windows": windows, "stock_codes": stock_codes, "window_start": window_start,
                      "window_end": window_end}, plan_sha256=cls._digest(content))


def load_plan(store: PilotDataStore, plan_sha256: str | None = None) -> BackfillPlan:
    ensure_backfill_tables(store)
    if plan_sha256 is None:
        rows = store.query(
            "SELECT payload_json FROM backfill_plans ORDER BY created_at_utc DESC, rowid DESC LIMIT 1"
        )
    else:
        rows = store.query("SELECT payload_json FROM backfill_plans WHERE plan_sha256 = ?", [plan_sha256])
    if not rows:
        raise PilotDataError("plan_missing", "no such backfill plan is stored")
    return BackfillPlan.model_validate(json.loads(rows[0][0]))


class BackfillRunReport(CaveatedResult):
    plan_sha256: str
    ist_date: date
    status: RunStatus
    stop_code: str | None
    calls_this_run: int
    calls_used_today: int
    relay_remaining_today: int | None
    windows_total: int
    windows_remaining: int
    counts: dict[str, int]
    error_codes: dict[str, int]


class _Stop(Exception):
    """Ends the run now. Carries the run status and the stable code."""

    def __init__(self, status: RunStatus, code: str) -> None:
        super().__init__(code)
        self.status: RunStatus = status
        self.code = code


@dataclass(frozen=True)
class _WindowState:
    status: WindowStatus
    attempt: int
    error_code: str | None


def _row_count(body: bytes) -> int | None:
    try:
        rows = json.loads(body)["Success"]
    except (ValueError, KeyError, TypeError):
        return None
    return len(rows) if isinstance(rows, list) else None


# --------------------------------------------------------------------------- engine


class BackfillEngine:
    def __init__(
        self,
        store: PilotDataStore,
        source: RelaySource | None,
        *,
        workspace: Literal["india"],
        clock: Callable[[], datetime] = utc_now,
        monotonic: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
        pace_seconds: float = PACE_SECONDS,
        daily_call_cap: int = DAILY_CALL_CAP,
        max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES,
    ) -> None:
        _check_workspace(store, workspace)
        if pace_seconds < PACE_SECONDS:
            raise PilotDataError("pace_too_fast", f"pacing may not be faster than {PACE_SECONDS} s per call")
        if not 1 <= daily_call_cap <= DAILY_CALL_CAP:
            raise PilotDataError("cap_invalid", f"the daily call cap must be between 1 and {DAILY_CALL_CAP}")
        if max_consecutive_failures < 1:
            raise PilotDataError("cap_invalid", "max_consecutive_failures must be at least 1")
        ensure_backfill_tables(store)
        self._store = store
        self._source = source
        self._workspace = workspace
        self._clock = clock
        self._sleeper = sleeper
        self._max_failures = max_consecutive_failures
        self._ledger = _CallLedger(
            store, workspace=workspace, clock=clock, monotonic=monotonic, sleeper=sleeper,
            pace_seconds=pace_seconds, daily_call_cap=daily_call_cap,
        )
        self._lock_path = store.root / "backfill.lock"
        self._holds_lock = False

    def _relay(self) -> RelaySource:
        if self._source is None:
            raise PilotDataError("relay_source_missing", "this engine was built without a relay source")
        return self._source

    # ---------------------------------------------------------------- lock
    def _acquire_lock(self, break_lock: bool) -> None:
        if break_lock:
            with contextlib.suppress(FileNotFoundError):
                self._lock_path.unlink()
        try:
            fd = os.open(self._lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise PilotDataError("backfill_locked", "another backfill run holds the lock") from None
        with os.fdopen(fd, "w") as handle:
            handle.write(f"{os.getpid()} {self._clock().isoformat()}\n")
        self._holds_lock = True

    def release_lock(self) -> None:
        if self._holds_lock:
            with contextlib.suppress(FileNotFoundError):
                self._lock_path.unlink()
            self._holds_lock = False

    # ----------------------------------------------------------- preflight
    def preflight(self, *, today_ist: date, break_lock: bool = False) -> None:
        """Refuse to start unless the relay session, budget, security master and probe are all good.

        On success this holds the run lock; the caller pairs it with release_lock() (run() does).
        """
        self._acquire_lock(break_lock)
        try:
            relay = self._relay()
            try:
                health = relay.health()
            except Exception as exc:
                raise _relay_failure(exc) from exc
            gate_health(health)
            try:
                budget = relay.budget()
            except Exception as exc:
                raise _relay_failure(exc) from exc
            gate_budget(budget, today_ist=today_ist)
            snapshot = require_fresh_snapshot(self._store, today_ist=today_ist)
            if snapshot.snapshot_date_basis != "relay_fetch":
                raise PilotDataError(
                    "security_master_not_relay_fetched", "today's security master was not fetched through the relay"
                )
            if latest_conclusive_probe(self._store) is None:
                raise PilotDataError("probe_missing", "no conclusive Breeze probe is stored")
        except BaseException:
            self.release_lock()
            raise

    # --------------------------------------------------------------- plan
    def build_plan(
        self,
        targets: TargetUniverseResult | Iterable[TargetMember],
        *,
        window_start: date,
        window_end: date,
    ) -> BackfillPlan:
        probe = latest_conclusive_probe(self._store)
        if probe is None:
            raise PilotDataError("probe_missing", "no conclusive Breeze probe is stored")
        if isinstance(targets, TargetUniverseResult):
            members: Iterable[TargetMember] = targets.members
            targets_sha256: str | None = targets.target_sha256
        else:
            members, targets_sha256 = targets, None
        codes = sorted({member.stock_code for member in members})
        if not codes:
            raise PilotDataError("targets_empty", "the target universe has no members")
        if not all(_STOCK_CODE.fullmatch(code) for code in codes):
            raise PilotDataError("target_stock_code_invalid", "a target stock code is not a Breeze ShortName")
        plan = BackfillPlan.create(
            workspace=self._workspace, window_start=window_start, window_end=window_end,
            strategy=probe.strategy, pad_days=PAD_DAYS,
            windows=plan_windows(window_start, window_end, probe.strategy, pad_days=PAD_DAYS),
            stock_codes=tuple(codes), probe_sha256=probe.probe_sha256(), targets_sha256=targets_sha256,
        )
        _append_one(
            self._store,
            "backfill_plans",
            {
                "plan_sha256": plan.plan_sha256, "workspace": self._workspace,
                "payload_json": json.dumps(plan.model_dump(mode="json"), sort_keys=True),
                "created_at_utc": utc_naive(self._clock()), "source_sha256": None,
                "row_sha256": plan.plan_sha256,
            },
            check="backfill_plan",
        )
        pending = [
            self._event_row(plan, code, window, "pending", 0, None, None)
            for code in plan.stock_codes
            for window in plan.windows
        ]
        outcome = self._store.append_rows("backfill_events", pending, check="backfill_event")
        if outcome.conflicts:
            raise PilotDataError("backfill_row_conflict", "backfill_events already holds a different row")
        return plan

    # ------------------------------------------------------------- events
    def _event_row(
        self,
        plan: BackfillPlan,
        stock_code: str,
        window: tuple[date, date],
        status: WindowStatus,
        attempt: int,
        error_code: str | None,
        source_sha256: str | None,
    ) -> dict[str, object]:
        identity = {
            "plan_sha256": plan.plan_sha256, "stock_code": stock_code, "window_from": window[0].isoformat(),
            "window_to": window[1].isoformat(), "status": status, "attempt": attempt,
        }
        return {
            "event_id": canonical_sha256(identity), "plan_sha256": plan.plan_sha256, "stock_code": stock_code,
            "window_from": window[0], "window_to": window[1], "status": status, "attempt": attempt,
            "error_code": error_code, "source_sha256": source_sha256, "at_utc": utc_naive(self._clock()),
            "row_sha256": canonical_sha256({**identity, "error_code": error_code, "source_sha256": source_sha256}),
        }

    def _states(self, plan: BackfillPlan) -> dict[tuple[str, date, date], _WindowState]:
        rows = self._store.query(
            "SELECT stock_code, window_from, window_to, status, attempt, error_code FROM backfill_events "
            "WHERE plan_sha256 = ? ORDER BY at_utc, rowid",
            [plan.plan_sha256],
        )
        states: dict[tuple[str, date, date], _WindowState] = {}
        for code, start, end, status, attempt, error_code in rows:
            states[(code, start, end)] = _WindowState(status, int(attempt), error_code)
        return states

    def _write_event(
        self,
        plan: BackfillPlan,
        stock_code: str,
        window: tuple[date, date],
        status: WindowStatus,
        attempt: int,
        error_code: str | None,
        source_sha256: str | None,
    ) -> None:
        _append_one(
            self._store, "backfill_events",
            self._event_row(plan, stock_code, window, status, attempt, error_code, source_sha256),
            check="backfill_event",
        )

    # ---------------------------------------------------------------- run
    def run(self, plan: BackfillPlan, *, today_ist: date, break_lock: bool = False) -> BackfillRunReport:
        self._check_plan(plan)
        self.preflight(today_ist=today_ist, break_lock=break_lock)
        try:
            return self._run_locked(plan, today_ist)
        finally:
            self.release_lock()

    def _check_plan(self, plan: BackfillPlan) -> None:
        _check_workspace(self._store, plan.workspace)
        stored = self._store.query("SELECT count(*) FROM backfill_plans WHERE plan_sha256 = ?", [plan.plan_sha256])
        if not stored[0][0]:
            raise PilotDataError("plan_missing", "the plan was not stored with build_plan")

    def _run_locked(self, plan: BackfillPlan, today_ist: date) -> BackfillRunReport:
        probe = latest_conclusive_probe(self._store)
        if probe is None:
            raise PilotDataError("probe_missing", "no conclusive Breeze probe is stored")
        convention = probe.convention()
        states = self._states(plan)
        todo = [
            (code, window)
            for code in plan.stock_codes
            for window in plan.windows
            if states.get((code, window[0], window[1]), _WindowState("pending", 0, None)).status
            not in TERMINAL_STATUSES
        ]
        used_before = self._ledger.used_today(today_ist)
        status: RunStatus = "complete"
        stop_code: str | None = None
        consecutive = 0
        for code, window in todo:
            prior = states.get((code, window[0], window[1]))
            attempt = (prior.attempt if prior is not None else 0) + 1
            try:
                outcome, detail = self._process_window(plan, code, window, attempt, convention, today_ist)
            except _Stop as stop:
                status, stop_code = stop.status, stop.code
                break
            if outcome == "failed":
                consecutive += 1
                if consecutive >= self._max_failures:
                    status, stop_code = "stopped_consecutive_failures", detail
                    break
            else:
                consecutive = 0
        report = self._report(plan, today_ist, status, stop_code, self._ledger.used_today(today_ist) - used_before)
        if report.status == "complete" and report.windows_remaining:
            return report.model_copy(update={"status": "completed_with_failed_windows"})
        return report

    def _process_window(
        self,
        plan: BackfillPlan,
        code: str,
        window: tuple[date, date],
        attempt: int,
        convention: DailyBarConvention,
        today_ist: date,
    ) -> tuple[WindowStatus, str | None]:
        """Fetch one window with at most one retry. Returns the window's new status and a code."""
        w_from, w_to = window
        query = {"stock_code": code, "from": w_from.isoformat(), "to": w_to.isoformat(), "exchange": "NSE"}
        empty_code: str | None = None
        empty_sha: str | None = None
        for try_number in (1, 2):
            try:
                call_id = self._ledger.reserve(
                    endpoint="historical", ist_date=today_ist, stock_code=code, window_from=w_from, window_to=w_to
                )
            except PilotDataError as exc:
                if exc.code == "daily_cap_reached":
                    raise _Stop("daily_cap_reached", exc.code) from None
                raise _Stop("stopped_fail_closed", exc.code) from None
            self._ledger.pace()
            try:
                payload = self._relay().daily_bars(code, w_from, w_to)
            except Exception as exc:
                action, error_code, sleep_for = classify_gateway_error(exc)
                self._ledger.finish(call_id, "error", error_code, None)
                if action == "stop":
                    raise _Stop("stopped_fail_closed", error_code) from None
                if action == "fail_window":
                    return self._fail(plan, code, window, attempt, error_code, None)
                if action == "empty":
                    empty_code, empty_sha = error_code, None
                    if try_number == 1:
                        continue
                    return self._flag_empty(plan, code, window, attempt, empty_code, empty_sha)
                if try_number == 1:  # retry_once
                    if sleep_for > 0:
                        self._sleeper(sleep_for)
                    continue
                return self._fail(plan, code, window, attempt, error_code, None)
            try:
                descriptor = validate_payload(
                    payload, expected_api=API_DAILY_BARS, kind=KIND_DAILY_BARS, expected_query=query
                )
            except PilotDataError as exc:
                self._ledger.finish(call_id, "rejected", exc.code, None)
                raise _Stop("stopped_fail_closed", exc.code) from None
            if payload.empty:
                self._store.register_source(descriptor, payload.body)
                _record_provenance(self._store, payload)
                self._ledger.finish(call_id, "empty", "breeze_empty_response", payload.sha256)
                empty_code, empty_sha = "breeze_empty_response", payload.sha256
                if try_number == 1:
                    continue
                return self._flag_empty(plan, code, window, attempt, empty_code, empty_sha)
            rows = _row_count(payload.body)
            if rows is not None and rows >= BREEZE_ROW_CAP:
                # Capped upstream, newest rows kept: the oldest days of this window are missing.
                self._ledger.finish(call_id, "capped", "breeze_capped_window", payload.sha256)
                return self._fail(plan, code, window, attempt, "breeze_capped_window", payload.sha256)
            self._store.register_source(descriptor, payload.body)
            _record_provenance(self._store, payload)
            try:
                ingested = ingest_breeze_response(
                    self._store, descriptor, payload.body, stock_code=code, requested_from=w_from,
                    requested_to=w_to, convention=convention,
                )
            except PilotDataError as exc:
                if exc.code == "breeze_empty_response":
                    self._ledger.finish(call_id, "empty", exc.code, payload.sha256)
                    empty_code, empty_sha = exc.code, payload.sha256
                    if try_number == 1:
                        continue
                    return self._flag_empty(plan, code, window, attempt, empty_code, empty_sha)
                if exc.code.startswith("breeze_"):
                    self._ledger.finish(call_id, "rejected", exc.code, payload.sha256)
                    return self._fail(plan, code, window, attempt, exc.code, payload.sha256)
                self._ledger.finish(call_id, "error", exc.code, payload.sha256)
                raise _Stop("stopped_fail_closed", exc.code) from None
            if ingested.bars_inserted + ingested.bars_identical == 0:
                # Every row was quarantined by the parser: nothing usable arrived for this window.
                self._ledger.finish(call_id, "rejected", "breeze_no_usable_bars", payload.sha256)
                return self._fail(plan, code, window, attempt, "breeze_no_usable_bars", payload.sha256)
            self._ledger.finish(call_id, "ok", None, payload.sha256)
            self._write_event(plan, code, window, "done", attempt, None, payload.sha256)
            return "done", None
        raise AssertionError("unreachable: the loop returns or continues on both tries")  # pragma: no cover

    def _fail(
        self,
        plan: BackfillPlan,
        code: str,
        window: tuple[date, date],
        attempt: int,
        error_code: str,
        source_sha256: str | None,
    ) -> tuple[WindowStatus, str | None]:
        self._write_event(plan, code, window, "failed", attempt, error_code, source_sha256)
        return "failed", error_code

    def _flag_empty(
        self,
        plan: BackfillPlan,
        code: str,
        window: tuple[date, date],
        attempt: int,
        reason_code: str,
        source_sha256: str | None,
    ) -> tuple[WindowStatus, str | None]:
        self._store.record_quarantine(
            [
                QuarantineRecord(
                    workspace=self._workspace, check="breeze_backfill", reason_code=reason_code, scope="raw",
                    stock_code=code, date_from=window[0], date_to=window[1],
                    detail={"plan_sha256": plan.plan_sha256, "calls": "2"},
                    evidence_sha256s=(source_sha256,) if source_sha256 else (),
                )
            ]
        )
        self._write_event(plan, code, window, "empty_flagged", attempt, reason_code, source_sha256)
        return "empty_flagged", reason_code

    # ------------------------------------------------------------- status
    def status(self, plan: BackfillPlan) -> BackfillRunReport:
        today_ist = self._clock().astimezone(IST).date()
        report = self._report(plan, today_ist, "incomplete", None, 0)
        return report.model_copy(update={"status": "complete" if report.windows_remaining == 0 else "incomplete"})

    def _report(
        self, plan: BackfillPlan, today_ist: date, status: RunStatus, stop_code: str | None, calls_this_run: int
    ) -> BackfillRunReport:
        states = self._states(plan)
        counts = {"pending": 0, "done": 0, "empty_flagged": 0, "failed": 0}
        errors: dict[str, int] = {}
        for code in plan.stock_codes:
            for window in plan.windows:
                state = states.get((code, window[0], window[1]), _WindowState("pending", 0, None))
                counts[state.status] += 1
                if state.status in ("failed", "empty_flagged") and state.error_code:
                    errors[state.error_code] = errors.get(state.error_code, 0) + 1
        remaining: int | None = None
        if self._source is not None:
            try:
                remaining = int(self._source.budget().remaining_today)
            except Exception:
                remaining = None
        return BackfillRunReport(
            workspace=self._workspace,
            caveats=standard_caveats(BREEZE_RAW_UNVERIFIED_CAVEAT),
            plan_sha256=plan.plan_sha256,
            ist_date=today_ist,
            status=status,
            stop_code=stop_code,
            calls_this_run=calls_this_run,
            calls_used_today=self._ledger.used_today(today_ist),
            relay_remaining_today=remaining,
            windows_total=len(plan.stock_codes) * len(plan.windows),
            windows_remaining=counts["pending"] + counts["failed"],
            counts=counts,
            error_codes=dict(sorted(errors.items())),
        )


# --------------------------------------------------------------------------- CLI

_RELAY_COMMANDS = frozenset({"health", "probe", "refresh-master", "run"})
_EXIT_RESUME_LATER = 3
_EXIT_FAIL_CLOSED = 2
_EXIT_INCOMPLETE = 1


def _caveat_payload() -> list[dict[str, str]]:
    return [caveat.model_dump() for caveat in standard_caveats(BREEZE_RAW_UNVERIFIED_CAVEAT)]


def _health_payload(source: RelaySource) -> dict[str, object]:
    health = source.health()
    payload: dict[str, object] = {
        "health": {
            "state": health.state, "login_pending": health.login_pending,
            "expires_at_ist": _iso(health.expires_at_ist), "egress_ok": health.egress_ok,
            "egress_checked_at_utc": _iso(health.egress_checked_at_utc),
            "server_time_utc": health.server_time_utc.isoformat(), "version": health.version,
        },
        "budget": None,
    }
    try:
        budget = source.budget()
    except Exception as exc:
        payload["budget_error_code"] = classify_gateway_error(exc)[1]
    else:
        payload["budget"] = {
            "calls_last_minute": budget.calls_last_minute, "calls_today_ist": budget.calls_today_ist,
            "per_minute_limit": budget.per_minute_limit, "per_day_limit": budget.per_day_limit,
            "remaining_today": budget.remaining_today, "ist_date": budget.ist_date.isoformat(),
        }
    return payload


def _exit_for_report(report: BackfillRunReport) -> int:
    if report.status == "complete":
        return 0
    if report.status == "daily_cap_reached" or report.stop_code == "relay_budget_day":
        return _EXIT_RESUME_LATER
    if report.status == "completed_with_failed_windows" or report.status == "incomplete":
        return _EXIT_INCOMPLETE
    return _EXIT_FAIL_CLOSED


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pilot_data.backfill",
        description="Breeze daily-bar backfill through the Phase 61 relay client. Exit codes: 0 complete, "
        "1 finished with failed windows, 2 fail-closed stop, 3 daily cap or relay day budget reached "
        "(resume next IST day).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def common(name: str) -> argparse.ArgumentParser:
        child = sub.add_parser(name)
        child.add_argument("--root", required=True, type=Path)
        child.add_argument("--workspace", required=True, choices=["india"])
        return child

    common("health")
    probe = common("probe")
    probe.add_argument("--window-start", type=date.fromisoformat)
    probe.add_argument("--window-end", type=date.fromisoformat)
    common("refresh-master")
    plan = common("plan")
    plan.add_argument("--window-start", required=True, type=date.fromisoformat)
    plan.add_argument("--window-end", required=True, type=date.fromisoformat)
    run = common("run")
    run.add_argument("--plan-sha256")
    run.add_argument("--break-lock", action="store_true")
    status = common("status")
    status.add_argument("--plan-sha256")
    return parser


def _dispatch(
    args: argparse.Namespace,
    store: PilotDataStore,
    source: RelaySource | None,
    *,
    today_ist: date,
    clock: Callable[[], datetime],
    monotonic: Callable[[], float],
    sleeper: Callable[[float], None],
) -> tuple[dict[str, object], int]:
    command = args.command
    if command == "health":
        return _health_payload(_need(source)), 0
    if command == "probe":
        end = args.window_end or today_ist - timedelta(days=1)
        start = args.window_start or add_years(end, -DEFAULT_WINDOW_YEARS)
        record = run_probe(
            store, _need(source), window_start=start, window_end=end, today_ist=today_ist,
            workspace=args.workspace, clock=clock, monotonic=monotonic, sleeper=sleeper,
        )
        body = record.model_dump(mode="json")
        body["probe_sha256"] = record.probe_sha256()
        return body, 0 if record.conclusive else _EXIT_FAIL_CLOSED
    if command == "refresh-master":
        done = refresh_security_master(store, _need(source), workspace=args.workspace, clock=clock)
        return {
            "snapshot_sha256": done.snapshot_sha256, "snapshot_date": done.snapshot_date.isoformat(),
            "row_count": done.row_count, "skipped_rows": done.skipped_rows,
            "changes": [change.model_dump(mode="json") for change in done.changes],
        }, 0
    engine = BackfillEngine(
        store, source, workspace=args.workspace, clock=clock, monotonic=monotonic, sleeper=sleeper
    )
    if command == "plan":
        universe = latest_target_universe(store, workspace=args.workspace)
        if universe is None:
            raise PilotDataError("targets_missing", "no target universe snapshot is stored")
        plan = engine.build_plan(universe, window_start=args.window_start, window_end=args.window_end)
        return {
            "plan_sha256": plan.plan_sha256, "strategy": plan.strategy,
            "windows": [[a.isoformat(), b.isoformat()] for a, b in plan.windows],
            "stock_codes": len(plan.stock_codes), "calls_needed": len(plan.stock_codes) * len(plan.windows),
        }, 0
    plan = load_plan(store, args.plan_sha256)
    report = (
        engine.run(plan, today_ist=today_ist, break_lock=args.break_lock)
        if command == "run"
        else engine.status(plan)
    )
    return report.model_dump(mode="json"), _exit_for_report(report)


def _need(source: RelaySource | None) -> RelaySource:
    if source is None:
        raise PilotDataError("relay_source_missing", "this command needs the relay")
    return source


def main(
    argv: list[str] | None = None,
    *,
    source: RelaySource | None = None,
    clock: Callable[[], datetime] = utc_now,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
) -> int:
    """`source`, `clock`, `monotonic` and `sleeper` are test seams. There is no URL, host or port option."""
    args = _build_parser().parse_args(argv)
    today_ist = clock().astimezone(IST).date()
    try:
        with contextlib.ExitStack() as stack:
            store = stack.enter_context(PilotDataStore(args.root, workspace=args.workspace))
            relay = source
            if relay is None and args.command in _RELAY_COMMANDS:
                relay = stack.enter_context(open_relay_source())
            payload, code = _dispatch(
                args, store, relay, today_ist=today_ist, clock=clock, monotonic=monotonic, sleeper=sleeper
            )
    except PilotDataError as exc:
        resume = exc.code in ("daily_cap_reached", "relay_budget_day")
        print(json.dumps({"caveats": _caveat_payload(), "error_code": exc.code, "error": str(exc)}, indent=2))
        return _EXIT_RESUME_LATER if resume else _EXIT_FAIL_CLOSED
    except Exception as exc:
        _, error_code, _ = classify_gateway_error(exc)
        print(json.dumps({"caveats": _caveat_payload(), "error_code": error_code}, indent=2))
        return _EXIT_RESUME_LATER if error_code == "relay_budget_day" else _EXIT_FAIL_CLOSED
    print(json.dumps({"caveats": _caveat_payload(), "result": payload}, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
