"""Probe, relay security master refresh, and the paced, capped, resumable Breeze backfill.

Every Breeze byte arrives through Phase 61's RelayClient (contract growin-relay/1); this module
has no transport code, takes no relay URL and never reads a credential. Rules it keeps:

* each relay payload is re-validated (contract, sha256, fetch time, request echo) before it is stored;
* calls are paced first, then a ledger row is written, then the call goes out; the daily cap counts those
  rows (a call the client documents as refused before any send, `tunnel_restart_backoff`, is recorded as `not_sent`
  and not counted; every other failure counts), and a
  run that crosses IST midnight stops instead of charging the next day's call to the old date;
* a window is `done` only when the rows prove it: they cover every NSE session in the window (from the
  stock's listing date on, and from the probed depth on) and nothing in it was quarantined. A window whose
  expected sessions the calendar can not establish is stored as `stored_unverified`, never `done`, and `verify`
  re-checks it against the calendar later without a Breeze call. A calendar-known window that still lacks
  sessions after one retry is stored as `stored_with_gaps` (gap dates recorded, terminal until
  `run --retry-flagged`); no row is ever invented for a gap day;
* a `breeze_responses` coverage row is written only for an answer judged complete, by one function
  (`breeze_bars.write_breeze_coverage`), and spans only the range that judgement checked (the listing or depth
  floor through the window end), never the range that was asked for. Gap and unverified windows keep their bars
  and provenance but claim no coverage, so a backtest never reads a bar-less session as covered. The probe's
  own answer goes through the same assessment; bars outside the window are never stored;
* a window that keeps failing is not retried forever: after `MAX_WINDOW_ATTEMPTS` attempts across runs it is
  `failed_exhausted` (terminal, flagged, re-queued by `run --retry-flagged`), and never-attempted windows run
  before failed ones, so a few bad symbols can not starve the rest of the plan;
* an upstream error envelope (Status other than 200, Error text) fails the window and is never read as empty;
* a request that returns 1000 rows was capped upstream (the cap keeps the newest rows), so it is
  never stored as a complete window;
* prices are stored raw, exactly as Breeze sent them; corporate actions are applied later, locally;
* an empty answer is retried once, then flagged and quarantined, never read as zero volume; a run with
  flagged windows ends `complete_with_flags` (exit 4) until they are re-queued with `run --retry-flagged` (or, for
  `stored_unverified`, promoted by `verify`);
* anything unrecognised stops the run.

Tables are append-only. The only file written outside the store is the run lock, an OS file lock (flock).
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal

from brokers.breeze_gateway import RelayError, TunnelError, UpstreamPayload
from pydantic import BaseModel, ConfigDict, model_validator

from .bhavcopy import quarantines_from_inputs
from .breeze_bars import (
    BreezeParse,
    WindowAssessment,
    ingest_breeze_response,
    parse_breeze_v2_daily,
    write_breeze_coverage,
)
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
    check_bars_envelope,
    classify_gateway_error,
    gate_budget,
    gate_health,
    open_relay_source,
    refused_before_send,
    validate_payload,
)
from .security_master import (
    MasterIngestOutcome,
    ingest_security_master,
    latest_snapshot,
    lookup_stock_code,
    require_fresh_snapshot,
)
from .sessions import previous_sessions, sessions_between
from .store import PilotDataStore
from .targets import TargetMember, TargetUniverseResult, latest_target_universe

PACE_SECONDS = 1.2  # 50 calls a minute, below the relay's 80
DAILY_CALL_CAP = 3500  # per IST day, below the relay's 4,500
MAX_CONSECUTIVE_FAILURES = 3
MAX_WINDOW_ATTEMPTS = 3  # attempts per window across runs before it is failed_exhausted
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
GAPS_WINDOW_CODE = "breeze_window_gaps"
UNVERIFIED_WINDOW_CODE = "breeze_window_unverified"
# DateOfListing in the ICICI master: tried in order; anything else leaves the listing floor unset (strict).
LISTING_DATE_FORMATS = (
    "%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d", "%d-%b-%y",
    "%Y-%m-%d %H:%M:%S", "%d-%b-%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S",
)

Strategy = Literal["two_windows", "yearly"]
WindowStatus = Literal[
    "pending", "done", "empty_flagged", "failed", "failed_exhausted", "stored_unverified", "stored_with_gaps"
]
RunStatus = Literal[
    "complete",
    "complete_with_flags",
    "completed_with_failed_windows",
    "daily_cap_reached",
    "stopped_ist_rollover",
    "stopped_fail_closed",
    "stopped_consecutive_failures",
    "incomplete",
]
# Terminal: a plain run never touches these again. `run --retry-flagged` re-queues empty_flagged,
# stored_with_gaps and failed_exhausted (a new Breeze call); `verify` promotes stored_unverified (no Breeze call).
FLAGGED_STATUSES = frozenset({"empty_flagged", "stored_unverified", "stored_with_gaps", "failed_exhausted"})
TERMINAL_STATUSES = frozenset({"done"}) | FLAGGED_STATUSES
RETRY_FLAGGED_STATUSES = frozenset({"empty_flagged", "stored_with_gaps", "failed_exhausted"})
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
    "window_to DATE, source_sha256 VARCHAR, row_sha256 VARCHAR NOT NULL, "
    "plan_sha256 VARCHAR, attempt INTEGER)"
)
# Columns an older ledger lacks: the window attempt a call belongs to, written with the reservation itself.
LEDGER_ATTEMPT_COLUMNS = {"plan_sha256": "VARCHAR", "attempt": "INTEGER"}
INTERRUPTED_ATTEMPT_CODE = "attempt_interrupted"
# Outcomes a first try records while the window is still open: the second try decides. When a stop lands
# between the tries (the cap, an IST rollover), that lone first-try row is not a spent attempt.
NON_FINAL_TRY1_OUTCOMES = frozenset({"empty", "incomplete"})
NON_FINAL_TRY1_ERROR_CODES = frozenset(
    {"upstream_timeout", "relay_budget_minute", "relay_unavailable", "relay_tunnel_restart_backoff", "breeze_no_data"}
)
OUTCOMES_DDL = (
    "CREATE TABLE IF NOT EXISTS relay_call_outcomes("
    "call_id VARCHAR PRIMARY KEY, outcome VARCHAR NOT NULL, error_code VARCHAR, source_sha256 VARCHAR, "
    "finished_at_utc TIMESTAMP NOT NULL, row_sha256 VARCHAR NOT NULL, stopped BOOLEAN)"
)
# A stop (fail-closed halt) is not a window failure: its attempt is released, not spent.
OUTCOME_COLUMNS = {"stopped": "BOOLEAN"}
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
    store.ensure_columns("relay_call_ledger", LEDGER_ATTEMPT_COLUMNS)
    store.ensure_columns("relay_call_outcomes", OUTCOME_COLUMNS)


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
    """Pace calls, reserve a ledger row just before each one, and count the day's historical calls.

    Order for a call: pace(), then reserve(), then dispatch. reserve() re-checks the IST date and the
    daily cap, so a long pacing sleep across midnight can not send a call charged to the old date.
    """

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
        # A `not_sent` outcome means the client refused the call before anything left the machine.
        rows = self._store.query(
            "SELECT count(*) FROM relay_call_ledger AS l WHERE l.workspace = ? AND l.ist_date = ? "
            "AND l.endpoint = 'historical' AND NOT EXISTS ("
            "SELECT 1 FROM relay_call_outcomes AS o WHERE o.call_id = l.call_id AND o.outcome = 'not_sent')",
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
        plan_sha256: str | None = None,
        attempt: int | None = None,
    ) -> str:
        now_ist = self._clock().astimezone(IST).date()
        if now_ist > ist_date:
            raise PilotDataError("ist_date_rollover", "the IST date moved on during the run; resume tomorrow")
        if now_ist < ist_date:
            raise PilotDataError("relay_clock_mismatch", "the clock's IST date is before the run date")
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
            "window_to": _iso(window_to), "plan_sha256": plan_sha256, "attempt": attempt,
        }
        row = {
            "call_id": call_id, "workspace": self._workspace, "ist_date": ist_date,
            "reserved_at_utc": utc_naive(self._clock()), "endpoint": endpoint, "stock_code": stock_code,
            "window_from": window_from, "window_to": window_to, "source_sha256": None,
            "row_sha256": canonical_sha256(identity), "plan_sha256": plan_sha256, "attempt": attempt,
        }
        outcome = self._store.append_rows("relay_call_ledger", [row], check="relay_call_ledger")
        if outcome.inserted != 1:
            raise PilotDataError("ledger_write_failed", "the call reservation was not written")
        self._last_start = self._monotonic()  # the call goes out right after this row is written
        return call_id

    def pace(self) -> None:
        """Wait out the pacing gap since the last reservation. Call before reserve()."""
        if self._last_start is not None:
            wait = self._pace - (self._monotonic() - self._last_start)
            if wait > 0:
                self._sleeper(wait)

    def finish(
        self, call_id: str, outcome: str, error_code: str | None, source_sha256: str | None, *, stop: bool = False
    ) -> None:
        """Record how a call ended. `stop=True` marks a halt of the run: that attempt is released, not spent."""
        row = {
            "call_id": call_id, "outcome": outcome, "error_code": error_code, "source_sha256": source_sha256,
            "finished_at_utc": utc_naive(self._clock()),
            "row_sha256": canonical_sha256(
                {"call_id": call_id, "outcome": outcome, "error_code": error_code, "source_sha256": source_sha256}
            ),
            "stopped": stop,
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
    ledger.pace()
    call_id = ledger.reserve(
        endpoint="historical", ist_date=today_ist, stock_code=stock_code, window_from=start, window_to=end
    )
    try:
        payload = source.daily_bars(stock_code, start, end)
    except Exception as exc:
        failure = _relay_failure(exc)
        ledger.finish(call_id, "not_sent" if refused_before_send(exc) else "error", failure.code, None)
        raise failure from exc
    query = {"stock_code": stock_code, "from": start.isoformat(), "to": end.isoformat(), "exchange": "NSE"}
    try:
        descriptor = validate_payload(
            payload, expected_api=API_DAILY_BARS, kind=KIND_DAILY_BARS, expected_query=query
        )
    except PilotDataError as exc:
        ledger.finish(call_id, "rejected", exc.code, None)
        raise
    try:
        check_bars_envelope(payload)
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
        # A capped or depth-limited body is not ingested at all (it lost days, and the engine fetches them again).
        # A whole answer goes through the same assessment as an engine window: its bars are always kept, and it
        # claims coverage only if every expected session has a bar, over the range that was checked. One missing
        # day still leaves the probe conclusive (>= 99 percent), but it must not cover the hole.
        descriptor = validate_payload(full, expected_api=API_DAILY_BARS, kind=KIND_DAILY_BARS)
        convention = record.convention()
        assessment = assess_window(
            store,
            parse_breeze_v2_daily(full.body, stock_code=PROBE_STOCK_CODE, convention=convention),
            (window_start, window_end),
            None,
            _listing_date_from_master(store, PROBE_STOCK_CODE, today_ist),
        )
        ingest_breeze_response(
            store, descriptor, full.body, stock_code=PROBE_STOCK_CODE, requested_from=window_start,
            requested_to=window_end, convention=convention, assessment=assessment,
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
        ledger.finish(call_id, "not_sent" if refused_before_send(exc) else "error", failure.code, None)
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
    window_start: date,
    window_end: date,
    strategy: Strategy,
    *,
    pad_days: int = PAD_DAYS,
    last_completed: date | None = None,
) -> tuple[tuple[date, date], ...]:
    """Split [window_start, window_end] and pad each piece by pad_days on both sides.

    Padding is wider than needed on purpose: rows are filtered by parsed trading date later, and the
    overlap is harmless because identical bars from two responses count once. With `last_completed`
    (the last completed IST trading day), no window ends after it: a padded end in the future would
    make breeze_responses claim coverage of days that have not happened.
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
    if last_completed is not None:
        if any(start > last_completed for start, _ in padded):
            raise PilotDataError("window_invalid", "a window starts after the last completed trading day")
        padded = tuple((start, min(end, last_completed)) for start, end in padded)
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
    flagged_windows: list[dict[str, str | None]]


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
    source_sha256: str | None = None


