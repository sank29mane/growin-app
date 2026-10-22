"""Operator-pinned NSE session calendar for the one session that has not happened yet (64-08, G-3).

The store's own calendar (`sessions.day_status`) is built from fetch outcomes, so it cannot say anything about
a day nobody has fetched. A forecast for session S needs an answer to "will S trade?" before S's files exist.
That answer comes from a file the operator saves from NSE and pins by sha256. This module ships the loader and
the check. It ships no holiday data: the real table is the operator's (decision Q-5, provisional).

File format `growin-nse-calendar/1` (JSON, unknown keys refused, no defaults):

    {"schema": "growin-nse-calendar/1", "exchange": "NSE", "segment": "CM",
     "basis": "operator_confirmed" | "test_fixture",
     "coverage": {"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"},
     "source": {"publisher", "reference", "published_on", "document_sha256", "saved_by", "saved_at_utc"},
     "holidays": [{"date", "name"}, ...],            # weekdays only
     "special_sessions": [{"date", "kind"}, ...]}    # for example a Muhurat or Saturday budget session

Saturday and Sunday are not sessions unless listed in `special_sessions`. A forecast never trusts a day outside
`coverage`, never trusts an unpinned file, and checks the table against what the store already knows.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from . import sessions as _sessions
from .core import Caveat, CaveatedResult, PilotDataError, standard_caveats
from .store import PilotDataStore

CALENDAR_SCHEMA = "growin-nse-calendar/1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

UPCOMING_UNKNOWN_CAVEAT = Caveat(
    code="UPCOMING_SESSION_FORECAST",
    text=(
        "An upcoming session is expected only on an operator-pinned calendar. It is a forecast: NSE can still "
        "close for an unscheduled reason, and the session's own files settle it later."
    ),
)


class _Strict(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)


class CalendarCoverage(_Strict):
    first: date = Field(alias="from")
    last: date = Field(alias="to")

    @model_validator(mode="after")
    def _ordered(self) -> "CalendarCoverage":
        if self.first > self.last:
            raise ValueError("coverage.from is after coverage.to")
        return self


class CalendarSource(_Strict):
    publisher: str = Field(min_length=1)
    reference: str = Field(min_length=1)
    published_on: date
    document_sha256: str
    saved_by: str = Field(min_length=1)
    saved_at_utc: datetime

    @field_validator("document_sha256")
    @classmethod
    def _hex(cls, value: str) -> str:
        if not _SHA256.match(value):
            raise ValueError("document_sha256 is not a lowercase sha256 hex")
        return value

    @field_validator("saved_at_utc")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("saved_at_utc needs a timezone")
        return value


class CalendarHoliday(_Strict):
    day: date = Field(alias="date")
    name: str = Field(min_length=1)


class CalendarSpecialSession(_Strict):
    day: date = Field(alias="date")
    kind: str = Field(min_length=1)


class PinnedCalendar(_Strict):
    """A parsed calendar file. `sha256` is the hash of the file's bytes; it is not part of the file."""

    schema_id: str = Field(alias="schema")
    exchange: Literal["NSE"]
    segment: Literal["CM"]
    basis: Literal["operator_confirmed", "test_fixture"]
    coverage: CalendarCoverage
    source: CalendarSource
    holidays: tuple[CalendarHoliday, ...]
    special_sessions: tuple[CalendarSpecialSession, ...]
    sha256: str = ""

    @field_validator("schema_id")
    @classmethod
    def _schema(cls, value: str) -> str:
        if value != CALENDAR_SCHEMA:
            raise ValueError(f"schema must be {CALENDAR_SCHEMA}")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> "PinnedCalendar":
        holiday_days = [h.day for h in self.holidays]
        special_days = [s.day for s in self.special_sessions]
        if len(set(holiday_days)) != len(holiday_days):
            raise ValueError("a holiday date is listed twice")
        if len(set(special_days)) != len(special_days):
            raise ValueError("a special session date is listed twice")
        for day in holiday_days:
            if day.weekday() >= 5:
                raise ValueError(f"holiday {day.isoformat()} is a weekend day")
        if set(holiday_days) & set(special_days):
            raise ValueError("a date is both a holiday and a special session")
        for day in (*holiday_days, *special_days):
            if not self.covers(day):
                raise ValueError(f"{day.isoformat()} is outside coverage")
        return self

    def covers(self, day: date) -> bool:
        return self.coverage.first <= day <= self.coverage.last

    def is_holiday(self, day: date) -> bool:
        return any(h.day == day for h in self.holidays)

    def is_special(self, day: date) -> bool:
        return any(s.day == day for s in self.special_sessions)

    def is_weekend(self, day: date) -> bool:
        return day.weekday() >= 5


