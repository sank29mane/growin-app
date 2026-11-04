"""Part 1 orchestration (lineages, spans, factor sets, the universe) and the Part 2 diagnostic report.

Every report leads with the survivorship and hindsight caveats. It is written read-only under
`<root>/reports/` with the first 12 hex characters of its hash in the file name.

Part 2 (`part2`) only reads what the probe, master refresh, backfill, cross-check and dataset stages already
stored. It opens the store read-only, calls no relay and writes nothing except its own JSON file.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import tempfile
from collections import Counter
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .adjustment import (
    AdjustmentPolicy,
    compute_factor_set,
    events_for_lineage,
    persist_factor_set,
    quarantine_unresolved,
)
from .backfill import BackfillEngine, latest_conclusive_probe, load_plan
from .core import (
    BREEZE_RAW_UNVERIFIED_CAVEAT,
    SURVEILLANCE_HISTORY_CAVEAT,
    Caveat,
    CaveatedResult,
    PilotDataError,
    canonical_sha256,
    standard_caveats,
)
from .corporate_actions import EVENTS_TABLE
from .crosscheck import CrossCheckReport, universe_report_digest
from .dataset import verify_dataset
from .history_quality import compute_span, record_history_quarantines
from .lineage import lineage_for_target, primary_bars_for_lineage
from .sessions import day_status, ensure_fetch_log, pr_missing_sessions, sessions_between
from .store import PilotDataStore
from .surveillance import first_snapshot_date
from .targets import latest_target_universe
from .universe import (
    NoTradingStatusSource,
    UniverseResult,
    UniversePolicy,
    classify_smallcap,
    evaluate_universe,
)


class UniverseSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    as_of: date
    mode: Literal["pilot", "research"]
    eligible_count: int
    exclusions_by_reason: dict[str, int]
    surveillance_applied: bool
    result_sha256: str | None
    error_code: str | None = None


class Part1Report(CaveatedResult):
    as_of: date
    window_start: date
    research_date: date
    sessions_by_year: dict[str, int]
    holidays: int
    unknown_days: int
    pr_missing_sessions: int
    files_by_kind: dict[str, int]
    quarantines_by_check_reason: dict[str, int]
    ca_events_by_kind: dict[str, int]
    ca_unresolved: list[dict[str, str]]
    targets_mapped: int
    targets_excluded_by_reason: dict[str, int]
    liquid_etfs: int
    lineages_multi_segment: int
    lineages_unresolved: list[dict[str, str]]
    short_history_count: int
    gap_range_count: int
    universe_pilot: UniverseSummary
    universe_research: UniverseSummary
    smallcap_counts: dict[str, int]
    report_sha256: str


def latest_session(store: PilotDataStore) -> date:
    """The most recent date whose calendar status is a session, found from the fetch log."""
    ensure_fetch_log(store)
    latest = store.query("SELECT max(trade_date) FROM nse_fetch_log")[0][0]
    if latest is None:
        raise PilotDataError("no_sessions", "no NSE files have been ingested yet")
    day = latest
    for _ in range(400):
        if day_status(store, day) == "session":
            return day
        day -= timedelta(days=1)
    raise PilotDataError("no_sessions", "no session found in the last 400 days of the fetch log")


def _summary(result: UniverseResult, mode: Literal["pilot", "research"]) -> UniverseSummary:
    return UniverseSummary(
        as_of=result.as_of, mode=mode, eligible_count=len(result.eligible_isins),
        exclusions_by_reason=dict(result.exclusions_by_reason),
        surveillance_applied=SURVEILLANCE_HISTORY_CAVEAT.code not in [c.code for c in result.caveats],
        result_sha256=result.result_sha256,
    )


def run_part1(
    store: PilotDataStore, *, as_of: date, window_start: date, research_date: date,
    workspace: Literal["india"],
) -> Part1Report:
    if workspace != store.workspace:
        raise PilotDataError("workspace_mismatch", "workspace differs from the store workspace")
    targets = latest_target_universe(store, workspace=workspace)
    if targets is None:
        raise PilotDataError("target_universe_missing", "build the target universe before running Part 1")
    sessions = sessions_between(store, window_start, as_of)  # fails closed on unknown days
    statuses = Counter(
        day_status(store, window_start + timedelta(days=offset))
        for offset in range((as_of - window_start).days + 1)
    )
    unknown_days = sum(count for status, count in statuses.items() if status in ("unknown", "pending", "inconsistent"))
    policy = AdjustmentPolicy()
    multi_segment = short_history = gap_ranges = 0
    unresolved_lineages: list[dict[str, str]] = []
    ca_unresolved: list[dict[str, str]] = []
    for member in targets.members:
        try:
            lineage = lineage_for_target(store, member, as_of=as_of, workspace=workspace)
        except PilotDataError as exc:
            if exc.code != "lineage_anchor_not_observed":
                raise
            unresolved_lineages.append(
                {"anchor_isin": member.anchor_isin, "stock_code": member.stock_code, "reason": exc.code,
                 "unresolved_before": ""}
            )
            continue
        if len(lineage.segments) > 1:
            multi_segment += 1
        if lineage.unresolved_before is not None:
            unresolved_lineages.append(
                {"anchor_isin": member.anchor_isin, "stock_code": member.stock_code,
                 "reason": lineage.unresolved_reason or "unknown",
                 "unresolved_before": lineage.unresolved_before.isoformat()}
            )
        span = compute_span(store, lineage, window_start=window_start, window_end=as_of, workspace=workspace)
        record_history_quarantines(store, lineage, span, workspace=workspace)
        short_history += int(span.short_history)
        gap_ranges += len(span.gap_ranges) + (1 if span.trailing_gap is not None else 0)
        bars = primary_bars_for_lineage(store, lineage, start=window_start, end=as_of)
        factor_set = compute_factor_set(
            lineage, bars, events_for_lineage(store, lineage), as_of=as_of, policy=policy
        )
        persist_factor_set(store, factor_set, workspace=workspace)
        quarantine_unresolved(store, lineage, factor_set, workspace=workspace)
        for item in factor_set.unresolved:
            ca_unresolved.append(
                {"anchor_isin": member.anchor_isin, "stock_code": member.stock_code,
                 "event_id": item.event_id or "", "ex_date": item.ex_date.isoformat() if item.ex_date else "",
                 "kind": item.kind, "reason": item.reason}
            )
    universe_policy = UniversePolicy()
    status = NoTradingStatusSource()  # Part 1 has no authoritative listing or suspension source
    try:
        pilot = _summary(
            evaluate_universe(
                store, as_of=as_of, targets=targets, policy=universe_policy, mode="pilot",
                allow_missing_surveillance_before=None, workspace=workspace, status_source=status,
            ),
            "pilot",
        )
    except PilotDataError as exc:
        if exc.code != "surveillance_snapshot_missing":
            raise
        pilot = UniverseSummary(
            as_of=as_of, mode="pilot", eligible_count=0, exclusions_by_reason={}, surveillance_applied=False,
            result_sha256=None, error_code=exc.code,
        )
    research = _summary(
        evaluate_universe(
            store, as_of=research_date, targets=targets, policy=universe_policy, mode="research",
            allow_missing_surveillance_before=first_snapshot_date(store) or date.max, workspace=workspace,
            status_source=status,
        ),
        "research",
    )
    quarantines = {
        f"{check}/{reason}": int(count)
        for check, reason, count in store.query(
            "SELECT check_name, reason_code, count(*) FROM quarantine_records GROUP BY check_name, reason_code "
            "ORDER BY check_name, reason_code"
        )
    }
    files = {
        kind: int(count)
        for kind, count in store.query(
            "SELECT file_kind, count(*) FROM bhavcopy_files WHERE trade_date >= ? AND trade_date <= ? "
            "GROUP BY file_kind ORDER BY file_kind",
            [window_start, as_of],
        )
    }
    ca_kinds: Counter[str] = Counter()
    if store.table_exists(EVENTS_TABLE):
        for (parts_json,) in store.query(f"SELECT parts_json FROM {EVENTS_TABLE}"):
            for part in json.loads(parts_json):
                ca_kinds[part["kind"]] += 1
    exclusions = Counter(e.reason for e in targets.exclusions)
    classes = Counter(classify_smallcap(store, targets).values())
    fields = {
        "as_of": as_of, "window_start": window_start, "research_date": research_date,
        "sessions_by_year": dict(sorted(Counter(str(day.year) for day in sessions).items())),
        "holidays": int(statuses.get("holiday", 0)), "unknown_days": unknown_days,
        "pr_missing_sessions": len(pr_missing_sessions(store, window_start, as_of)),
        "files_by_kind": files, "quarantines_by_check_reason": quarantines,
        "ca_events_by_kind": dict(sorted(ca_kinds.items())), "ca_unresolved": ca_unresolved,
        "targets_mapped": sum(1 for m in targets.members if m.kind == "nifty500"),
        "targets_excluded_by_reason": dict(sorted(exclusions.items())),
        "liquid_etfs": sum(1 for m in targets.members if m.kind == "liquid_etf"),
        "lineages_multi_segment": multi_segment, "lineages_unresolved": unresolved_lineages,
        "short_history_count": short_history, "gap_range_count": gap_ranges, "universe_pilot": pilot,
        "universe_research": research, "smallcap_counts": dict(sorted(classes.items())),
    }
    caveats = standard_caveats(SURVEILLANCE_HISTORY_CAVEAT)
    digest_input = {
        key: (value.model_dump(mode="json") if isinstance(value, BaseModel) else
              value.isoformat() if isinstance(value, date) else value)
        for key, value in fields.items()
    }
    digest = canonical_sha256({"fields": digest_input, "caveats": [c.code for c in caveats]})
    return Part1Report(workspace=workspace, caveats=caveats, report_sha256=digest, **fields)


def _write_json_report(root: Path, prefix: str, report: CaveatedResult, as_of: date, digest: str) -> Path:
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"{prefix}-{as_of.isoformat()}-{digest[:12]}.json"
    dumped = report.model_dump(mode="json")
    ordered = {"caveats": dumped.pop("caveats"), **dumped}
    text = json.dumps(ordered, indent=2)
    if path.exists():
        # Never trust or overwrite what is already there: it must be exactly this report, and read-only.
        if path.read_text(encoding="utf-8") != text:
            raise PilotDataError("report_file_altered", f"{path.name} exists and differs from this report")
        if stat.S_IMODE(path.stat().st_mode) != 0o444:
            raise PilotDataError("report_file_writable", f"{path.name} exists but is not read-only (0444)")
        return path
    fd, tmp = tempfile.mkstemp(dir=reports, prefix=".tmp-")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    os.chmod(path, 0o444)
    return path


def write_report(root: Path, report: Part1Report) -> Path:
    return _write_json_report(root, "part1", report, report.as_of, report.report_sha256)


# --------------------------------------------------------------------------- Part 2

_BACKFILL_TABLES = (
    "breeze_probe_records", "backfill_plans", "backfill_events", "relay_call_ledger", "relay_call_outcomes",
    "relay_provenance",
)
_MASTER_TABLES = ("security_master_snapshots", "security_master_changes")
REFUSAL_CROSSCHECK_MISSING = "crosscheck_missing"
REFUSAL_ADJUSTED = "breeze_adjusted_detected"
REFUSAL_NOT_BUILT = "dataset_not_built"
REFUSAL_RUN_MISMATCH = "dataset_run_mismatch"
REFUSAL_WINDOW_MISMATCH = "dataset_window_mismatch"


class ProbeSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy: Literal["two_windows", "yearly"]
    convention_times: tuple[str, ...]
    truncation_detected: bool
    depth_limited: bool
    probe_sha256: str


class MasterRefreshSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    snapshot_sha256: str
    snapshot_date: date
    changes_by_kind: dict[str, int]


class BackfillSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    plan_sha256: str
    plan_window_start: date
    plan_window_end: date
    windows_by_status: dict[str, int]
    calls_by_ist_date: dict[str, int]
    max_calls_one_day: int


class CrossCheckSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str
    accepted_total: int
    totals_by_reason: dict[str, int]
    rawness_counts: dict[str, int]
    members_checked: int
    members_not_fetched: int


class DatasetSummary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dataset_sha256: str
    row_count: int
    export_path: str


class Part2Report(CaveatedResult):
    """What the real-data stages left in the store. A section is None when its stage has not run."""

    as_of: date
    window_start: date
    window_end: date
    probe: ProbeSummary | None
    master_refresh: MasterRefreshSummary | None
    backfill: BackfillSummary | None
    crosscheck: CrossCheckSummary | None
    dataset: DatasetSummary | None
    dataset_refusal_code: str | None
    report_sha256: str


def _have_tables(store: PilotDataStore, names: tuple[str, ...]) -> bool:
    return all(store.table_exists(name) for name in names)


def _probe_summary(store: PilotDataStore) -> ProbeSummary | None:
    if not _have_tables(store, _BACKFILL_TABLES):
        return None
    record = latest_conclusive_probe(store)
    if record is None:
        return None
    return ProbeSummary(
        strategy=record.strategy, convention_times=record.allowed_times,
        truncation_detected=record.truncation_detected, depth_limited=record.depth_limited,
        probe_sha256=record.probe_sha256(),
    )


def _master_summary(store: PilotDataStore) -> MasterRefreshSummary | None:
    if not _have_tables(store, _MASTER_TABLES):
        return None
    rows = store.query(
        "SELECT snapshot_sha256, snapshot_date FROM security_master_snapshots WHERE snapshot_date_basis = 'relay_fetch' "
        "ORDER BY snapshot_date DESC, ingested_at_utc DESC, snapshot_sha256 LIMIT 1"
    )
    if not rows:
        return None
    sha, day = rows[0]
    changes = {
        kind: int(count)
        for kind, count in store.query(
            "SELECT change_kind, count(*) FROM security_master_changes WHERE new_snapshot_sha256 = ? "
            "GROUP BY change_kind ORDER BY change_kind",
            [sha],
        )
    }
    return MasterRefreshSummary(snapshot_sha256=sha, snapshot_date=day, changes_by_kind=changes)


def _backfill_summary(store: PilotDataStore, workspace: Literal["india"]) -> BackfillSummary | None:
    if not _have_tables(store, _BACKFILL_TABLES):
        return None
    try:
        plan = load_plan(store)
    except PilotDataError as exc:
        if exc.code != "plan_missing":
            raise
        return None
    # No relay source: status() reads the events and the ledger and never calls out.
    counts = BackfillEngine(store, None, workspace=workspace).status(plan).counts
    # Same rule as the engine's daily cap: a call the client refused before sending (`not_sent`) is not counted.
    per_day = {
        day.isoformat(): int(count)
        for day, count in store.query(
            "SELECT l.ist_date, count(*) FROM relay_call_ledger AS l WHERE l.workspace = ? "
            "AND l.endpoint = 'historical' AND NOT EXISTS ("
            "SELECT 1 FROM relay_call_outcomes AS o WHERE o.call_id = l.call_id AND o.outcome = 'not_sent') "
            "GROUP BY l.ist_date ORDER BY l.ist_date",
            [workspace],
        )
    }
    return BackfillSummary(
        plan_sha256=plan.plan_sha256, plan_window_start=plan.window_start, plan_window_end=plan.window_end,
        windows_by_status=dict(counts), calls_by_ist_date=per_day, max_calls_one_day=max(per_day.values(), default=0),
    )


def _load_crosscheck(
    store: PilotDataStore, run_id: str | None, *, as_of: date, window_start: date, window_end: date
) -> CrossCheckReport | None:
    """The named run, or the newest universe-wide run for this as-of and window. Never a different window's run."""
    if not store.table_exists("crosscheck_runs"):
        if run_id is not None:
            raise PilotDataError("crosscheck_run_missing", "no cross-check run with that id")
        return None
    rows = store.query(
        "SELECT run_id, result_json, result_sha256 FROM crosscheck_runs ORDER BY started_at_utc DESC, rowid DESC"
    )
    if run_id is not None:
        rows = [row for row in rows if row[0] == run_id]
        if not rows:
            raise PilotDataError("crosscheck_run_missing", "no cross-check run with that id")
    for row_run_id, result_json, result_sha256 in rows:
        payload = json.loads(result_json)
        if "members" not in payload:
            if run_id is not None:
                raise PilotDataError("crosscheck_run_unsupported", "run is not a universe-wide cross-check")
            continue  # a single-member tracer run
        found = CrossCheckReport.model_validate(payload)
        # Recompute the hash from the stored content with the function the cross-check wrote it with. Comparing the
        # two stored copies alone would pass a row whose content was edited and both hashes left alone.
        recomputed = universe_report_digest(
            run_id=found.run_id, targets_sha256=found.targets_sha256, window_start=found.window_start,
            window_end=found.window_end, as_of=found.as_of, members=found.members,
            totals_by_reason=found.totals_by_reason, rawness_counts=found.rawness_counts, caveats=found.caveats,
        )
        if (
            row_run_id != found.run_id
            or recomputed != found.report_sha256
            or recomputed != result_sha256
            or found.accepted_total != sum(m.accepted_count for m in found.members)
        ):
            raise PilotDataError("crosscheck_record_corrupt", "a stored cross-check no longer matches its hash")
        if (found.as_of, found.window_start, found.window_end) != (as_of, window_start, window_end):
            if run_id is not None:
                raise PilotDataError("crosscheck_window_mismatch", "that run covers a different as-of or window")
            continue
        return found
    return None


