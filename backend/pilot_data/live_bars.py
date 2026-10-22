"""Strategy-ready bars as of a session that has not traded yet (64-08, G-1b, decision D-33).

``bars_as_of`` re-runs the cross-check and the adjustment recipe of ``build_dataset_snapshot`` over
``[window_start, as_of]`` against a seeded session store. Nothing dated after ``as_of`` reaches a reader: the
window ends at ``as_of``, ``day_status`` is never asked about ``decision_session`` or later, and every row carries an
as-of-scoped composition id instead of the 59 run id (which hashes every Breeze answer and quarantine in the store,
whatever its date).

One look-ahead path sits in code this module may not edit: ``build_lineage`` links an ISIN change to a split event
without checking when the event was first seen. ``_as_of_lineage`` removes any link whose evidence event was first
seen after ``as_of``, which is the lineage an earlier run would have built. The cross-check runs on that trimmed
lineage (``lineage_override``), so which bhavcopy rows match, which earlier quarantines exclude a day and which bars
are accepted never depend on an ISIN or an event the store only learned after ``as_of``. Rows are written only into
a store that carries a ``session_store_seeds`` row, never into the research store.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import date, timedelta, timezone
from pathlib import Path
from typing import Literal

from .adjustment import (
    AdjustmentPolicy, adjusted_series, compute_factor_set, events_for_lineage, quarantine_unresolved,
    verify_source_unadjusted,
)
from .breeze_bars import breeze_bars_for
from .core import (
    BREEZE_RAW_UNVERIFIED_CAVEAT, CaveatedResult, PilotDataError, canonical_sha256, standard_caveats,
)
from .corporate_actions import SIGHTINGS_TABLE, CorporateActionEvent, events_for_symbol
from .crosscheck import accepted_bars, run_crosscheck
from .dataset import DatasetRow, DividendAmountUnknownEvent, read_dataset_rows, verify_dataset
from .lineage import build_lineage, primary_bars_for_lineage
from .models import CrossCheckTolerances, Lineage, RawDailyBar
from .nse_calendar import PinnedCalendar
from .session_store import load_seed_report
from .sessions import FINAL_STATUSES, day_status
from .store import PilotDataStore
from .targets import TargetUniverseResult

ROW_SOURCE = "breeze_v2_1day_via_relay"


class LiveBarsResult(CaveatedResult):
    decision_session: date
    as_of: date
    window_start: date
    targets_sha256: str
    tolerances: CrossCheckTolerances
    rows: tuple[DatasetRow, ...]
    lineage_hashes: dict[str, str]
    factor_set_hashes: dict[str, str]
    # same type and attribute name as DatasetManifest, so strategy_india.data.events_from_manifest takes it unchanged
    dividend_amount_unknown_events: dict[str, tuple[DividendAmountUnknownEvent, ...]]
    source_sha256s: tuple[str, ...]
    store_crosscheck_run_id: str  # provenance only: the 59 run id sees post-as_of data and is outside every identity claim
    composition_sha256: str


def _require_session_store(store: PilotDataStore) -> None:
    if store.read_only:
        raise PilotDataError("store_read_only", "bars_as_of writes cross-check runs and needs a writable session store")
    if not store.table_exists("session_store_seeds") or not store.query("SELECT 1 FROM session_store_seeds LIMIT 1"):
        raise PilotDataError("not_a_session_store", "the store has no session_store_seeds row; seed a session store first")


def _calendar_verdict(calendar: PinnedCalendar | None, day: date) -> Literal["non_session", "session", "special", "unknown"]:
    if calendar is None or not calendar.covers(day):
        return "unknown"
    if calendar.is_special(day):
        return "special"
    return "non_session" if calendar.is_weekend(day) or calendar.is_holiday(day) else "session"


def _check_dates(store: PilotDataStore, decision_session: date, as_of: date, window_start: date,
                 calendar: PinnedCalendar | None) -> None:
    if as_of >= decision_session:
        raise PilotDataError("as_of_not_before_decision", "as_of must be before the decision session")
    if window_start > as_of:
        raise PilotDataError("window_after_as_of", "window_start is after as_of")
    if day_status(store, as_of) != "session":
        raise PilotDataError("as_of_not_session", "as_of is not a session in the store's calendar")
    day = as_of + timedelta(days=1)
    while day < decision_session:  # decision_session itself is never asked about
        status = day_status(store, day)
        listed = _calendar_verdict(calendar, day)
        if status == "session":
            raise PilotDataError("stale_as_of", f"{day.isoformat()} is a session between as_of and the decision session")
        if status in FINAL_STATUSES:
            if listed in ("session", "special"):  # the store says no session, the pinned table says there may be one
                raise PilotDataError("calendar_conflict", f"{day.isoformat()} is {status} in the store but the pinned "
                                                          f"calendar lists it as a {listed} day")
        elif status == "unknown" and listed == "non_session":
            pass  # the store has no fetch evidence for the day and the pinned table covers it as a weekend or holiday
        else:  # pending and inconsistent never pass; an unknown day could be a session: fail closed, as sessions_between does
            raise PilotDataError("calendar_unknown_dates",
                                 f"{day.isoformat()} between as_of and the decision session is {status}, not a known non-session")
        day += timedelta(days=1)


def _link_first_seen(store: PilotDataStore, lineage: Lineage, index: int) -> date | None:
    """First-seen file date of the event that evidences the link from segment `index` to the next one."""
    evidence = lineage.segments[index].evidence
    newer = lineage.segments[index + 1]
    if evidence is None:
        return None
    event_id = evidence.split("|", 1)[0]
    for event in events_for_symbol(store, newer.nse_symbol, ex_from=newer.valid_from, ex_to=newer.valid_from):
        if event.event_id == event_id:
            return event.first_seen_file_date
    raise PilotDataError("lineage_link_event_missing", f"link event {event_id[:12]} is not in the events table")


def _as_of_lineage(store: PilotDataStore, lineage: Lineage, as_of: date) -> Lineage:
    """The lineage `build_lineage` would give if events first seen after as_of did not exist yet."""
    cut: int | None = None
    for index, segment in enumerate(lineage.segments[:-1]):
        if segment.link != "isin_change_split":
            continue
        first_seen = _link_first_seen(store, lineage, index)
        if first_seen is not None and first_seen > as_of:
            cut = index + 1  # the walk goes newest to oldest, so the newest unknown link ends it
    if cut is None:
        return lineage
    kept = lineage.segments[cut:]
    return lineage.model_copy(update={
        "segments": kept, "resolved_from": kept[0].valid_from, "unresolved_before": kept[0].valid_from,
        "unresolved_reason": "isin_change_unexplained",
    })


def _as_of_events(store: PilotDataStore, lineage: Lineage, as_of: date) -> tuple[CorporateActionEvent, ...]:
    """Events as an earlier run would have read them: unseen events dropped, evidence from sightings on or before as_of only."""
    out: list[CorporateActionEvent] = []
    for event in events_for_lineage(store, lineage):
        if event.first_seen_file_date > as_of:
            continue
        seen = store.query(
            f"SELECT series, file_date, source_sha256 FROM {SIGHTINGS_TABLE} WHERE event_id = ? AND file_date <= ? "
            "ORDER BY file_date, series", [event.event_id, as_of])
        out.append(event.model_copy(update={
            "series_seen": tuple(sorted({row[0] for row in seen})), "last_seen_file_date": max(row[1] for row in seen),
            "sightings": len(seen), "evidence_sha256s": tuple(sorted({row[2] for row in seen})),
        }))
    return tuple(out)


def _as_of_rawness(store: PilotDataStore, member, lineage: Lineage, reference, factor_set, *, window_start: date,
                   as_of: date, workspace: Literal["india"]) -> str:
    """Rawness of Breeze against the trimmed lineage, the way the cross-check derives it for the lineage it built."""
    candidate = []
    for bar in breeze_bars_for(store, member.stock_code, start=window_start, end=as_of).bars:
        isin = lineage.isin_on(bar.trade_date)
        if isin is None:
            continue
        candidate.append(RawDailyBar(
            trade_date=bar.trade_date, isin=isin, series=member.anchor_series,
            nse_symbol=lineage.symbol_on(bar.trade_date) or "", open=bar.open, high=bar.high, low=bar.low,
            close=bar.close, volume=bar.volume, traded_value=None, source_kind="breeze", source_sha256=bar.source_sha256))
    return verify_source_unadjusted(reference, candidate, factor_set, source_label="breeze_v2_1day",
                                    workspace=workspace).overall


def _events_payload(events: dict[str, tuple[DividendAmountUnknownEvent, ...]]) -> dict[str, list[list[str]]]:
    return {anchor: sorted([event.ex_date.isoformat(), event.event_id] for event in found)
            for anchor, found in sorted(events.items())}


def bars_as_of(store: PilotDataStore, *, targets: TargetUniverseResult, decision_session: date, as_of: date,
               window_start: date, tolerances: CrossCheckTolerances, workspace: Literal["india"],
               calendar: PinnedCalendar | None = None) -> LiveBarsResult:
    """`calendar` is the operator's pinned table: it lets a day between as_of and the decision session that the store
    has no fetch evidence for (a weekend a Friday-bounded seed never logged) count as a non-session. It is never read
    for the decision session itself and never overrides store evidence."""
    if workspace != store.workspace:
        raise PilotDataError("workspace_mismatch", "workspace differs from the store workspace")
    _require_session_store(store)
    _check_dates(store, decision_session, as_of, window_start, calendar)
    report = run_crosscheck(store, targets=targets, window_start=window_start, window_end=as_of, as_of=as_of,
                            tolerances=tolerances, workspace=workspace,
                            lineage_override=lambda member, built: _as_of_lineage(store, built, as_of))
    accepted = accepted_bars(store, report.run_id)
    policy = AdjustmentPolicy()
    # The cross-check ran on the as-of lineage too, so its acceptance, its earlier-quarantine exclusions and its
    # rawness never saw a link first seen after as_of. The rawness it computed still read every event of that lineage,
    # so a trimmed lineage recomputes it from the events an earlier run would have had.
    states: dict[str, dict] = {}
    for member in report.members:
        if member.status != "checked":
            continue
        built = build_lineage(store, anchor_isin=member.anchor_isin, anchor_series=member.anchor_series, as_of=as_of,
                              workspace=workspace, stock_code=member.stock_code)
        lineage = _as_of_lineage(store, built, as_of)
        if lineage.content_sha256() != member.lineage_sha256:
            raise PilotDataError("lineage_drift", f"as-of lineage for {member.stock_code} differs from the cross-check run")
        reference = primary_bars_for_lineage(store, lineage, start=window_start, end=as_of)
        factor_set = compute_factor_set(lineage, reference, _as_of_events(store, lineage, as_of), as_of=as_of, policy=policy)
        from_store = compute_factor_set(lineage, reference, events_for_lineage(store, lineage), as_of=as_of, policy=policy)
        if from_store.factor_set_sha256 != member.factor_set_sha256:
            raise PilotDataError("factor_set_drift", f"factor set for {member.stock_code} differs from the cross-check run")
        if lineage is built:
            rawness = member.rawness_overall
        else:
            rawness = _as_of_rawness(store, member, lineage, reference, factor_set, window_start=window_start,
                                     as_of=as_of, workspace=workspace)
        states[member.stock_code] = {"member": member, "lineage": lineage, "factor_set": factor_set, "rawness": rawness}
    if any(state["rawness"] == "adjusted_detected" for state in states.values()):
        raise PilotDataError("breeze_adjusted_detected", "Breeze history looks adjusted; no bars are composed")
    parts: list[dict] = []
    for stock_code in sorted({bar.stock_code for bar in accepted}):
        member, lineage, factor_set = (states[stock_code][key] for key in ("member", "lineage", "factor_set"))
        mine = [bar for bar in accepted if bar.stock_code == stock_code]
        if not mine:
            continue
        stored = {
            (row[0], row[1]): row[2:] for row in store.query(
                "SELECT trade_date, source_sha256, open, high, low, close, volume FROM breeze_bars_raw "
                "WHERE stock_code = ? AND trade_date >= ? AND trade_date <= ?", [stock_code, window_start, as_of])
        }
        raw_bars: list[RawDailyBar] = []
        fetched: dict[str, str] = {}
        for bar in mine:
            values = stored[(bar.trade_date, bar.breeze_source_sha256)]
            raw_bars.append(RawDailyBar(
                trade_date=bar.trade_date, isin=bar.isin, series=bar.series,
                nse_symbol=lineage.symbol_on(bar.trade_date) or "", open=values[0], high=values[1], low=values[2],
                close=values[3], volume=int(values[4]), traded_value=None, source_kind="breeze",
                source_sha256=bar.breeze_source_sha256))
            if bar.breeze_source_sha256 not in fetched:
                when = store.query("SELECT first_fetched_at_utc FROM source_files WHERE source_sha256 = ?",
                                   [bar.breeze_source_sha256])[0][0]
                fetched[bar.breeze_source_sha256] = when.replace(tzinfo=timezone.utc).isoformat()
        series = adjusted_series(raw_bars, factor_set, as_of=as_of, workspace=workspace)
        quarantine_unresolved(store, lineage, factor_set, workspace=workspace)
        parts.append({"stock_code": stock_code, "member": member, "mine": mine, "lineage": lineage,
                      "factor_set": factor_set, "raw_bars": raw_bars, "fetched": fetched, "series": series,
                      "rawness": states[stock_code]["rawness"]})
    if not parts:
        raise PilotDataError("live_bars_empty", "the cross-check accepted no bars as of this session")
    unknown_events: dict[str, tuple[DividendAmountUnknownEvent, ...]] = {}
    for part in parts:
        found = part["factor_set"].unknown_dividend_events()
        if found:
            unknown_events[part["member"].anchor_isin] = tuple(
                DividendAmountUnknownEvent(event_id=item.event_id, ex_date=item.ex_date) for item in found)
    lineage_hashes = {p["member"].anchor_isin: p["lineage"].content_sha256() for p in parts}
    factor_hashes = {p["member"].anchor_isin: p["factor_set"].factor_set_sha256 for p in parts}
    pairs = sorted([bar.breeze_row_sha256, bar.bhav_row_sha256] for p in parts for bar in p["mine"])
    composition = canonical_sha256({
        "targets": targets.target_sha256, "tolerances": tolerances.model_dump(mode="json"),
        "window": [window_start.isoformat(), as_of.isoformat()], "as_of": as_of.isoformat(),
        "lineages": dict(sorted(lineage_hashes.items())), "factor_sets": dict(sorted(factor_hashes.items())),
        "rawness": {p["stock_code"]: p["rawness"] for p in parts}, "accepted": pairs,
        "dividend_amount_unknown_events": _events_payload(unknown_events),
    })
    rows: list[DatasetRow] = []
    for part in parts:
        member, series = part["member"], part["series"]
        basis = ("as_traded_breeze_raw_confirmed" if part["rawness"] == "raw_confirmed"
                 else "as_traded_breeze_unverified")
        by_day = {bar.trade_date: bar for bar in part["mine"]}
        symbols = {raw.trade_date: raw.nse_symbol for raw in part["raw_bars"]}
        for adj in series.bars:
            accepted_bar = by_day[adj.trade_date]
            rows.append(DatasetRow(
                workspace="india", anchor_isin=member.anchor_isin, isin=adj.isin, nse_symbol=symbols[adj.trade_date],
                series=accepted_bar.series, stock_code=part["stock_code"], trade_date=adj.trade_date,
                raw_open=adj.raw_open, raw_high=adj.raw_high, raw_low=adj.raw_low, raw_close=adj.raw_close,
                raw_volume=adj.raw_volume, adj_open=adj.adj_open, adj_high=adj.adj_high, adj_low=adj.adj_low,
                adj_close=adj.adj_close, adj_volume=adj.adj_volume, adjusted_quarantined=adj.adjusted_quarantined,
                dividend_amount_unknown=adj.dividend_amount_unknown,
                dividend_amount_unknown_ex_date=adj.dividend_amount_unknown_ex_date, raw_adjustment_basis=basis,
                adjusted_basis=series.adjustment_basis, source=ROW_SOURCE,
                source_sha256=accepted_bar.breeze_source_sha256, fetched_at_utc=part["fetched"][accepted_bar.breeze_source_sha256],
                bhavcopy_source_sha256=accepted_bar.bhav_source_sha256, crosscheck_run_id=composition))
    rows.sort(key=lambda row: (row.anchor_isin, row.trade_date))
    all_raw = all(p["rawness"] == "raw_confirmed" for p in parts)
    sources = sorted({sha for p in parts for bar in p["mine"] for sha in (bar.breeze_source_sha256, bar.bhav_source_sha256)})
    return LiveBarsResult(
        workspace="india", caveats=standard_caveats(*(() if all_raw else (BREEZE_RAW_UNVERIFIED_CAVEAT,))),
        decision_session=decision_session, as_of=as_of, window_start=window_start,
        targets_sha256=targets.target_sha256, tolerances=tolerances, rows=tuple(rows),
        lineage_hashes=dict(sorted(lineage_hashes.items())), factor_set_hashes=dict(sorted(factor_hashes.items())),
        dividend_amount_unknown_events=dict(sorted(unknown_events.items())), source_sha256s=tuple(sources),
        store_crosscheck_run_id=report.run_id, composition_sha256=composition,
    )


# --------------------------------------------------------------------------- parity CLI
def _row_view(row: DatasetRow) -> dict[str, object]:
    return {name: value for name, value in row.model_dump().items() if name != "crosscheck_run_id"}


def _parity(dataset_dir: Path, session_root: Path, decision_session: date) -> tuple[dict, bool]:
    manifest = verify_dataset(dataset_dir, workspace="india")
    seed = load_seed_report(session_root)
    if seed.targets.target_sha256 != manifest.targets_sha256:
        raise PilotDataError("seed_targets_mismatch", "the seeded targets are not the dataset's pinned targets")
    frozen = read_dataset_rows(dataset_dir)
    started = time.perf_counter()
    with PilotDataStore(session_root, workspace="india") as store:
        result = bars_as_of(store, targets=seed.targets, decision_session=decision_session, as_of=manifest.window_end,
                            window_start=manifest.window_start, tolerances=CrossCheckTolerances(), workspace="india")
    wall = time.perf_counter() - started
    mine = {(row.anchor_isin, row.trade_date): _row_view(row) for row in result.rows}
    theirs = {(row.anchor_isin, row.trade_date): _row_view(row) for row in frozen}
    anchors = sorted({anchor for anchor, _ in mine} | {anchor for anchor, _ in theirs})
    per_anchor = {
        anchor: {"frozen_rows": sum(1 for key in theirs if key[0] == anchor),
                 "live_rows": sum(1 for key in mine if key[0] == anchor),
                 "equal": {key: value for key, value in mine.items() if key[0] == anchor}
                 == {key: value for key, value in theirs.items() if key[0] == anchor}}
        for anchor in anchors
    }
    differing = sorted(anchor for anchor in set(result.factor_set_hashes) | set(manifest.factor_set_hashes)
                       if result.factor_set_hashes.get(anchor) != manifest.factor_set_hashes.get(anchor))
    ok = mine == theirs and not differing
    return {"anchors": per_anchor, "factor_set_differs": differing, "wall_seconds": round(wall, 3),
            "composition_sha256": result.composition_sha256, "rows": len(result.rows), "equal": ok}, ok


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pilot_data.live_bars", description="Offline checks of strategy-ready bars.")
    sub = parser.add_subparsers(dest="command", required=True)
    parity = sub.add_parser("parity")
    parity.add_argument("--dataset", required=True, type=Path)
    parity.add_argument("--session-root", required=True, type=Path)
    parity.add_argument("--decision-session", required=True, type=date.fromisoformat)
    args = parser.parse_args(argv)
    caveats = [caveat.model_dump() for caveat in standard_caveats(BREEZE_RAW_UNVERIFIED_CAVEAT)]
    try:
        summary, ok = _parity(args.dataset, args.session_root, args.decision_session)
    except PilotDataError as error:
        print(json.dumps({"caveats": caveats, "error_code": error.code, "error": str(error)}, indent=2))
        return 2
    print(json.dumps({"caveats": caveats, "parity": summary}, indent=2, default=str))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
