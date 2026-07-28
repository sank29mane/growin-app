import base64
import json
import uuid

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from httpx import ASGITransport, AsyncClient

from app_context import state
from execution import ApprovalConflict, ExecutionLedger, ExecutionService, OrderAck, PaperDispatcher
from simulation import PreFlightSimulator, RiskSwarmGate
from server import app


@pytest.fixture
def signed_execution(tmp_path):
    original_service = state._execution_service
    original_ledger = state._execution_ledger
    original_policy_connection = state._preflight_policy_connection
    original_proposals = state.trade_proposals.copy()
    original_authority = state.execution_authority
    ledger = ExecutionLedger(
        tmp_path / "execution.sqlite3", workspace="uk", require_approval=True
    )
    policy_connection = state._local_preflight_policy_connection()
    service = ExecutionService(
        PaperDispatcher(),
        ledger,
        require_approval=True,
        simulator=PreFlightSimulator(),
        risk_gate=RiskSwarmGate(),
        require_runtime_preflight=True,
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
    ledger.configure_paper_budget("invest", "GBP", "10000")
    service.reserve(proposal_id)
    state.trade_proposals[proposal_id] = proposal
    yield service, ledger, proposal
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


@pytest.mark.asyncio
async def test_enrollment_requires_local_one_time_token(signed_execution):
    service, ledger, _ = signed_execution
    _, public_key = _key_material()

    rejected = await _post(
        "/api/ai/trade/approval/enroll",
        {
            "public_key_x963_b64": base64.b64encode(public_key).decode(),
            "enrollment_token": "not-the-local-bootstrap-token",
        },
    )

    assert rejected.status_code == 403
    assert ledger.get_approval_key() is None
    assert service._approval_service.enrollment_token_path.exists()


@pytest.mark.asyncio
async def test_approval_status_and_explicit_uat_proposal_are_paper_only(signed_execution):
    service, ledger, _ = signed_execution

    status = await _get("/api/ai/trade/approval/status")
    assert status.status_code == 200
    assert status.json() == {"mode": "paper", "enrolled": False, "key_id": None}

    response = await _post("/api/ai/trade/approval/uat-proposal", {})
    assert response.status_code == 200
    proposal = response.json()
    assert set(proposal) == {
        "proposal_id", "ticker", "action", "quantity", "reasoning", "status"
    }
    assert proposal["quantity"] == 1.0
    assert proposal["ticker"] == "PAPER-UAT"
    durable = service.get_proposal(proposal["proposal_id"])
    assert durable["broker"] == "paper"
    assert durable["mode"] == "PAPER"
    assert durable["account"] == "paper-uat-v2"
    assert durable["status"] == "PENDING"
    assert ledger.get_admission(proposal["proposal_id"]).decision.value == "ADMITTED"

    resumed = (await _post("/api/ai/trade/approval/uat-proposal", {})).json()
    assert resumed["proposal_id"] == proposal["proposal_id"]


@pytest.mark.asyncio
async def test_completed_uat_check_releases_only_its_bounded_reservation(signed_execution):
    service, ledger, _ = signed_execution
    private_key, public_key = _key_material()
    token = service._approval_service.enrollment_token_path.read_text()
    enrolled = await _post(
        "/api/ai/trade/approval/enroll",
        {
            "public_key_x963_b64": base64.b64encode(public_key).decode(),
            "enrollment_token": token,
        },
    )
    assert enrolled.status_code == 200

    proposal = (await _post("/api/ai/trade/approval/uat-proposal", {})).json()
    challenge = await _post(
        "/api/ai/trade/approval/challenge", {"proposal_id": proposal["proposal_id"]}
    )
    assert challenge.status_code == 200
    challenge_body = challenge.json()
    signed = private_key.sign(
        base64.b64decode(challenge_body["signed_payload_b64"]), ec.ECDSA(hashes.SHA256())
    )
    completed = await _post(
        "/api/ai/trade/approval/complete",
        {
            "proposal_id": proposal["proposal_id"],
            "challenge_id": challenge_body["challenge_id"],
            "signature_der_b64": base64.b64encode(signed).decode(),
        },
    )
    assert completed.status_code == 200
    assert "reservation released" in completed.json()["message"]
    assert ledger.get_reservation(proposal["proposal_id"]).state == "SETTLED"
    assert ledger.get_paper_budget("paper-uat-v2", "GBP").available == 1

    next_check = await _post("/api/ai/trade/approval/uat-proposal", {})
    assert next_check.status_code == 200


@pytest.mark.asyncio
async def test_requote_uat_verifies_fresh_limit_signature_without_dispatch(signed_execution):
    service, ledger, _ = signed_execution
    private_key, public_key = _key_material()
    token = service._approval_service.enrollment_token_path.read_text()
    enrolled = await _post(
        "/api/ai/trade/approval/enroll",
        {
            "public_key_x963_b64": base64.b64encode(public_key).decode(),
            "enrollment_token": token,
        },
    )
    assert enrolled.status_code == 200

    created = await _post("/api/ai/trade/requote/uat-proposal", {})
    assert created.status_code == 200, created.text
    proposal = created.json()
    assert proposal["ticker"] == "PAPER-REQUOTE-UAT"
    assert proposal["status"] == "PENDING"
    durable = service.get_proposal(proposal["proposal_id"])
    assert durable["account"] == "paper-requote-uat-v1"
    assert durable["broker"] == "paper"
    assert durable["mode"] == "PAPER"
    assert durable["order_type"] == "LIMIT"
    assert durable["limit_price"] == "1"
    assert durable["replaces_proposal_id"]
    assert durable["requote_id"]
    parent = ledger.get_order(durable["replaces_proposal_id"])
    assert parent is not None
    assert parent.state == "CANCELLED"
    assert ledger.list_attempts(parent.proposal_id) == []
    assert ledger.get_reservation(proposal["proposal_id"]).state == "ACTIVE"

    challenge = await _post(
        "/api/ai/trade/approval/challenge", {"proposal_id": proposal["proposal_id"]}
    )
    assert challenge.status_code == 200
    challenge_body = challenge.json()
    payload = json.loads(base64.b64decode(challenge_body["signed_payload_b64"]))
    assert payload["order_type"] == "LIMIT"
    assert payload["limit_price"] == "1"
    assert payload["replaces_proposal_id"] == durable["replaces_proposal_id"]
    assert payload["requote_id"] == durable["requote_id"]

    signature = private_key.sign(
        base64.b64decode(challenge_body["signed_payload_b64"]), ec.ECDSA(hashes.SHA256())
    )
    verified = await _post(
        "/api/ai/trade/requote/uat/verify",
        {
            "proposal_id": proposal["proposal_id"],
            "challenge_id": challenge_body["challenge_id"],
            "signature_der_b64": base64.b64encode(signature).decode(),
        },
    )
    assert verified.status_code == 200
    assert "No order was dispatched" in verified.json()["message"]
    assert ledger.list_attempts(proposal["proposal_id"]) == []
    assert ledger.get_order(proposal["proposal_id"]).state == "PENDING"

    blocked_dispatch = await _post(
        "/api/ai/trade/approval/complete",
        {
            "proposal_id": proposal["proposal_id"],
            "challenge_id": challenge_body["challenge_id"],
            "signature_der_b64": base64.b64encode(signature).decode(),
        },
    )
    assert blocked_dispatch.status_code == 409
    assert ledger.list_attempts(proposal["proposal_id"]) == []


def test_local_requote_fixture_acknowledgement_rejects_normal_accounts(signed_execution):
    _, ledger, proposal = signed_execution

    with pytest.raises(ApprovalConflict, match="not authorized"):
        ledger.acknowledge_local_requote_fixture(
            proposal["proposal_id"],
            OrderAck(
                proposal_id=proposal["proposal_id"],
                broker="paper",
                broker_order_id="must-not-be-created",
            ),
        )
    assert ledger.get_order(proposal["proposal_id"]).state == "PENDING"


@pytest.mark.asyncio
async def test_signed_route_executes_exact_challenge_once(signed_execution):
    service, ledger, proposal = signed_execution
    private_key, public_key = _key_material()
    token = service._approval_service.enrollment_token_path.read_text()

    enrolled = await _post(
        "/api/ai/trade/approval/enroll",
        {
            "public_key_x963_b64": base64.b64encode(public_key).decode(),
            "enrollment_token": token,
        },
    )
    assert enrolled.status_code == 200
    assert not service._approval_service.enrollment_token_path.exists()

    unsigned = await _post(
        "/api/ai/trade/approve",
        {"proposal_id": proposal["proposal_id"], "decision": "APPROVED"},
    )
    assert unsigned.status_code == 503

    challenge = await _post(
        "/api/ai/trade/approval/challenge",
        {"proposal_id": proposal["proposal_id"]},
    )
    assert challenge.status_code == 200
    challenge_body = challenge.json()
    payload = base64.b64decode(challenge_body["signed_payload_b64"], validate=True)
    payload_json = json.loads(payload)
    assert payload_json["proposal_id"] == proposal["proposal_id"]
    assert payload_json["mode"] == "PAPER"
    assert payload_json["purpose"] == "growin.execution.dispatch"

    signature = private_key.sign(payload, ec.ECDSA(hashes.SHA256()))
    completed = await _post(
        "/api/ai/trade/approval/complete",
        {
            "proposal_id": proposal["proposal_id"],
            "challenge_id": challenge_body["challenge_id"],
            "signature_der_b64": base64.b64encode(signature).decode(),
        },
    )
    assert completed.status_code == 200
    assert completed.json()["execution_details"]["broker"] == "paper"
    assert len(ledger.list_attempts(proposal["proposal_id"])) == 1

    replay = await _post(
        "/api/ai/trade/approval/complete",
        {
            "proposal_id": proposal["proposal_id"],
            "challenge_id": challenge_body["challenge_id"],
            "signature_der_b64": base64.b64encode(signature).decode(),
        },
    )
    assert replay.status_code == 200
    assert replay.json()["execution_details"]["idempotent_replay"] is True
    assert len(ledger.list_attempts(proposal["proposal_id"])) == 1


@pytest.mark.asyncio
async def test_invalid_signature_does_not_consume_challenge(signed_execution):
    service, ledger, proposal = signed_execution
    private_key, public_key = _key_material()
    other_key, _ = _key_material()
    token = service._approval_service.enrollment_token_path.read_text()
    await _post(
        "/api/ai/trade/approval/enroll",
        {
            "public_key_x963_b64": base64.b64encode(public_key).decode(),
            "enrollment_token": token,
        },
    )
    challenge = (
        await _post(
            "/api/ai/trade/approval/challenge",
            {"proposal_id": proposal["proposal_id"]},
        )
    ).json()
    payload = base64.b64decode(challenge["signed_payload_b64"], validate=True)

    bad_signature = other_key.sign(payload, ec.ECDSA(hashes.SHA256()))
    rejected = await _post(
        "/api/ai/trade/approval/complete",
        {
            "proposal_id": proposal["proposal_id"],
            "challenge_id": challenge["challenge_id"],
            "signature_der_b64": base64.b64encode(bad_signature).decode(),
        },
    )
    assert rejected.status_code == 403
    assert ledger.list_attempts(proposal["proposal_id"]) == []

    good_signature = private_key.sign(payload, ec.ECDSA(hashes.SHA256()))
    accepted = await _post(
        "/api/ai/trade/approval/complete",
        {
            "proposal_id": proposal["proposal_id"],
            "challenge_id": challenge["challenge_id"],
            "signature_der_b64": base64.b64encode(good_signature).decode(),
        },
    )
    assert accepted.status_code == 200
