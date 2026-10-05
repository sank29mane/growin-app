"""Resumable NSE bhavcopy ingestion (PR zip, UDiFF and legacy CM) with a session-aware CLI."""

from __future__ import annotations

import argparse
import json
import shutil
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable

import httpx
from pydantic import BaseModel

from .bhavcopy import (
    check_same_date_consistency,
    ingest_cm_legacy,
    ingest_pr_zip,
    ingest_udiff,
)
from .core import PilotDataError, canonical_sha256, sha256_hex, standard_caveats, utc_naive, utc_now
from .corporate_actions import derive_corporate_actions
from .nse_http import NoFile, NseHttp, build_default_client
from .sessions import FINAL_STATUSES, day_status, ensure_fetch_log
from .store import PilotDataStore

MONTHS = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
MAX_CONSECUTIVE_FAILED_DAYS = 5
DEFAULT_MIN_FREE_BYTES = 3 * 1024**3


def pr_zip_url(d: date) -> str:
    return f"https://nsearchives.nseindia.com/archives/equities/bhavcopy/pr/PR{d.day:02d}{d.month:02d}{d.year % 100:02d}.zip"


def udiff_url(d: date) -> str:
    return f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{d:%Y%m%d}_F_0000.csv.zip"


def cm_legacy_url(d: date) -> str:
    mon = MONTHS[d.month - 1]
    return (
        f"https://nsearchives.nseindia.com/content/historical/EQUITIES/{d.year}/{mon}/"
        f"cm{d.day:02d}{mon}{d.year}bhav.csv.zip"
    )


def free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


class DayOutcome(BaseModel):
    trade_date: date
    outcomes: dict[str, str]
    status: str
    quarantined: int
    failed: bool


class RangeOutcome(BaseModel):
    start: date
    end: date
    days_processed: int
    skipped_final: int
    statuses: dict[str, int]
    files_by_kind: dict[str, int]
    quarantines_by_reason: dict[str, int]
    failed_dates: tuple[date, ...]


def _log_attempt(
    store: PilotDataStore, *, d: date, kind: str, outcome: str, http_status: int | None, error_code: str | None,
    source_sha256: str | None, url: str, attempted_at: datetime,
) -> None:
    attempt_id = uuid.uuid4().hex
    row = {
        "attempt_id": attempt_id, "trade_date": d, "file_kind": kind, "outcome": outcome,
        "http_status": http_status, "error_code": error_code, "source_sha256": source_sha256, "url": url,
        "attempted_at_utc": utc_naive(attempted_at),
    }
    row["row_sha256"] = canonical_sha256(
        {**{k: v for k, v in row.items() if k not in {"trade_date", "attempted_at_utc"}},
         "trade_date": d.isoformat(), "attempted_at_utc": utc_naive(attempted_at).isoformat()}
    )
    store.append_rows("nse_fetch_log", [row], check="nse_fetch_log")


def ingest_day(
    store: PilotDataStore, http: NseHttp, d: date, *, workspace: str, clock: Callable[[], datetime] = utc_now
) -> DayOutcome:
    if workspace != store.workspace:
        raise PilotDataError("workspace_mismatch", "workspace differs from the store workspace")
    ensure_fetch_log(store)
    outcomes: dict[str, str] = {}
    errors: dict[str, str] = {}

    def attempt(kind: str, url: str, descriptor_kind: str, ingest) -> str:
        attempted_at = clock()
        try:
            fetched = http.fetch(url, expect="zip")
        except PilotDataError as exc:
            _log_attempt(store, d=d, kind=kind, outcome="failed", http_status=None, error_code=exc.code,
                         source_sha256=None, url=url, attempted_at=attempted_at)
            outcomes[kind] = "failed"
            return "failed"
        if isinstance(fetched, NoFile):
            _log_attempt(store, d=d, kind=kind, outcome="no_file", http_status=404, error_code=None,
                         source_sha256=None, url=url, attempted_at=attempted_at)
            outcomes[kind] = "no_file"
            return "no_file"
        digest = sha256_hex(fetched.content)
        try:
            ingest(store, fetched.descriptor(descriptor_kind, d), fetched.content, trade_date=d)
        except PilotDataError as exc:
            _log_attempt(store, d=d, kind=kind, outcome="failed", http_status=fetched.status, error_code=exc.code,
                         source_sha256=digest, url=url, attempted_at=attempted_at)
            outcomes[kind] = "failed"
            errors[kind] = exc.code
            return "failed"
        _log_attempt(store, d=d, kind=kind, outcome="ingested", http_status=fetched.status, error_code=None,
                     source_sha256=digest, url=url, attempted_at=attempted_at)
        outcomes[kind] = "ingested"
        return "ingested"

    weekend = d.weekday() >= 5
    pr = attempt("pr", pr_zip_url(d), "pr_zip", ingest_pr_zip)
    # NSE has published misdated weekend PR zips (PR060424.zip holds the
    # 4 June 2024 members). When a weekend PR zip has no member for the date,
    # ask for the primary files too, so a confirmed absence settles the day.
    misdated_pr = pr == "failed" and errors.get("pr") == "pr_member_missing"
    proceed = not weekend or pr == "ingested" or misdated_pr
    if proceed:
        udiff = attempt("udiff", udiff_url(d), "udiff_cm", ingest_udiff)
        if udiff == "no_file":
            attempt("cm_legacy", cm_legacy_url(d), "cm_legacy", ingest_cm_legacy)
    quarantined = 0
    if "ingested" in (outcomes.get("udiff"), outcomes.get("cm_legacy")):
        quarantined = len(check_same_date_consistency(store, d, workspace="india"))
    return DayOutcome(
        trade_date=d, outcomes=outcomes, status=day_status(store, d), quarantined=quarantined,
        failed="failed" in outcomes.values(),
    )