def _dataset_state(
    check: CrossCheckReport | None, dataset_dir: Path | None, workspace: Literal["india"]
) -> tuple[DatasetSummary | None, str | None]:
    """Exactly one of the two is set: a verified dataset for this run, or the reason there is none."""
    if check is None:
        return None, REFUSAL_CROSSCHECK_MISSING
    if check.rawness_counts.get("adjusted_detected", 0) > 0 or any(
        m.rawness_overall == "adjusted_detected" for m in check.members
    ):
        return None, REFUSAL_ADJUSTED  # vendor-adjusted history is never published, whatever sits on disk
    if dataset_dir is None:
        return None, REFUSAL_NOT_BUILT
    try:
        manifest = verify_dataset(dataset_dir, workspace=workspace)
    except PilotDataError as exc:
        return None, exc.code
    if (manifest.crosscheck_run_id, manifest.report_sha256, manifest.targets_sha256) != (
        check.run_id, check.report_sha256, check.targets_sha256
    ):
        return None, REFUSAL_RUN_MISMATCH
    if (manifest.as_of, manifest.window_start, manifest.window_end) != (check.as_of, check.window_start, check.window_end):
        return None, REFUSAL_WINDOW_MISMATCH
    return (
        DatasetSummary(
            dataset_sha256=manifest.dataset_sha256, row_count=manifest.row_count,
            export_path=str(Path(dataset_dir).resolve()),
        ),
        None,
    )


