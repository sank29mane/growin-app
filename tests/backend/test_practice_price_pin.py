"""PR #557 fix 5: a practice LIMIT order's notional is its limit price.

The route-level cap test lives in test_uk_practice_admission.py. This is the
service-level half: an admission that arrives without the pinned price is denied
instead of being valued at the simulator fill, which the ledger's own per-position
cap check would then trust.
"""

from __future__ import annotations

from execution import ExecutionLedger, ExecutionService, VenueBinding
from execution.venue import PRICE_SOURCE_OPERATOR_RECORDED
from t212_practice_testkit import PRACTICE_ACCOUNT, practice_proposal_dict


def test_a_bound_limit_order_without_a_pinned_price_is_denied_not_valued_at_the_simulator_fill(tmp_path):
    binding = VenueBinding(venue="t212_practice", account_id=PRACTICE_ACCOUNT, currency="GBP")
    with ExecutionLedger(tmp_path / "pin.sqlite3", workspace="uk", venue=binding) as ledger:
        service = ExecutionService(None, ledger, simulator=None, risk_gate=None)
        # The simulator fill (0.01) is far below the limit (50p = GBP 0.50). Valued there,
        # the cap check would see a fiftieth of what the order can cost.
        denied = service.admit(
            practice_proposal_dict("np-1", quantity="1", limit_price="50"),
            currency="GBP", price_source=PRICE_SOURCE_OPERATOR_RECORDED,
            simulator_evidence={"simulated_fill_price": "0.01"}, risk_evidence={"scaled_size": "1"},
        )
        assert denied.decision.value == "DENIED"
        assert denied.reason_code == "PRICE_NOT_PINNED_TO_LIMIT"

        pinned = service.admit(
            practice_proposal_dict("np-2", quantity="1", limit_price="50"),
            currency="GBP", price="0.5", price_source=PRICE_SOURCE_OPERATOR_RECORDED,
            simulator_evidence={"simulated_fill_price": "0.01"}, risk_evidence={"scaled_size": "1"},
        )
        assert pinned.decision.value == "ADMITTED"
        assert str(pinned.price) == "0.5" and str(pinned.notional) == "0.5"
