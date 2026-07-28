"""Deterministic, network-free market-event replay."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from .models import MarketDataEvent, MarketDataSubscription


class ReplayMarketDataProvider:
    """Replays an immutable event sequence only after explicit start."""

    def __init__(
        self,
        events: Iterable[MarketDataEvent],
        *,
        name: str = "local-replay",
    ) -> None:
        self._name = name
        self._source_events = tuple(events)
        self._events: deque[MarketDataEvent] = deque()
        self._instruments: frozenset[str] = frozenset()
        self._running = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def running(self) -> bool:
        return self._running

    async def start(self, subscription: MarketDataSubscription) -> None:
        if self._running:
            raise RuntimeError("replay provider is already running")
        self._instruments = frozenset(item.key for item in subscription.instruments)
        self._events = deque(self._source_events)
        self._running = True

    async def stop(self) -> None:
        self._running = False
        self._events.clear()
        self._instruments = frozenset()

    async def next_event(self) -> MarketDataEvent | None:
        if not self._running:
            raise RuntimeError("replay provider is stopped")
        if not self._events:
            return None
        return self._events.popleft()
