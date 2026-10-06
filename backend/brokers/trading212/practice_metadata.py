"""Instrument and exchange-schedule cache for practice admission (66-03, D-20).

Two governed GETs fill it (``metadata/instruments`` and ``metadata/exchanges``).
Both endpoints refresh every ten minutes at Trading 212, so this cache holds
them for ten minutes and refuses to answer from data older than thirty. Reads
are async; the lookups admission uses are plain synchronous methods over the
cached data, so admission itself never touches the network.

Everything fails closed: an unknown ticker is ``None``, an unknown or stale
schedule is "closed". Time is read only through the injected clock.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Optional

from .practice_transport import PracticeTransport, PracticeTransportError

CACHE_TTL_SECONDS = 600.0
CACHE_MAX_AGE_SECONDS = 1800.0
# The newest schedule event must be this recent for the schedule to say anything.
SCHEDULE_EVENT_MAX_AGE_SECONDS = 36 * 3600.0

_OPEN_EVENTS = frozenset({"OPEN", "BREAK_END"})


class MetadataUnavailable(Exception):
    """A metadata read failed or its answer was not usable. Carries a stable code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class InstrumentInfo:
    ticker: str
    currency_code: str
    max_open_quantity: Optional[Decimal]
    working_schedule_id: Optional[int]


def _epoch(value: Any) -> Optional[float]:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _decimal(value: Any) -> Optional[Decimal]:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


class PracticeMetadata:
    def __init__(
        self,
        transport: PracticeTransport,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._transport = transport
        self._clock = clock
        self._instruments: dict[str, InstrumentInfo] = {}
        self._schedules: dict[int, list[tuple[float, str]]] = {}
        self._instruments_at: Optional[float] = None
        self._exchanges_at: Optional[float] = None

    def now(self) -> float:
        return self._clock()

    async def refresh_if_stale(self) -> None:
        """Refresh whichever half is older than the TTL. Raises ``MetadataUnavailable``."""

        now = self._clock()
        if self._instruments_at is None or now - self._instruments_at >= CACHE_TTL_SECONDS:
            await self._refresh_instruments()
        if self._exchanges_at is None or now - self._exchanges_at >= CACHE_TTL_SECONDS:
            await self._refresh_exchanges()

    async def _get_list(self, path: str) -> list[Any]:
        try:
            response = await self._transport.get(path)
        except PracticeTransportError:
            raise MetadataUnavailable("METADATA_UNAVAILABLE") from None
        if response.status_code != 200:
            raise MetadataUnavailable("METADATA_UNAVAILABLE")
        try:
            body = response.json()
        except ValueError:
            raise MetadataUnavailable("METADATA_INVALID") from None
        if not isinstance(body, list):
            raise MetadataUnavailable("METADATA_INVALID")
        return body

    async def _refresh_instruments(self) -> None:
        body = await self._get_list("/equity/metadata/instruments")
        parsed: dict[str, InstrumentInfo] = {}
        for item in body:
            if not isinstance(item, Mapping):
                continue
            ticker = item.get("ticker")
            currency = item.get("currencyCode")
            if not isinstance(ticker, str) or not ticker or not isinstance(currency, str):
                continue
            schedule = item.get("workingScheduleId")
            parsed[ticker] = InstrumentInfo(
                ticker=ticker,
                currency_code=currency,
                max_open_quantity=_decimal(item.get("maxOpenQuantity")),
                working_schedule_id=(
                    schedule if isinstance(schedule, int) and not isinstance(schedule, bool) else None
                ),
            )
        self._instruments = parsed
        self._instruments_at = self._clock()

    async def _refresh_exchanges(self) -> None:
        body = await self._get_list("/equity/metadata/exchanges")
        schedules: dict[int, list[tuple[float, str]]] = {}
        for exchange in body:
            if not isinstance(exchange, Mapping):
                continue
            for schedule in exchange.get("workingSchedules") or []:
                if not isinstance(schedule, Mapping):
                    continue
                schedule_id = schedule.get("id")
                if not isinstance(schedule_id, int) or isinstance(schedule_id, bool):
                    continue
                events: list[tuple[float, str]] = []
                for event in schedule.get("timeEvents") or []:
                    if not isinstance(event, Mapping):
                        continue
                    when = _epoch(event.get("date"))
                    kind = event.get("type")
                    if when is not None and isinstance(kind, str):
                        events.append((when, kind))
                schedules[schedule_id] = sorted(events)
        self._schedules = schedules
        self._exchanges_at = self._clock()

    # --- lookups used by admission -------------------------------------------

    def fresh(self) -> bool:
        """True when both halves were loaded within the maximum age."""

        if self._instruments_at is None or self._exchanges_at is None:
            return False
        now = self._clock()
        return (
            now - self._instruments_at <= CACHE_MAX_AGE_SECONDS
            and now - self._exchanges_at <= CACHE_MAX_AGE_SECONDS
        )

    def instrument(self, ticker: str) -> Optional[InstrumentInfo]:
        return self._instruments.get(ticker)

    def exchange_open(self, schedule_id: Optional[int]) -> bool:
        """True only when the cached schedule says the exchange is open right now."""

        if schedule_id is None:
            return False
        events = self._schedules.get(schedule_id)
        if not events:
            return False
        now = self._clock()
        latest: Optional[tuple[float, str]] = None
        for when, kind in events:
            if when <= now:
                latest = (when, kind)
            else:
                break
        if latest is None or now - latest[0] > SCHEDULE_EVENT_MAX_AGE_SECONDS:
            return False
        return latest[1] in _OPEN_EVENTS
