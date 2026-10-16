"""Every /api/ai/trade route names a workspace and refuses the wrong one (ISO-01, ISO-02).

A request whose workspace differs from the open ledger's pin must be refused with
HTTP 409 before any service call, and must leave the ledger file untouched. Rows
are read back through a mode=ro connection, not through the service under test.
"""

import base64
import sqlite3
import uuid

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from httpx import ASGITransport, AsyncClient

from app_context import AppState, state
from execution import (
    ExecutionLedger,
    ExecutionService,
    LedgerReader,
    PaperDispatcher,
)
from server import app
from simulation import PreFlightSimulator, RiskSwarmGate
from regime_testkit import shipped_map

MISMATCH_DETAIL = "Workspace does not match the open execution ledger"
REJECTION_UNAVAILABLE = "Trade rejection is unavailable: no execution ledger is open"
WATCHED_TABLES = (
    "approval_keys",
    "approval_challenges",
    "order_intents",
    "dispatch_attempts",
    "execution_approvals",
    "paper_budgets",
    "execution_events",
)


@pytest.fixture
def uk_routes(tmp_path):
    """A UK ledger opened in app state with one admitted, reserved UK proposal."""

    original_service = state._execution_service
    original_ledger = state._execution_ledger
    original_policy_connection = state._preflight_policy_connection
    original_proposals = state.trade_proposals.copy()
    original_authority = state.execution_authority
    ledger = ExecutionLedger(
        tmp_path / "execution.sqlite3", workspace="uk", require_approval=True
    )
    policy_connection = AppState._local_preflight_policy_connection()
    service = ExecutionService(
        PaperDispatcher(),
        ledger,
        require_approval=True,
        simulator=PreFlightSimulator(),
        risk_gate=RiskSwarmGate(),
        require_runtime_preflight=True,
        regime_severity_map=shipped_map(),
    )
    state.execution_service = service
    state._execution_ledger = ledger
    state._preflight_policy_connection = policy_connection
    state.execution_authority = True
    state.trade_proposals.clear()
    proposal_id = str(uuid.uuid4())
    proposal = {
        "proposal_id": proposal_id,
        "workspace": "uk",
        "account": "invest",
        "broker": "paper",
        "mode": "PAPER",
        "ticker": "AAPL",
        "action": "BUY",
        "quantity": "2",
        "status": "PENDING",
    }
    service.register_proposal(proposal)
    service.admit(
        proposal,
        currency="GBP",
        price="100",
        **state._local_paper_preflight(),
    )
    ledger.configure_paper_budget("invest", "GBP", "10000", workspace="uk")
    service.reserve(proposal_id)
    state.trade_proposals[proposal_id] = proposal
    try:
        yield service, ledger, proposal
    finally:
        ledger.close()
        policy_connection.close()
        state._execution_service = original_service
        state._execution_ledger = original_ledger
        state._preflight_policy_connection = original_policy_connection
        state.execution_authority = original_authority
        state.trade_proposals.clear()
        state.trade_proposals.update(original_proposals)


def _key_material():
    private_key = ec.generate_private_key(ec.SECP256R1())
    public_key = private_key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )
    return private_key, public_key


def _snapshot(ledger):
    """Row counts and order states read through a separate read-only connection."""

    connection = sqlite3.connect(f"file:{ledger.path}?mode=ro", uri=True)
    try:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in WATCHED_TABLES
        }
        counts["order_states"] = tuple(
            connection.execute(
                "SELECT proposal_id, state FROM order_projection ORDER BY proposal_id"
            ).fetchall()
        )
        return counts
    finally:
        connection.close()


async def _post(endpoint, body):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(endpoint, json=body)


async def _get(endpoint):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.get(endpoint)


async def _enroll(service):
    private_key, public_key = _key_material()
    token = service._approval_service.enrollment_token_path.read_text()
    enrolled = await _post(
        "/api/ai/trade/approval/enroll",
        {
            "public_key_x963_b64": base64.b64encode(public_key).decode(),
            "enrollment_token": token,
            "workspace": "uk",
        },
    )
    assert enrolled.status_code == 200, enrolled.text
    return private_key