def load_pinned_calendar(path: Path | None, *, expected_sha256: str | None) -> PinnedCalendar | None:
    """Load the operator's calendar. No path, no pin or no file means unpinned (None).

    A file that exists but does not hash to the pin raises `calendar_pin_mismatch`, and one that does not parse
    raises `calendar_invalid`. A tampered file never degrades to "unpinned".
    """
    if path is None or expected_sha256 is None:
        return None
    candidate = Path(path)
    if not candidate.is_file():
        return None
    content = candidate.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    if digest != expected_sha256:
        raise PilotDataError("calendar_pin_mismatch", "the calendar file does not hash to the pinned sha256")
    try:
        parsed = json.loads(content.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("the calendar file is not a JSON object")
        if "sha256" in parsed:
            raise ValueError("the calendar file may not carry a sha256 key")
        return PinnedCalendar.model_validate({**parsed, "sha256": digest})
    except (ValueError, ValidationError, UnicodeDecodeError) as error:
        raise PilotDataError("calendar_invalid", f"the calendar file is not valid: {error}") from error


class UpcomingSessionCheck(CaveatedResult):
    status: Literal["expected", "unknown"]
    reason: str | None
    prior_session: date
    session: date
    calendar_sha256: str | None
    calendar_basis: str | None


def _verdict(status: Literal["expected", "unknown"], reason: str | None, prior_session: date, session: date,
             calendar: PinnedCalendar | None) -> UpcomingSessionCheck:
    return UpcomingSessionCheck(
        workspace="india", caveats=standard_caveats(UPCOMING_UNKNOWN_CAVEAT), status=status, reason=reason,
        prior_session=prior_session, session=session,
        calendar_sha256=None if calendar is None else calendar.sha256,
        calendar_basis=None if calendar is None else calendar.basis,
    )


def _store_contradicts(store: PilotDataStore, calendar: PinnedCalendar, prior_session: date) -> bool:
    """True when store evidence dated on or before prior_session disagrees with the table. Reads nothing later."""
    _sessions.ensure_fetch_log(store)
    last = min(prior_session, calendar.coverage.last)
    seen = store.query(
        "SELECT DISTINCT trade_date FROM nse_fetch_log WHERE trade_date >= ? AND trade_date <= ? ORDER BY trade_date",
        [calendar.coverage.first, last],
    )
    for (day,) in seen:
        if calendar.is_special(day):
            continue
        listed_holiday = calendar.is_holiday(day)
        plain_weekday = not calendar.is_weekend(day) and not listed_holiday
        if not (listed_holiday or plain_weekday):
            continue
        status = _sessions.day_status(store, day)
        if (listed_holiday and status == "session") or (plain_weekday and status == "holiday"):
            return True
    return False


def upcoming_session_check(store: PilotDataStore, *, prior_session: date, session: date,
                           calendar: PinnedCalendar | None) -> UpcomingSessionCheck:
    """Is `session` expected to trade, with `prior_session` the last session before it?

    `expected` only when the pinned calendar covers every day involved, the store agrees `prior_session` was a
    session, nothing between is a session, `session` is an ordinary session day and no store evidence dated on
    or before `prior_session` contradicts the table. Anything else is `unknown` with exactly one reason. The
    store's `day_status` is never asked about a day after `prior_session`.
    """
    if session <= prior_session:
        raise PilotDataError("upcoming_session_order", "session must be after prior_session")

    def unknown(reason: str) -> UpcomingSessionCheck:
        return _verdict("unknown", reason, prior_session, session, calendar)

    if calendar is None:
        return unknown("session_calendar_unknown")
    between = [prior_session + timedelta(days=n) for n in range(1, (session - prior_session).days)]
    if not calendar.covers(session) or any(not calendar.covers(day) for day in between):
        return unknown("session_calendar_unknown")
    if _sessions.day_status(store, prior_session) != "session":
        return unknown("prior_session_not_session")
    if any(calendar.is_special(day) or (not calendar.is_weekend(day) and not calendar.is_holiday(day))
           for day in between):
        return unknown("prior_session_not_last")
    if calendar.is_special(session):
        return unknown("session_special_unsupported")
    if calendar.is_weekend(session) or calendar.is_holiday(session):
        return unknown("session_not_expected")
    if _store_contradicts(store, calendar, prior_session):
        return unknown("session_calendar_conflict")
    return _verdict("expected", None, prior_session, session, calendar)
