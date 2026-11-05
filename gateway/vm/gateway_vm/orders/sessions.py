"""Which sessions a replay must cover (63-05 fix round 1, special sessions 2026-11).

The Option B replay used to take its calendar from the bars it got back, so a
history with no bars, or a history that stopped early, looked complete. The
required sessions now come from here, independent of any Breeze answer: every
weekday from the first unevaluated day to yesterday, less the dated exchange
holidays in data/nse_trading_holidays.json, plus the dated exceptional sessions
(a Sunday Muhurat session) listed there under special_sessions. A required
session with no close for a held ISIN stops the replay (MarkMissing), so the VM
never judges drawdown, halt or stops on a partial history.

The table loader and the coverage rule live in limits.py (re-exported here): the shadow
replay loads limits.py without this module, and its session check needs the calendar.
The table is loaded once at start from a repo file and validated strictly. A
malformed table is config_invalid. An empty holiday list is legal but means
every holiday refuses orders until the operator fills it from the NSE circular.
A special session is required the moment it is listed: if Breeze never serves its
close, orders stay refused until the operator removes the entry (fail closed).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

from .limits import (  # noqa: F401  (re-exported: the calendar itself lives in limits.py)
    HOLIDAY_SCHEMA,
    HOLIDAY_TABLE_PATH,
    calendar_covers,
    covered_through,
    load_calendar,
)


def load_holidays(path: str | Path = HOLIDAY_TABLE_PATH) -> frozenset[date]:
    """The holiday dates, or a LimitsError if the table cannot be trusted."""
    return load_calendar(path)[0]


def load_special_sessions(path: str | Path = HOLIDAY_TABLE_PATH) -> frozenset[date]:
    """The exceptional session dates (weekend or otherwise), or a LimitsError."""
    return load_calendar(path)[1]


def expected_sessions(
    first: date, last: date, holidays: Iterable[date], special_sessions: Iterable[date]
) -> list[date]:
    """Every weekday in [first, last] that is not a listed holiday, plus every listed
    special session, oldest first."""
    if isinstance(first, datetime) or isinstance(last, datetime):
        raise TypeError("dates, not datetimes")
    skip = frozenset(holidays)
    extra = frozenset(special_sessions)
    out: list[date] = []
    day = first
    while day <= last:
        if day in extra or (day.weekday() < 5 and day not in skip):
            out.append(day)
        day += timedelta(days=1)
    return out


COVERAGE_WARN_DAYS = 30


def is_session_day(day: date, holidays: Iterable[date], special_sessions: Iterable[date]) -> bool:
    """A normal weekday that is not a listed holiday, or a listed special session."""
    if day in frozenset(special_sessions):
        return True
    return day.weekday() < 5 and day not in frozenset(holidays)


def previous_trading_day(
    day: date, holidays: Iterable[date], special_sessions: Iterable[date]
) -> date:
    """The latest session day strictly before ``day`` (a listed special session counts)."""
    if isinstance(day, datetime):
        raise TypeError("a date, not a datetime")
    skip = frozenset(holidays)
    extra = frozenset(special_sessions)
    prior = day - timedelta(days=1)
    while not is_session_day(prior, skip, extra):
        prior -= timedelta(days=1)
    return prior


def coverage_warning(
    today: date, holidays: Iterable[date], special_sessions: Iterable[date]
) -> str | None:
    """A line for the operator when the table does not cover ``today`` or its covered
    run ends within COVERAGE_WARN_DAYS, else None. The warning keys off the last year
    of the unbroken run of covered years, so a later year after a gap does not hide it.
    ``special_sessions`` is accepted for symmetry and does not count as coverage."""
    listed = frozenset(holidays)
    if not listed:
        return "holiday table lists no holidays: orders are refused until the NSE list is added"
    through = covered_through(listed, today)
    if through is None:
        return f"holiday table has no rows for {today.year}: orders are refused until that year is added"
    left = (through - today).days
    if left > COVERAGE_WARN_DAYS:
        return None
    return f"holiday table covers through {through.isoformat()} ({left} days): add the next NSE holiday list"
