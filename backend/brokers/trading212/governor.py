"""Deterministic per-endpoint rate governor for one Trading 212 account (UKT-03, D-14).

Pure module: no HTTP, no randomness, no import of any other Growin module.
The clock and the sleep function are injected so every wait is reproducible.

Policy, from the published limits (66-RESEARCH section 3):

* One key per endpoint template, never per concrete id: ``GET /equity/orders/1``
  and ``GET /equity/orders/2`` share ``GET /equity/orders/{id}``.
* Minimum-interval spacing of ``period / limit`` between sends on one key. This
  is spacing, not a burst bucket, so the wait is the same on every run.
* After a response, ``x-ratelimit-remaining: 0`` holds the key until
  ``x-ratelimit-reset`` (a Unix timestamp read through the injected clock).
* An endpoint that is not in the table raises. There is no default limit.

The governor never retries anything. A caller that gets a 429 on a GET may call
``hold_after_throttle`` and send once more; an order POST or DELETE is never
resent (D-10a, D-15).
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Mapping, Optional

__all__ = [
    "EndpointLimit",
    "Governor",
    "LIMIT_TABLE",
    "UnknownEndpointError",
    "endpoint_template",
]


class UnknownEndpointError(ValueError):
    """The method and path match no published Trading 212 endpoint."""


@dataclass(frozen=True)
class EndpointLimit:
    """``limit`` requests per ``period`` seconds."""

    limit: int
    period: float

    @property
    def interval(self) -> float:
        return self.period / self.limit


# Pinned from the official v0 spec as recorded in 66-RESEARCH section 3.
# The spec does publish the pies limits (the 66-02 review read GET /equity/pies/{id}
# as 1 per 5 s). Both pies reads stay pinned at 1 per 30 s: for {id} that is
# stricter than the spec, which only slows deprecated reads (66-CONTEXT "Deferred").
LIMIT_TABLE: Mapping[str, EndpointLimit] = {
    "POST /equity/orders/limit": EndpointLimit(1, 2.0),
    "POST /equity/orders/stop": EndpointLimit(1, 2.0),
    "POST /equity/orders/stop_limit": EndpointLimit(1, 2.0),
    "POST /equity/orders/market": EndpointLimit(50, 60.0),
    "DELETE /equity/orders/{id}": EndpointLimit(50, 60.0),
    "GET /equity/orders/{id}": EndpointLimit(1, 1.0),
    "GET /equity/positions": EndpointLimit(1, 1.0),
    "GET /equity/orders": EndpointLimit(1, 5.0),
    "GET /equity/account/summary": EndpointLimit(1, 5.0),
    "GET /equity/history/orders": EndpointLimit(20, 60.0),
    "GET /equity/history/dividends": EndpointLimit(20, 60.0),
    "GET /equity/history/transactions": EndpointLimit(20, 60.0),
    "GET /equity/metadata/instruments": EndpointLimit(1, 50.0),
    "GET /equity/metadata/exchanges": EndpointLimit(1, 30.0),
    "POST /equity/history/exports": EndpointLimit(1, 30.0),
    "GET /equity/history/exports": EndpointLimit(1, 60.0),
    "GET /equity/pies": EndpointLimit(1, 30.0),
    "GET /equity/pies/{id}": EndpointLimit(1, 30.0),
}

_ID = r"[^/?#]+"
_PATTERNS: tuple[tuple[str, "re.Pattern[str]", str], ...] = tuple(
    (method, re.compile(pattern), template)
    for method, pattern, template in (
        ("POST", r"/equity/orders/limit", "POST /equity/orders/limit"),
        ("POST", r"/equity/orders/stop", "POST /equity/orders/stop"),
        ("POST", r"/equity/orders/stop_limit", "POST /equity/orders/stop_limit"),
        ("POST", r"/equity/orders/market", "POST /equity/orders/market"),
        ("DELETE", rf"/equity/orders/{_ID}", "DELETE /equity/orders/{id}"),
        ("GET", rf"/equity/orders/{_ID}", "GET /equity/orders/{id}"),
        ("GET", r"/equity/positions", "GET /equity/positions"),
        ("GET", r"/equity/orders", "GET /equity/orders"),
        ("GET", r"/equity/account/summary", "GET /equity/account/summary"),
        ("GET", r"/equity/history/orders", "GET /equity/history/orders"),
        ("GET", r"/equity/history/dividends", "GET /equity/history/dividends"),
        ("GET", r"/equity/history/transactions", "GET /equity/history/transactions"),
        ("GET", r"/equity/metadata/instruments", "GET /equity/metadata/instruments"),
        ("GET", r"/equity/metadata/exchanges", "GET /equity/metadata/exchanges"),
        ("POST", r"/equity/history/exports", "POST /equity/history/exports"),
        ("GET", r"/equity/history/exports", "GET /equity/history/exports"),
        ("GET", r"/equity/pies", "GET /equity/pies"),
        ("GET", rf"/equity/pies/{_ID}", "GET /equity/pies/{id}"),
    )
)

assert {template for _, _, template in _PATTERNS} == set(LIMIT_TABLE)


def endpoint_template(method: str, path: str) -> str:
    """Return the limit-table key for a request, or raise ``UnknownEndpointError``.

    ``path`` is the part after the API base (``equity/orders/42?limit=5`` and
    ``/api/v0/equity/orders/42`` both resolve); the query string is ignored.
    """

    clean = path.split("?", 1)[0].split("#", 1)[0]
    if "/api/v0/" in clean:
        clean = clean.split("/api/v0", 1)[1]
    clean = "/" + clean.strip("/")
    verb = method.upper()
    for pattern_method, pattern, template in _PATTERNS:
        if pattern_method == verb and pattern.fullmatch(clean):
            return template
    raise UnknownEndpointError(f"no Trading 212 limit for {verb} {clean}")


def _number(headers: Mapping[str, str], name: str) -> Optional[float]:
    raw = headers.get(name)
    if raw is None:
        return None
    try:
        return float(str(raw).strip())
    except ValueError:
        return None


class Governor:
    """One instance per Trading 212 account (one budget, whatever the API key)."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        limits: Optional[Mapping[str, EndpointLimit]] = None,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._limits = dict(LIMIT_TABLE if limits is None else limits)
        self._next_send: dict[str, float] = {}
        self._hold_until: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def template(self, method: str, path: str) -> str:
        template = endpoint_template(method, path)
        if template not in self._limits:
            raise UnknownEndpointError(f"no limit configured for {template}")
        return template

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    async def acquire(self, method: str, path: str) -> str:
        """Wait until a send on this endpoint is allowed, then reserve the slot.

        Returns the endpoint template. Concurrent callers on one key are served
        one at a time, each spaced ``period / limit`` after the previous one.
        """

        key = self.template(method, path)
        async with self._lock(key):
            while True:
                now = self._clock()
                ready = max(self._next_send.get(key, now), self._hold_until.get(key, now))
                if now >= ready:
                    break
                await self._sleep(ready - now)
            self._next_send[key] = self._clock() + self._limits[key].interval
        return key

    def observe(self, key: str, headers: Mapping[str, str]) -> None:
        """Record a response's ``x-ratelimit-*`` headers for ``key``.

        ``remaining == 0`` holds the key until ``reset``. A zero remaining count
        with no usable reset holds for one full period from now.
        """

        if key not in self._limits:
            raise UnknownEndpointError(f"no limit configured for {key}")
        lowered = {str(name).lower(): value for name, value in headers.items()}
        remaining = _number(lowered, "x-ratelimit-remaining")
        if remaining is None or remaining > 0:
            return
        reset = _number(lowered, "x-ratelimit-reset")
        if reset is None:
            reset = self._clock() + self._limits[key].period
        self._hold_until[key] = max(self._hold_until.get(key, 0.0), reset)

    def hold_after_throttle(self, key: str, headers: Mapping[str, str]) -> float:
        """Hold ``key`` after a 429 and return the seconds until it is free.

        Waits for ``x-ratelimit-reset`` when the response carries one in the
        future; otherwise for one full period of this endpoint.
        """

        if key not in self._limits:
            raise UnknownEndpointError(f"no limit configured for {key}")
        lowered = {str(name).lower(): value for name, value in headers.items()}
        now = self._clock()
        reset = _number(lowered, "x-ratelimit-reset")
        if reset is None or reset <= now:
            reset = now + self._limits[key].period
        self._hold_until[key] = max(self._hold_until.get(key, 0.0), reset)
        return reset - now
