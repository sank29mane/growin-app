"""Paper budget, position and pending-reservation methods name their workspace (ISO-01, ISO-02).

UK-only UAT builders refuse an India ledger before any write; the India paper
preparation stamps its workspace from the open ledger (decision 2). AppState
objects are built by hand so these tests do not depend on start_execution's
signature.
"""

import sqlite3

import pytest
from httpx import ASGITransport, AsyncClient

from app_context import AppState, state
from execution import (
    ExecutionLedger,
    ExecutionService,
    LedgerError,
    PaperDispatcher,
    WorkspaceMismatch,
)
from server import app
from simulation import PreFlightSimulator, RiskSwarmGate

UAT_ACCOUNTS = ("paper-uat-v2", "paper-uat", "paper-requote-uat-v1")


def _open_state(path, workspace) -> AppState:
    """An AppState holding an open, pinned ledger and a paper-only service."""

    app_state = AppState()
    ledger = ExecutionLedger(path, workspace=workspace, require_approval=True)
    app_state._execution_ledger = ledger
    app_state._preflight_policy_connection = AppState._local_preflight_policy_connection()
    app_state._execution_service = ExecutionService(
        PaperDispatcher(),
        ledger,
        require_approval=True,
        simulator=PreFlightSimulator(),
        risk_gate=RiskSwarmGate(),
        require_runtime_preflight=True,
    )
    app_state.execution_authority = True
    return app_state


def _close(app_state: AppState) -> None:
    app_state._preflight_policy_connection.close()
    app_state._execution_ledger.close()


def _rows(path, sql, parameters=()):
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return connection.execute(sql, parameters).fetchall()
    finally:
        connection.close()


def _count(path, table):
    return _rows(path, f"SELECT COUNT(*) FROM {table}")[0][0]


# --- the four ledger methods ---


def test_scoped_methods_require_the_workspace_and_refuse_the_other(tmp_path):
    with ExecutionLedger(tmp_path / "uk.sqlite3", workspace="uk") as ledger:
        calls = {
            "configure_paper_budget": lambda **kw: ledger.configure_paper_budget(
                "invest", "GBP", "100", **kw
            ),
            "get_paper_budget": lambda **kw: ledger.get_paper_budget("invest", "GBP", **kw),
            "find_active_pending_reservation": lambda **kw: (
                ledger.find_active_pending_reservation("invest", **kw)
            ),
            "get_paper_position": lambda **kw: ledger.get_paper_position(
                "invest", "GBP", "VUSA", **kw
            ),
        }
        for name, call in calls.items():
            with pytest.raises(TypeError):
                call()
            with pytest.raises(WorkspaceMismatch):
                call(workspace="india")
            assert name

        assert _count(ledger.path, "paper_budgets") == 0
        assert ledger.configure_paper_budget(
            "invest", "GBP", "100", workspace="uk"
        ).amount == 100
        assert ledger.get_paper_budget("invest", "GBP", workspace="uk") is not None
        assert ledger.find_active_pending_reservation("invest", workspace="uk") is None
        assert ledger.get_paper_position("invest", "GBP", "VUSA", workspace="uk") is None
        assert _count(ledger.path, "paper_budgets") == 1


# --- UK-only UAT builders on an India ledger (the tracer) ---


@pytest.mark.parametrize("builder", ["create_paper_approval_check", "create_paper_requote_check"])
def test_uat_builders_refuse_an_india_ledger_and_write_nothing(tmp_path, builder):
    india = _open_state(tmp_path / "india.sqlite3", "india")
    try:
        with pytest.raises(WorkspaceMismatch):
            getattr(india, builder)()

        path = india._execution_ledger.path
        assert _rows(
            path,
            "SELECT COUNT(*) FROM paper_budgets WHERE account IN (?, ?, ?)",
            UAT_ACCOUNTS,
        ) == [(0,)]
        assert _count(path, "paper_budgets") == 0
        assert _count(path, "order_intents") == 0
    finally:
        _close(india)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint", ["/api/ai/trade/approval/uat-proposal", "/api/ai/trade/requote/uat-proposal"]
)
async def test_uat_routes_are_409_on_an_india_ledger(tmp_path, endpoint):
    india = _open_state(tmp_path / "india.sqlite3", "india")
    original = (
        state._execution_service,
        state._execution_ledger,
        state._preflight_policy_connection,
        state.execution_authority,
    )
    state._execution_service = india._execution_service
    state._execution_ledger = india._execution_ledger
    state._preflight_policy_connection = india._preflight_policy_connection
    state.execution_authority = True
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(endpoint, json={"workspace": "india"})

        assert response.status_code == 409
        assert _count(india._execution_ledger.path, "paper_budgets") == 0
        assert _count(india._execution_ledger.path, "order_intents") == 0
    finally:
        (
            state._execution_service,
            state._execution_ledger,
            state._preflight_policy_connection,
            state.execution_authority,
        ) = original
        _close(india)


def test_uat_builders_still_work_on_a_uk_ledger(tmp_path):
    uk = _open_state(tmp_path / "uk.sqlite3", "uk")
    try:
        approval = uk.create_paper_approval_check()
        requote = uk.create_paper_requote_check()

        assert approval["workspace"] == "uk"
        assert requote["workspace"] == "uk"
        assert uk._execution_ledger.get_paper_budget(
            "paper-uat-v2", "GBP", workspace="uk"
        ) is not None
    finally:
        _close(uk)


# --- India paper preparation: stamped from the ledger (decision 2) ---


def test_india_preparation_on_a_uk_ledger_is_refused_and_writes_nothing(tmp_path):
    uk = _open_state(tmp_path / "uk.sqlite3", "uk")
    try:
        with pytest.raises(LedgerError, match="India market admission requires"):
            uk.prepare_india_paper_local(symbol="RELIANCE", quantity="1")

        assert _count(uk._execution_ledger.path, "order_intents") == 0
    finally:
        _close(uk)


def test_india_preparation_with_no_open_ledger_raises():
    with pytest.raises(LedgerError, match="local paper execution authority is unavailable"):
        AppState().prepare_india_paper_local(symbol="RELIANCE", quantity="1")


def test_india_preparation_stamps_the_workspace_from_the_open_ledger(tmp_path):
    india = _open_state(tmp_path / "india.sqlite3", "india")
    try:
        proposal, _ = india.prepare_india_paper_local(symbol="RELIANCE", quantity="1")

        assert proposal["workspace"] == "india"
        row = _rows(
            india._execution_ledger.path,
            "SELECT canonical_json FROM order_intents WHERE proposal_id = ?",
            (proposal["proposal_id"],),
        )
        assert '"workspace":"india"' in row[0][0].replace(" ", "")
    finally:
        _close(india)


# --- startup: an unsupported workspace leaves execution disabled ---


@pytest.mark.parametrize("workspace", ["us", "", None, 7])
@pytest.mark.parametrize("use_path", [False, True])
def test_start_execution_with_a_bad_workspace_stays_disabled(tmp_path, workspace, use_path):
    app_state = AppState()
    path = tmp_path / "execution.sqlite3" if use_path else None

    started = app_state.start_execution(path, workspace=workspace)

    assert started is False
    assert app_state.execution_authority is False
    assert app_state.execution_startup_error
    assert app_state.execution_service.execution_enabled is False
    assert app_state._execution_ledger is None
    if use_path:
        assert not (tmp_path / "execution.sqlite3").exists()
