"""Daily reference refresh with missed-run detection, plus a launchd template and installer.

The job collects the NSE bhavcopy day, the ASM and GSM snapshots and the price band files every
session. It cannot recover ASM or GSM history from before collection began: NSE serves those
lists for today only, so a missed day stays missing and the continuity report keeps it visible.

The security master is never downloaded here. All ICICI and Breeze traffic leaves the VM through
the relay, so in Part 1 that step records `skipped` with `security_master_refresh_requires_relay`.

This module never runs launchctl and never starts a subprocess. install_launchd writes the plist
only; the operator runs the printed commands.
"""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import tempfile
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Literal
from xml.sax.saxutils import escape

import httpx
from pydantic import BaseModel, ConfigDict

from .core import (
    IST,
    SURVEILLANCE_HISTORY_CAVEAT,
    CaveatedResult,
    PilotDataError,
    canonical_sha256,
    standard_caveats,
    utc_naive,
    utc_now,
)
from .nse_http import NseHttp, build_default_client
from .nse_ingest import ingest_day
from .price_bands import ensure_band_tables, fetch_band_files
from .sessions import FINAL_STATUSES, _statuses, day_status
from .store import PilotDataStore
from .surveillance import ensure_surveillance_tables, fetch_surveillance, first_snapshot_date

LAUNCHD_LABEL = "com.growin.pilot-reference-refresh"
TEMPLATE_PATH = Path(__file__).parent / "templates" / "com.growin.pilot-reference-refresh.plist.template"
RefreshStep = Literal["bhavcopy_day", "surveillance", "price_bands", "security_master"]
STEP_ORDER: tuple[RefreshStep, ...] = ("bhavcopy_day", "surveillance", "price_bands", "security_master")
SURVEILLANCE_NOTE = (
    "ASM and GSM are served for today only; this job cannot recover surveillance history from before "
    "collection began or for a missed day."
)
_PLACEHOLDERS = ("UV_BIN", "REPO_ROOT", "DATA_ROOT", "LOG_DIR", "HOUR", "MINUTE")

RUNS_DDL = (
    "CREATE TABLE IF NOT EXISTS reference_refresh_runs("
    "run_id VARCHAR NOT NULL, step VARCHAR NOT NULL, ist_date DATE NOT NULL, started_at_utc TIMESTAMP NOT NULL, "
    "finished_at_utc TIMESTAMP NOT NULL, outcome VARCHAR NOT NULL, error_code VARCHAR, detail_json VARCHAR NOT NULL, "
    "row_sha256 VARCHAR NOT NULL, source_sha256 VARCHAR NOT NULL, PRIMARY KEY(run_id, step))"
)


def ensure_refresh_tables(store: PilotDataStore) -> None:
    ensure_surveillance_tables(store)
    ensure_band_tables(store)
    store.ensure_table("reference_refresh_runs", RUNS_DDL, key_columns=("run_id", "step"))


class StepOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    step: RefreshStep
    outcome: Literal["ok", "skipped", "failed"]
    error_code: str | None = None
    detail: dict[str, str] = {}


class ContinuityReport(CaveatedResult):
    first_collection_date: date | None
    latest_session: date | None
    missing_asm: tuple[date, ...]
    missing_gsm: tuple[date, ...]
    missing_band_list: tuple[date, ...]
    unknown_calendar_dates: tuple[date, ...]
    last_successful_run_ist_date: date | None
    fresh: bool
    codes: tuple[str, ...]


class RefreshRunReport(CaveatedResult):
    run_id: str
    ist_date: date
    steps: tuple[StepOutcome, ...]
    continuity: ContinuityReport
    surveillance_history_recoverable: Literal[False] = False
    surveillance_history_note: str = SURVEILLANCE_NOTE
    report_sha256: str