def _signature_body(proposal_id, challenge_id, workspace="uk"):
    return {
        "proposal_id": proposal_id,
        "challenge_id": challenge_id,
        "signature_der_b64": base64.b64encode(b"\x30" * 16).decode(),
        "workspace": workspace,
    }


# --- challenge (the tracer) ---


@pytest.mark.asyncio
async def test_challenge_for_the_open_workspace_is_issued(uk_routes):
    service, _, proposal = uk_routes
    await _enroll(service)

    response = await _post(
        "/api/ai/trade/approval/challenge",
        {"proposal_id": proposal["proposal_id"], "workspace": "uk"},
    )

    assert response.status_code == 200, response.text
    payload = base64.b64decode(response.json()["signed_payload_b64"])
    assert b'"workspace":"uk"' in payload.replace(b" ", b"")


@pytest.mark.asyncio
async def test_challenge_naming_another_workspace_is_409_and_writes_nothing(uk_routes):
    service, ledger, proposal = uk_routes
    await _enroll(service)
    before = _snapshot(ledger)

    response = await _post(
        "/api/ai/trade/approval/challenge",
        {"proposal_id": proposal["proposal_id"], "workspace": "india"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == MISMATCH_DETAIL
    after = _snapshot(ledger)
    assert after["approval_challenges"] == before["approval_challenges"] == 0
    assert after == before


@pytest.mark.asyncio
@pytest.mark.parametrize("workspace", [None, "us", "UK", ""])
async def test_challenge_without_a_valid_workspace_is_422(uk_routes, workspace):
    _, _, proposal = uk_routes
    body = {"proposal_id": proposal["proposal_id"]}
    if workspace is not None:
        body["workspace"] = workspace

    response = await _post("/api/ai/trade/approval/challenge", body)

    assert response.status_code == 422


# --- every other route: 422 without workspace, 409 with the other one ---


@pytest.mark.asyncio
async def test_enroll_requires_workspace_and_refuses_the_other(uk_routes):
    service, ledger, _ = uk_routes
    _, public_key = _key_material()
    token = service._approval_service.enrollment_token_path.read_text()
    body = {
        "public_key_x963_b64": base64.b64encode(public_key).decode(),
        "enrollment_token": token,
    }
    before = _snapshot(ledger)

    missing = await _post("/api/ai/trade/approval/enroll", body)
    mismatch = await _post(
        "/api/ai/trade/approval/enroll", {**body, "workspace": "india"}
    )

    assert missing.status_code == 422
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"] == MISMATCH_DETAIL
    assert _snapshot(ledger) == before
    assert before["approval_keys"] == 0
    # The one-time token was not consumed by the refused request.
    assert service._approval_service.enrollment_token_path.exists()


@pytest.mark.asyncio
async def test_status_requires_workspace_and_reports_the_open_one(uk_routes):
    missing = await _get("/api/ai/trade/approval/status")
    ok = await _get("/api/ai/trade/approval/status?workspace=uk")
    mismatch = await _get("/api/ai/trade/approval/status?workspace=india")
    unknown = await _get("/api/ai/trade/approval/status?workspace=us")

    assert missing.status_code == 422
    assert unknown.status_code == 422
    assert ok.status_code == 200
    assert ok.json() == {
        "mode": "paper",
        "enrolled": False,
        "key_id": None,
        "workspace": "uk",
    }
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"] == MISMATCH_DETAIL


@pytest.mark.asyncio
async def test_status_with_no_open_ledger_is_disabled_and_workspace_null():
    original_service = state._execution_service
    original_ledger = state._execution_ledger
    original_authority = state.execution_authority
    state._execution_ledger = None
    state.execution_service = ExecutionService()
    state.execution_authority = False
    try:
        response = await _get("/api/ai/trade/approval/status?workspace=uk")
    finally:
        state._execution_service = original_service
        state._execution_ledger = original_ledger
        state.execution_authority = original_authority

    assert response.status_code == 200
    assert response.json() == {
        "mode": "disabled",
        "enrolled": False,
        "key_id": None,
        "workspace": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint",
    ["/api/ai/trade/approval/uat-proposal", "/api/ai/trade/requote/uat-proposal"],
)
async def test_uat_proposal_bodies_are_strict_and_workspace_checked(uk_routes, endpoint):
    _, ledger, _ = uk_routes
    before = _snapshot(ledger)

    empty = await _post(endpoint, {})
    extra = await _post(endpoint, {"workspace": "uk", "account": "invest"})
    mismatch = await _post(endpoint, {"workspace": "india"})

    assert empty.status_code == 422
    assert extra.status_code == 422
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"] == MISMATCH_DETAIL
    assert _snapshot(ledger) == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "endpoint",
    ["/api/ai/trade/approval/complete", "/api/ai/trade/requote/uat/verify"],
)
async def test_signature_routes_require_workspace_and_refuse_the_other(uk_routes, endpoint):
    service, ledger, proposal = uk_routes
    await _enroll(service)
    challenge = (
        await _post(
            "/api/ai/trade/approval/challenge",
            {"proposal_id": proposal["proposal_id"], "workspace": "uk"},
        )
    ).json()
    before = _snapshot(ledger)
    body = _signature_body(proposal["proposal_id"], challenge["challenge_id"])
    del body["workspace"]

    missing = await _post(endpoint, body)
    mismatch = await _post(endpoint, {**body, "workspace": "india"})

    assert missing.status_code == 422
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"] == MISMATCH_DETAIL
    assert _snapshot(ledger) == before
    assert before["dispatch_attempts"] == 0
    assert before["execution_approvals"] == 0


@pytest.mark.asyncio
async def test_approve_requires_workspace_and_refuses_the_other(uk_routes):
    _, ledger, proposal = uk_routes
    before = _snapshot(ledger)
    body = {"proposal_id": proposal["proposal_id"], "decision": "APPROVED"}

    missing = await _post("/api/ai/trade/approve", body)
    mismatch = await _post("/api/ai/trade/approve", {**body, "workspace": "india"})

    assert missing.status_code == 422
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"] == MISMATCH_DETAIL
    assert _snapshot(ledger) == before
    assert before["dispatch_attempts"] == 0


@pytest.mark.asyncio
async def test_reject_requires_workspace_and_refuses_the_other(uk_routes):
    _, ledger, proposal = uk_routes
    before = _snapshot(ledger)
    body = {"proposal_id": proposal["proposal_id"], "decision": "REJECTED"}

    missing = await _post("/api/ai/trade/reject", body)
    mismatch = await _post("/api/ai/trade/reject", {**body, "workspace": "india"})

    assert missing.status_code == 422
    assert mismatch.status_code == 409
    assert mismatch.json()["detail"] == MISMATCH_DETAIL
    assert _snapshot(ledger) == before
    assert dict(before["order_states"])[proposal["proposal_id"]] == "PENDING"


@pytest.mark.asyncio
async def test_reject_with_open_uk_ledger_records_rejection(uk_routes):
    _, ledger, proposal = uk_routes

    response = await _post(
        "/api/ai/trade/reject",
        {
            "proposal_id": proposal["proposal_id"],
            "decision": "REJECTED",
            "workspace": "uk",
            "notes": "Too risky right now",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"
    with LedgerReader(ledger.path, workspace="uk") as reader:
        assert reader.get_order(proposal["proposal_id"]).state == "REJECTED"


@pytest.mark.asyncio
async def test_unknown_workspace_value_is_422_on_every_body_route(uk_routes):
    _, _, proposal = uk_routes
    pid = proposal["proposal_id"]
    cases = {
        "/api/ai/trade/approve": {"proposal_id": pid, "decision": "APPROVED"},
        "/api/ai/trade/reject": {"proposal_id": pid, "decision": "REJECTED"},
        "/api/ai/trade/approval/uat-proposal": {},
        "/api/ai/trade/requote/uat-proposal": {},
    }
    for endpoint, body in cases.items():
        response = await _post(endpoint, {**body, "workspace": "us"})
        assert response.status_code == 422, endpoint