def _row_count(body: bytes) -> int | None:
    try:
        rows = json.loads(body)["Success"]
    except (ValueError, KeyError, TypeError):
        return None
    return len(rows) if isinstance(rows, list) else None


def _settled_status(report: BackfillRunReport) -> RunStatus:
    """The status of a run that stopped for no reason: failed windows and flagged windows are not `complete`."""
    if report.windows_remaining:
        return "completed_with_failed_windows"
    if any(report.counts[name] for name in FLAGGED_STATUSES):
        return "complete_with_flags"
    return "complete"


def _parse_listing_date(raw: str) -> date | None:
    """DateOfListing from the master, or None when blank or in a format this code does not know."""
    text = raw.strip()
    for fmt in LISTING_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _ranges(expected: Iterable[date], gaps: set[date]) -> str:
    """Gap dates as `from..to` runs over consecutive expected sessions, for the quarantine detail."""
    runs: list[str] = []
    start = end = None
    for day in expected:
        if day in gaps:
            start = start or day
            end = day
            continue
        if start is not None:
            runs.append(start.isoformat() if start == end else f"{start.isoformat()}..{end.isoformat()}")  # type: ignore[union-attr]
            start = end = None
    if start is not None:
        runs.append(start.isoformat() if start == end else f"{start.isoformat()}..{end.isoformat()}")  # type: ignore[union-attr]
    return ",".join(runs)


