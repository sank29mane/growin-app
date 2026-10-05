"""NSE session calendar derived from fetch outcomes. It fails closed on unknown dates.

A date is a holiday only when every NSE file for it returned 404 on attempts made after
that date in IST. A same-day 404 proves nothing (NSE publishes in the evening), so it is
pending. Failed, never-attempted and inconsistent dates make session queries raise.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Literal

from .core import IST, PilotDataError
from .store import PilotDataStore

DayStatus = Literal["session", "holiday", "weekend_no_session", "pending", "unknown", "inconsistent"]
FINAL_STATUSES = frozenset({"session", "holiday", "weekend_no_session"})
PRIMARY_KINDS = ("udiff", "cm_legacy")

FETCH_LOG_DDL = (
    "CREATE TABLE IF NOT EXISTS nse_fetch_log("
    "attempt_id VARCHAR PRIMARY KEY, trade_date DATE NOT NULL, file_kind VARCHAR NOT NULL, "
    "outcome VARCHAR NOT NULL, http_status INTEGER, error_code VARCHAR, source_sha256 VARCHAR, "
    "url VARCHAR NOT NULL, attempted_at_utc TIMESTAMP NOT NULL, row_sha256 VARCHAR NOT NULL)"
)


def ensure_fetch_log(store: PilotDataStore) -> None:
    store.ensure_table("nse_fetch_log", FETCH_LOG_DDL, key_columns=("attempt_id",))


def _ist_date(naive_utc: datetime) -> date:
    return naive_utc.replace(tzinfo=timezone.utc).astimezone(IST).date()


def _latest_attempts(store: PilotDataStore, start: date, end: date) -> dict[date, dict[str, tuple[str, date]]]:
    """Folded (outcome, IST attempt date) per (trade date, file kind) within [start, end].

    The latest attempt wins, except that an `ingested` attempt is never overridden: once a file
    for that kind is stored, a later 404 or transport failure cannot un-ingest it.
    """
    ensure_fetch_log(store)
    rows = store.query(
        "SELECT trade_date, file_kind, outcome, attempted_at_utc FROM nse_fetch_log "
        "WHERE trade_date >= ? AND trade_date <= ? ORDER BY attempted_at_utc, rowid",
        [start, end],
    )
    latest: dict[date, dict[str, tuple[str, date]]] = {}
    for trade_date, kind, outcome, attempted in rows:
        kinds = latest.setdefault(trade_date, {})
        if kinds.get(kind, ("", trade_date))[0] == "ingested":
            continue
        kinds[kind] = (outcome, _ist_date(attempted))
    return latest


def _status_from(day: date, attempts: dict[str, tuple[str, date]] | None) -> DayStatus:
    if not attempts:
        return "unknown"
    for kind in PRIMARY_KINDS:
        if attempts.get(kind, ("", day))[0] == "ingested":
            return "session"
    pr = attempts.get("pr")
    if day.weekday() >= 5:
        if pr is None:
            return "unknown"
        if pr[0] == "no_file":
            return "weekend_no_session" if pr[1] > day else "pending"
        if pr[0] == "ingested":
            return _after_pr_ingested(attempts)
        # A failed weekend PR settles nothing by itself. Primaries are only asked
        # for after a misdated PR zip; both confirmed absent after the day means
        # no weekend session.
        if pr[0] == "failed" and all(
            kind in attempts and attempts[kind][0] == "no_file" and attempts[kind][1] > day for kind in PRIMARY_KINDS
        ):
            return "weekend_no_session"
        return "unknown"
    if pr is not None and pr[0] == "ingested":
        return _after_pr_ingested(attempts)
    kinds = ("pr", *PRIMARY_KINDS)
    if all(kind in attempts and attempts[kind][0] == "no_file" for kind in kinds):
        return "holiday" if all(attempts[kind][1] > day for kind in kinds) else "pending"
    return "unknown"


def _after_pr_ingested(attempts: dict[str, tuple[str, date]]) -> DayStatus:
    if all(kind in attempts and attempts[kind][0] == "no_file" for kind in PRIMARY_KINDS):
        return "inconsistent"
    return "unknown"


def day_status(store: PilotDataStore, d: date) -> DayStatus:
    return _status_from(d, _latest_attempts(store, d, d).get(d))


def _statuses(store: PilotDataStore, start: date, end: date) -> list[tuple[date, DayStatus]]:
    latest = _latest_attempts(store, start, end)
    out: list[tuple[date, DayStatus]] = []
    day = start
    while day <= end:
        out.append((day, _status_from(day, latest.get(day))))
        day += timedelta(days=1)
    return out


def _unknown_error(bad: list[date]) -> PilotDataError:
    listed = ", ".join(day.isoformat() for day in bad[:10])
    more = "" if len(bad) <= 10 else f" (and {len(bad) - 10} more)"
    return PilotDataError("calendar_unknown_dates", f"unknown, pending or inconsistent dates: {listed}{more}")


def sessions_between(store: PilotDataStore, start: date, end: date) -> tuple[date, ...]:
    if start > end:
        return ()
    sessions: list[date] = []
    bad: list[date] = []
    for day, status in _statuses(store, start, end):
        if status == "session":
            sessions.append(day)
        elif status not in FINAL_STATUSES:
            bad.append(day)
    if bad:
        raise _unknown_error(bad)
    return tuple(sessions)


def previous_sessions(store: PilotDataStore, d: date, n: int) -> tuple[date, ...]:
    """The n most recent sessions on or before d (ascending). Fails closed on unknown days it walks."""
    if n < 1:
        return ()
    found: list[date] = []
    span = n * 2 + 30
    upper = d
    while len(found) < n:
        lower = upper - timedelta(days=span)
        chunk = _statuses(store, lower, upper)
        for day, status in reversed(chunk):
            if status == "session":
                found.append(day)
                if len(found) == n:
                    break
            elif status not in FINAL_STATUSES:
                raise _unknown_error([day])
        upper = lower - timedelta(days=1)
        if upper < date(2000, 1, 1):
            raise PilotDataError("calendar_unknown_dates", "ran out of calendar history before finding enough sessions")
    return tuple(sorted(found))


def pr_missing_sessions(store: PilotDataStore, start: date, end: date) -> tuple[date, ...]:
    """Sessions with no ingested PR file. Reported, not fatal: Bc announcements repeat across days."""
    sessions = sessions_between(store, start, end)
    latest = _latest_attempts(store, start, end)
    return tuple(
        day for day in sessions if latest.get(day, {}).get("pr", ("", day))[0] != "ingested"
    )
