from datetime import datetime, timezone
from decimal import Decimal

import pytest

from market_data import IndiaInstrument, MarketDataError, MarketDataSession, RegimeClassifier, ReplayMarketDataProvider, TopOfBook


NOW = datetime(2026, 7, 28, 4, 0, tzinfo=timezone.utc)
INSTRUMENT = IndiaInstrument(symbol="RELIANCE")


def quote(sequence: int) -> TopOfBook:
    bid = Decimal("1400") + Decimal(sequence) / Decimal("100")
    return TopOfBook(
        instrument=INSTRUMENT, source="local-replay", bid=bid, ask=bid + Decimal("0.10"),
        observed_at=NOW, received_at=NOW, sequence=sequence,
    )


@pytest.mark.asyncio
async def test_classifier_binds_deterministic_evidence_to_fresh_snapshot():
    session = MarketDataSession(ReplayMarketDataProvider([]), clock=lambda: NOW)
    await session.start((INSTRUMENT,))
    for sequence in (1, 2, 3):
        session.ingest(quote(sequence))
    evidence = RegimeClassifier().evidence(session, INSTRUMENT, now=NOW)
    assert evidence.source_snapshot_id == session.snapshot(INSTRUMENT, now=NOW).snapshot_id
    assert evidence.model_version.startswith("gmm-p256:")
    assert evidence.regime_id >= 0


@pytest.mark.asyncio
async def test_classifier_rejects_insufficient_window_without_default_regime():
    session = MarketDataSession(ReplayMarketDataProvider([]), clock=lambda: NOW)
    await session.start((INSTRUMENT,))
    session.ingest(quote(1))
    with pytest.raises(MarketDataError) as error:
        RegimeClassifier().evidence(session, INSTRUMENT, now=NOW)
    assert error.value.code == "REGIME_WINDOW_INSUFFICIENT"
