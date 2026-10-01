from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from execution.models import OrderSide
from execution.requote import (
    LocalPaperVenue,
    QuoteEvidence,
    RequotePolicy,
    RequoteValidationError,
    evaluate_requote,
)


def _evidence(**overrides):
    values = {
        "bid": Decimal("99.90"),
        "ask": Decimal("100.10"),
        "volatility": Decimal("0.05"),
        "cost": Decimal("0.01"),
        "tick_size": Decimal("0.01"),
        "regime_id": 1,
        "observed_at": datetime.now(timezone.utc),
        "source": "local-fixture",
    }
    values.update(overrides)
    return QuoteEvidence(**values)


def test_local_paper_venue_is_the_only_accepted_capability():
    venue = LocalPaperVenue()

    assert venue.mode == "LOCAL_PAPER"
    assert venue.external_network is False
    assert venue.supports_limit is True
    assert venue.supports_replace is False
    assert venue.supports_cancel is False

    with pytest.raises(RequoteValidationError, match="local-paper"):
        LocalPaperVenue(mode="PAPER")

    with pytest.raises(RequoteValidationError, match="network"):
        LocalPaperVenue(external_network=True)


def test_buy_candidate_is_decimal_deterministic_and_tick_aligned():
    policy = RequotePolicy(regime_multipliers={1: Decimal("1.5")})
    evidence = _evidence()

    first = evaluate_requote(
        side=OrderSide.BUY,
        evidence=evidence,
        policy=policy,
        venue=LocalPaperVenue(),
    )
    second = evaluate_requote(
        side=OrderSide.BUY,
        evidence=evidence,
        policy=policy,
        venue=LocalPaperVenue(),
    )

    assert first.limit_price == Decimal("100.09")
    assert first.limit_price == second.limit_price
    assert first.limit_price % Decimal("0.01") == 0
    assert Decimal("99.90") <= first.limit_price <= Decimal("100.10")
    assert first.snapshot_hash == second.snapshot_hash


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("bid", Decimal("0"), "bid"),
        ("ask", Decimal("NaN"), "ask"),
        ("tick_size", Decimal("0"), "tick size"),
        ("cost", Decimal("-0.01"), "cost"),
    ],
)
def test_invalid_numeric_evidence_fails_closed(field, value, message):
    values = _evidence().__dict__.copy()
    values[field] = value

    with pytest.raises(RequoteValidationError, match=message):
        QuoteEvidence(**values)


@pytest.mark.parametrize(
    "offset_seconds", [-31, 1],
)
def test_stale_or_future_evidence_fails_closed(offset_seconds):
    with pytest.raises(RequoteValidationError, match="timestamp"):
        evaluate_requote(
            side=OrderSide.BUY,
            evidence=_evidence(
                observed_at=datetime.now(timezone.utc)
                + timedelta(seconds=offset_seconds)
            ),
            policy=RequotePolicy(max_age_seconds=30),
            venue=LocalPaperVenue(),
        )


def test_unknown_regime_and_sell_are_rejected_before_candidate_creation():
    with pytest.raises(RequoteValidationError, match="regime"):
        evaluate_requote(
            side=OrderSide.BUY,
            evidence=_evidence(regime_id=99),
            policy=RequotePolicy(regime_multipliers={1: Decimal("1.5")}),
            venue=LocalPaperVenue(),
        )

    with pytest.raises(RequoteValidationError, match="BUY"):
        evaluate_requote(
            side=OrderSide.SELL,
            evidence=_evidence(),
            policy=RequotePolicy(regime_multipliers={1: Decimal("1.5")}),
            venue=LocalPaperVenue(),
        )
