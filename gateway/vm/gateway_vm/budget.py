"""Call ceilings for the relay (OD-4, C7): the BudgetGate implementation.

Breeze calls: a rolling minute window in memory and an IST-day count that is
persisted before the call is made. The relay is the one place that sees every
caller's calls, so its day count is authoritative and survives a restart. When
the persisted count cannot be read or written, no Breeze call is allowed
(BudgetUnavailable) rather than guessing.

Security master downloads: a rolling hour window in memory, not counted as
Breeze calls and never written to the counter file.

One lock covers the windows, the cached count and the save, so concurrent
charges cannot lose an increment. No network call happens under the lock.
"""

from __future__ import annotations

import math
import threading
from collections import deque
from datetime import date, datetime, time as dtime, timedelta

from .counter_store import CounterStore, CounterUnreadable
from .session import IST, BudgetExceeded, BudgetUnavailable
from .transport import Clock

_MINUTE_S = 60.0
_HOUR_S = 3600.0


def _ist_today(clock: Clock) -> date:
    return clock.now_utc().astimezone(IST).date()


def _seconds_until_ist_midnight(clock: Clock) -> int:
    now_ist = clock.now_utc().astimezone(IST)
    midnight = datetime.combine(now_ist.date() + timedelta(days=1), dtime(0, 0), tzinfo=IST)
    return max(1, math.ceil((midnight - now_ist).total_seconds()))


def _retry_after(oldest: float, window_s: float, now: float) -> int:
    return max(1, min(int(window_s), math.ceil(oldest + window_s - now)))


class Budget:
    """Implements BudgetGate."""

    def __init__(
        self,
        clock: Clock,
        *,
        per_minute: int,
        per_day: int,
        security_master_per_hour: int,
        store: CounterStore,
    ) -> None:
        self._clock = clock
        self._per_minute = per_minute
        self._per_day = per_day
        self._master_per_hour = security_master_per_hour
        self._store = store
        self._lock = threading.Lock()
        self._minute: deque[float] = deque()
        self._hour: deque[float] = deque()
        self._loaded = False
        self._day: date | None = None
        self._count = 0
        self._save_failed = False  # the last save failed: the file is not trustworthy

    # ----------------------------------------------------------- internals

    def _sync_day_locked(self) -> date:
        """Load the persisted count once, then roll to today. Raises BudgetUnavailable.

        A stored date later than today in IST (a tampered file or a clock that
        went backwards) is not trusted: it refuses instead of resetting.
        """
        today = _ist_today(self._clock)
        if not self._loaded:
            try:
                stored = self._store.load()
            except CounterUnreadable:
                raise BudgetUnavailable("counter_unreadable") from None
            if stored is not None:
                self._day, self._count = stored
            else:
                self._day, self._count = today, 0
            self._loaded = True
        if self._day is None or self._day > today:  # fail closed, not an assert that -O strips
            raise BudgetUnavailable("counter_unreadable")
        if self._day < today:
            self._day, self._count = today, 0
        return today

    @staticmethod
    def _prune(window: deque[float], now: float, length_s: float) -> None:
        while window and window[0] <= now - length_s:
            window.popleft()

    # -------------------------------------------------------------- public

    def charge(self, kind: str) -> None:
        if kind == "breeze":
            self._charge_breeze()
        elif kind == "security_master":
            self._charge_security_master()
        else:
            raise ValueError("unknown budget kind")

    def _charge_breeze(self) -> None:
        with self._lock:
            today = self._sync_day_locked()
            now = self._clock.monotonic()
            self._prune(self._minute, now, _MINUTE_S)
            if len(self._minute) >= self._per_minute:
                raise BudgetExceeded(
                    "budget_minute", _retry_after(self._minute[0], _MINUTE_S, now)
                )
            if self._count >= self._per_day:
                raise BudgetExceeded("budget_day", _seconds_until_ist_midnight(self._clock))
            new_count = self._count + 1
            try:
                self._store.save(today, new_count)  # durable before the call is made
            except CounterUnreadable:
                self._save_failed = True
                raise BudgetUnavailable("counter_unreadable") from None
            self._save_failed = False
            self._count = new_count
            self._minute.append(now)

    def _charge_security_master(self) -> None:
        with self._lock:
            now = self._clock.monotonic()
            self._prune(self._hour, now, _HOUR_S)
            if len(self._hour) >= self._master_per_hour:
                raise BudgetExceeded(
                    "security_master_hourly", _retry_after(self._hour[0], _HOUR_S, now)
                )
            self._hour.append(now)

    def snapshot(self) -> dict:
        """The C5 budget numbers. Never raises.

        When the counter cannot be read the relay refuses every Breeze call, so
        the numbers say exactly that: the day is fully used and nothing remains.
        """
        with self._lock:
            now = self._clock.monotonic()
            self._prune(self._minute, now, _MINUTE_S)
            try:
                today = self._sync_day_locked()
                used = self._per_day if self._save_failed else self._count
            except BudgetUnavailable:
                today = _ist_today(self._clock)
                used = self._per_day
            return {
                "calls_last_minute": len(self._minute),
                "calls_today_ist": used,
                "per_minute_limit": self._per_minute,
                "per_day_limit": self._per_day,
                "remaining_today": max(0, self._per_day - used),
                "ist_date": today.isoformat(),
            }