def _listing_date_from_master(store: PilotDataStore, code: str, today_ist: date) -> date | None:
    """DateOfListing from the newest security master snapshot; None (strict) when absent or unparseable."""
    snapshot = latest_snapshot(store, on_or_before=today_ist)
    if snapshot is None:
        return None
    try:
        row = lookup_stock_code(store, snapshot, code)
    except PilotDataError:  # an ambiguous code gives no listing floor
        return None
    return None if row is None else _parse_listing_date(row.listing_raw)


def assess_window(
    store: PilotDataStore,
    parsed: BreezeParse,
    window: tuple[date, date],
    depth_floor: date | None,
    listing_date: date | None,
) -> WindowAssessment:
    """What the rows prove about a window. The engine and the probe both judge an answer here.

    Expected sessions: the stored NSE calendar's sessions from the window start, raised to the probed
    depth and to the stock's listing date. The listing date is a floor only when it is consistent with
    the answer (no later than the first in-window bar or quarantined row); a later date is evidently
    wrong and the floor stays at the window start, so the leading sessions are gaps. A date the parser
    quarantined counts as a gap, and a window with no good bar in it is never `complete`. When the
    calendar does not cover the window nothing can be established and the answer is `unverified`;
    there is no density guess. A `complete` verdict carries the range it checked as `span`: the floor
    used through the window end. That span, not the requested window, is what a coverage row may claim.
    """
    w_from, w_to = window
    start = w_from if depth_floor is None else max(w_from, depth_floor)
    returned = {bar.trade_date for bar in parsed.bars if start <= bar.trade_date <= w_to}
    flagged = {
        item.trade_date for item in parsed.parse_quarantine_inputs
        if item.trade_date is not None and start <= item.trade_date <= w_to
    }
    if listing_date is not None and listing_date > start:
        evidence = returned | flagged
        if not evidence and listing_date > w_to:
            # Nothing of the window itself came back (out-of-window bars do not count): the stock was not listed.
            return WindowAssessment("before_listing", (), {})
        if not evidence or listing_date <= min(evidence):
            start = listing_date
        # else the master's date is later than a day the stock evidently traded: it is wrong, the floor stays
    try:
        expected = sessions_between(store, start, w_to)
    except PilotDataError as exc:
        if exc.code != "calendar_unknown_dates":
            raise
        return WindowAssessment("unverified", (), {"reason": "calendar_unknown_dates", "bars": str(len(returned))})
    missing = {day for day in expected if day not in returned}
    gaps = sorted(missing | flagged)
    if not gaps and returned:
        return WindowAssessment("complete", (), {}, (start, w_to))
    detail = {"gap_sessions": str(len(gaps)), "gap_ranges": _ranges(sorted(set(expected) | flagged), set(gaps))}
    if gaps:
        detail["first_gap"] = gaps[0].isoformat()
    if not returned:
        detail["reason"] = "no_bars_in_window"
    if flagged:
        detail["quarantined_dates"] = str(len(flagged))
    return WindowAssessment("gaps", tuple(gaps), detail)


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
        max_window_attempts: int = MAX_WINDOW_ATTEMPTS,
    ) -> None:
        _check_workspace(store, workspace)
        if pace_seconds < PACE_SECONDS:
            raise PilotDataError("pace_too_fast", f"pacing may not be faster than {PACE_SECONDS} s per call")
        if not 1 <= daily_call_cap <= DAILY_CALL_CAP:
            raise PilotDataError("cap_invalid", f"the daily call cap must be between 1 and {DAILY_CALL_CAP}")
        if max_consecutive_failures < 1:
            raise PilotDataError("cap_invalid", "max_consecutive_failures must be at least 1")
        if max_window_attempts < 1:
            raise PilotDataError("cap_invalid", "max_window_attempts must be at least 1")
        ensure_backfill_tables(store)
        self._store = store
        self._source = source
        self._workspace = workspace
        self._clock = clock
        self._sleeper = sleeper
        self._max_failures = max_consecutive_failures
        self._max_attempts = max_window_attempts
        self._ledger = _CallLedger(
            store, workspace=workspace, clock=clock, monotonic=monotonic, sleeper=sleeper,
            pace_seconds=pace_seconds, daily_call_cap=daily_call_cap,
        )
        self._lock_path = store.root / "backfill.lock"
        self._lock_fd: int | None = None
        self._lock_record: bytes | None = None
        self._listing_dates: dict[str, date | None] = {}

    def _relay(self) -> RelaySource:
        if self._source is None:
            raise PilotDataError("relay_source_missing", "this engine was built without a relay source")
        return self._source

    # ---------------------------------------------------------------- lock
    def _acquire_lock(self, break_lock: bool) -> None:
        """Hold an OS file lock (flock) on the lock file for the whole run.

        The OS drops the lock when the holder dies, so a live owner is never displaced: a held lock is
        refused even with break_lock. What break_lock clears is only a stale owner record, left by a run
        that died without releasing, and only after the flock itself was won. The file is never deleted
        (deleting an flock file lets two runs lock two different files).
        """
        if self._lock_fd is not None:
            raise PilotDataError("backfill_locked", "this engine already holds the backfill lock")
        fd = os.open(self._lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise PilotDataError("backfill_locked", "another backfill run holds the lock") from None
        try:
            if os.pread(fd, 4096, 0).strip() and not break_lock:
                raise PilotDataError("backfill_lock_stale", "an earlier run died holding the lock; use break-lock")
            record = f"{os.getpid()} {uuid.uuid4().hex} {self._clock().isoformat()}\n".encode()
            os.ftruncate(fd, 0)
            os.pwrite(fd, record, 0)
        except BaseException:
            os.close(fd)  # closing drops the flock
            raise
        self._lock_fd, self._lock_record = fd, record

    def release_lock(self) -> None:
        """Release the flock; clear the owner record only if it is still this run's (pid and start token)."""
        fd, record = self._lock_fd, self._lock_record
        if fd is None:
            return
        self._lock_fd = self._lock_record = None
        try:
            if os.pread(fd, 4096, 0) == record:
                os.ftruncate(fd, 0)
        finally:
            os.close(fd)  # closing drops the flock

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
            windows=plan_windows(
                window_start, window_end, probe.strategy, pad_days=PAD_DAYS, last_completed=self._last_completed_day()
            ),
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

    def _last_completed_day(self) -> date:
        """The last IST trading day that is over: yesterday or earlier, never today (still in session)."""
        yesterday = self._clock().astimezone(IST).date() - timedelta(days=1)
        try:
            return previous_sessions(self._store, yesterday, 1)[0]
        except PilotDataError as exc:
            if exc.code != "calendar_unknown_dates":
                raise
            return yesterday  # an unknown calendar can only lower this, never raise it past yesterday

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

    def _event_states(self, plan: BackfillPlan) -> dict[tuple[str, date, date], _WindowState]:
        rows = self._store.query(
            "SELECT stock_code, window_from, window_to, status, attempt, error_code, source_sha256 "
            "FROM backfill_events WHERE plan_sha256 = ? ORDER BY at_utc, rowid",
            [plan.plan_sha256],
        )
        states: dict[tuple[str, date, date], _WindowState] = {}
        for code, start, end, status, attempt, error_code, source in rows:
            states[(code, start, end)] = _WindowState(status, int(attempt), error_code, source)
        return states

    def _interrupted_attempts(
        self, plan: BackfillPlan, events: dict[tuple[str, date, date], _WindowState]
    ) -> dict[tuple[str, date, date], _WindowState]:
        """Attempts whose calls are on the ledger but whose event row never landed (a crash, or a stop).

        The ledger row carries the attempt it belongs to, written with the reservation, so an attempt is
        counted once whether or not its event row was written. A call the client refused before sending
        (`not_sent`) used nothing and a halted attempt (`stopped`) is released; neither is counted. An attempt
        whose only call is a non-final first-try outcome (a timeout, an empty answer, an incomplete one) was
        stopped before its second try could run, so it is released too. The state's code is the last recorded
        outcome's code, or `attempt_interrupted` when the process died before any outcome.
        """
        latest, _calls = self._scan_attempts(plan)
        interrupted: dict[tuple[str, date, date], _WindowState] = {}
        for key, (attempt, error_code, source, stopped, lone_non_final) in latest.items():
            recorded = events.get(key)
            if stopped or lone_non_final or (recorded is not None and recorded.attempt >= attempt):
                continue
            status: WindowStatus = "failed_exhausted" if attempt >= self._max_attempts else "failed"
            interrupted[key] = _WindowState(status, attempt, error_code, source)
        return interrupted

    def _scan_attempts(
        self, plan: BackfillPlan
    ) -> tuple[
        dict[tuple[str, date, date], tuple[int, str, str | None, bool, bool]],
        dict[tuple[str, date, date, int], int],
    ]:
        """Per window, its newest attempt on the ledger (attempt, code, source, stopped, lone non-final try 1),
        and the ledger rows per (window, attempt). A refused send is still a row, so it is still a try."""
        if "attempt" not in self._store.columns("relay_call_ledger"):
            return {}, {}
        rows = self._store.query(
            "SELECT l.stock_code, l.window_from, l.window_to, l.attempt, o.error_code, o.source_sha256, o.stopped, "
            "o.outcome FROM relay_call_ledger AS l LEFT JOIN relay_call_outcomes AS o ON o.call_id = l.call_id "
            "WHERE l.workspace = ? AND l.plan_sha256 = ? AND l.endpoint = 'historical' AND l.attempt IS NOT NULL "
            "ORDER BY l.call_id",
            [self._workspace, plan.plan_sha256],
        )
        newest: dict[tuple[str, date, date], tuple[int, str, str | None, bool, bool]] = {}
        calls: dict[tuple[str, date, date, int], int] = {}
        for code, start, end, attempt, error_code, source, stopped, outcome in rows:
            key = (code, start, end)
            calls[(*key, int(attempt))] = calls.get((*key, int(attempt)), 0) + 1  # a refused send is still a try
            if outcome == "not_sent":
                continue
            if key in newest and int(attempt) < newest[key][0]:
                continue
            non_final = outcome in NON_FINAL_TRY1_OUTCOMES or (
                outcome == "error" and error_code in NON_FINAL_TRY1_ERROR_CODES
            )
            newest[key] = (int(attempt), error_code or INTERRUPTED_ATTEMPT_CODE, source, bool(stopped), non_final)
        # "Lone" is decided once every row is in: a refused try 2 (not_sent) lands after the counted try 1 and
        # must still end the release, or the attempt resumes at try 3 and has no send left to make.
        latest = {
            key: (attempt, code, source, stopped, non_final and calls[(*key, attempt)] == 1)
            for key, (attempt, code, source, stopped, non_final) in newest.items()
        }
        return latest, calls

    def _released_tries(
        self, plan: BackfillPlan, events: dict[tuple[str, date, date], _WindowState]
    ) -> dict[tuple[str, date, date], tuple[int, int]]:
        """Windows whose newest attempt was stopped after try 1: (attempt, tries already on the ledger).

        The released attempt resumes at the next try, so a window never sends more than two calls per attempt
        however many times a cap or midnight stops it between the tries (the pre-release bound).
        """
        latest, calls = self._scan_attempts(plan)
        released: dict[tuple[str, date, date], tuple[int, int]] = {}
        for key, (attempt, _code, _source, stopped, lone_non_final) in latest.items():
            recorded = events.get(key)
            if stopped or not lone_non_final or (recorded is not None and recorded.attempt >= attempt):
                continue
            released[key] = (attempt, calls[(*key, attempt)])
        return released

    def _states(self, plan: BackfillPlan) -> dict[tuple[str, date, date], _WindowState]:
        events = self._event_states(plan)
        return {**events, **self._interrupted_attempts(plan, events)}

    def _recover_interrupted(self, plan: BackfillPlan) -> None:
        """Write the event an interrupted attempt never got, so the budget holds across crashes."""
        events = self._event_states(plan)
        for (code, start, end), state in self._interrupted_attempts(plan, events).items():
            self._write_event(plan, code, (start, end), state.status, state.attempt, state.error_code, state.source_sha256)

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
    def run(
        self,
        plan: BackfillPlan,
        *,
        today_ist: date,
        break_lock: bool = False,
        retry_flagged: bool = False,
    ) -> BackfillRunReport:
        """Run the plan's unfinished windows. `retry_flagged` also re-queues `empty_flagged`, `stored_with_gaps`
        and `failed_exhausted` windows (a new Breeze call each); `stored_unverified` is left to `verify`."""
        self._check_plan(plan)
        self.preflight(today_ist=today_ist, break_lock=break_lock)
        try:
            return self._run_locked(plan, today_ist, retry_flagged)
        finally:
            self.release_lock()

    def _check_plan(self, plan: BackfillPlan) -> None:
        _check_workspace(self._store, plan.workspace)
        stored = self._store.query("SELECT count(*) FROM backfill_plans WHERE plan_sha256 = ?", [plan.plan_sha256])
        if not stored[0][0]:
            raise PilotDataError("plan_missing", "the plan was not stored with build_plan")

    def _run_locked(self, plan: BackfillPlan, today_ist: date, retry_flagged: bool) -> BackfillRunReport:
        probe = latest_conclusive_probe(self._store)
        if probe is None:
            raise PilotDataError("probe_missing", "no conclusive Breeze probe is stored")
        convention = probe.convention()
        depth_floor = probe.earliest_date if probe.depth_limited else None
        self._listing_dates = {}
        finished = TERMINAL_STATUSES - RETRY_FLAGGED_STATUSES if retry_flagged else TERMINAL_STATUSES
        self._recover_interrupted(plan)
        events = self._event_states(plan)
        states = {**events, **self._interrupted_attempts(plan, events)}
        released = self._released_tries(plan, events)
        todo = [
            (code, window)
            for code in plan.stock_codes
            for window in plan.windows
            if states.get((code, window[0], window[1]), _WindowState("pending", 0, None)).status not in finished
        ]
        # Never-attempted windows first, then the ones that already failed, fewest attempts first (the sort is
        # stable, so plan order holds within a group). Without this, the same few bad windows sort first on every
        # resume, trip the consecutive-failure stop and starve everything behind them.
        todo.sort(key=lambda item: states.get((item[0], item[1][0], item[1][1]), _WindowState("pending", 0, None)).attempt)
        used_before = self._ledger.used_today(today_ist)
        status: RunStatus = "complete"
        stop_code: str | None = None
        consecutive = 0
        for code, window in todo:
            prior = states.get((code, window[0], window[1]))
            attempt = (prior.attempt if prior is not None else 0) + 1
            resumed_attempt, tries_used = released.get((code, window[0], window[1]), (0, 0))
            try:
                outcome, detail = self._process_window(
                    plan, code, window, attempt, convention, depth_floor, self._listing_date(code, today_ist),
                    today_ist, first_try=min(tries_used + 1, 2) if resumed_attempt == attempt else 1,
                )
            except _Stop as stop:
                status, stop_code = stop.status, stop.code
                break
            if outcome in ("failed", "failed_exhausted"):
                consecutive += 1
                if consecutive >= self._max_failures:
                    status, stop_code = "stopped_consecutive_failures", detail
                    break
            else:
                consecutive = 0
        report = self._report(plan, today_ist, status, stop_code, self._ledger.used_today(today_ist) - used_before)
        if report.status == "complete":
            return report.model_copy(update={"status": _settled_status(report)})
        return report

    def _process_window(
        self,
        plan: BackfillPlan,
        code: str,
        window: tuple[date, date],
        attempt: int,
        convention: DailyBarConvention,
        depth_floor: date | None,
        listing_date: date | None,
        today_ist: date,
        *,
        first_try: int = 1,
    ) -> tuple[WindowStatus, str | None]:
        """Fetch one window with at most one retry. Returns the window's new status and a code.

        `first_try` is 2 for an attempt a stop released after try 1: the tries already on the ledger count.
        """
        w_from, w_to = window
        query = {"stock_code": code, "from": w_from.isoformat(), "to": w_to.isoformat(), "exchange": "NSE"}
        empty_code: str | None = None
        empty_sha: str | None = None
        for try_number in range(first_try, 3):
            self._ledger.pace()
            try:
                call_id = self._ledger.reserve(
                    endpoint="historical", ist_date=today_ist, stock_code=code, window_from=w_from, window_to=w_to,
                    plan_sha256=plan.plan_sha256, attempt=attempt,
                )
            except PilotDataError as exc:
                if exc.code == "daily_cap_reached":
                    raise _Stop("daily_cap_reached", exc.code) from None
                if exc.code == "ist_date_rollover":
                    raise _Stop("stopped_ist_rollover", exc.code) from None
                raise _Stop("stopped_fail_closed", exc.code) from None
            try:
                payload = self._relay().daily_bars(code, w_from, w_to)
            except Exception as exc:
                action, error_code, sleep_for = classify_gateway_error(exc)
                halts = action == "stop" or (action == "retry_then_stop" and try_number == 2)
                self._ledger.finish(
                    call_id, "not_sent" if refused_before_send(exc) else "error", error_code, None, stop=halts
                )
                if action == "stop":
                    raise _Stop("stopped_fail_closed", error_code) from None
                if action == "fail_window":
                    return self._fail(plan, code, window, attempt, error_code, None)
                if action == "empty":
                    empty_code, empty_sha = error_code, None
                    if listing_date is not None and listing_date > w_to:
                        return self._before_listing(plan, code, window, attempt, empty_sha)
                    if try_number == 1:
                        continue
                    return self._flag_empty(plan, code, window, attempt, empty_code, empty_sha)
                if try_number == 1:  # retry_once and retry_then_stop
                    if sleep_for > 0:
                        self._sleeper(sleep_for)
                    continue
                if action == "retry_then_stop":
                    raise _Stop("stopped_fail_closed", error_code) from None
                return self._fail(plan, code, window, attempt, error_code, None)
            try:
                descriptor = validate_payload(
                    payload, expected_api=API_DAILY_BARS, kind=KIND_DAILY_BARS, expected_query=query
                )
            except PilotDataError as exc:
                self._ledger.finish(call_id, "rejected", exc.code, None, stop=True)
                raise _Stop("stopped_fail_closed", exc.code) from None
            try:
                check_bars_envelope(payload)
            except PilotDataError as exc:
                fails_window = exc.code.startswith("breeze_")
                self._ledger.finish(call_id, "rejected", exc.code, None, stop=not fails_window)
                if fails_window:  # an upstream error or a wrong envelope: never empty, never stored
                    return self._fail(plan, code, window, attempt, exc.code, None)
                raise _Stop("stopped_fail_closed", exc.code) from None
            if payload.empty:
                self._store.register_source(descriptor, payload.body)
                _record_provenance(self._store, payload)
                self._ledger.finish(call_id, "empty", "breeze_empty_response", payload.sha256)
                empty_code, empty_sha = "breeze_empty_response", payload.sha256
                if listing_date is not None and listing_date > w_to:
                    return self._before_listing(plan, code, window, attempt, empty_sha)
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
                parsed = parse_breeze_v2_daily(payload.body, stock_code=code, convention=convention)
            except PilotDataError as exc:
                if exc.code.startswith("breeze_"):
                    self._ledger.finish(call_id, "rejected", exc.code, payload.sha256)
                    return self._fail(plan, code, window, attempt, exc.code, payload.sha256)
                self._ledger.finish(call_id, "error", exc.code, payload.sha256, stop=True)
                raise _Stop("stopped_fail_closed", exc.code) from None
            if not parsed.bars:
                # Every row was quarantined by the parser: nothing usable arrived for this window.
                self._quarantine_parse(parsed, payload.sha256)
                self._ledger.finish(call_id, "rejected", "breeze_no_usable_bars", payload.sha256)
                return self._fail(plan, code, window, attempt, "breeze_no_usable_bars", payload.sha256)
            assessment = self._assess(parsed, window, depth_floor, listing_date)
            if assessment.verdict == "gaps" and try_number == 1:
                # Not stored: one retry first. The first answer stays on record as a source and a provenance row.
                self._ledger.finish(call_id, "incomplete", GAPS_WINDOW_CODE, payload.sha256)
                continue
            if assessment.verdict == "before_listing":
                # Only out-of-window bars came back for a window that ended before the listing: store none of them.
                self._ledger.finish(call_id, "ok", None, payload.sha256)
                return self._before_listing(plan, code, window, attempt, payload.sha256)
            try:
                # Bars in the window are always kept (with their parse quarantines); the coverage row is written,
                # by write_breeze_coverage, only for a complete verdict and only over the range it checked.
                ingest_breeze_response(
                    self._store, descriptor, payload.body, stock_code=code, requested_from=w_from,
                    requested_to=w_to, convention=convention, assessment=assessment,
                )
            except PilotDataError as exc:
                self._ledger.finish(call_id, "error", exc.code, payload.sha256, stop=True)
                raise _Stop("stopped_fail_closed", exc.code) from None
            self._ledger.finish(call_id, "ok", None, payload.sha256)
            if assessment.verdict == "complete":
                self._write_event(plan, code, window, "done", attempt, None, payload.sha256)
                return "done", None
            return self._flag_stored(plan, code, window, attempt, assessment, payload.sha256, calls=try_number)
        raise AssertionError("unreachable: the loop returns or continues on both tries")  # pragma: no cover

    def _quarantine_parse(self, parsed: BreezeParse, source_sha256: str) -> None:
        self._store.record_quarantine(
            quarantines_from_inputs(
                parsed.parse_quarantine_inputs, check="breeze_parse", source_sha256=source_sha256
            )
        )

    def _listing_date(self, code: str, today_ist: date) -> date | None:
        """DateOfListing from the newest security master snapshot, cached for the run."""
        if code not in self._listing_dates:
            self._listing_dates[code] = _listing_date_from_master(self._store, code, today_ist)
        return self._listing_dates[code]

    def _assess(
        self,
        parsed: BreezeParse,
        window: tuple[date, date],
        depth_floor: date | None,
        listing_date: date | None,
    ) -> WindowAssessment:
        return assess_window(self._store, parsed, window, depth_floor, listing_date)

    def _fail(
        self,
        plan: BackfillPlan,
        code: str,
        window: tuple[date, date],
        attempt: int,
        error_code: str,
        source_sha256: str | None,
    ) -> tuple[WindowStatus, str | None]:
        # The attempt count is the budget: the Nth failed attempt, across runs, is terminal until --retry-flagged.
        status: WindowStatus = "failed_exhausted" if attempt >= self._max_attempts else "failed"
        self._write_event(plan, code, window, status, attempt, error_code, source_sha256)
        return status, error_code

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

    def _before_listing(
        self, plan: BackfillPlan, code: str, window: tuple[date, date], attempt: int, source_sha256: str | None
    ) -> tuple[WindowStatus, str | None]:
        """An empty answer for a window that ended before the stock's listing date expected nothing: done.

        The master's date is only trusted together with Breeze's own empty answer. No bars and no coverage
        claim are stored, so nothing here says the stock traded or that its days were fetched.
        """
        self._write_event(plan, code, window, "done", attempt, "before_listing", source_sha256)
        return "done", None

    def _flag_stored(
        self,
        plan: BackfillPlan,
        code: str,
        window: tuple[date, date],
        attempt: int,
        assessment: WindowAssessment,
        source_sha256: str,
        *,
        calls: int,
    ) -> tuple[WindowStatus, str | None]:
        """The bars are stored; record why the window is not `done` and write its terminal flagged status."""
        unverified = assessment.verdict == "unverified"
        reason = UNVERIFIED_WINDOW_CODE if unverified else GAPS_WINDOW_CODE
        self._store.record_quarantine(
            [
                QuarantineRecord(
                    workspace=self._workspace, check="breeze_backfill", reason_code=reason, scope="raw",
                    stock_code=code, date_from=window[0], date_to=window[1],
                    detail={"plan_sha256": plan.plan_sha256, "calls": str(calls), **assessment.detail},
                    evidence_sha256s=(source_sha256,),
                )
            ]
        )
        status: WindowStatus = "stored_unverified" if unverified else "stored_with_gaps"
        self._write_event(plan, code, window, status, attempt, reason, source_sha256)
        return status, reason

    # ------------------------------------------------------------- verify
    def verify(self, plan: BackfillPlan, *, today_ist: date, break_lock: bool = False) -> BackfillRunReport:
        """Re-evaluate `stored_unverified` windows against the calendar. Makes no Breeze call.

        A window whose expected sessions are now established is promoted to `done` (and only then claims
        coverage) when each has a bar, else it becomes `stored_with_gaps` with no coverage row; a window the
        calendar still can not establish is left as it is.
        """
        self._check_plan(plan)
        self._acquire_lock(break_lock)
        try:
            probe = latest_conclusive_probe(self._store)
            if probe is None:
                raise PilotDataError("probe_missing", "no conclusive Breeze probe is stored")
            convention = probe.convention()
            depth_floor = probe.earliest_date if probe.depth_limited else None
            self._listing_dates = {}
            for (code, w_from, w_to), state in self._states(plan).items():
                if state.status != "stored_unverified" or state.source_sha256 is None:
                    continue
                window = (w_from, w_to)
                parsed = parse_breeze_v2_daily(
                    self._store.read_blob(state.source_sha256), stock_code=code, convention=convention
                )
                assessment = self._assess(parsed, window, depth_floor, self._listing_date(code, today_ist))
                if assessment.verdict == "unverified":
                    continue
                if assessment.verdict == "complete":
                    span = assessment.span
                    assert span is not None  # a complete verdict always carries the range it checked
                    write_breeze_coverage(
                        self._store, assessment, source_sha256=state.source_sha256, stock_code=code,
                        convention=convention,
                        bars_parsed=sum(1 for bar in parsed.bars if span[0] <= bar.trade_date <= span[1]),
                    )
                    self._write_event(plan, code, window, "done", state.attempt, None, state.source_sha256)
                elif assessment.verdict == "before_listing":
                    self._before_listing(plan, code, window, state.attempt, state.source_sha256)
                else:
                    self._flag_stored(
                        plan, code, window, state.attempt, assessment, state.source_sha256, calls=1
                    )
            report = self._report(plan, today_ist, "incomplete", None, 0)
            return report.model_copy(
                update={"status": "incomplete" if report.windows_remaining else _settled_status(report)}
            )
        finally:
            self.release_lock()

    # ------------------------------------------------------------- status
    def status(self, plan: BackfillPlan) -> BackfillRunReport:
        today_ist = self._clock().astimezone(IST).date()
        report = self._report(plan, today_ist, "incomplete", None, 0)
        return report.model_copy(update={"status": "incomplete" if report.windows_remaining else _settled_status(report)})

    def _report(
        self, plan: BackfillPlan, today_ist: date, status: RunStatus, stop_code: str | None, calls_this_run: int
    ) -> BackfillRunReport:
        states = self._states(plan)
        counts = {
            "pending": 0, "done": 0, "empty_flagged": 0, "failed": 0, "failed_exhausted": 0,
            "stored_unverified": 0, "stored_with_gaps": 0,
        }
        errors: dict[str, int] = {}
        flagged: list[dict[str, str | None]] = []
        for code in plan.stock_codes:
            for window in plan.windows:
                state = states.get((code, window[0], window[1]), _WindowState("pending", 0, None))
                counts[state.status] += 1
                if state.status in ("failed", *FLAGGED_STATUSES) and state.error_code:
                    errors[state.error_code] = errors.get(state.error_code, 0) + 1
                if state.status in FLAGGED_STATUSES:
                    flagged.append(
                        {"stock_code": code, "window_from": window[0].isoformat(), "window_to": window[1].isoformat(),
                         "status": state.status, "error_code": state.error_code}
                    )
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
            flagged_windows=flagged,
        )


