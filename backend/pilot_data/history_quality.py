"""Per-name spans, internal and trailing gaps, and unresolved-identity quarantine (D-07).

A name listed during the window keeps its shorter history. Only sessions the lineage
covers are expected to have bars; gaps inside that range are quarantined for both the raw
and adjusted layers.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict

from .bhavcopy import ensure_bhavcopy_tables
from .core import CaveatedResult, PilotDataError, standard_caveats
from .models import Lineage, QuarantineRecord
from .sessions import sessions_between
from .store import PilotDataStore


class GapRange(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    start: date
    end: date
    sessions: int


class SpanReport(CaveatedResult):
    anchor_isin: str
    stock_code: str | None
    window_start: date
    window_end: date
    first_session: date | None
    last_session: date | None
    sessions_expected: int
    sessions_present: int
    gap_ranges: tuple[GapRange, ...]
    trailing_gap: GapRange | None
    short_history: bool
    listed_within_window: bool
    unresolved_before: date | None
    lineage_sha256: str


def _merge(sessions: list[date], missing: list[date]) -> list[GapRange]:
    """Merge missing sessions that are consecutive in session order into ranges."""
    index = {day: i for i, day in enumerate(sessions)}
    ranges: list[GapRange] = []
    run: list[date] = []
    for day in missing:
        if run and index[day] == index[run[-1]] + 1:
            run.append(day)
        else:
            if run:
                ranges.append(GapRange(start=run[0], end=run[-1], sessions=len(run)))
            run = [day]
    if run:
        ranges.append(GapRange(start=run[0], end=run[-1], sessions=len(run)))
    return ranges


def compute_span(
    store: PilotDataStore, lineage: Lineage, *, window_start: date, window_end: date,
    workspace: Literal["india"],
) -> SpanReport:
    if workspace != store.workspace:
        raise PilotDataError("workspace_mismatch", "workspace differs from the store workspace")
    ensure_bhavcopy_tables(store)
    sessions = list(sessions_between(store, window_start, window_end))
    floor = max(window_start, lineage.resolved_from, lineage.unresolved_before or date.min)
    covered = [day for day in sessions if day >= floor]
    present_days: set[date] = set()
    for segment in lineage.segments:
        lo, hi = max(window_start, segment.valid_from), min(window_end, segment.valid_to)
        if lo > hi:
            continue
        for (day,) in store.query(
            "SELECT DISTINCT trade_date FROM bhavcopy_bars WHERE isin = ? AND file_kind IN ('udiff', 'cm_legacy') "
            "AND trade_date >= ? AND trade_date <= ?",
            [segment.isin, lo, hi],
        ):
            if lineage.isin_on(day) == segment.isin:
                present_days.add(day)
    present = [day for day in covered if day in present_days]
    first_session = present[0] if present else None
    last_session = present[-1] if present else None
    internal: list[date] = []
    trailing: list[date] = []
    leading_hole = False
    for day in covered:
        if day in present_days:
            continue
        if first_session is None:
            trailing.append(day)
        elif day < first_session:
            # The lineage already covers this session, so a missing bar is a hole, not a later listing.
            if lineage.resolved_from < first_session:
                internal.append(day)
                leading_hole = True
        elif day < last_session:  # type: ignore[operator]
            internal.append(day)
        else:
            trailing.append(day)
    trailing_ranges = _merge(sessions, trailing)
    short_history = first_session is None or (bool(sessions) and first_session > sessions[0])
    listed_within_window = (
        short_history
        and lineage.unresolved_before is None
        and lineage.resolved_from >= window_start
        and not leading_hole
    )
    return SpanReport(
        workspace="india",
        caveats=standard_caveats(),
        anchor_isin=lineage.anchor_isin,
        stock_code=lineage.stock_code,
        window_start=window_start,
        window_end=window_end,
        first_session=first_session,
        last_session=last_session,
        sessions_expected=len(covered),
        sessions_present=len(present),
        gap_ranges=tuple(_merge(sessions, internal)),
        trailing_gap=trailing_ranges[0] if trailing_ranges else None,
        short_history=short_history,
        listed_within_window=listed_within_window,
        unresolved_before=lineage.unresolved_before,
        lineage_sha256=lineage.content_sha256(),
    )


def record_history_quarantines(
    store: PilotDataStore, lineage: Lineage, span: SpanReport, *, workspace: Literal["india"]
) -> tuple[QuarantineRecord, ...]:
    if workspace != store.workspace:
        raise PilotDataError("workspace_mismatch", "workspace differs from the store workspace")
    records: list[QuarantineRecord] = []

    def record(check: str, reason: str, start: date, end: date, isin: str, symbol: str | None, detail: dict[str, str]):
        records.append(
            QuarantineRecord(
                workspace="india", check=check, reason_code=reason, scope="both", isin=isin, nse_symbol=symbol,
                stock_code=lineage.stock_code, series=lineage.anchor_series, date_from=start, date_to=end,
                detail=detail, evidence_sha256s=(span.lineage_sha256,),
            )
        )

    for gap in span.gap_ranges:
        record("history_gap", "internal_gap", gap.start, gap.end, lineage.isin_on(gap.start) or lineage.anchor_isin,
               lineage.symbol_on(gap.start), {"sessions": str(gap.sessions)})
    if span.trailing_gap is not None:
        gap = span.trailing_gap
        record("history_gap", "trailing_gap", gap.start, gap.end, lineage.isin_on(gap.start) or lineage.anchor_isin,
               lineage.symbol_on(gap.start), {"sessions": str(gap.sessions)})
    if lineage.unresolved_before is not None and lineage.unresolved_before > span.window_start:
        end = min(span.window_end, lineage.unresolved_before - timedelta(days=1))
        record("identity", "isin_unresolved", span.window_start, end, lineage.anchor_isin,
               lineage.symbol_on(lineage.unresolved_before),
               {"unresolved_reason": lineage.unresolved_reason or "unknown",
                "unresolved_before": lineage.unresolved_before.isoformat()})
    store.record_quarantine(records)
    return tuple(records)
