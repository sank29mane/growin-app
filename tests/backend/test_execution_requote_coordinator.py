from datetime import datetime, timezone
from decimal import Decimal

from execution.ledger import ExecutionLedger
from execution.models import OrderAck, OrderSide
from execution.requote import (
    LocalPaperVenue,
    QuoteEvidence,
    RequoteCoordinator,
    RequotePolicy,
)
from execution.service import ExecutionService, _intent_from_proposal


class _DispatcherSentinel:
    async def dispatch(self, _intent):  # pragma: no cover - must never be called
        raise AssertionError("coordinator must not dispatch")


def _setup_parent(ledger):
    proposal = {
        "proposal_id": "coordinator-parent",
        "workspace": "uk",
        "account": "invest",
        "broker": "paper",
        "mode": "PAPER",
        "ticker": "VUSA",
        "action": "BUY",
        "quantity": "2",
    }
    service = ExecutionService(_DispatcherSentinel(), ledger)
    service.admit(
        proposal,
        currency="GBP",
        price="10",
        simulator_evidence={"simulated_fill_price": "10"},
        risk_evidence={"scaled_size": "2"},
    )
    ledger.configure_paper_budget("invest", "GBP", "100")
    service.reserve(proposal["proposal_id"])
    intent = _intent_from_proposal(proposal)
    ledger.claim_intent(intent)
    ledger.finalize(
        proposal["proposal_id"],
        OrderAck(
            proposal_id=proposal["proposal_id"],
            broker="paper",
            broker_order_id="coordinator-paper-order",
        ),
    )
    return proposal


def _evidence():
    return QuoteEvidence(
        bid=Decimal("9.90"),
        ask=Decimal("10.10"),
        volatility=Decimal("0.05"),
        cost=Decimal("0.01"),
        tick_size=Decimal("0.01"),
        regime_id=1,
        observed_at=datetime.now(timezone.utc),
        source="local-test",
    )


def test_coordinator_persists_candidate_then_stops_without_dispatch(tmp_path):
    with ExecutionLedger(tmp_path / "execution.sqlite3") as ledger:
        proposal = _setup_parent(ledger)
        coordinator = RequoteCoordinator(ledger)

        first = coordinator.evaluate_and_record(
            proposal_id=proposal["proposal_id"],
            side=OrderSide.BUY,
            evidence=_evidence(),
            policy=RequotePolicy(regime_multipliers={1: Decimal("1.5")}),
            venue=LocalPaperVenue(),
        )

        assert first.record.state == "BLOCKED_NO_MUTATION_CAPABILITY"
        assert first.candidate.limit_price == Decimal("10.09")
        assert ledger.get_order(proposal["proposal_id"]).state == "ACKNOWLEDGED"
        assert ledger.get_reservation(proposal["proposal_id"]).state == "ACTIVE"
        assert len(ledger.list_attempts(proposal["proposal_id"])) == 1


def test_identical_snapshot_replays_one_durable_candidate(tmp_path):
    with ExecutionLedger(tmp_path / "execution.sqlite3") as ledger:
        proposal = _setup_parent(ledger)
        coordinator = RequoteCoordinator(ledger)
        evidence = _evidence()
        arguments = {
            "proposal_id": proposal["proposal_id"],
            "side": OrderSide.BUY,
            "evidence": evidence,
            "policy": RequotePolicy(regime_multipliers={1: Decimal("1.5")}),
            "venue": LocalPaperVenue(),
        }

        first = coordinator.evaluate_and_record(**arguments)
        replay = coordinator.evaluate_and_record(**arguments)

        assert first.record.requote_id == replay.record.requote_id
        assert len(ledger.list_requotes(proposal["proposal_id"])) == 1