def ingest_range(
    store: PilotDataStore,
    http: NseHttp,
    start: date,
    end: date,
    *,
    workspace: str,
    force: bool = False,
    clock: Callable[[], datetime] = utc_now,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
    disk_free: Callable[[Path], int] = free_bytes,
) -> RangeOutcome:
    if workspace != store.workspace:
        raise PilotDataError("workspace_mismatch", "workspace differs from the store workspace")
    ensure_fetch_log(store)
    statuses: dict[str, int] = {}
    files: dict[str, int] = {}
    failed_dates: list[date] = []
    processed = skipped = consecutive_failed = 0
    day = start
    while day <= end:
        current = day_status(store, day)
        if not force and current in FINAL_STATUSES:
            skipped += 1
            statuses[current] = statuses.get(current, 0) + 1
            day += timedelta(days=1)
            continue
        free = disk_free(store.root)
        if free < min_free_bytes:
            raise PilotDataError(
                "disk_floor_reached", f"{free} bytes free is below the {min_free_bytes} byte floor; stopping cleanly"
            )
        outcome = ingest_day(store, http, day, workspace=workspace, clock=clock)
        processed += 1
        statuses[outcome.status] = statuses.get(outcome.status, 0) + 1
        for kind, result in outcome.outcomes.items():
            if result == "ingested":
                files[kind] = files.get(kind, 0) + 1
        if outcome.failed:
            failed_dates.append(day)
            consecutive_failed += 1
            if consecutive_failed >= MAX_CONSECUTIVE_FAILED_DAYS:
                raise PilotDataError(
                    "nse_ingest_aborted",
                    f"{MAX_CONSECUTIVE_FAILED_DAYS} consecutive days failed; NSE may be throttling",
                )
        else:
            consecutive_failed = 0
        day += timedelta(days=1)
    derive_corporate_actions(store, workspace="india")
    by_reason = {
        reason: count
        for reason, count in store.query(
            "SELECT reason_code, count(*) FROM quarantine_records WHERE date_from >= ? AND date_from <= ? "
            "AND check_name IN ('bhavcopy_parse', 'bhavcopy_consistency') GROUP BY reason_code ORDER BY reason_code",
            [start, end],
        )
    }
    return RangeOutcome(
        start=start, end=end, days_processed=processed, skipped_final=skipped, statuses=dict(sorted(statuses.items())),
        files_by_kind=dict(sorted(files.items())), quarantines_by_reason=by_reason, failed_dates=tuple(failed_dates),
    )


def _caveat_payload() -> list[dict[str, str]]:
    return [caveat.model_dump() for caveat in standard_caveats()]


def main(argv: list[str] | None = None, *, client: httpx.Client | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pilot_data.nse_ingest", description="Ingest public NSE bhavcopy files.")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--workspace", required=True, choices=["india"])
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--min-interval-seconds", type=float, default=1.0)
    parser.add_argument("--min-free-gib", type=float, default=3.0)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    owns_client = client is None
    http_client = client or build_default_client()
    try:
        http = NseHttp(http_client, min_interval_seconds=args.min_interval_seconds)
        with PilotDataStore(args.root, workspace=args.workspace) as store:
            outcome = ingest_range(
                store, http, args.start, args.end, workspace=args.workspace, force=args.force,
                min_free_bytes=int(args.min_free_gib * 1024**3),
            )
    except PilotDataError as exc:
        print(json.dumps({"caveats": _caveat_payload(), "error_code": exc.code, "error": str(exc)}, indent=2))
        return 2
    finally:
        if owns_client:
            http_client.close()
    print(json.dumps({"caveats": _caveat_payload(), "range": outcome.model_dump(mode="json")}, indent=2))
    return 2 if outcome.failed_dates else 0


if __name__ == "__main__":
    raise SystemExit(main())
