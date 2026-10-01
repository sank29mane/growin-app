import base64
import json
import uuid

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from httpx import ASGITransport, AsyncClient

from app_context import state
from execution import ExecutionLedger, ExecutionService, PaperDispatcher
from server import app


@pytest.fixture
def signed_execution(tmp_path):
    original_service = state._execution_service
    original_proposals = state.trade_proposals.copy()
    ledger = ExecutionLedger(
        tmp_path / "execution.sqlite3", workspace="uk", require_approval=True
    )
    service = ExecutionService(
        PaperDispatcher(), ledger, require_approval=True
    )
    state.execution_service = service
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
        simulator_evidence={"simulated_fill_price": "100"},
        risk_evidence={"scaled_size": "2"},
    )
    ledger.configure_paper_budget("invest", "GBP", "10000")
    service.reserve(proposal_id)
    state.trade_proposals[proposal_id] = proposal
    yield service, ledger, proposal
    ledger.close()
    state._execution_service = original_service
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
