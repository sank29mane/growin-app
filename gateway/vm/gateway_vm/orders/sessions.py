"""Which sessions a replay must cover (63-05 fix round 1, special sessions 2026-11).

The Option B replay used to take its calendar from the bars it got back, so a
history with no bars, or a history that stopped early, looked complete. The
required sessions now come from here, independent of any Breeze answer: every
weekday from the first unevaluated day to yesterday, less the dated exchange
holidays in data/nse_trading_holidays.json, plus the dated exceptional sessions
(a Sunday Muhurat session) listed there under special_sessions. A required
session with no close for a held ISIN stops the replay (MarkMissing), so the VM
never judges drawdown, halt or stops on a partial history.

The table is loaded once at start from a repo file and validated strictly. A
malformed table is config_invalid. An empty holiday list is legal but means
every holiday refuses orders until the operator fills it from the NSE circular.
A special session is required the moment it is listed: if Breeze never serves its
close, orders stay refused until the operator removes the entry (fail closed).
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

from .limits import LimitsError

HOLIDAY_TABLE_PATH = Path(__file__).parent / "data" / "nse_trading_holidays.json"
HOLIDAY_SCHEMA = "growin.gateway.nse_trading_holidays/2"
_MAX_FILE = 65536


def _strict(text: str) -> object:
    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        out: dict[str, object] = {}
        for key, value in items:
            if key in out:
                raise ValueError("duplicate key")
            out[key] = value
        return out

    def bad(_: str) -> object:
        raise ValueError("number")

    return json.loads(text, object_pairs_hook=pairs, parse_float=bad, parse_constant=bad)


def _dates(listed: object, *, weekdays_only: bool) -> frozenset[date]:
    if not isinstance(listed, list):
        raise ValueError("not a list")
    days: list[date] = []
    for item in listed:
        if not isinstance(item, str) or len(item) != 10:
            raise ValueError("date")
        day = date.fromisoformat(item)
        if weekdays_only and day.weekday() >= 5:
            raise ValueError("weekend")
        days.append(day)
    if len(set(days)) != len(days):
        raise ValueError("duplicate")
    return frozenset(days)


def load_calendar(path: str | Path = HOLIDAY_TABLE_PATH) -> tuple[frozenset[date], frozenset[date]]:
    """(holidays, special_sessions), or a LimitsError if the table cannot be trusted."""
    try:
        file = Path(path)
        if file.stat().st_size > _MAX_FILE:
            raise ValueError("too large")
        raw = _strict(file.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema") != HOLIDAY_SCHEMA:
            raise ValueError("schema")
        holidays = _dates(raw["holidays"], weekdays_only=True)
        special = _dates(raw["special_sessions"], weekdays_only=False)
        if holidays & special:
            raise ValueError("a date cannot be both a holiday and a special session")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise LimitsError("holiday table is unreadable") from exc
    return holidays, special


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


def covered_through(holidays: Iterable[date], special_sessions: Iterable[date]) -> date | None:
    """Last day the table speaks for: 31 December of the latest year it lists.

    None when the table lists nothing at all (a legal but empty table says nothing
    about coverage, so no coverage rule applies to it).
    """
    years = [d.year for d in (*holidays, *special_sessions)]
    return date(max(years), 12, 31) if years else None


def calendar_covers(day: date, holidays: Iterable[date], special_sessions: Iterable[date]) -> bool:
    """False once ``day`` is past the last year the table lists: an unlisted holiday
    there would read as a normal session, so the day must be refused, not guessed."""
    through = covered_through(holidays, special_sessions)
    return through is None or day <= through


def coverage_warning(
    today: date, holidays: Iterable[date], special_sessions: Iterable[date]
) -> str | None:
    """A line for the operator once the table is within COVERAGE_WARN_DAYS of its end
    (or past it), else None. No order is refused for this until the end has passed."""
    through = covered_through(holidays, special_sessions)
    if through is None:
        return None
    left = (through - today).days
    if left > COVERAGE_WARN_DAYS:
        return None
    if left < 0:
        return f"holiday table ended {through.isoformat()}: orders are refused until the next year is added"
    return f"holiday table ends {through.isoformat()} ({left} days): add the next NSE holiday list"