# --------------------------------------------------------------------------- continuity
def _previous_weekday(day: date) -> date:
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def continuity_report(store: PilotDataStore, *, today_ist: date) -> ContinuityReport:
    ensure_refresh_tables(store)
    first = first_snapshot_date(store)
    latest: date | None = None
    for offset in range(0, 31):
        candidate = today_ist - timedelta(days=offset)
        if day_status(store, candidate) == "session":
            latest = candidate
            break
    unknown: list[date] = []
    missing_asm: list[date] = []
    missing_gsm: list[date] = []
    missing_band: list[date] = []
    if first is not None and latest is not None and first <= latest:
        asm_days = {r[0] for r in store.query(
            "SELECT DISTINCT effective_date FROM surveillance_snapshots WHERE list_name = 'asm'")}
        gsm_days = {r[0] for r in store.query(
            "SELECT DISTINCT effective_date FROM surveillance_snapshots WHERE list_name = 'gsm'")}
        band_days = {r[0] for r in store.query(
            "SELECT DISTINCT file_date FROM price_band_files WHERE file_kind = 'list'")}
        for day, status in _statuses(store, first, latest):
            if status == "session":
                if day not in asm_days:
                    missing_asm.append(day)
                if day not in gsm_days:
                    missing_gsm.append(day)
                if day not in band_days:
                    missing_band.append(day)
            elif status not in FINAL_STATUSES:
                unknown.append(day)
    successful = store.query(
        "SELECT max(a.ist_date) FROM reference_refresh_runs a JOIN reference_refresh_runs b ON a.run_id = b.run_id "
        "WHERE a.step = 'surveillance' AND a.outcome = 'ok' AND b.step = 'price_bands' AND b.outcome = 'ok'"
    )[0][0]
    recent = successful is not None and successful >= _previous_weekday(today_ist)
    codes: list[str] = []
    if unknown:
        codes.append("calendar_unknown_dates")
    if missing_asm or missing_gsm or missing_band:
        codes.append("reference_refresh_gap")
    if not recent:
        codes.append("reference_refresh_stale")
    return ContinuityReport(
        workspace="india", caveats=standard_caveats(SURVEILLANCE_HISTORY_CAVEAT), first_collection_date=first,
        latest_session=latest, missing_asm=tuple(missing_asm), missing_gsm=tuple(missing_gsm),
        missing_band_list=tuple(missing_band), unknown_calendar_dates=tuple(unknown),
        last_successful_run_ist_date=successful, fresh=not codes, codes=tuple(codes),
    )


def _listed(days: tuple[date, ...]) -> str:
    return ", ".join(day.isoformat() for day in days[:10])


def require_reference_freshness(store: PilotDataStore, *, today_ist: date) -> ContinuityReport:
    report = continuity_report(store, today_ist=today_ist)
    if report.unknown_calendar_dates:
        raise PilotDataError("calendar_unknown_dates",
                             f"unknown calendar dates since collection began: {_listed(report.unknown_calendar_dates)}")
    gaps = tuple(sorted(set(report.missing_asm) | set(report.missing_gsm) | set(report.missing_band_list)))
    if gaps:
        raise PilotDataError("reference_refresh_gap", f"reference data missing for sessions: {_listed(gaps)}")
    if "reference_refresh_stale" in report.codes:
        raise PilotDataError(
            "reference_refresh_stale",
            f"no successful surveillance and price band run on {today_ist.isoformat()} or the previous weekday",
        )
    return report


# --------------------------------------------------------------------------- the daily run
def _record_step(store: PilotDataStore, run_id: str, ist_date: date, started: datetime, outcome: StepOutcome) -> None:
    finished = datetime.now(timezone.utc)
    store.append_rows(
        "reference_refresh_runs",
        [{"run_id": run_id, "step": outcome.step, "ist_date": ist_date, "started_at_utc": utc_naive(started),
          "finished_at_utc": utc_naive(finished), "outcome": outcome.outcome, "error_code": outcome.error_code,
          "detail_json": json.dumps(outcome.detail, sort_keys=True),
          "row_sha256": canonical_sha256({"run": run_id, "step": outcome.step, "outcome": outcome.outcome}),
          "source_sha256": run_id}],
        check="reference_refresh_run",
    )


