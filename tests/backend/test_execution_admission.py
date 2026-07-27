from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from execution import (
    AdmissionDecision,
    ExecutionLedger,
    ExecutionService,
    PaperDispatcher,
    OrderIntent,
)


def intent(proposal_id="admit-1", **overrides):
    values = {
        "proposal_id": proposal_id,
        "workspace": "uk",
        "account": "invest",
        "broker": "paper",
        "mode": "PAPER",
        "ticker": "VUSA",
        "side": "BUY",
        "quantity": Decimal("2"),
    }
    values.update(overrides)
    return OrderIntent(**values)


def evidence(service, value="2", **kwargs):
    return service.admit(
        intent(kwargs.pop("proposal_id", "admit-1")),
        currency="GBP",
        price="100",
        simulator_evidence=kwargs.pop("simulator_evidence", {"simulated_fill_price": "100"}),
        risk_evidence=kwargs.pop("risk_evidence", {"scaled_size": value}),
        **kwargs,
    )


def test_missing_or_denied_evidence_records_no_reservation_or_dispatch(tmp_path):
    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock()
    with ExecutionLedger(tmp_path / "execution.sqlite3") as ledger:
        service = ExecutionService(dispatcher, ledger)
        denied = evidence(service, risk_evidence={"allowed": False, "scaled_size": 0})
        assert denied.decision is AdmissionDecision.DENIED
        assert ledger.get_reservation("admit-1") is None
        assert ledger.get_admission("admit-1").decision is AdmissionDecision.DENIED
        with pytest.raises(Exception, match="admission"):
            service.reserve("admit-1")
        dispatcher.dispatch.assert_not_awaited()


@pytest.mark.parametrize(
    "simulator_evidence",
    [
        {"simulated_fill_price": "NaN"},
        {"simulated_fill_price": "Infinity"},
        {"simulated_fill_price": "0"},
    ],
)
def test_non_finite_and_zero_simulator_values_deny(tmp_path, simulator_evidence):
    with ExecutionLedger(tmp_path / "execution.sqlite3") as ledger:
        service = ExecutionService(PaperDispatcher(), ledger)
        result = evidence(service, simulator_evidence=simulator_evidence)
        assert result.decision is AdmissionDecision.DENIED
        assert ledger.get_reservation("admit-1") is None


def test_stale_and_sell_evidence_fail_closed(tmp_path):
    with ExecutionLedger(tmp_path / "execution.sqlite3") as ledger:
        service = ExecutionService(PaperDispatcher(), ledger)
        stale = evidence(
            service,
            evidence_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
        assert stale.decision is AdmissionDecision.DENIED
        sell = intent("sell", side="SELL")
        result = service.admit(
            sell,
            currency="GBP",
            price="100",
            simulator_evidence={"simulated_fill_price": "100"},
            risk_evidence={"scaled_size": "2"},
        )
        assert result.decision is AdmissionDecision.DENIED
        assert ledger.get_reservation("sell") is None


def test_admitted_evidence_is_decimal_and_immutable(tmp_path):
    with ExecutionLedger(tmp_path / "execution.sqlite3") as ledger:
        service = ExecutionService(PaperDispatcher(), ledger)
        result = evidence(service, value="1.5")
        assert result.decision is AdmissionDecision.ADMITTED
        assert result.final_quantity == Decimal("1.5")
        assert result.notional == Decimal("150.0")
        replay = ledger.record_admission(intent(), result)
        assert replay.evidence_hash == result.evidence_hash
        with pytest.raises(Exception, match="immutable"):
            service.admit(
                intent(),
                currency="GBP",
                price="101",
                simulator_evidence={"simulated_fill_price": "101"},
                risk_evidence={"scaled_size": "1.5"},
            )
