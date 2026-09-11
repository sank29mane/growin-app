from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from execution.ledger import ExecutionLedger, intent_hash
from execution.models import OrderAck, OrderSide, ReconciliationSnapshot, ReconciliationStatus
from execution.requote import LocalPaperVenue, QuoteEvidence, RequoteCoordinator, RequotePolicy, RequoteValidationError
from execution.service import ExecutionService, _intent_from_proposal


class _DispatcherSentinel:
    async def dispatch(self, _intent):  # pragma: no cover - replacement never dispatches
        raise AssertionError("replacement preparation must not dispatch")


def _parent(ledger):
    proposal = {"proposal_id": "parent", "workspace": "uk", "account": "invest", "broker": "paper", "mode": "PAPER", "ticker": "VUSA", "action": "BUY", "quantity": "2"}
    service = ExecutionService(_DispatcherSentinel(), ledger)
    service.admit(proposal, currency="GBP", price="10", simulator_evidence={"simulated_fill_price": "10"}, risk_evidence={"scaled_size": "2"})
    ledger.configure_paper_budget("invest", "GBP", "100")
    service.reserve("parent")
    ledger.claim_intent(_intent_from_proposal(proposal))
    ledger.finalize("parent", OrderAck(proposal_id="parent", broker="paper", broker_order_id="paper-parent"))
    return service, proposal


def _candidate(ledger, proposal, observed_at=None):
    observed_at = observed_at or datetime.now(timezone.utc)
    return RequoteCoordinator(ledger).evaluate_and_record(
        proposal_id=proposal["proposal_id"], side=OrderSide.BUY,
        evidence=QuoteEvidence(bid=Decimal("9.90"), ask=Decimal("10.10"), volatility=Decimal("0.05"), cost=Decimal("0.01"), tick_size=Decimal("0.01"), regime_id=1, observed_at=observed_at, source="local-test"),
        policy=RequotePolicy(regime_multipliers={1: Decimal("1.5")}), venue=LocalPaperVenue(), now=observed_at,
    )


def _reconcile(service, status, quantity, notional, fingerprint):
    return service.reconcile(ReconciliationSnapshot(proposal_id="parent", broker_order_id="paper-parent", source="local-test", cumulative_quantity=Decimal(quantity), cumulative_notional=Decimal(notional), status=status, evidence_fingerprint=fingerprint, observed_at=datetime.now(timezone.utc)))


def test_partial_cancel_prepares_fresh_limit_intent_without_dispatch(tmp_path):
    with ExecutionLedger(tmp_path / "execution.sqlite3") as ledger:
        service, proposal = _parent(ledger)
        evaluated = _candidate(ledger, proposal)
        _reconcile(service, ReconciliationStatus.PARTIALLY_FILLED, "1", "10", "partial")
        _reconcile(service, ReconciliationStatus.CANCELLED, "1", "10", "cancelled")

        prepared = RequoteCoordinator(ledger).prepare_replacement(requote_id=evaluated.record.requote_id, replacement_proposal_id="replacement")

        assert prepared.record.state == "REPLACEMENT_PREPARED"
        assert prepared.intent.order_type.value == "LIMIT"
        assert prepared.intent.limit_price == Decimal("10.09")
        assert prepared.intent.quantity == Decimal("1")
        assert prepared.intent.replaces_proposal_id == "parent"
        assert ledger.get_order("replacement").state == "PENDING"
        assert ledger.get_reservation("parent").state == "SETTLED"
        assert ledger.get_reservation("replacement") is None
        assert intent_hash(prepared.intent) != ledger.get_order("parent").intent_hash

        admission = service.admit(
            prepared.intent,
            currency="GBP",
            price=prepared.intent.limit_price,
            simulator_evidence={"simulated_fill_price": str(prepared.intent.limit_price)},
            risk_evidence={"scaled_size": str(prepared.intent.quantity)},
        )
        assert admission.proposal_id == "replacement"
        service.reserve("replacement")
        assert ledger.get_reservation("replacement").state == "ACTIVE"


def test_replacement_requires_cancelled_parent_and_unexpired_evidence(tmp_path):
    with ExecutionLedger(tmp_path / "execution.sqlite3") as ledger:
        service, proposal = _parent(ledger)
        evaluated = _candidate(ledger, proposal)
        _reconcile(service, ReconciliationStatus.UNKNOWN, "0", "0", "unknown")
        with pytest.raises(RequoteValidationError, match="CANCELLED"):
            RequoteCoordinator(ledger).prepare_replacement(requote_id=evaluated.record.requote_id, replacement_proposal_id="blocked")
        assert ledger.get_reservation("parent").state == "ACTIVE"

    with ExecutionLedger(tmp_path / "expired.sqlite3") as ledger:
        service, proposal = _parent(ledger)
        observed_at = datetime.now(timezone.utc) - timedelta(seconds=31)
        # The candidate was valid when evaluated, but cannot survive its policy expiry.
        evaluated = _candidate(ledger, proposal, observed_at=observed_at)
        _reconcile(service, ReconciliationStatus.CANCELLED, "0", "0", "cancelled")
        with pytest.raises(RequoteValidationError, match="expired"):
            RequoteCoordinator(ledger).prepare_replacement(requote_id=evaluated.record.requote_id, replacement_proposal_id="expired", now=datetime.now(timezone.utc) + timedelta(seconds=1))


@pytest.mark.parametrize(
    ("status", "quantity", "notional"),
    [(ReconciliationStatus.REJECTED, "0", "0"), (ReconciliationStatus.FILLED, "2", "20")],
)
def test_rejected_or_filled_parent_cannot_prepare_replacement(tmp_path, status, quantity, notional):
    with ExecutionLedger(tmp_path / "execution.sqlite3") as ledger:
        service, proposal = _parent(ledger)
        evaluated = _candidate(ledger, proposal)
        _reconcile(service, status, quantity, notional, status.value.lower())

        with pytest.raises(RequoteValidationError, match="CANCELLED"):
            RequoteCoordinator(ledger).prepare_replacement(
                requote_id=evaluated.record.requote_id,
                replacement_proposal_id=f"blocked-{status.value.lower()}",
            )