def run_daily(
    store: PilotDataStore, http: NseHttp, *, today_ist: date, workspace: Literal["india"],
    clock: Callable[[], datetime] = utc_now,
) -> RefreshRunReport:
    if workspace != store.workspace:
        raise PilotDataError("workspace_mismatch", "workspace differs from the store workspace")
    ensure_refresh_tables(store)
    run_id = uuid.uuid4().hex
    outcomes: list[StepOutcome] = []

    def run_step(step: RefreshStep, action: Callable[[], StepOutcome]) -> None:
        started = datetime.now(timezone.utc)
        try:
            outcome = action()
        except PilotDataError as exc:
            outcome = StepOutcome(step=step, outcome="failed", error_code=exc.code)
        outcomes.append(outcome)
        _record_step(store, run_id, today_ist, started, outcome)

    def bhavcopy() -> StepOutcome:
        day = ingest_day(store, http, today_ist, workspace=workspace, clock=clock)
        detail = {"status": day.status, **{f"file_{kind}": result for kind, result in day.outcomes.items()}}
        if day.failed:
            return StepOutcome(step="bhavcopy_day", outcome="failed", error_code="nse_ingest_failed", detail=detail)
        return StepOutcome(step="bhavcopy_day", outcome="ok", detail=detail)

    def surveillance() -> StepOutcome:
        asm, gsm = fetch_surveillance(store, http)
        return StepOutcome(step="surveillance", outcome="ok",
                           detail={"asm_effective": asm.effective_date.isoformat(),
                                   "gsm_effective": gsm.effective_date.isoformat()})

    def bands() -> StepOutcome:
        if day_status(store, today_ist) in ("holiday", "weekend_no_session"):
            return StepOutcome(step="price_bands", outcome="skipped", error_code="not_a_session")
        fetched = fetch_band_files(store, http, today_ist, workspace=workspace)
        detail = {"list": fetched.list_outcome, "changes": fetched.changes_outcome}
        if fetched.list_outcome != "ingested" or fetched.changes_outcome == "failed":
            return StepOutcome(step="price_bands", outcome="failed", error_code="price_band_files_unavailable",
                               detail=detail)
        return StepOutcome(step="price_bands", outcome="ok", detail=detail)

    def master() -> StepOutcome:
        return StepOutcome(step="security_master", outcome="skipped",
                           error_code="security_master_refresh_requires_relay")

    run_step("bhavcopy_day", bhavcopy)
    run_step("surveillance", surveillance)
    run_step("price_bands", bands)
    run_step("security_master", master)
    continuity = continuity_report(store, today_ist=today_ist)
    caveats = standard_caveats(SURVEILLANCE_HISTORY_CAVEAT)
    digest = canonical_sha256(
        {"run_id": run_id, "ist_date": today_ist.isoformat(), "steps": [o.model_dump(mode="json") for o in outcomes],
         "continuity": continuity.content_sha256()}
    )
    report = RefreshRunReport(workspace="india", caveats=caveats, run_id=run_id, ist_date=today_ist,
                              steps=tuple(outcomes), continuity=continuity, report_sha256=digest)
    write_run_report(store.root, report)
    return report


def write_run_report(root: Path, report: RefreshRunReport) -> Path:
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"reference-refresh-{report.ist_date.isoformat()}-{report.report_sha256[:12]}.json"
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


# --------------------------------------------------------------------------- launchd template and installer
def render_launchd_plist(*, uv_bin: Path, repo_root: Path, data_root: Path, hour: int, minute: int) -> str:
    template = TEMPLATE_PATH.read_text(encoding="utf-8")
    unknown = set(re.findall(r"\{\{([A-Z_]+)\}\}", template)) - set(_PLACEHOLDERS)
    if unknown:
        raise PilotDataError("launchd_template_invalid", f"template has unknown placeholders: {sorted(unknown)}")
    values = {
        "UV_BIN": str(uv_bin), "REPO_ROOT": str(repo_root), "DATA_ROOT": str(data_root),
        "LOG_DIR": str(Path(data_root) / "logs"), "HOUR": str(int(hour)), "MINUTE": str(int(minute)),
    }
    rendered = template
    for name, value in values.items():
        rendered = rendered.replace("{{" + name + "}}", escape(value))
    if "{{" in rendered or "}}" in rendered:
        raise PilotDataError("launchd_template_invalid", "template still holds unfilled placeholders")
    try:
        plistlib.loads(rendered.encode("utf-8"))
    except Exception as exc:
        raise PilotDataError("launchd_template_invalid", "rendered template is not a valid plist") from exc
    return rendered


class InstallOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str
    written: bool
    plist: str
    commands: tuple[str, ...]


