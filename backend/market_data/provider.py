"""Capability-limited provider port for market data only."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .models import MarketDataEvent, MarketDataSubscription


@runtime_checkable
class ReadOnlyMarketDataProvider(Protocol):
    """No account, position, execution, cancel, or reconciliation authority."""

    @property
    def name(self) -> str: ...

    async def start(self, subscription: MarketDataSubscription) -> None: ...

    async def stop(self) -> None: ...

    async def next_event(self) -> MarketDataEvent | None: ...
