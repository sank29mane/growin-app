"""Broker-neutral, read-only market-data contracts and session lifecycle."""

from .admission import (
    MarketPreflightContext,
    RegimeEvidence,
    build_market_preflight_context,
)
from .models import (
    IndiaInstrument,
    Instrument,
    MarketDataEvent,
    MarketDataSubscription,
    MarketSnapshot,
    TopOfBook,
    TradeTick,
    UkInstrument,
)
from .provider import ReadOnlyMarketDataProvider
from .replay import ReplayMarketDataProvider
from .regime import RegimeClassifier
from .session import (
    MarketDataError,
    MarketDataSession,
    MarketDataSessionState,
)

__all__ = [
    "IndiaInstrument",
    "Instrument",
    "MarketDataError",
    "MarketDataEvent",
    "MarketDataSubscription",
    "MarketDataSession",
    "MarketDataSessionState",
    "MarketSnapshot",
    "MarketPreflightContext",
    "ReadOnlyMarketDataProvider",
    "RegimeEvidence",
    "RegimeClassifier",
    "ReplayMarketDataProvider",
    "TopOfBook",
    "TradeTick",
    "UkInstrument",
    "build_market_preflight_context",
]
