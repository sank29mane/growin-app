"""PR #557 fix 5: a practice LIMIT order's notional is its limit price.

The route-level cap test lives in test_uk_practice_admission.py. This is the
service-level half: an admission that arrives without the pinned price is denied
instead of being valued at the simulator fill, which the ledger's own per-position
cap check would then trust.
"""

from __future__ import annotations

from decimal import Decimal
from regime_testkit import bound_admit, gated
from execution import ExecutionLedger, ExecutionService, VenueBinding
from execution.venue import PRICE_SOURCE_OPERATOR_RECORDED
from t212_practice_testkit import PRACTICE_ACCOUNT, practice_proposal_dict


def test_a_bound_limit_order_without_a_pinned_price_is_denied_not_valued_at_the_simulator_fill(tmp_path):
    binding = VenueBinding(venue="t212_practice", account_id=PRACTICE_ACCOUNT, currency="GBP")
    with ExecutionLedger(tmp_path / "pin.sqlite3", workspace="uk", venue=binding) as ledger:
        service = ExecutionService(None, ledger, simulator=None, **gated())
        # The simulator fill (0.01) is far below the limit (50p = GBP 0.50). Valued there,
        # the cap check would see a fiftieth of what the order can cost.
        denied = service.admit(
            practice_proposal_dict("np-1", quantity="1", limit_price="50"),
            currency="GBP", price_source=PRICE_SOURCE_OPERATOR_RECORDED,
            simulator_evidence={"simulated_fill_price": "0.01"}, risk_evidence={"scaled_size": "1"}, **bound_admit(),
        )
        assert denied.decision.value == "DENIED"
        assert denied.reason_code == "PRICE_NOT_PINNED_TO_LIMIT"

        pinned = service.admit(
            practice_proposal_dict("np-2", quantity="1", limit_price="50"),
            currency="GBP", price="0.5", price_divisor="100", price_source=PRICE_SOURCE_OPERATOR_RECORDED,
            simulator_evidence={"simulated_fill_price": "0.01"}, risk_evidence={"scaled_size": "1"}, **bound_admit(),
        )
        assert pinned.decision.value == "ADMITTED"
        assert pinned.price == Decimal("0.5") and pinned.notional == Decimal("0.5")


def _admit(service, proposal_id, *, limit_price, price, divisor, sim_fill="0.01"):
    return service.admit(
        practice_proposal_dict(proposal_id, quantity="1", limit_price=limit_price),
        currency="GBP", price=price, price_divisor=divisor,
        price_source=PRICE_SOURCE_OPERATOR_RECORDED,
        simulator_evidence={"simulated_fill_price": sim_fill}, risk_evidence={"scaled_size": "1"}, **bound_admit(),
    )


def test_a_wrong_explicit_price_is_denied_and_the_correct_one_admitted(tmp_path):
    """Round 3 P1: a 50p LIMIT admitted at price 0.01 once recorded GBP 0.01, not GBP 0.50."""

    binding = VenueBinding(venue="t212_practice", account_id=PRACTICE_ACCOUNT, currency="GBP")
    with ExecutionLedger(tmp_path / "wrong.sqlite3", workspace="uk", venue=binding) as ledger:
        service = ExecutionService(None, ledger, simulator=None, **gated())
        wrong = _admit(service, "w-1", limit_price="50", price="0.01", divisor="100")
        assert wrong.decision.value == "DENIED"
        assert wrong.reason_code == "PRICE_NOT_PINNED_TO_LIMIT"
        assert str(wrong.notional) == "0"

        right = _admit(service, "w-2", limit_price="50", price="0.50", divisor="100")
        assert right.decision.value == "ADMITTED"
        assert right.price == Decimal("0.50") and right.notional == Decimal("0.50")


def test_a_pence_and_pound_mix_up_is_denied_in_both_directions(tmp_path):
    binding = VenueBinding(venue="t212_practice", account_id=PRACTICE_ACCOUNT, currency="GBP")
    with ExecutionLedger(tmp_path / "units.sqlite3", workspace="uk", venue=binding) as ledger:
        service = ExecutionService(None, ledger, simulator=None, **gated())
        # GBX instrument: 50p is GBP 0.50. Passing the pence figure as pounds is 100x too big.
        pence_as_pounds = _admit(service, "u-1", limit_price="50", price="50", divisor="100")
        assert pence_as_pounds.reason_code == "PRICE_NOT_PINNED_TO_LIMIT"
        # GBP instrument: a GBP 50 LIMIT is GBP 50. Dividing by 100 anyway is 100x too small.
        pounds_as_pence = _admit(service, "u-2", limit_price="50", price="0.5", divisor="1")
        assert pounds_as_pence.reason_code == "PRICE_NOT_PINNED_TO_LIMIT"
        # The right figure for each unit is admitted.
        assert _admit(service, "u-3", limit_price="50", price="0.5", divisor="100").decision.value == "ADMITTED"
        assert _admit(service, "u-4", limit_price="50", price="50", divisor="1").decision.value == "ADMITTED"


def test_a_bound_limit_order_without_the_unit_divisor_is_denied(tmp_path):
    binding = VenueBinding(venue="t212_practice", account_id=PRACTICE_ACCOUNT, currency="GBP")
    with ExecutionLedger(tmp_path / "nodiv.sqlite3", workspace="uk", venue=binding) as ledger:
        service = ExecutionService(None, ledger, simulator=None, **gated())
        # Price equals the limit, so the missing divisor is the only reason to deny.
        denied = _admit(service, "d-1", limit_price="50", price="50", divisor=None)
        assert denied.decision.value == "DENIED"
        assert denied.reason_code == "PRICE_NOT_PINNED_TO_LIMIT"
