"""Explicit market-data session with fail-closed normalization state."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Callable

from .models import (
    IndiaInstrument,
    MarketDataEvent,
    MarketDataSubscription,
    MarketSnapshot,
    TopOfBook,
    TradeTick,
)
from .provider import ReadOnlyMarketDataProvider


class MarketDataSessionState(str, Enum):
    STOPPED = "STOPPED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    FAILED = "FAILED"


class MarketDataError(RuntimeError):
    """Stable fail-closed error suitable for service/API translation."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class MarketDataSession:
    """Owns one explicitly activated, workspace-scoped market-data stream."""

    def __init__(
        self,
        provider: ReadOnlyMarketDataProvider,
        *,
        max_window_size: int = 256,
        max_age_seconds: float = 5.0,
        max_future_skew_seconds: float = 1.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_window_size < 1:
            raise ValueError("max_window_size must be positive")
        if max_age_seconds <= 0:
            raise ValueError("max_age_seconds must be positive")
        if max_future_skew_seconds < 0:
            raise ValueError("max_future_skew_seconds cannot be negative")
        self._provider = provider
        self._max_window_size = max_window_size
        self._max_age = timedelta(seconds=max_age_seconds)
        self._max_future_skew = timedelta(seconds=max_future_skew_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._state = MarketDataSessionState.STOPPED
        self._instruments: dict[str, IndiaInstrument] = {}
        self._subscription: MarketDataSubscription | None = None
        self._last_sequence: dict[str, int] = {}
        self._quotes: dict[str, TopOfBook] = {}
        self._trades: dict[str, TradeTick] = {}
        self._windows: dict[str, deque[TopOfBook]] = defaultdict(
            lambda: deque(maxlen=self._max_window_size)
        )

    @property
    def state(self) -> MarketDataSessionState:
        return self._state

    @property
    def provider_name(self) -> str:
        return self._provider.name

    @property
    def instruments(self) -> tuple[IndiaInstrument, ...]:
        return tuple(self._instruments.values())

    @property
    def subscription(self) -> MarketDataSubscription | None:
        return self._subscription

    async def start(self, instruments: tuple[IndiaInstrument, ...]) -> None:
        if self._state is not MarketDataSessionState.STOPPED:
            raise MarketDataError("SESSION_STATE_CONFLICT", "market-data session is not stopped")
        if not instruments:
            raise MarketDataError("NO_INSTRUMENTS", "at least one instrument is required")
        try:
            subscription = MarketDataSubscription(instruments=instruments)
        except ValueError as exc:
            raise MarketDataError("INVALID_SUBSCRIPTION", "market-data subscription is invalid") from exc
        keys = [item.key for item in subscription.instruments]

        self._state = MarketDataSessionState.STARTING
        self._subscription = subscription
        self._instruments = dict(zip(keys, subscription.instruments, strict=True))
        self._clear_observations()
        try:
            await self._provider.start(subscription)
        except Exception as exc:
            self._state = MarketDataSessionState.FAILED
            self._instruments = {}
            self._subscription = None
            raise MarketDataError("PROVIDER_START_FAILED", "market-data provider failed to start") from exc
        self._state = MarketDataSessionState.RUNNING

    async def stop(self) -> None:
        if self._state is MarketDataSessionState.STOPPED:
            return
        self._state = MarketDataSessionState.STOPPING
        try:
            await self._provider.stop()
        finally:
            self._instruments = {}
            self._subscription = None
            self._clear_observations()
            self._state = MarketDataSessionState.STOPPED

    async def poll_once(self) -> MarketDataEvent | None:
        self._require_running()
        try:
            event = await self._provider.next_event()
            if event is not None:
                self.ingest(event)
            return event
        except MarketDataError:
            await self._provider.stop()
            self._state = MarketDataSessionState.FAILED
            raise
        except Exception as exc:
            self._state = MarketDataSessionState.FAILED
            raise MarketDataError("PROVIDER_READ_FAILED", "market-data provider read failed") from exc

    def ingest(self, event: MarketDataEvent) -> None:
        self._require_running()
        key = event.instrument.key
        if key not in self._instruments:
            raise MarketDataError(
                "INSTRUMENT_OUT_OF_SCOPE",
                "market-data event is outside the configured session",
            )
        if event.source != self._provider.name:
            raise MarketDataError("SOURCE_MISMATCH", "market-data source does not match provider")
        now = self._validate_now(self._clock())
        if (
            event.observed_at - now > self._max_future_skew
            or event.received_at - now > self._max_future_skew
        ):
            raise MarketDataError(
                "FUTURE_EVENT",
                "market-data observation is unacceptably far in the future",
            )
        previous = self._last_sequence.get(key)
        if previous is not None and event.sequence <= previous:
            raise MarketDataError(
                "OUT_OF_ORDER_EVENT",
                "market-data sequence must increase monotonically",
            )
        self._last_sequence[key] = event.sequence
        if isinstance(event, TopOfBook):
            self._quotes[key] = event
            self._windows[key].append(event)
        elif isinstance(event, TradeTick):
            self._trades[key] = event
        else:
            raise MarketDataError("UNSUPPORTED_EVENT", "unsupported market-data event")

    def snapshot(
        self,
        instrument: IndiaInstrument,
        *,
        now: datetime | None = None,
    ) -> MarketSnapshot:
        self._require_running()
        key = self._require_instrument(instrument)
        quote = self._quotes.get(key)
        if quote is None:
            raise MarketDataError("SNAPSHOT_UNAVAILABLE", "top-of-book snapshot is unavailable")
        checked_now = self._validate_now(now or self._clock())
        if quote.observed_at - checked_now > self._max_future_skew:
            raise MarketDataError("FUTURE_EVENT", "top-of-book snapshot is in the future")
        if checked_now - quote.observed_at > self._max_age:
            raise MarketDataError("STALE_SNAPSHOT", "top-of-book snapshot is stale")
        trade = self._trades.get(key)
        if trade is not None and checked_now - trade.observed_at > self._max_age:
            trade = None
        return MarketSnapshot(
            instrument=instrument,
            source=quote.source,
            bid=quote.bid,
            ask=quote.ask,
            bid_size=quote.bid_size,
            ask_size=quote.ask_size,
            quote_observed_at=quote.observed_at,
            quote_received_at=quote.received_at,
            quote_sequence=quote.sequence,
            last_trade_price=None if trade is None else trade.price,
            last_trade_quantity=None if trade is None else trade.quantity,
            trade_observed_at=None if trade is None else trade.observed_at,
            trade_received_at=None if trade is None else trade.received_at,
            trade_sequence=None if trade is None else trade.sequence,
            trade_id=None if trade is None else trade.trade_id,
        )

    def tick_window(
        self,
        instrument: IndiaInstrument,
        *,
        now: datetime | None = None,
    ) -> dict[str, list[float]]:
        self.snapshot(instrument, now=now)
        key = self._require_instrument(instrument)
        quotes = tuple(self._windows[key])
        return {
            "bid": [float(quote.bid) for quote in quotes],
            "ask": [float(quote.ask) for quote in quotes],
            "spread": [
                float((quote.ask - quote.bid) / ((quote.ask + quote.bid) / 2))
                for quote in quotes
            ],
        }

    def _require_running(self) -> None:
        if self._state is not MarketDataSessionState.RUNNING:
            raise MarketDataError("SESSION_NOT_RUNNING", "market-data session is not running")

    def _require_instrument(self, instrument: IndiaInstrument) -> str:
        key = instrument.key
        configured = self._instruments.get(key)
        if configured is None or configured != instrument:
            raise MarketDataError(
                "INSTRUMENT_OUT_OF_SCOPE",
                "instrument is outside the configured session",
            )
        return key

    @staticmethod
    def _validate_now(now: datetime) -> datetime:
        if now.tzinfo is None:
            raise MarketDataError("INVALID_CLOCK", "freshness clock must be timezone-aware")
        return now

    def _clear_observations(self) -> None:
        self._last_sequence.clear()
        self._quotes.clear()
        self._trades.clear()
        self._windows.clear()
