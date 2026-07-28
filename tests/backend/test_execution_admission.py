from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from app_context import AppState
from execution import (
    AdmissionDecision,
    ExecutionLedger,
    ExecutionService,
    PaperDispatcher,
    OrderIntent,
)
from simulation import PreFlightSimulator, RiskSwarmGate


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
        assert ledger.get_order("admit-1").state == "REJECTED"
        with pytest.raises(Exception, match="admission"):
            service.reserve("admit-1")
        dispatcher.dispatch.assert_not_awaited()


def preflight_context(connection, *, spread=0.02):
    return {
        "tick_window": {"bid": [99.0], "ask": [101.0], "spread": [spread]},
        "portfolio_state": {"equity": 100.0, "peak_equity": 100.0},
        "regime_id": 0,
        "current_spread_pct": spread,
        "risk_db_connection": connection,
    }


def preflight_service(dispatcher, ledger):
    return ExecutionService(
        dispatcher,
        ledger,
        simulator=PreFlightSimulator(),
        risk_gate=RiskSwarmGate(),
        require_runtime_preflight=True,
    )


def policy_connection():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE scaling_policies (regime_id INTEGER PRIMARY KEY, scale_multiplier REAL NOT NULL)"
    )
    connection.execute(
        "INSERT INTO scaling_policies (regime_id, scale_multiplier) VALUES (0, 1)"
    )
    return connection


def test_runtime_preflight_requires_context_and_rejects_before_dispatch(tmp_path):
    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock()
    with ExecutionLedger(tmp_path / "execution.sqlite3") as ledger:
        service = preflight_service(dispatcher, ledger)
        denied = service.admit(intent("missing-context"), currency="GBP", price="100")

        assert denied.decision is AdmissionDecision.DENIED
        assert ledger.get_order("missing-context").state == "REJECTED"
        assert ledger.get_reservation("missing-context") is None
        with pytest.raises(Exception, match="admission"):
            service.reserve("missing-context")
        dispatcher.dispatch.assert_not_awaited()


def test_runtime_preflight_uses_components_and_rejects_swarm_gate_failure(tmp_path):
    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock()
    with ExecutionLedger(tmp_path / "execution.sqlite3") as ledger:
        connection = policy_connection()
        try:
            service = preflight_service(dispatcher, ledger)
            admitted = service.admit(
                intent("admitted-runtime"),
                currency="GBP",
                **preflight_context(connection),
            )
            blocked = service.admit(
                intent("blocked-runtime"),
                currency="GBP",
                **preflight_context(connection, spread=0.06),
            )
        finally:
            connection.close()

        assert admitted.decision is AdmissionDecision.ADMITTED
        assert admitted.final_quantity == Decimal("2")
        assert admitted.simulator_fill_price > 0
        assert blocked.decision is AdmissionDecision.DENIED
        assert ledger.get_order("blocked-runtime").state == "REJECTED"
        assert ledger.get_reservation("blocked-runtime") is None
        dispatcher.dispatch.assert_not_awaited()


def test_app_startup_uses_runtime_preflight_for_local_paper_uat(tmp_path):
    app_state = AppState()
    try:
        assert app_state.start_execution(tmp_path / "execution.sqlite3")
        proposal = app_state.create_paper_approval_check()
        admission = app_state._execution_ledger.get_admission(proposal["proposal_id"])

        assert admission.decision is AdmissionDecision.ADMITTED
        assert admission.simulator_fill_price > 0
        assert admission.risk_quantity == Decimal("1")
    finally:
        app_state.close_execution()


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
