from datetime import datetime, timezone
from decimal import Decimal

import pytest

from execution import (
    ExecutionLedger,
    ExecutionService,
    OrderAck,
    PaperDispatcher,
    ReconciliationSnapshot,
)
from execution.service import _intent_from_proposal


def setup_order(ledger, pid="recon", quantity="2"):
    service = ExecutionService(PaperDispatcher(), ledger)
    proposal = {
        "proposal_id": pid,
        "workspace": "uk",
        "account": "invest",
        "broker": "paper",
        "mode": "PAPER",
        "ticker": "VUSA",
        "action": "BUY",
        "quantity": quantity,
    }
    service.admit(
        proposal,
        currency="GBP",
        price="10",
        simulator_evidence={"simulated_fill_price": "10"},
        risk_evidence={"scaled_size": quantity},
    )
    ledger.configure_paper_budget("invest", "GBP", "100")
    service.reserve(pid)
    ledger.claim_intent(_intent_from_proposal(proposal))
    ledger.finalize(pid, OrderAck(proposal_id=pid, broker="paper", broker_order_id=f"bo-{pid}"))
    return service


def snap(pid, status, qty, notional, fingerprint, broker_id=None):
    return ReconciliationSnapshot(
        proposal_id=pid,
        broker_order_id=broker_id or f"bo-{pid}",
        source="paper",
        cumulative_quantity=Decimal(str(qty)),
        cumulative_notional=Decimal(str(notional)),
        status=status,
        evidence_fingerprint=fingerprint,
        observed_at=datetime.now(timezone.utc),
    )


def test_partial_fill_then_cancel_releases_only_unfilled_amount(tmp_path):
    with ExecutionLedger(tmp_path / "execution.sqlite3") as ledger:
        service = setup_order(ledger)
        service.reconcile(snap("recon", "PARTIALLY_FILLED", 1, 10, "partial"))
        reservation = ledger.get_reservation("recon")
        assert reservation.consumed == Decimal("10")
        assert reservation.outstanding == Decimal("10")
        service.reconcile(snap("recon", "CANCELLED", 1, 10, "cancel"))
        reservation = ledger.get_reservation("recon")
        assert reservation.consumed == Decimal("10")
        assert reservation.released == Decimal("10")
        assert ledger.get_paper_budget("invest", "GBP").available == Decimal("90")


def test_full_fill_consumes_funds_and_updates_position_once(tmp_path):
    with ExecutionLedger(tmp_path / "execution.sqlite3") as ledger:
        service = setup_order(ledger)
        evidence = snap("recon", "FILLED", 2, 20, "filled")
        service.reconcile(evidence)
        service.reconcile(evidence)
        reservation = ledger.get_reservation("recon")
        assert reservation.consumed == Decimal("20")
        assert reservation.released == Decimal("0")
        assert ledger.get_paper_position("invest", "GBP", "VUSA") == {
            "quantity": "2",
            "notional": "20",
        }
        assert len([e for e in ledger.list_events("recon") if e.event_type == "RECONCILIATION_APPLIED"]) == 1


def test_overfill_non_monotonic_and_broker_id_mismatch_fail_closed(tmp_path):
    with ExecutionLedger(tmp_path / "execution.sqlite3") as ledger:
        service = setup_order(ledger)
        service.reconcile(snap("recon", "PARTIALLY_FILLED", 1, 10, "partial"))
        with pytest.raises(Exception, match="overfills"):
            service.reconcile(snap("recon", "FILLED", 3, 30, "over"))
        with pytest.raises(Exception, match="non-monotonic"):
            service.reconcile(snap("recon", "PARTIALLY_FILLED", 0, 0, "lower"))
        with pytest.raises(Exception, match="broker order id"):
            service.reconcile(snap("recon", "FILLED", 2, 20, "wrong", "other"))


def test_unknown_retains_reservation_and_requires_evidence(tmp_path):
    with ExecutionLedger(tmp_path / "execution.sqlite3") as ledger:
        service = setup_order(ledger)
        service.reconcile(snap("recon", "UNKNOWN", 0, 0, "unknown"))
        assert ledger.get_reservation("recon").state == "ACTIVE"
        with pytest.raises(Exception, match="UNKNOWN"):
            ledger.claim("recon")
        service.reconcile(snap("recon", "ACKNOWLEDGED", 0, 0, "resolved"))
        assert ledger.get_order("recon").state == "ACKNOWLEDGED"