def _part2_caveats(check: CrossCheckReport | None) -> tuple[Caveat, ...]:
    checked = [m for m in check.members if m.status == "checked"] if check is not None else []
    all_raw = bool(checked) and all(m.rawness_overall == "raw_confirmed" for m in checked)
    return standard_caveats(*(() if all_raw else (BREEZE_RAW_UNVERIFIED_CAVEAT,)))


def build_part2_report(
    store: PilotDataStore,
    *,
    as_of: date,
    window_start: date,
    window_end: date,
    crosscheck_run_id: str | None,
    dataset_dir: Path | None,
    workspace: Literal["india"],
) -> Part2Report:
    """Summarise probe, master refresh, backfill, cross-check and dataset from the store. Writes nothing."""
    if workspace != store.workspace:
        raise PilotDataError("workspace_mismatch", "workspace differs from the store workspace")
    if window_start > window_end:
        raise PilotDataError("window_invalid", "window_start is after window_end")
    check = _load_crosscheck(
        store, crosscheck_run_id, as_of=as_of, window_start=window_start, window_end=window_end
    )
    crosscheck = None
    if check is not None:
        crosscheck = CrossCheckSummary(
            run_id=check.run_id, accepted_total=check.accepted_total, totals_by_reason=dict(check.totals_by_reason),
            rawness_counts=dict(check.rawness_counts),
            members_checked=sum(1 for m in check.members if m.status == "checked"),
            members_not_fetched=sum(1 for m in check.members if m.status == "not_fetched"),
        )
    dataset, refusal = _dataset_state(check, dataset_dir, workspace)
    caveats = _part2_caveats(check)
    fields = {
        "as_of": as_of, "window_start": window_start, "window_end": window_end,
        "probe": _probe_summary(store), "master_refresh": _master_summary(store),
        "backfill": _backfill_summary(store, workspace), "crosscheck": crosscheck, "dataset": dataset,
        "dataset_refusal_code": refusal,
    }
    digest_input = {
        key: (value.model_dump(mode="json") if isinstance(value, BaseModel) else
              value.isoformat() if isinstance(value, date) else value)
        for key, value in fields.items()
    }
    digest = canonical_sha256({"fields": digest_input, "caveats": [c.code for c in caveats]})
    return Part2Report(workspace=workspace, caveats=caveats, report_sha256=digest, **fields)