# --------------------------------------------------------------------------- CLI

INTERNAL_ERROR_CODE = "internal_error"
_RELAY_COMMANDS = frozenset({"health", "probe", "refresh-master", "run"})
_EXIT_FLAGGED = 4
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
    if report.status == "complete_with_flags":
        return _EXIT_FLAGGED
    if (
        report.status in ("daily_cap_reached", "stopped_ist_rollover")
        or report.stop_code == "relay_budget_day"
    ):
        return _EXIT_RESUME_LATER
    if report.status == "completed_with_failed_windows" or report.status == "incomplete":
        return _EXIT_INCOMPLETE
    return _EXIT_FAIL_CLOSED


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pilot_data.backfill",
        description="Breeze daily-bar backfill through the Phase 61 relay client. Exit codes: 0 complete, "
        "1 finished with failed windows, 2 fail-closed stop, 3 daily cap, relay day budget or IST midnight "
        "reached (resume next IST day), 4 every window finished but some are flagged: empty, stored with gaps or "
        "failed after the attempt budget (re-queue them with run --retry-flagged) or stored unverified (re-check "
        "with verify).",
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
    run.add_argument("--break-lock", action="store_true", help="clear a stale owner record; never a held lock")
    run.add_argument("--retry-flagged", action="store_true", help="also re-queue windows flagged empty, stored with gaps or failed_exhausted")
    verify = common("verify")
    verify.add_argument("--plan-sha256")
    verify.add_argument("--break-lock", action="store_true", help="clear a stale owner record; never a held lock")
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
    if command == "run":
        report = engine.run(plan, today_ist=today_ist, break_lock=args.break_lock, retry_flagged=args.retry_flagged)
    elif command == "verify":
        report = engine.verify(plan, today_ist=today_ist, break_lock=args.break_lock)
    else:
        report = engine.status(plan)
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
        resume = exc.code in ("daily_cap_reached", "relay_budget_day", "ist_date_rollover")
        print(json.dumps({"caveats": _caveat_payload(), "error_code": exc.code, "error": str(exc)}, indent=2))
        return _EXIT_RESUME_LATER if resume else _EXIT_FAIL_CLOSED
    except (RelayError, TunnelError) as exc:
        _, error_code, _ = classify_gateway_error(exc)
        print(json.dumps({"caveats": _caveat_payload(), "error_code": error_code}, indent=2))
        return _EXIT_RESUME_LATER if error_code == "relay_budget_day" else _EXIT_FAIL_CLOSED
    except Exception:
        # A local fault (DuckDB, the lock file, a bug) is not a gateway problem; say so, and print no message.
        print(json.dumps({"caveats": _caveat_payload(), "error_code": INTERNAL_ERROR_CODE}, indent=2))
        return _EXIT_FAIL_CLOSED
    print(json.dumps({"caveats": _caveat_payload(), "result": payload}, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
