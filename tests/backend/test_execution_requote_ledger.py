from datetime import datetime, timezone
from decimal import Decimal

import pytest

from execution.ledger import ExecutionLedger, RequoteConflict
from execution.models import OrderAck, ReconciliationSnapshot, ReconciliationStatus
from execution.service import ExecutionService, _intent_from_proposal


def _setup_acknowledged_parent(ledger: ExecutionLedger, proposal_id: str = "parent"):
    proposal = {
        "proposal_id": proposal_id,
        "workspace": "uk",
        "account": "invest",
        "broker": "paper",
        "mode": "PAPER",
        "ticker": "VUSA",
        "action": "BUY",
        "quantity": "2",
    }
    service = ExecutionService(ledger=ledger)
    service.admit(
        proposal,
        currency="GBP",
        price="10",
        simulator_evidence={"simulated_fill_price": "10"},
        risk_evidence={"scaled_size": "2"},
    )
    ledger.configure_paper_budget("invest", "GBP", "100")
    service.reserve(proposal_id)
    intent = _intent_from_proposal(proposal)
    ledger.claim_intent(intent)
    ledger.finalize(
        proposal_id,
        OrderAck(
            proposal_id=proposal_id,
            broker="paper",
            broker_order_id=f"paper-{proposal_id}",
        ),
    )
    order = ledger.get_order(proposal_id)
    assert order is not None
    return order


def _candidate(limit_price: str = "10.05"):
    return {
        "side": "BUY",
        "limit_price": limit_price,
        "lower_bound": "9.90",
        "upper_bound": "10.10",
        "policy_version": "local-paper-v1",
    }


def _record(ledger, order, *, requote_id="rq-1", key="parent:snapshot-1", candidate=None):
    assert order.acknowledgment is not None
    return ledger.record_requote_intent(
        requote_id=requote_id,
        proposal_id=order.proposal_id,
        parent_intent_hash=order.intent_hash,
        parent_reconciliation_fingerprint=f"ack:{order.acknowledgment.broker_order_id}",
        idempotency_key=key,
        snapshot_hash="snapshot-1",
        candidate=candidate or _candidate(),
    )


def test_requote_candidate_is_immutable_and_idempotent(tmp_path):
    with ExecutionLedger(tmp_path / "execution.sqlite3") as ledger:
        order = _setup_acknowledged_parent(ledger)
        created = _record(ledger, order)
        replay = _record(ledger, order)

        assert created == replay
        assert created.state == "EVALUATED"
        assert ledger.list_requote_events(created.requote_id)[0]["event_type"] == "REQUOTE_EVALUATED"

        with pytest.raises(RequoteConflict, match="idempotency identity"):
            _record(ledger, order, candidate=_candidate("10.06"))


def test_requote_blocks_locally_without_touching_parent_or_reservation(tmp_path):
    with ExecutionLedger(tmp_path / "execution.sqlite3") as ledger:
        order = _setup_acknowledged_parent(ledger)
        recorded = _record(ledger, order)
        blocked = ledger.block_requote(recorded.requote_id, "NO_MUTATION_CAPABILITY")

        assert blocked.state == "BLOCKED_NO_MUTATION_CAPABILITY"
        assert ledger.get_order(order.proposal_id).state == "ACKNOWLEDGED"
        assert ledger.get_reservation(order.proposal_id).state == "ACTIVE"
        assert [event["event_type"] for event in ledger.list_requote_events(recorded.requote_id)] == [
            "REQUOTE_EVALUATED",
            "REQUOTE_BLOCKED",
        ]


def test_requote_recovery_requires_fresh_evidence_after_restart(tmp_path):
    path = tmp_path / "execution.sqlite3"
    with ExecutionLedger(path) as ledger:
        order = _setup_acknowledged_parent(ledger)
        recorded = _record(ledger, order)
        assert recorded.state == "EVALUATED"

    with ExecutionLedger(path) as reopened:
        recovered = reopened.get_requote("rq-1")
        assert recovered is not None
        assert recovered.state == "BLOCKED_RESTART_REQUIRES_FRESH_EVIDENCE"
        assert reopened.get_reservation("parent").state == "ACTIVE"


def test_unknown_parent_and_workspace_control_fail_closed(tmp_path):
    with ExecutionLedger(tmp_path / "execution.sqlite3") as ledger:
        order = _setup_acknowledged_parent(ledger)
        assert order.acknowledgment is not None
        ledger.reconcile(
            ReconciliationSnapshot(
                proposal_id=order.proposal_id,
                broker_order_id=order.acknowledgment.broker_order_id,
                source="local-test",
                cumulative_quantity=Decimal("0"),
                cumulative_notional=Decimal("0"),
                status=ReconciliationStatus.UNKNOWN,
                evidence_fingerprint="unknown-parent",
                observed_at=datetime.now(timezone.utc),
            )
        )
        with pytest.raises(RequoteConflict, match="not eligible"):
            _record(ledger, order)
        assert ledger.get_reservation(order.proposal_id).state == "ACTIVE"

        ledger.engage_workspace_control("REQUOTE_TEST")
        with pytest.raises(RequoteConflict, match="control"):
            _record(ledger, order, requote_id="rq-2", key="parent:snapshot-2")
