import asyncio
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from app_context import AppState
from execution import ExecutionLedger, ExecutionService, PaperDispatcher
from execution.service import ExecutionDisabledError


def proposal(proposal_id="durable-1", **overrides):
    return {
        "proposal_id": proposal_id,
        "ticker": "VUSA",
        "action": "BUY",
        "quantity": "2.5",
        "workspace": "uk",
        "account": "invest",
        "mode": "PAPER",
        "status": "PENDING",
        **overrides,
    }


@pytest.mark.asyncio
async def test_acknowledgement_replays_after_service_and_ledger_restart(tmp_path):
    db_path = tmp_path / "execution.sqlite3"
    original = proposal()
    with ExecutionLedger(db_path) as ledger:
        first_service = ExecutionService(PaperDispatcher(), ledger)
        first_service.admit(
            original,
            price="100",
            simulator_evidence={"simulated_fill_price": "100"},
            risk_evidence={"scaled_size": "2.5"},
        )
        ledger.configure_paper_budget("invest", "GBP", "1000")
        first_service.reserve(original["proposal_id"])
        with pytest.raises(ExecutionDisabledError, match="Signed approval"):
            await first_service.approve(original)

    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock()
    with ExecutionLedger(db_path) as reopened:
        second_service = ExecutionService(dispatcher, reopened)
        with pytest.raises(ExecutionDisabledError, match="Signed approval"):
            await second_service.approve(proposal())
    dispatcher.dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_changed_intent_conflicts_after_service_restart(tmp_path):
    db_path = tmp_path / "execution.sqlite3"
    with ExecutionLedger(db_path) as ledger:
        service = ExecutionService(PaperDispatcher(), ledger)
        service.admit(
            proposal(), price="100", simulator_evidence={"simulated_fill_price": "100"},
            risk_evidence={"scaled_size": "2.5"}
        )
        ledger.configure_paper_budget("invest", "GBP", "1000")
        service.reserve("durable-1")

    with ExecutionLedger(db_path) as reopened:
        service = ExecutionService(PaperDispatcher(), reopened)
        with pytest.raises(ExecutionDisabledError, match="Signed approval"):
            await service.approve(proposal(quantity="99"))


@pytest.mark.asyncio
async def test_cross_service_claims_dispatch_once_by_database_authority(tmp_path):
    db_path = tmp_path / "execution.sqlite3"
    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock()
    with ExecutionLedger(db_path) as ledger:
        first_service = ExecutionService(dispatcher, ledger)
        first_service.admit(
            proposal(), price="100", simulator_evidence={"simulated_fill_price": "100"},
            risk_evidence={"scaled_size": "2.5"}
        )
        ledger.configure_paper_budget("invest", "GBP", "1000")
        first_service.reserve("durable-1")
        with pytest.raises(ExecutionDisabledError, match="Signed approval"):
            await first_service.approve(proposal())

    dispatcher.dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_wait_holds_no_sqlite_write_transaction(tmp_path):
    db_path = tmp_path / "execution.sqlite3"
    with ExecutionLedger(db_path) as ledger:
        service = ExecutionService(PaperDispatcher(), ledger)
        service.admit(
            proposal(), price="100", simulator_evidence={"simulated_fill_price": "100"},
            risk_evidence={"scaled_size": "2.5"}
        )
        ledger.configure_paper_budget("invest", "GBP", "1000")
        service.reserve("durable-1")
        with pytest.raises(ExecutionDisabledError, match="Signed approval"):
            await service.approve(proposal())

        observer = sqlite3.connect(db_path, timeout=0.1)
        try:
            state = observer.execute(
                "SELECT state FROM order_projection WHERE proposal_id = ?",
                ("durable-1",),
            ).fetchone()[0]
            observer.execute("BEGIN IMMEDIATE")
            observer.rollback()
        finally:
            observer.close()

        assert state == "PENDING"


def test_app_state_owns_one_local_paper_authority_and_reopens(tmp_path):
    db_path = tmp_path / "execution.sqlite3"
    first = AppState()
    second = AppState()
    try:
        assert first.start_execution(db_path) is True
        assert first.execution_authority is True
        assert first.execution_service.execution_enabled is True
        first.register_trade_proposal(proposal("state-owned"))

        assert second.start_execution(db_path) is False
        assert second.execution_authority is False
        assert second.execution_service.execution_enabled is False
    finally:
        first.close_execution()
        second.close_execution()

    replacement = AppState()
    try:
        assert replacement.start_execution(db_path) is True
        restored = replacement.get_trade_proposal("state-owned")
        assert restored is not None
        assert restored["status"] == "PENDING"
    finally:
        replacement.close_execution()
