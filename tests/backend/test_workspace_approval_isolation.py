"""Approval and kill-switch entry points require and check the workspace (ISO-01, ISO-02).

Real ledgers under tmp_path, real P-256 signatures, no mocked verifier.
"""

import ast
import base64
import inspect
import sqlite3
import uuid

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from httpx import ASGITransport, AsyncClient

import routes.ai_routes as ai_routes
from app_context import AppState, state
from execution import (
    ApprovalService,
    ExecutionLedger,
    ExecutionService,
    PaperDispatcher,
    WorkspaceMismatch,
)
from server import app
from simulation import PreFlightSimulator, RiskSwarmGate
from regime_testkit import shipped_map


def _count(ledger, table):
    connection = sqlite3.connect(f"file:{ledger.path}?mode=ro", uri=True)
    try:
        return connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        connection.close()


def _key_material():
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return private_key, public_key


def _proposal(workspace="uk", currency_account="invest"):
    return {
        "proposal_id": str(uuid.uuid4()),
        "workspace": workspace,
        "account": currency_account,
        "broker": "paper",
        "mode": "PAPER",
        "ticker": "AAPL",
        "action": "BUY",
        "quantity": "2",
        "status": "PENDING",
    }


def _admitted_service(ledger, workspace="uk"):
    """A service over ``ledger`` with one admitted, reserved proposal."""

    policy_connection = AppState._local_preflight_policy_connection()
    helper = AppState()
    helper._preflight_policy_connection = policy_connection
    service = ExecutionService(
        PaperDispatcher(),
        ledger,
        require_approval=True,
        simulator=PreFlightSimulator(),
        risk_gate=RiskSwarmGate(),
        require_runtime_preflight=True,
        regime_severity_map=shipped_map(),
    )
    proposal = _proposal(workspace)
    currency = "GBP" if workspace == "uk" else "INR"
    service.register_proposal(proposal)
    service.admit(
        proposal,
        currency=currency,
        price="100",
        **helper._local_paper_preflight(),
    )
    ledger.configure_paper_budget("invest", currency, "10000", workspace=workspace)
    service.reserve(proposal["proposal_id"])
    return service, proposal, policy_connection


@pytest.fixture
def uk_stack(tmp_path):
    ledger = ExecutionLedger(
        tmp_path / "uk.sqlite3", workspace="uk", require_approval=True
    )
    service, proposal, policy_connection = _admitted_service(ledger)
    try:
        yield service, ledger, proposal
    finally:
        ledger.close()
        policy_connection.close()


@pytest.fixture
def uk_routes(uk_stack):
    """The UK stack swapped into app state for HTTP tests."""

    service, ledger, proposal = uk_stack
    original = (
        state._execution_service,
        state._execution_ledger,
        state._preflight_policy_connection,
        state.execution_authority,
        state.trade_proposals.copy(),
    )
    state.execution_service = service
    state._execution_ledger = ledger
    state._preflight_policy_connection = AppState._local_preflight_policy_connection()
    state.execution_authority = True
    state.trade_proposals.clear()
    state.trade_proposals[proposal["proposal_id"]] = proposal
    try:
        yield service, ledger, proposal
    finally:
        state._preflight_policy_connection.close()
        (
            state._execution_service,
            state._execution_ledger,
            state._preflight_policy_connection,
            state.execution_authority,
            proposals,
        ) = original
        state.trade_proposals.clear()
        state.trade_proposals.update(proposals)


# --- Task 1: the challenge path ---


def test_service_challenge_for_another_workspace_raises_and_writes_nothing(uk_stack):
    service, ledger, proposal = uk_stack
    before = _count(ledger, "approval_challenges")

    with pytest.raises(WorkspaceMismatch):
        service.create_approval_challenge(proposal["proposal_id"], workspace="india")
    with pytest.raises(TypeError):
        service.create_approval_challenge(proposal["proposal_id"])

    assert _count(ledger, "approval_challenges") == before == 0


def test_approval_service_challenge_for_another_workspace_raises(uk_stack):
    _, ledger, proposal = uk_stack
    approval = ApprovalService(ledger)

    with pytest.raises(WorkspaceMismatch):
        approval.create_challenge(proposal["proposal_id"], workspace="india")
    with pytest.raises(TypeError):
        approval.create_challenge(proposal["proposal_id"])

    assert _count(ledger, "approval_challenges") == 0


@pytest.mark.asyncio
async def test_challenge_route_passes_the_workspace_through(uk_routes):
    service, _, proposal = uk_routes
    _, public_key = _key_material()
    token = service._approval_service.enrollment_token_path.read_text()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        enrolled = await client.post(
            "/api/ai/trade/approval/enroll",
            json={
                "public_key_x963_b64": base64.b64encode(public_key).decode(),
                "enrollment_token": token,
                "workspace": "uk",
            },
        )
        assert enrolled.status_code == 200, enrolled.text
        response = await client.post(
            "/api/ai/trade/approval/challenge",
            json={"proposal_id": proposal["proposal_id"], "workspace": "uk"},
        )

    assert response.status_code == 200, response.text


# --- every route call into the approval services names the workspace ---

_WORKSPACE_REQUIRED_CALLS = {
    "enroll_approval_key",
    "approval_key_id",
    "create_approval_challenge",
    "verify_approval_signature_for_uat",
    "approve_signed",
    "engage_workspace_control",
    "create_control_challenge",
    "clear_workspace_control",
}


def test_every_route_call_into_the_approval_services_passes_workspace():
    tree = ast.parse(inspect.getsource(ai_routes))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _WORKSPACE_REQUIRED_CALLS
    ]

    assert {call.func.attr for call in calls} >= {
        "enroll_approval_key",
        "approval_key_id",
        "create_approval_challenge",
        "verify_approval_signature_for_uat",
        "approve_signed",
    }
    for call in calls:
        keywords = {keyword.arg for keyword in call.keywords}
        assert "workspace" in keywords, f"{call.func.attr} at line {call.lineno}"
