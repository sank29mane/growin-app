"""Part 1 orchestration: lineages, spans, factor sets and the universe, summarised in one report.

The report leads with the survivorship and hindsight caveats. It is written read-only under
`<root>/reports/` with the first 12 hex characters of its hash in the file name.
"""

from __future__ import annotations

import argparse
import json
import os
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
from .core import (
    SURVEILLANCE_HISTORY_CAVEAT,
    CaveatedResult,
    PilotDataError,
    canonical_sha256,
    standard_caveats,
)
from .corporate_actions import EVENTS_TABLE
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


def write_report(root: Path, report: Part1Report) -> Path:
    reports = root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    path = reports / f"part1-{report.as_of.isoformat()}-{report.report_sha256[:12]}.json"
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


def _caveat_payload() -> list[dict[str, str]]:
    return [caveat.model_dump() for caveat in standard_caveats(SURVEILLANCE_HISTORY_CAVEAT)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pilot_data.report", description="Part 1 report for the India pilot data.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("latest-session", "part1"):
        child = sub.add_parser(name)
        child.add_argument("--root", required=True, type=Path)
        child.add_argument("--workspace", required=True, choices=["india"])
        if name == "part1":
            child.add_argument("--as-of", required=True, type=date.fromisoformat)
            child.add_argument("--window-start", required=True, type=date.fromisoformat)
            child.add_argument("--research-date", type=date.fromisoformat)
    args = parser.parse_args(argv)
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
