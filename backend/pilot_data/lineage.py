"""Date-bounded ISIN lineage built by walking primary bhavcopy observations backwards.

A face-value split changes an ISIN on the ex-date while symbol and token stay. A link
between two ISINs needs two independent pieces of evidence: identity continuity (same
token, or same symbol and series) and a typed split or consolidation event on the exact
change date. Anything else stops the walk; earlier dates then resolve to no ISIN and are
quarantined, never guessed (G2). Price behaviour is never used as evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from .bhavcopy import NON_REGULAR_SERIES, ensure_bhavcopy_tables
from .core import PilotDataError
from .corporate_actions import events_for_symbol
from .models import IsinSegment, Lineage, RawDailyBar
from .security_master import latest_snapshot, lookup_stock_code
from .sessions import sessions_between
from .store import PilotDataStore

LINK_MAX_GAP_SESSIONS = 20
_PRIMARY = ("udiff", "cm_legacy")


@dataclass(frozen=True)
class _Obs:
    day: date
    symbol: str
    series: str
    token: int | None


def _udiff_dates(store: PilotDataStore) -> set[date]:
    return {
        row[0]
        for row in store.query("SELECT DISTINCT trade_date FROM bhavcopy_files WHERE file_kind = 'udiff'")
    }


def _observations(
    store: PilotDataStore, isin: str, *, before: date | None, as_of: date, anchor_series: str, udiff_dates: set[date]
) -> list[_Obs]:
    """One observation per date for an ISIN: UDiFF preferred, the anchor series preferred."""
    rows = store.query(
        "SELECT trade_date, file_kind, series, nse_symbol, token FROM bhavcopy_bars "
        "WHERE isin = ? AND file_kind IN ('udiff', 'cm_legacy') AND trade_date <= ? "
        + ("AND trade_date < ? " if before is not None else "")
        + "ORDER BY trade_date, series",
        [isin, as_of] + ([before] if before is not None else []),
    )
    best: dict[date, tuple[int, _Obs]] = {}
    for day, kind, series, symbol, token in rows:
        if (kind == "udiff") != (day in udiff_dates):
            continue
        rank = 0 if series == anchor_series else 1
        current = best.get(day)
        obs = _Obs(day, symbol, series, int(token) if token is not None else None)
        if current is None or rank < current[0]:
            best[day] = (rank, obs)
    return [best[day][1] for day in sorted(best)]


def _runs(observations: list[_Obs]) -> list[list[_Obs]]:
    """Split a single-ISIN observation list into runs of constant symbol (oldest first)."""
    runs: list[list[_Obs]] = []
    for obs in observations:
        if runs and runs[-1][-1].symbol == obs.symbol:
            runs[-1].append(obs)
        else:
            runs.append([obs])
    return runs


def _predecessor_candidates(
    store: PilotDataStore, *, current_isin: str, first: _Obs, udiff_dates: set[date]
) -> tuple[date | None, dict[str, bool]]:
    """Most recent date before `first.day` with a differing-ISIN row matching by token or symbol+series.

    Returns the date and a map candidate ISIN -> whether the token rule (a) matched."""
    clauses = ["(nse_symbol = ? AND series = ?)"]
    params: list[object] = [first.symbol, first.series]
    if first.token is not None:
        clauses.append("token = ?")
        params.append(first.token)
    rows = store.query(
        "SELECT trade_date, file_kind, isin, token, nse_symbol, series FROM bhavcopy_bars "
        "WHERE file_kind IN ('udiff', 'cm_legacy') AND trade_date < ? AND isin IS NOT NULL AND isin <> ? "
        f"AND ({' OR '.join(clauses)}) ORDER BY trade_date DESC",
        [first.day, current_isin, *params],
    )
    found_day: date | None = None
    candidates: dict[str, bool] = {}
    for day, kind, isin, token, symbol, series in rows:
        if (kind == "udiff") != (day in udiff_dates):
            continue
        if found_day is None:
            found_day = day
        if day != found_day:
            break
        by_token = first.token is not None and token is not None and int(token) == first.token
        candidates[isin] = candidates.get(isin, False) or by_token
    return found_day, candidates


def build_lineage(
    store: PilotDataStore,
    *,
    anchor_isin: str,
    anchor_series: str,
    as_of: date,
    workspace: Literal["india"],
    stock_code: str | None = None,
) -> Lineage:
    if workspace != store.workspace:
        raise PilotDataError("workspace_mismatch", "workspace differs from the store workspace")
    ensure_bhavcopy_tables(store)
    udiff_dates = _udiff_dates(store)
    anchor_obs = _observations(
        store, anchor_isin, before=None, as_of=as_of, anchor_series=anchor_series, udiff_dates=udiff_dates
    )
    if not anchor_obs:
        raise PilotDataError("lineage_anchor_not_observed", "anchor ISIN has no primary row on or before as_of")
    earliest = store.query(
        "SELECT min(trade_date) FROM bhavcopy_files WHERE file_kind IN ('udiff', 'cm_legacy')"
    )[0][0]
    sessions = sessions_between(store, earliest, as_of)
    position = {day: index for index, day in enumerate(sessions)}

    blocks: list[tuple[str, list[_Obs], str | None]] = [(anchor_isin, anchor_obs, None)]  # newest first
    unresolved_reason: str | None = None
    visited = {anchor_isin}
    current_isin, current_obs = anchor_isin, anchor_obs
    while True:
        first = current_obs[0]
        found_day, candidates = _predecessor_candidates(
            store, current_isin=current_isin, first=first, udiff_dates=udiff_dates
        )
        if found_day is None:
            break
        if first.day not in position or found_day not in position:
            raise PilotDataError("calendar_unknown_dates", "identity change falls outside the known calendar")
        if position[first.day] - position[found_day] > LINK_MAX_GAP_SESSIONS:
            unresolved_reason = "isin_change_gap_too_long"
            break
        if len(candidates) != 1 or next(iter(candidates)) in visited:
            unresolved_reason = "isin_change_ambiguous"
            break
        (predecessor, by_token), = candidates.items()
        event = next(
            (
                e
                for e in events_for_symbol(store, first.symbol, ex_from=first.day, ex_to=first.day)
                if e.ex_date == first.day and any(p.kind in ("split", "consolidation") for p in e.parts)
            ),
            None,
        )
        if event is None:
            unresolved_reason = "isin_change_unexplained"
            break
        evidence = event.event_id + (f"|token:{first.token}" if by_token else "")
        older_obs = _observations(
            store, predecessor, before=first.day, as_of=as_of, anchor_series=anchor_series, udiff_dates=udiff_dates
        )
        if not older_obs:
            unresolved_reason = "isin_change_unexplained"
            break
        blocks.append((predecessor, older_obs, evidence))
        visited.add(predecessor)
        current_isin, current_obs = predecessor, older_obs

    # Build segments oldest to newest. `blocks[i][2]` is the evidence linking block i to block i-1 (newer).
    pieces: list[tuple[str, list[_Obs], str, str | None]] = []  # isin, run, link, evidence
    for index in range(len(blocks) - 1, -1, -1):
        isin, observations, evidence = blocks[index]
        runs = _runs(observations)
        for run_index, run in enumerate(runs):
            if run_index < len(runs) - 1:
                pieces.append((isin, run, "isin_continuity", None))
            elif index == 0:
                pieces.append((isin, run, "anchor", None))
            else:
                pieces.append((isin, run, "isin_change_split", evidence))
    segments: list[IsinSegment] = []
    for index, (isin, run, link, evidence) in enumerate(pieces):
        valid_from = run[0].day
        if index + 1 < len(pieces):
            next_from = pieces[index + 1][1][0].day
            valid_to = sessions[position[next_from] - 1]
        else:
            valid_to = as_of
        token = next((obs.token for obs in reversed(run) if obs.token is not None), None)
        segments.append(
            IsinSegment(
                isin=isin, nse_symbol=run[-1].symbol, valid_from=valid_from, valid_to=valid_to, link=link,
                evidence=evidence, token=token,
            )
        )
    resolved_from = segments[0].valid_from
    return Lineage(
        workspace=workspace,
        anchor_isin=anchor_isin,
        anchor_series=anchor_series,
        stock_code=stock_code,
        segments=tuple(segments),
        resolved_from=resolved_from,
        unresolved_before=resolved_from if unresolved_reason else None,
        unresolved_reason=unresolved_reason,
        built_as_of=as_of,
        basis="bhavcopy_walk",
    )


def lineage_for_target(store: PilotDataStore, member, *, as_of: date, workspace: Literal["india"]) -> Lineage:
    return build_lineage(
        store, anchor_isin=member.anchor_isin, anchor_series=member.anchor_series, as_of=as_of,
        workspace=workspace, stock_code=member.stock_code,
    )


def lineage_for_stock_code(
    store: PilotDataStore, *, stock_code: str, as_of: date, workspace: Literal["india"]
) -> Lineage:
    snapshot = latest_snapshot(store, on_or_before=as_of)
    if snapshot is None:
        raise PilotDataError("security_master_missing", "no security master snapshot on or before as_of")
    row = lookup_stock_code(store, snapshot, stock_code)
    if row is None:
        raise PilotDataError("stock_code_unmapped", "stock_code has no live row in the master")
    return build_lineage(
        store, anchor_isin=row.isin, anchor_series=row.series, as_of=as_of, workspace=workspace,
        stock_code=stock_code,
    )


def primary_bars_for_lineage(
    store: PilotDataStore, lineage: Lineage, *, start: date, end: date
) -> tuple[RawDailyBar, ...]:
    """Primary bars for the ISIN the lineage assigns to each date, UDiFF preferred, in date order."""
    ensure_bhavcopy_tables(store)
    udiff_dates = _udiff_dates(store)
    out: dict[date, tuple[int, RawDailyBar]] = {}
    for segment in lineage.segments:
        lo, hi = max(start, segment.valid_from), min(end, segment.valid_to)
        if lo > hi:
            continue
        rows = store.query(
            "SELECT trade_date, file_kind, series, nse_symbol, open, high, low, close, volume, traded_value, "
            "source_sha256 FROM bhavcopy_bars WHERE isin = ? AND file_kind IN ('udiff', 'cm_legacy') "
            "AND trade_date >= ? AND trade_date <= ? ORDER BY trade_date, series",
            [segment.isin, lo, hi],
        )
        for day, kind, series, symbol, o, h, l, c, volume, value, source in rows:
            if (kind == "udiff") != (day in udiff_dates):
                continue
            if series in NON_REGULAR_SERIES:
                continue
            rank = 0 if series == lineage.anchor_series else 1
            if day in out and out[day][0] <= rank:
                continue
            out[day] = (
                rank,
                RawDailyBar(
                    trade_date=day, isin=segment.isin, series=series, nse_symbol=symbol, open=o, high=h, low=l,
                    close=c, volume=int(volume), traded_value=value, source_kind="bhavcopy", source_sha256=source,
                ),
            )
    return tuple(out[day][1] for day in sorted(out))
