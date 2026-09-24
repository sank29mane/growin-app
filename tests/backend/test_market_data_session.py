from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from market_data import (
    IndiaInstrument,
    MarketDataError,
    MarketDataSession,
    MarketDataSessionState,
    ReadOnlyMarketDataProvider,
    ReplayMarketDataProvider,
    TopOfBook,
    TradeTick,
)


NOW = datetime(2026, 7, 28, 4, 0, tzinfo=timezone.utc)
RELIANCE = IndiaInstrument(symbol="RELIANCE")
TCS = IndiaInstrument(symbol="TCS")


def quote(sequence, *, instrument=RELIANCE, observed_at=NOW):
    return TopOfBook(
        instrument=instrument,
        source="local-replay",
        bid=Decimal("1400") + Decimal(sequence) / Decimal("100"),
        ask=Decimal("1400.1") + Decimal(sequence) / Decimal("100"),
        observed_at=observed_at,
        received_at=observed_at,
        sequence=sequence,
    )


def trade(sequence, *, instrument=RELIANCE):
    return TradeTick(
        instrument=instrument,
        source="local-replay",
        price="1400.05",
        quantity="3",
        observed_at=NOW,
        received_at=NOW,
        sequence=sequence,
    )


@pytest.mark.asyncio
async def test_session_and_provider_do_nothing_until_explicit_start():
    provider = ReplayMarketDataProvider([quote(1)])
    session = MarketDataSession(provider, clock=lambda: NOW)

    assert session.state is MarketDataSessionState.STOPPED
    assert provider.running is False
    with pytest.raises(MarketDataError) as error:
        await session.poll_once()
    assert error.value.code == "SESSION_NOT_RUNNING"

    await session.start((RELIANCE,))
    assert session.state is MarketDataSessionState.RUNNING
    assert provider.running is True
    assert session.subscription is not None
    assert session.subscription.read_only is True


@pytest.mark.asyncio
async def test_replay_is_deterministic_and_snapshot_contains_quote_trade_lineage():
    events = [quote(1), trade(2)]

    async def replay_once():
        session = MarketDataSession(
            ReplayMarketDataProvider(events),
            clock=lambda: NOW,
        )
        await session.start((RELIANCE,))
        assert await session.poll_once() == events[0]
        assert await session.poll_once() == events[1]
        result = session.snapshot(RELIANCE).model_dump_json()
        await session.stop()
        return result

    assert await replay_once() == await replay_once()


@pytest.mark.asyncio
async def test_session_rejects_out_of_order_and_out_of_scope_events():
    provider = ReplayMarketDataProvider([])
    session = MarketDataSession(provider, clock=lambda: NOW)
    await session.start((RELIANCE,))
    session.ingest(quote(2))

    with pytest.raises(MarketDataError) as ordering:
        session.ingest(quote(2))
    assert ordering.value.code == "OUT_OF_ORDER_EVENT"

    with pytest.raises(MarketDataError) as scope:
        session.ingest(quote(3, instrument=TCS))
    assert scope.value.code == "INSTRUMENT_OUT_OF_SCOPE"


@pytest.mark.asyncio
async def test_stale_snapshot_fails_closed_and_stale_trade_is_omitted():
    session = MarketDataSession(
        ReplayMarketDataProvider([]),
        max_age_seconds=5,
        clock=lambda: NOW,
    )
    await session.start((RELIANCE,))
    session.ingest(quote(1, observed_at=NOW - timedelta(seconds=6)))
    with pytest.raises(MarketDataError) as stale:
        session.snapshot(RELIANCE)
    assert stale.value.code == "STALE_SNAPSHOT"

    await session.stop()
    await session.start((RELIANCE,))
    session.ingest(quote(1))
    session.ingest(
        TradeTick(
            instrument=RELIANCE,
            source="local-replay",
            price="1400",
            quantity="1",
            observed_at=NOW - timedelta(seconds=6),
            received_at=NOW,
            sequence=2,
        )
    )
    assert session.snapshot(RELIANCE).last_trade_price is None


@pytest.mark.asyncio
async def test_future_observation_fails_closed():
    session = MarketDataSession(
        ReplayMarketDataProvider([]),
        max_future_skew_seconds=1,
        clock=lambda: NOW,
    )
    await session.start((RELIANCE,))
    with pytest.raises(MarketDataError) as future:
        session.ingest(quote(1, observed_at=NOW + timedelta(seconds=2)))
    assert future.value.code == "FUTURE_EVENT"


@pytest.mark.asyncio
async def test_tick_window_is_bounded_and_stop_clears_observations():
    session = MarketDataSession(
        ReplayMarketDataProvider([]),
        max_window_size=2,
        clock=lambda: NOW,
    )
    await session.start((RELIANCE,))
    for sequence in (1, 2, 3):
        session.ingest(quote(sequence))

    window = session.tick_window(RELIANCE)
    assert len(window["bid"]) == 2
    assert window["bid"][0] == pytest.approx(1400.02)

    await session.stop()
    assert session.state is MarketDataSessionState.STOPPED
    with pytest.raises(MarketDataError) as stopped:
        session.snapshot(RELIANCE)
    assert stopped.value.code == "SESSION_NOT_RUNNING"


def test_provider_contract_has_no_account_or_order_authority():
    provider = ReplayMarketDataProvider([])
    assert isinstance(provider, ReadOnlyMarketDataProvider)
    forbidden = {
        "get_account",
        "get_positions",
        "get_orders",
        "place_order",
        "submit_order",
        "cancel_order",
        "replace_order",
    }
    assert forbidden.isdisjoint(dir(provider))
