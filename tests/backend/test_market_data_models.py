from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from market_data import IndiaInstrument, MarketSnapshot, TopOfBook, TradeTick


NOW = datetime(2026, 7, 28, 4, 0, tzinfo=timezone.utc)
INSTRUMENT = IndiaInstrument(symbol="RELIANCE")


def quote(**overrides):
    values = {
        "instrument": INSTRUMENT,
        "source": "local-replay",
        "bid": "1400.10",
        "ask": "1400.20",
        "observed_at": NOW,
        "received_at": NOW,
        "sequence": 1,
    }
    values.update(overrides)
    return TopOfBook(**values)


def test_india_instrument_identity_is_explicit_and_frozen():
    assert INSTRUMENT.key == "india:NSE:CASH:RELIANCE"
    assert INSTRUMENT.currency == "INR"
    with pytest.raises(ValidationError):
        IndiaInstrument(symbol="reliance")
    with pytest.raises(ValidationError):
        INSTRUMENT.symbol = "TCS"
    with pytest.raises(ValidationError):
        IndiaInstrument(symbol="TCS", api_key="must-not-be-accepted")


@pytest.mark.parametrize(
    "overrides",
    [
        {"bid": "1401", "ask": "1400"},
        {"bid": "0"},
        {"ask": "NaN"},
        {"ask": "1234567890123.12345678"},
        {"observed_at": datetime(2026, 7, 28, 4, 0)},
    ],
)
def test_quote_rejects_crossed_nonpositive_nonfinite_and_naive_data(overrides):
    with pytest.raises(ValidationError):
        quote(**overrides)


def test_trade_requires_positive_values_and_timezone_lineage():
    trade = TradeTick(
        instrument=INSTRUMENT,
        source="local-replay",
        price="1400.15",
        quantity="2",
        observed_at=NOW,
        received_at=NOW,
        sequence=2,
    )
    assert trade.price == Decimal("1400.15")
    with pytest.raises(ValidationError):
        trade.model_copy(update={"quantity": Decimal("0")}).model_validate(
            trade.model_dump() | {"quantity": "0"}
        )


def test_snapshot_calculates_decimal_mid_and_spread_and_is_immutable():
    snapshot = MarketSnapshot(
        instrument=INSTRUMENT,
        source="local-replay",
        bid="100",
        ask="102",
        quote_observed_at=NOW,
        quote_received_at=NOW,
        quote_sequence=1,
    )
    assert snapshot.mid == Decimal("101")
    assert snapshot.spread_pct == Decimal("2") / Decimal("101")
    assert snapshot.snapshot_id == snapshot.model_copy().snapshot_id
    with pytest.raises(ValidationError):
        snapshot.bid = Decimal("99")