def install_launchd(
    *, uv_bin: Path, repo_root: Path, data_root: Path, hour: int, minute: int, home: Path, system_timezone: str,
    replace: bool = False, dry_run: bool = False,
) -> InstallOutcome:
    """Write the user agent plist only. The launchctl commands are returned, never run."""
    if system_timezone != "Asia/Kolkata":
        raise PilotDataError("launchd_timezone_not_ist", f"system time zone is {system_timezone!r}, not Asia/Kolkata")
    for label, path in (("uv_bin", uv_bin), ("repo_root", repo_root), ("data_root", data_root), ("home", home)):
        if not Path(path).is_absolute():
            raise PilotDataError("launchd_path_relative", f"{label} must be an absolute path")
    if (Path(repo_root) / ".git").is_file():
        raise PilotDataError("launchd_repo_is_worktree", "repo_root is a git worktree; install from the main checkout")
    if not Path(uv_bin).is_file():
        raise PilotDataError("launchd_uv_missing", "uv_bin does not exist")
    plist = render_launchd_plist(uv_bin=uv_bin, repo_root=repo_root, data_root=data_root, hour=hour, minute=minute)
    target = Path(home) / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"
    uid = os.getuid()
    commands = (f"launchctl bootstrap gui/{uid} {target}", f"launchctl print gui/{uid}/{LAUNCHD_LABEL}")
    if dry_run:
        return InstallOutcome(path=str(target), written=False, plist=plist, commands=commands)
    if target.exists() and target.read_text(encoding="utf-8") != plist and not replace:
        raise PilotDataError("launchd_plist_exists", "a different plist is already installed; pass replace to overwrite")
    target.parent.mkdir(parents=True, exist_ok=True)
    (Path(data_root) / "logs").mkdir(parents=True, exist_ok=True)
    if target.exists():
        os.chmod(target, 0o644)
    target.write_text(plist, encoding="utf-8")
    os.chmod(target, 0o644)
    return InstallOutcome(path=str(target), written=True, plist=plist, commands=commands)


# --------------------------------------------------------------------------- CLI
def _caveat_payload() -> list[dict[str, str]]:
    return [caveat.model_dump() for caveat in standard_caveats(SURVEILLANCE_HISTORY_CAVEAT)]


def _system_timezone() -> str:
    try:
        target = os.readlink("/etc/localtime")
    except OSError as exc:
        raise PilotDataError("launchd_timezone_unknown", "could not read the system time zone") from exc
    return target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else target


def main(argv: list[str] | None = None, *, client: httpx.Client | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pilot_data.reference_refresh", description="Daily reference refresh.")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run-daily")
    run.add_argument("--root", required=True, type=Path)
    run.add_argument("--workspace", required=True, choices=["india"])
    run.add_argument("--min-interval-seconds", type=float, default=1.0)
    fresh = sub.add_parser("check-freshness")
    fresh.add_argument("--root", required=True, type=Path)
    fresh.add_argument("--workspace", required=True, choices=["india"])
    fresh.add_argument("--today", type=date.fromisoformat)
    install = sub.add_parser("install-launchd")
    install.add_argument("--repo-root", required=True, type=Path)
    install.add_argument("--root", required=True, type=Path)
    install.add_argument("--uv-bin", required=True, type=Path)
    install.add_argument("--hour", type=int, default=20)
    install.add_argument("--minute", type=int, default=45)
    install.add_argument("--dry-run", action="store_true")
    install.add_argument("--replace", action="store_true")
    install.add_argument("--home", type=Path, default=None)
    install.add_argument("--system-timezone", default=None)
    args = parser.parse_args(argv)
    owns_client = client is None and args.command == "run-daily"
    http_client = client or (build_default_client() if owns_client else None)
    try:
        if args.command == "install-launchd":
            outcome = install_launchd(
                uv_bin=args.uv_bin, repo_root=args.repo_root, data_root=args.root, hour=args.hour,
                minute=args.minute, home=args.home or Path.home(),
                system_timezone=args.system_timezone or _system_timezone(), replace=args.replace,
                dry_run=args.dry_run,
            )
            if args.dry_run:
                print(outcome.plist)
            else:
                print(json.dumps({"path": outcome.path, "commands": list(outcome.commands)}, indent=2))
            return 0
        with PilotDataStore(args.root, workspace=args.workspace) as store:
            today = getattr(args, "today", None) or datetime.now(IST).date()
            if args.command == "check-freshness":
                report = require_reference_freshness(store, today_ist=today)
                print(json.dumps({"caveats": _caveat_payload(), "continuity": report.model_dump(mode="json")}, indent=2))
                return 0
            http = NseHttp(http_client, min_interval_seconds=args.min_interval_seconds)
            report = run_daily(store, http, today_ist=today, workspace=args.workspace)
    except PilotDataError as exc:
        print(json.dumps({"caveats": _caveat_payload(), "error_code": exc.code, "error": str(exc)}, indent=2))
        return 2
    finally:
        if owns_client and http_client is not None:
            http_client.close()
    ordered = {"caveats": [c.model_dump() for c in report.caveats], **{k: v for k, v in report.model_dump(mode="json").items()
                                                                         if k != "caveats"}}
    print(json.dumps(ordered, indent=2))
    return 2 if any(step.outcome == "failed" for step in report.steps) else 0


if __name__ == "__main__":
    raise SystemExit(main())
