"""Seed an isolated session store from research-store blobs (64-08, G-1a, decision D-33).

The research store is opened once with ``read_only=True`` and never written, not even to read a descriptor. Every
seeded file is re-ingested from its stored bytes through the public ingest function, with a descriptor rebuilt from
the research ``source_files`` row, so the seeded sha256, byte size and first fetch time equal the research ones.
Nothing here calls NSE, Breeze, the relay, the VM or GCP.

Index lists (Nifty 500, Smallcap 250) are seeded the same way, limited to snapshots whose IST fetch date is on or before
``window_start`` (the cutoff ``latest_index_list(fetched_by=window_start)`` applies). A list fetched later never enters
the session store, so neither the S-1 universe nor the small-cap classification can read it. They are not only target
inputs: the shadow signal step classifies small caps from them, and ``open_run`` refuses a store that has none.

``SEED_TABLES`` names every table the research store can hold and how the seed treats it. A table with no entry is a
test failure, so a new table forces a decision. Before it returns, the seed re-opens the session store read-only and
runs a parity table against the research store: the same public readers, the same answers over the seeded window.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Literal

import duckdb
from pydantic import BaseModel, ConfigDict

from .backfill import assess_window, latest_conclusive_probe
from .bhavcopy import (
    check_same_date_consistency, ingest_cm_legacy, ingest_pr_zip, ingest_udiff, primary_bars_on,
)
from .breeze_bars import breeze_bars_for, ingest_breeze_response, parse_breeze_v2_daily
from .core import (
    CaveatedResult, PilotDataError, SourceDescriptor, canonical_sha256, standard_caveats, utc_naive,
)
from .constituents import EXPECTED_MEMBERS, ListName, ensure_constituent_tables, fetched_by_cutoff_utc, ingest_index_list
from .corporate_actions import EVENTS_TABLE, SIGHTINGS_TABLE, derive_corporate_actions, events_for_symbol
from .dataset import verify_dataset
from .models import CrossCheckTolerances, DailyBarConvention, QuarantineRecord  # noqa: F401
from .price_bands import (
    BandConvention, BandResolver, current_band_convention, ensure_band_tables, ingest_band_changes,
    ingest_band_list, record_band_convention,
)
from .security_master import (
    SnapshotBasis, ensure_master_tables, ingest_security_master, latest_snapshot, master_rows,
)
from .sessions import day_status, ensure_fetch_log
from .store import DB_FILE_NAME, PilotDataStore
from .surveillance import ensure_surveillance_tables, ingest_surveillance, snapshot_for, surveillance_entries
from .targets import TargetUniverseResult

SeedMethod = Literal["reingest", "derive", "copy", "pinned", "own", "not_seeded"]
SEED_REPORT_FILE = "seed_report.json"
SEEDS_DDL = (
    "CREATE TABLE IF NOT EXISTS session_store_seeds("
    "seed_report_sha256 VARCHAR PRIMARY KEY, research_identity_sha256 VARCHAR NOT NULL, "
    "dataset_sha256 VARCHAR NOT NULL, window_start DATE NOT NULL, window_end DATE NOT NULL, "
    "seeded_at_utc TIMESTAMP NOT NULL, row_sha256 VARCHAR NOT NULL, source_sha256 VARCHAR NOT NULL)"
)
BAND_PARITY_OFFSET_DAYS = 15
# Quarantine checks that re-ingest regenerates from the stored bytes. The seed does not copy them, so the parity
# table proves the regeneration instead of trusting a copy.
REGENERATED_CHECKS = frozenset({"bhavcopy_parse", "bhavcopy_consistency", "breeze_parse", "security_master_parse"})

SEED_TABLES: dict[str, SeedMethod] = {
    "pilot_store_identity": "own",
    "session_store_seeds": "own",
    "source_files": "reingest",
    "fetch_events": "reingest",
    "security_master_snapshots": "reingest",
    "security_master_rows": "reingest",
    "security_master_changes": "reingest",
    "bhavcopy_files": "reingest",
    "bhavcopy_bars": "reingest",
    "bhavcopy_ca_raw": "reingest",
    "bhavcopy_etf_info": "reingest",
    EVENTS_TABLE: "derive",
    SIGHTINGS_TABLE: "derive",
    "nse_fetch_log": "copy",
    "breeze_bars_raw": "reingest",
    "breeze_responses": "reingest",
    "surveillance_snapshots": "reingest",
    "surveillance_entries": "reingest",
    "price_band_files": "reingest",
    "price_band_list_rows": "reingest",
    "price_band_change_rows": "reingest",
    "price_band_conventions": "copy",
    "index_list_snapshots": "reingest",
    "index_list_members": "reingest",
    "quarantine_records": "copy",
    "breeze_probe_records": "pinned",
    "target_universe_snapshots": "pinned",
    "crosscheck_runs": "not_seeded",
    "crosscheck_accepted": "not_seeded",
    "adjustment_factor_sets": "not_seeded",
    "dataset_snapshots": "not_seeded",
    "universe_evaluations": "not_seeded",
    "reference_refresh_runs": "not_seeded",
    "price_band_fetch_log": "not_seeded",
    "price_band_archive_depth": "not_seeded",
    "band_coverage_reports": "not_seeded",
    "backfill_plans": "not_seeded",
    "backfill_events": "not_seeded",
    "relay_call_ledger": "not_seeded",
    "relay_call_outcomes": "not_seeded",
    "relay_provenance": "not_seeded",
}
NOT_SEEDED_REASONS: dict[str, str] = {
    "crosscheck_runs": "a cross-check run is recomputed per decision session",
    "crosscheck_accepted": "accepted bars belong to a run the session store does not carry",
    "adjustment_factor_sets": "factor sets are recomputed as of each decision",
    "dataset_snapshots": "the frozen dataset is pinned by directory and hash, not copied",
    "universe_evaluations": "evaluations are recomputed from the seeded reference data",
    "reference_refresh_runs": "a refresh history says nothing about the session store's freshness",
    "price_band_fetch_log": "band fetch history belongs to the research run",
    "price_band_archive_depth": "archive probes belong to the research run",
    "band_coverage_reports": "coverage reports belong to the research run",
    "backfill_plans": "backfill plans are research-run bookkeeping",
    "backfill_events": "backfill events are research-run bookkeeping",
    "relay_call_ledger": "the session store spends no relay budget",
    "relay_call_outcomes": "the session store spends no relay budget",
    "relay_provenance": "the session store spends no relay budget",
}
_PINNED_REASONS = {
    "breeze_probe_records": "the probed bar convention is returned in the report, not stored",
    "target_universe_snapshots": "the frozen dataset's target universe is returned in the report, not stored",
}


class ParityRow(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reader: str
    compared: int
    detail: str


class SeedReport(CaveatedResult):
    window_start: date
    window_end: date
    dataset_sha256: str
    research_identity_sha256: str
    targets: TargetUniverseResult
    targets_sha256: str
    breeze_convention: DailyBarConvention | None
    band_convention: BandConvention | None
    sources_by_kind: dict[str, tuple[str, ...]]
    fetch_log_rows: int
    quarantines_copied: int
    table_methods: dict[str, str]
    parity: tuple[ParityRow, ...]
    report_sha256: str

    def expected_sha256(self) -> str:
        return canonical_sha256({**self.model_dump(mode="json"), "report_sha256": ""})


@dataclass(frozen=True)
class _Source:
    sha: str
    source: str
    kind: str
    locator: str
    first_fetched: datetime
    for_date: date | None

    def descriptor(self) -> SourceDescriptor:
        # the research column is naive UTC; utc_naive refuses a naive value, and the seeded column must come back equal
        return SourceDescriptor(source=self.source, kind=self.kind, locator=self.locator,
                                fetched_at=self.first_fetched.replace(tzinfo=timezone.utc), for_date=self.for_date)


def _sources(research: PilotDataStore, shas: list[str] | set[str]) -> dict[str, _Source]:
    found: dict[str, _Source] = {}
    for sha in sorted(set(shas)):
        rows = research.query(
            "SELECT source_sha256, source, kind, locator, first_fetched_at_utc, for_date FROM source_files "
            "WHERE source_sha256 = ?", [sha],
        )
        if not rows:
            raise PilotDataError("seed_blob_missing", f"research store has no source_files row for {sha[:12]}")
        found[sha] = _Source(*rows[0])
    return found


def _ordered(research: PilotDataStore, shas: set[str]) -> list[_Source]:
    return sorted(_sources(research, shas).values(), key=lambda item: (item.first_fetched, item.sha))


# --------------------------------------------------------------------------- refusals
def _inside(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


def _refuse_overlap(session_root: Path, research_root: Path, dataset_dir: Path) -> None:
    session = Path(session_root).resolve()
    for other in (Path(research_root).resolve(), Path(dataset_dir).resolve()):
        if _inside(session, other) or _inside(other, session):
            raise PilotDataError("seed_root_overlap", "session_root overlaps the research store or the dataset")
    if session.exists() and (session.is_file() or any(session.iterdir())):
        raise PilotDataError("seed_root_not_empty", "session_root exists and is not empty")


def _identity_hash(research: PilotDataStore) -> str:
    rows = research.query("SELECT workspace, created_at_utc, schema_version FROM pilot_store_identity")
    return canonical_sha256([[row[0], row[1].isoformat(), int(row[2])] for row in rows])


def _require_research_tables(research: PilotDataStore) -> dict[str, bool]:
    for table in ("nse_fetch_log", "bhavcopy_files", "bhavcopy_bars", "breeze_responses", "breeze_bars_raw",
                  "security_master_snapshots", "target_universe_snapshots", EVENTS_TABLE, SIGHTINGS_TABLE):
        if not research.table_exists(table):
            raise PilotDataError("seed_research_incomplete", f"the research store has no {table} table")
    return {
        "bands": research.table_exists("price_band_files"),
        "surveillance": research.table_exists("surveillance_snapshots"),
        "probe": research.table_exists("breeze_probe_records"),
        "pr": research.table_exists("bhavcopy_ca_raw"),
        "index_lists": research.table_exists("index_list_snapshots") and research.table_exists("index_list_members"),
    }


def _pinned_targets(research: PilotDataStore, dataset_sha256: str, targets_sha256: str) -> TargetUniverseResult:
    found = research.query("SELECT payload_json FROM target_universe_snapshots WHERE target_sha256 = ?",
                           [targets_sha256])
    if not found:
        raise PilotDataError("seed_targets_missing", "no target universe snapshot has the dataset's targets_sha256")
    targets = TargetUniverseResult.model_validate(json.loads(found[0][0]))
    if targets.target_sha256 != targets_sha256:
        raise PilotDataError("seed_targets_missing", "the stored target universe does not hash to the dataset's pin")
    return targets


# --------------------------------------------------------------------------- seeding steps
def _in_window(day: date, window: tuple[date, date]) -> bool:
    return window[0] <= day <= window[1]


def _seed_security_master(research, session, window, kinds) -> list[str]:
    rows = research.query(
        "SELECT source_sha256, snapshot_date, snapshot_date_basis FROM security_master_snapshots "
        "WHERE snapshot_date <= ? ORDER BY snapshot_date, ingested_at_utc, snapshot_sha256", [window[1]])
    copied: list[str] = []
    sources = _sources(research, {row[0] for row in rows})
    for sha, snapshot_date, basis in rows:
        basis_value: SnapshotBasis = basis
        ingest_security_master(session, sources[sha].descriptor(), research.read_blob(sha),
                               snapshot_date=snapshot_date, snapshot_date_basis=basis_value)
        copied.append(sha)
    return copied


def _seed_bhavcopy(research, session, window, optional) -> dict[str, list[str]]:
    primaries = research.query(
        "SELECT source_sha256, file_kind, trade_date FROM bhavcopy_files WHERE file_kind IN ('udiff', 'cm_legacy') "
        "AND trade_date >= ? AND trade_date <= ?", list(window))
    by_kind: dict[str, list[str]] = {"udiff": [], "cm_legacy": [], "pr_zip": []}
    dates: dict[str, date] = {}
    kinds: dict[str, str] = {}
    for sha, kind, day in primaries:
        dates[sha], kinds[sha] = day, kind
    pr_rows = research.query(
        "SELECT source_sha256, trade_date FROM bhavcopy_files WHERE file_kind = 'pr_zip' AND trade_date <= ?",
        [window[1]])
    early_needed: set[str] = set()
    if optional["pr"]:
        early_needed = {
            row[0] for row in research.query(
                "SELECT DISTINCT source_sha256 FROM bhavcopy_ca_raw WHERE ex_date >= ? AND ex_date <= ?",
                list(window))
        }
    for sha, day in pr_rows:
        if _in_window(day, window) or (day < window[0] and sha in early_needed):
            dates[sha], kinds[sha] = day, "pr_zip"
    ordered = _ordered(research, set(dates))
    for item in ordered:
        kind, day = kinds[item.sha], dates[item.sha]
        content = research.read_blob(item.sha)
        if kind == "udiff":
            ingest_udiff(session, item.descriptor(), content, trade_date=day)
        elif kind == "cm_legacy":
            ingest_cm_legacy(session, item.descriptor(), content, trade_date=day)
        else:
            ingest_pr_zip(session, item.descriptor(), content, trade_date=day)
        by_kind[kind].append(item.sha)
    for day in sorted({dates[sha] for sha in dates if kinds[sha] in ("udiff", "cm_legacy")}):
        check_same_date_consistency(session, day, workspace="india")
    return by_kind


def _copy_fetch_log(research, session, window) -> int:
    ensure_fetch_log(session)
    rows = research.query(
        "SELECT attempt_id, trade_date, file_kind, outcome, http_status, error_code, source_sha256, url, "
        "attempted_at_utc, row_sha256 FROM nse_fetch_log WHERE trade_date >= ? AND trade_date <= ? "
        "ORDER BY attempted_at_utc, rowid", list(window))
    names = ("attempt_id", "trade_date", "file_kind", "outcome", "http_status", "error_code", "source_sha256", "url",
             "attempted_at_utc", "row_sha256")
    present = {row[0] for row in session.query("SELECT source_sha256 FROM source_files")}
    for row in rows:
        if row[3] == "ingested" and row[6] is not None and row[6] not in present:
            raise PilotDataError("seed_blob_missing", f"fetch-log row {row[0][:12]} names a blob that was not seeded")
    if rows:
        session.append_rows("nse_fetch_log", [dict(zip(names, row)) for row in rows], check="nse_fetch_log")
    return len(rows)


def _breeze_convention(research, stored_json: str | None, optional) -> DailyBarConvention:
    if stored_json is not None:
        return DailyBarConvention.model_validate(json.loads(stored_json))
    probe = latest_conclusive_probe(research) if optional["probe"] else None
    if probe is None:
        raise PilotDataError("seed_breeze_convention_missing", "no stored convention and no conclusive probe")
    return probe.convention()


def _clip(low: date, high: date, window: tuple[date, date]) -> tuple[date, date] | None:
    start, end = max(low, window[0]), min(high, window[1])
    return (start, end) if start <= end else None


def _seed_breeze(research, session, window, optional) -> list[str]:
    coverage = {
        row[0]: row for row in research.query(
            "SELECT source_sha256, stock_code, requested_from, requested_to, convention_json FROM breeze_responses")
    }
    stored = {
        row[0]: row for row in research.query(
            "SELECT source_sha256, stock_code, min(trade_date), max(trade_date) FROM breeze_bars_raw "
            "GROUP BY source_sha256, stock_code")
    }
    copied: list[str] = []
    for item in _ordered(research, set(coverage) | set(stored)):
        row = coverage.get(item.sha)
        bars = stored.get(item.sha)
        code = (row or bars)[1]
        span = _clip(row[2], row[3], window) if row else None
        bar_range = _clip(bars[2], bars[3], window) if bars else None
        if row is not None:
            joined = (min(row[2], bars[2]) if bars else row[2], max(row[3], bars[3]) if bars else row[3])
        else:
            joined = (bars[2], bars[3])
        request = _clip(joined[0], joined[1], window)
        if request is None or (span is None and bar_range is None):
            continue  # nothing of this answer sits inside the seed window
        convention = _breeze_convention(research, row[4] if row else None, optional)
        content = research.read_blob(item.sha)
        assessment = None
        if span is not None:
            assessment = assess_window(session, parse_breeze_v2_daily(content, stock_code=code, convention=convention),
                                       span, None, None)
            if assessment.verdict != "complete" or assessment.span != span:
                raise PilotDataError(
                    "seed_coverage_span",
                    f"{code} source {item.sha[:12]}: the session calendar does not support the research coverage "
                    f"span {span[0].isoformat()}..{span[1].isoformat()} (verdict {assessment.verdict})")
        ingest_breeze_response(session, item.descriptor(), content, stock_code=code, requested_from=request[0],
                               requested_to=request[1], convention=convention, assessment=assessment)
        copied.append(item.sha)
    return copied


def _seed_surveillance(research, session, window) -> list[str]:
    rows = research.query(
        "SELECT source_sha256, list_name FROM surveillance_snapshots WHERE effective_date >= ? AND effective_date <= ?",
        list(window))
    copied: list[str] = []
    listed = {row[0]: row[1] for row in rows}
    for item in _ordered(research, set(listed)):
        ingest_surveillance(session, item.descriptor(), research.read_blob(item.sha), list_name=listed[item.sha])
        copied.append(item.sha)
    return copied


def _seed_bands(research, session, window) -> dict[str, list[str]]:
    rows = research.query(
        "SELECT source_sha256, file_kind, file_date FROM price_band_files WHERE file_date >= ? AND file_date <= ?",
        list(window))
    out: dict[str, list[str]] = {"price_band_list": [], "price_band_changes": []}
    sources = _sources(research, {row[0] for row in rows})
    # one blob can be filed on several dates (an unchanged list), so the unit is (blob, kind, file date)
    for sha, kind, file_date in sorted(rows, key=lambda row: (sources[row[0]].first_fetched, row[2], row[0])):
        content = research.read_blob(sha)
        if kind == "list":
            ingest_band_list(session, sources[sha].descriptor(), content, file_date=file_date, workspace="india")
            bucket = "price_band_list"
        else:
            ingest_band_changes(session, sources[sha].descriptor(), content, file_date=file_date, workspace="india")
            bucket = "price_band_changes"
        if sha not in out[bucket]:
            out[bucket].append(sha)
    return out


def _seed_index_lists(research, session, window) -> list[str]:
    """Every list fetched by ``window[0]`` (IST date), oldest first, so ``latest_index_list`` ties break as they did."""
    rows = research.query(
        "SELECT source_sha256, list_name, fetched_at_utc FROM index_list_snapshots WHERE fetched_at_utc < ? "
        "ORDER BY fetched_at_utc, rowid", [fetched_by_cutoff_utc(window[0])])
    sources = _sources(research, {row[0] for row in rows})
    copied: list[str] = []
    for sha, name, fetched in rows:
        if name not in EXPECTED_MEMBERS:
            raise PilotDataError("seed_index_list_unknown", f"snapshot {sha[:12]} names an unknown list {name!r}")
        if sources[sha].first_fetched != fetched:
            raise PilotDataError("seed_index_list_fetch_time",
                                 f"snapshot {sha[:12]} was fetched at a time its source_files row does not carry")
        list_name: ListName = name
        ingest_index_list(session, sources[sha].descriptor(), research.read_blob(sha), list_name=list_name)
        if sha not in copied:
            copied.append(sha)
    return copied


def _upstream_quarantine_rows(store: PilotDataStore, window: tuple[date, date]) -> list[tuple]:
    return store.query(
        "SELECT quarantine_id, workspace, check_name, reason_code, scope, isin, nse_symbol, stock_code, series, "
        "date_from, date_to, detail_json, evidence_json, run_id FROM quarantine_records "
        "WHERE scope IN ('raw', 'both') AND check_name NOT IN ('crosscheck', 'rawness') "
        "AND (date_to IS NULL OR date_to >= ?) AND (date_from IS NULL OR date_from <= ?) ORDER BY quarantine_id",
        [window[0], window[1]])


def _copy_quarantines(research, session, window) -> int:
    records = []
    for row in _upstream_quarantine_rows(research, window):
        if row[2] in REGENERATED_CHECKS:
            continue
        records.append(QuarantineRecord(
            workspace="india", check=row[2], reason_code=row[3], scope=row[4], isin=row[5], nse_symbol=row[6],
            stock_code=row[7], series=row[8], date_from=row[9], date_to=row[10], detail=json.loads(row[11]),
            evidence_sha256s=tuple(json.loads(row[12])), run_id=row[13]))
    return session.record_quarantine(records) if records else 0


# --------------------------------------------------------------------------- parity
def _fail(reader: str, detail: str) -> PilotDataError:
    return PilotDataError("seed_parity_failed", f"reader {reader}: {detail}")


def _merged(ranges: list[tuple[date, date]]) -> tuple[tuple[date, date], ...]:
    merged: list[tuple[date, date]] = []
    for low, high in sorted(ranges):
        if merged and low <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], high))
        else:
            merged.append((low, high))
    return tuple(merged)


def _event_view(event, window_end: date, window_start: date):
    """Identity and first sighting always; sighting counts and evidence only when no later PR zip can differ."""
    core = (event.event_id, event.nse_symbol, event.ex_date, event.record_date, event.purpose_norm, event.parts,
            event.adjustable, event.first_seen_file_date)
    if event.last_seen_file_date <= window_end:
        return core + (event.series_seen, event.last_seen_file_date, event.sightings, event.evidence_sha256s)
    return core


def _parity(research: PilotDataStore, session: PilotDataStore, *, targets: TargetUniverseResult,
            window: tuple[date, date], optional: dict[str, bool], sources: dict[str, list[str]],
            band_offset: int) -> tuple[ParityRow, ...]:
    out: list[ParityRow] = []
    days = [window[0] + timedelta(days=n) for n in range((window[1] - window[0]).days + 1)]
    statuses = {day: day_status(research, day) for day in days}
    for day in days:
        if day_status(session, day) != statuses[day]:
            raise _fail("day_status", f"{day.isoformat()} differs")
    out.append(ParityRow(reader="day_status", compared=len(days), detail="every window date"))
    sessions = [day for day in days if statuses[day] == "session"]
    for day in days:
        if primary_bars_on(session, day) != primary_bars_on(research, day):
            raise _fail("primary_bars_on", f"{day.isoformat()} differs")
    out.append(ParityRow(reader="primary_bars_on", compared=len(days), detail="every window date"))
    for member in targets.members:
        mine, theirs = (breeze_bars_for(s, member.stock_code, start=window[0], end=window[1])
                        for s in (session, research))
        clipped = _merged([c for c in (_clip(low, high, window) for low, high in theirs.covered_dates) if c])
        if (mine.bars, mine.conflicts, mine.covered_dates) != (theirs.bars, theirs.conflicts, clipped):
            raise _fail("breeze_bars_for", f"{member.stock_code} bars, conflicts or coverage differ")
    out.append(ParityRow(reader="breeze_bars_for", compared=len(targets.members), detail="per target stock_code"))
    for member in targets.members:
        got = [_event_view(e, window[1], window[0]) for e in
               events_for_symbol(session, member.nse_symbol, ex_from=window[0], ex_to=window[1])]
        want = [_event_view(e, window[1], window[0]) for e in
                events_for_symbol(research, member.nse_symbol, ex_from=window[0], ex_to=window[1])
                if e.first_seen_file_date <= window[1] and (e.ex_date is not None or e.first_seen_file_date >= window[0])]
        if got != want:
            raise _fail("events_for_symbol", f"{member.nse_symbol} differs")
    out.append(ParityRow(reader="events_for_symbol", compared=len(targets.members), detail="per target symbol"))
    if optional["surveillance"]:
        count = 0
        for name in ("asm", "gsm"):
            for day in days:
                a, b = snapshot_for(session, name, day), snapshot_for(research, name, day)
                if a != b:
                    raise _fail("snapshot_for", f"{name} {day.isoformat()} differs")
                if a is not None and surveillance_entries(session, a) != surveillance_entries(research, b):
                    raise _fail("surveillance_entries", f"{name} {day.isoformat()} differs")
                count += 1
        out.append(ParityRow(reader="snapshot_for", compared=count, detail="asm and gsm per window date"))
    if optional["bands"]:
        floor = window[0] + timedelta(days=band_offset)
        mine_resolver, theirs_resolver = BandResolver(session), BandResolver(research)
        count = 0
        for member in targets.members:
            for day in (d for d in sessions if d >= floor):
                if mine_resolver.observe(member.anchor_isin, day) != theirs_resolver.observe(member.anchor_isin, day):
                    raise _fail("band_on", f"{member.anchor_isin} {day.isoformat()} differs")
                count += 1
        out.append(ParityRow(reader="band_on", compared=count, detail=f"targets x sessions from {floor.isoformat()}"))
    if optional["index_lists"]:
        cutoff = fetched_by_cutoff_utc(window[0])
        snapshots = ("SELECT source_sha256, list_name, fetched_at_utc, member_count, placeholders_json, row_sha256 "
                     "FROM index_list_snapshots{where} ORDER BY source_sha256, list_name")
        members = ("SELECT m.source_sha256, m.list_name, m.isin, m.nse_symbol, m.series, m.company_name, m.industry, "
                   "m.row_sha256 FROM index_list_members m{join} ORDER BY m.source_sha256, m.list_name, m.isin")
        # every eligible snapshot, not only the newest per name: an older one is what an earlier decision day reads
        want_snapshots = research.query(snapshots.format(where=" WHERE fetched_at_utc < ?"), [cutoff])
        got_snapshots = session.query(snapshots.format(where=""))  # unbounded: the store holds nothing later
        if got_snapshots != want_snapshots:
            raise _fail("latest_index_list", f"the seeded snapshots ({len(got_snapshots)}) differ from the research "
                                              f"snapshots fetched by window_start ({len(want_snapshots)}): hash, list, "
                                              "fetch time, member count, placeholders or row hash")
        want_members = research.query(members.format(join=" JOIN index_list_snapshots s ON s.source_sha256 = "
                                                          "m.source_sha256 AND s.list_name = m.list_name "
                                                          "WHERE s.fetched_at_utc < ?"), [cutoff])
        got_members = session.query(members.format(join=""))
        if got_members != want_members:
            raise _fail("latest_index_list", f"the seeded index list members ({len(got_members)}) differ from the "
                                              f"research members of those snapshots ({len(want_members)})")
        out.append(ParityRow(reader="latest_index_list", compared=len(want_snapshots) + len(want_members),
                             detail="every snapshot fetched by window_start with its metadata, and every member row"))
    a, b = latest_snapshot(session, on_or_before=window[1]), latest_snapshot(research, on_or_before=window[1])
    if a != b or (a is not None and master_rows(session, a) != master_rows(research, b)):
        raise _fail("latest_snapshot", "the newest master snapshot or its rows differ")
    out.append(ParityRow(reader="latest_snapshot", compared=1, detail="newest snapshot on or before window_end"))
    ids_session = [row[0] for row in _upstream_quarantine_rows(session, window)]
    ids_research = [row[0] for row in _upstream_quarantine_rows(research, window)]
    if ids_session != ids_research:
        raise _fail("upstream_quarantine_ids", f"{len(ids_session)} seeded against {len(ids_research)} research ids")
    out.append(ParityRow(reader="upstream_quarantine_ids", compared=len(ids_research), detail="raw or both, in window"))
    compared = 0
    for shas in sources.values():
        for sha in shas:
            if session.read_blob(sha) != research.read_blob(sha):
                raise _fail("source_files", f"blob {sha[:12]} differs")
            row_s = session.query("SELECT * FROM source_files WHERE source_sha256 = ?", [sha])
            row_r = research.query("SELECT * FROM source_files WHERE source_sha256 = ?", [sha])
            if row_s != row_r or not row_r:
                raise _fail("source_files", f"row {sha[:12]} differs")
            compared += 1
    out.append(ParityRow(reader="source_files", compared=compared, detail="blob bytes and source_files row"))
    return tuple(out)


# --------------------------------------------------------------------------- entry point
def _open_research(research_root: Path) -> PilotDataStore:
    try:
        return PilotDataStore(research_root, workspace="india", read_only=True)
    except duckdb.Error as error:  # a lock held by another process, or an unreadable file
        raise PilotDataError("seed_research_unavailable", f"the research store could not be opened read-only: {error}") from error


def seed_session_store(session_root: Path, research_root: Path, *, dataset_dir: Path, window_start: date,
                       window_end: date, workspace: Literal["india"],
                       band_parity_offset_days: int = BAND_PARITY_OFFSET_DAYS) -> SeedReport:
    if workspace != "india":
        raise PilotDataError("workspace_invalid", "only the india workspace is supported")
    if window_start > window_end:
        raise PilotDataError("seed_window_invalid", "window_start is after window_end")
    window = (window_start, window_end)
    session_root, research_root, dataset_dir = Path(session_root), Path(research_root), Path(dataset_dir)
    _refuse_overlap(session_root, research_root, dataset_dir)
    manifest = verify_dataset(dataset_dir, workspace=workspace)
    with _open_research(research_root) as research:
        optional = _require_research_tables(research)
        targets = _pinned_targets(research, manifest.dataset_sha256, manifest.targets_sha256)
        identity = _identity_hash(research)
        breeze_convention = None
        if optional["probe"]:
            probe = latest_conclusive_probe(research)
            breeze_convention = None if probe is None else probe.convention()
        sources: dict[str, list[str]] = {}
        with PilotDataStore(session_root, workspace="india") as session:
            for ensure in (ensure_fetch_log, ensure_master_tables, ensure_band_tables, ensure_surveillance_tables,
                           ensure_constituent_tables):
                ensure(session)
            sources["security_master"] = _seed_security_master(research, session, window, optional)
            sources.update({f"bhavcopy_{kind}": shas for kind, shas in _seed_bhavcopy(research, session, window, optional).items()})
            derive_corporate_actions(session, workspace="india")
            fetch_rows = _copy_fetch_log(research, session, window)
            sources["breeze"] = _seed_breeze(research, session, window, optional)
            if optional["surveillance"]:
                sources["surveillance"] = _seed_surveillance(research, session, window)
            if optional["bands"]:
                sources.update(_seed_bands(research, session, window))
                convention = current_band_convention(research)
                if convention is not None:
                    record_band_convention(session, convention, workspace="india")
            if optional["index_lists"]:
                sources["index_list"] = _seed_index_lists(research, session, window)
            copied = _copy_quarantines(research, session, window)
        band_convention = current_band_convention(research) if optional["bands"] else None
        with PilotDataStore(session_root, workspace="india", read_only=True) as seeded:
            parity = _parity(research, seeded, targets=targets, window=window, optional=optional, sources=sources,
                              band_offset=band_parity_offset_days)
        methods = dict(sorted(SEED_TABLES.items()))
        draft = SeedReport(
            workspace="india", caveats=standard_caveats(), window_start=window_start, window_end=window_end,
            dataset_sha256=manifest.dataset_sha256, research_identity_sha256=identity, targets=targets,
            targets_sha256=targets.target_sha256, breeze_convention=breeze_convention,
            band_convention=band_convention, sources_by_kind={k: tuple(v) for k, v in sorted(sources.items())},
            fetch_log_rows=fetch_rows, quarantines_copied=copied, table_methods=methods, parity=parity,
            report_sha256="",
        )
        report = draft.model_copy(update={"report_sha256": draft.expected_sha256()})
    _write_report(session_root, report)
    return report


def _write_report(session_root: Path, report: SeedReport) -> None:
    with PilotDataStore(session_root, workspace="india") as session:
        session.ensure_table("session_store_seeds", SEEDS_DDL, key_columns=("seed_report_sha256",))
        session.append_rows(
            "session_store_seeds",
            [{"seed_report_sha256": report.report_sha256, "research_identity_sha256": report.research_identity_sha256,
              "dataset_sha256": report.dataset_sha256, "window_start": report.window_start,
              "window_end": report.window_end, "seeded_at_utc": utc_naive(datetime.now(timezone.utc)),
              "row_sha256": canonical_sha256({"report": report.report_sha256}),
              "source_sha256": report.report_sha256}],
            check="session_store_seed",
        )
    target = session_root / SEED_REPORT_FILE
    target.write_text(json.dumps(report.model_dump(mode="json"), sort_keys=True, indent=2), encoding="utf-8")
    os.chmod(target, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


def load_seed_report(session_root: Path) -> SeedReport:
    """Read ``seed_report.json`` back and refuse one whose content no longer hashes to its own report_sha256."""
    path = Path(session_root) / SEED_REPORT_FILE
    try:
        report = SeedReport.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError) as error:
        raise PilotDataError("seed_report_missing", "seed_report.json is missing or unreadable") from error
    if report.expected_sha256() != report.report_sha256:
        raise PilotDataError("seed_report_tampered", "seed_report.json does not hash to its report_sha256")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pilot_data.session_store", description="Seed an isolated session store.")
    sub = parser.add_subparsers(dest="command", required=True)
    seed = sub.add_parser("seed")
    seed.add_argument("--research-root", required=True, type=Path)
    seed.add_argument("--dataset", required=True, type=Path)
    seed.add_argument("--session-root", required=True, type=Path)
    seed.add_argument("--window-start", required=True, type=date.fromisoformat)
    seed.add_argument("--window-end", required=True, type=date.fromisoformat)
    seed.add_argument("--workspace", required=True, choices=["india"])
    args = parser.parse_args(argv)
    try:
        report = seed_session_store(
            args.session_root, args.research_root, dataset_dir=args.dataset, window_start=args.window_start,
            window_end=args.window_end, workspace=args.workspace)
    except PilotDataError as error:
        print(json.dumps({"caveats": [c.model_dump() for c in standard_caveats()], "error_code": error.code,
                          "error": str(error)}, indent=2))
        return 2
    print(json.dumps({"caveats": [c.model_dump() for c in report.caveats],
                      "seed_report": str(args.session_root / SEED_REPORT_FILE),
                      "seed_report_sha256": report.report_sha256}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