def write_part2_report(root: Path, report: Part2Report) -> Path:
    return _write_json_report(root, "part2", report, report.as_of, report.report_sha256)


def _caveat_payload() -> list[dict[str, str]]:
    return [caveat.model_dump() for caveat in standard_caveats(SURVEILLANCE_HISTORY_CAVEAT)]


def _main_part2(args: argparse.Namespace) -> int:
    try:
        with PilotDataStore(args.root, workspace=args.workspace, read_only=True) as store:
            report = build_part2_report(
                store, as_of=args.as_of, window_start=args.window_start, window_end=args.window_end,
                crosscheck_run_id=args.run_id, dataset_dir=args.dataset_dir, workspace=args.workspace,
            )
        path = write_part2_report(args.root, report)
    except PilotDataError as exc:
        caveats = [c.model_dump() for c in standard_caveats(BREEZE_RAW_UNVERIFIED_CAVEAT)]
        print(json.dumps({"caveats": caveats, "error_code": exc.code, "error": str(exc)}, indent=2))
        return 2
    summary = {
        "report_path": str(path), "report_sha256": report.report_sha256,
        "probe_strategy": report.probe.strategy if report.probe else None,
        "max_calls_one_day": report.backfill.max_calls_one_day if report.backfill else None,
        "crosscheck_run_id": report.crosscheck.run_id if report.crosscheck else None,
        "rawness_counts": report.crosscheck.rawness_counts if report.crosscheck else None,
        "dataset_sha256": report.dataset.dataset_sha256 if report.dataset else None,
        "dataset_refusal_code": report.dataset_refusal_code,
    }
    print(json.dumps({"caveats": [c.model_dump() for c in report.caveats], "summary": summary}, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pilot_data.report", description="Part 1 and Part 2 reports for the India pilot data.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("latest-session", "part1", "part2"):
        child = sub.add_parser(name)
        child.add_argument("--root", required=True, type=Path)
        child.add_argument("--workspace", required=True, choices=["india"])
        if name in ("part1", "part2"):
            child.add_argument("--as-of", required=True, type=date.fromisoformat)
            child.add_argument("--window-start", required=True, type=date.fromisoformat)
        if name == "part1":
            child.add_argument("--research-date", type=date.fromisoformat)
        if name == "part2":
            child.add_argument("--window-end", required=True, type=date.fromisoformat)
            child.add_argument("--run-id")
            child.add_argument("--dataset-dir", type=Path)
    args = parser.parse_args(argv)
    if args.command == "part2":
        return _main_part2(args)
    try:
        with PilotDataStore(args.root, workspace=args.workspace) as store:
            if args.command == "latest-session":
                print(latest_session(store).isoformat())
                return 0
            research_date = args.research_date
            if research_date is None:
                research_date = args.as_of - timedelta(days=365)
                for _ in range(30):
                    if day_status(store, research_date) == "session":
                        break
                    research_date -= timedelta(days=1)
                else:
                    raise PilotDataError("research_date_unavailable", "no session found near one year before as_of")
            report = run_part1(
                store, as_of=args.as_of, window_start=args.window_start, research_date=research_date,
                workspace=args.workspace,
            )
            path = write_report(args.root, report)
    except PilotDataError as exc:
        print(json.dumps({"caveats": _caveat_payload(), "error_code": exc.code, "error": str(exc)}, indent=2))
        return 2
    summary = {
        "report_path": str(path), "report_sha256": report.report_sha256, "sessions_by_year": report.sessions_by_year,
        "unknown_days": report.unknown_days, "targets_mapped": report.targets_mapped,
        "liquid_etfs": report.liquid_etfs, "lineages_multi_segment": report.lineages_multi_segment,
        "lineages_unresolved": len(report.lineages_unresolved), "ca_unresolved": len(report.ca_unresolved),
        "universe_pilot": report.universe_pilot.model_dump(mode="json"),
        "universe_research": report.universe_research.model_dump(mode="json"),
    }
    print(json.dumps({"caveats": _caveat_payload(), "summary": summary}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
