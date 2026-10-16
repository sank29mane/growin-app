import asyncio
import base64
import uuid
from datetime import datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

from regime_testkit import bound_admit, gated
import pytest
from httpx import ASGITransport, AsyncClient

from app_context import state
from execution import (
    ApprovalService,
    BrokerExecutionError,
    BrokerOutcomeUnknownError,
    ExecutionConflictError,
    ExecutionLedger,
    ExecutionService,
    OrderAck,
)
from server import app
from venue_seam_testkit import enroll, private_key, sign


@pytest.fixture(autouse=True)
def reset_execution_state():
    original_service = state._execution_service
    state.trade_proposals.clear()
    state.execution_service = ExecutionService(**gated())
    yield
    state.trade_proposals.clear()
    state._execution_service = original_service


def add_proposal(**overrides):
    proposal_id = overrides.pop("proposal_id", str(uuid.uuid4()))
    proposal = {
        "proposal_id": proposal_id,
        "ticker": "TQQQ",
        "action": "BUY",
        "quantity": 10.5,
        "reasoning": "NPU High-Velocity Signal",
        "status": "PENDING",
        "timestamp": datetime.now().timestamp(),
        **overrides,
    }
    state.trade_proposals[proposal_id] = proposal
    return proposal


async def post_approval(proposal_id: str, decision: str = "APPROVED"):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(
            "/api/ai/trade/approve",
            json={"proposal_id": proposal_id, "decision": decision, "workspace": "uk"},
        )


@pytest.mark.asyncio
async def test_approve_trade_is_fail_closed_by_default():
    proposal = add_proposal()

    response = await post_approval(proposal["proposal_id"])

    assert response.status_code == 503
    assert response.json()["detail"] == "Broker execution is currently disabled"
    assert proposal["status"] == "PENDING"


class ScriptedDispatcher:
    """A test-local recording dispatcher. It replaces the deleted MCP dispatcher.

    ``outcome`` is an OrderAck to return, or an exception to raise; ``gate`` is
    an optional (started, release) pair of asyncio events that holds dispatch
    open so concurrency can be observed.
    """

    def __init__(self, outcome, gate=None):
        self.outcome = outcome
        self.gate = gate
        self.intents = []

    async def dispatch(self, intent):
        self.intents.append(intent)
        if self.gate is not None:
            started, release = self.gate
            started.set()
            await release.wait()
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome.model_copy(update={"proposal_id": intent.proposal_id})


def _ack(order_id: str) -> OrderAck:
    return OrderAck(proposal_id="placeholder", broker="paper", broker_order_id=order_id)


@pytest.fixture
def signed_stack(tmp_path):
    """A real ledger and signed-approval service behind the routes, with a swappable dispatcher."""

    saved = (state._execution_ledger, state.execution_authority)
    ledger = ExecutionLedger(tmp_path / "hitl.sqlite3", workspace="uk", require_approval=True)
    approval = ApprovalService(ledger)
    key = private_key()
    enroll(approval, key)
    ledger.configure_paper_budget("invest", "GBP", "10000", workspace="uk")

    class Stack:
        def __init__(self):
            self.ledger = ledger
            self.key = key

        def install(self, dispatcher):
            state._execution_ledger = ledger
            state.execution_authority = True
            state.execution_service = ExecutionService(
                dispatcher, ledger, require_approval=True, approval_service=approval, **gated(),
            )
            return state.execution_service

        def add_admitted_proposal(self, **overrides):
            proposal = durable_proposal(**overrides)
            service = state.execution_service
            service.register_proposal(proposal)
            service.admit(
                proposal,
                currency="GBP",
                price="100",
                simulator_evidence={"simulated_fill_price": "100"},
                risk_evidence={"scaled_size": str(proposal["quantity"])}, **bound_admit(),
            )
            service.reserve(proposal["proposal_id"])
            state.trade_proposals[proposal["proposal_id"]] = proposal
            return proposal

    yield Stack()
    ledger.close()
    state._execution_ledger, state.execution_authority = saved


def durable_proposal(**overrides):
    proposal_id = overrides.pop("proposal_id", str(uuid.uuid4()))
    return {
        "proposal_id": proposal_id,
        "workspace": "uk",
        "account": "invest",
        "broker": "paper",
        "mode": "PAPER",
        "ticker": "TQQQ",
        "action": "BUY",
        "quantity": "10.5",
        "reasoning": "NPU High-Velocity Signal",
        "status": "PENDING",
        **overrides,
    }


async def _sign_challenge(client, stack, proposal_id):
    challenge = await client.post(
        "/api/ai/trade/approval/challenge",
        json={"proposal_id": proposal_id, "workspace": "uk"},
    )
    assert challenge.status_code == 200, challenge.text
    body = challenge.json()
    signature = sign(stack.key, base64.b64decode(body["signed_payload_b64"]))
    return body["challenge_id"], base64.b64encode(signature).decode("ascii")


async def _complete(client, proposal_id, challenge_id, signature_b64):
    return await client.post(
        "/api/ai/trade/approval/complete",
        json={
            "proposal_id": proposal_id,
            "challenge_id": challenge_id,
            "signature_der_b64": signature_b64,
            "workspace": "uk",
        },
    )


def _client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
async def test_signed_approval_dispatches_once_and_returns_the_broker_ack(signed_stack):
    """Former test_approve_trade_success_uses_canonical_t212_contract."""
    dispatcher = ScriptedDispatcher(_ack("T212-123"))
    signed_stack.install(dispatcher)
    proposal = signed_stack.add_admitted_proposal()
    pid = proposal["proposal_id"]

    async with _client() as client:
        challenge_id, signature = await _sign_challenge(client, signed_stack, pid)
        response = await _complete(client, pid, challenge_id, signature)

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "success"
    assert data["execution_details"]["broker_order_id"] == "T212-123"
    assert signed_stack.ledger.get_order(pid).state == "ACKNOWLEDGED"
    assert state.get_trade_proposal(pid)["status"] == "ACKNOWLEDGED"
    assert len(dispatcher.intents) == 1
    sent = dispatcher.intents[0]
    assert (sent.ticker, sent.side.value, sent.quantity) == ("TQQQ", "BUY", Decimal("10.5"))


@pytest.mark.asyncio
async def test_concurrent_approvals_dispatch_only_once_and_replay_ack(signed_stack):
    """Former test_concurrent_approvals_dispatch_only_once_and_replay_ack."""
    started, release = asyncio.Event(), asyncio.Event()
    dispatcher = ScriptedDispatcher(_ack("T212-CONCURRENT"), gate=(started, release))
    signed_stack.install(dispatcher)
    proposal = signed_stack.add_admitted_proposal()
    pid = proposal["proposal_id"]

    async with _client() as client:
        challenge_id, signature = await _sign_challenge(client, signed_stack, pid)
        first = asyncio.create_task(_complete(client, pid, challenge_id, signature))
        await started.wait()
        second = asyncio.create_task(_complete(client, pid, challenge_id, signature))
        await asyncio.sleep(0)
        release.set()
        responses = await asyncio.gather(first, second)

    assert [response.status_code for response in responses] == [200, 200]
    replay_flags = {
        response.json()["execution_details"]["idempotent_replay"] for response in responses
    }
    assert replay_flags == {False, True}
    assert len(dispatcher.intents) == 1


@pytest.mark.asyncio
async def test_broker_error_fails_closed_without_leaking_its_text(signed_stack):
    """Former test_mcp_error_content_fails_closed."""
    dispatcher = ScriptedDispatcher(BrokerExecutionError("Trade blocked due to price variance"))
    signed_stack.install(dispatcher)
    proposal = signed_stack.add_admitted_proposal()
    pid = proposal["proposal_id"]

    async with _client() as client:
        challenge_id, signature = await _sign_challenge(client, signed_stack, pid)
        response = await _complete(client, pid, challenge_id, signature)

    assert response.status_code == 502
    assert response.json()["detail"] == "Broker rejected the trade"
    assert signed_stack.ledger.get_order(pid).state == "FAILED"
    assert "Trade blocked" not in str(response.content)


@pytest.mark.asyncio
async def test_timeout_becomes_unknown_and_cannot_be_retried(signed_stack):
    """Former test_timeout_becomes_unknown_and_cannot_be_retried.

    The retry now answers 409, not 502: the signed approval evidence is
    consumed by the first attempt, so the second never reaches the broker call.
    """
    dispatcher = ScriptedDispatcher(asyncio.TimeoutError())
    signed_stack.install(dispatcher)
    proposal = signed_stack.add_admitted_proposal()
    pid = proposal["proposal_id"]

    async with _client() as client:
        challenge_id, signature = await _sign_challenge(client, signed_stack, pid)
        first = await _complete(client, pid, challenge_id, signature)
        second = await _complete(client, pid, challenge_id, signature)

    assert first.status_code == 502
    assert "reconciliation is required" in first.json()["detail"]
    assert second.status_code == 409
    assert signed_stack.ledger.get_order(pid).state == "UNKNOWN"
    assert len(dispatcher.intents) == 1


@pytest.mark.asyncio
async def test_generic_dispatch_failure_is_sanitized_and_not_retried(signed_stack):
    """Former test_generic_dispatch_failure_is_sanitized_and_not_retried."""
    dispatcher = ScriptedDispatcher(
        RuntimeError("TRADING212_SECRET_SENTINEL connection refused")
    )
    signed_stack.install(dispatcher)
    proposal = signed_stack.add_admitted_proposal()
    pid = proposal["proposal_id"]

    async with _client() as client:
        challenge_id, signature = await _sign_challenge(client, signed_stack, pid)
        first = await _complete(client, pid, challenge_id, signature)
        second = await _complete(client, pid, challenge_id, signature)

    assert first.status_code == 502
    assert second.status_code == 409
    assert first.json()["detail"] == "Broker rejected the trade"
    assert signed_stack.ledger.get_order(pid).state == "FAILED"
    assert "TRADING212_SECRET_SENTINEL" not in str(first.content)
    assert "TRADING212_SECRET_SENTINEL" not in str(second.content)
    assert "TRADING212_SECRET_SENTINEL" not in str(state.get_trade_proposal(pid))
    assert len(dispatcher.intents) == 1


@pytest.mark.asyncio
async def test_idempotency_key_rejects_changed_order_intent(signed_stack):
    """Former test_idempotency_key_rejects_changed_order_intent.

    The changed intent is now refused at registration (the ledger holds the
    immutable one), and replaying the same signed approval returns the stored
    ack without a second dispatch.
    """
    dispatcher = ScriptedDispatcher(_ack("T212-IMMUTABLE"))
    service = signed_stack.install(dispatcher)
    proposal = signed_stack.add_admitted_proposal()
    pid = proposal["proposal_id"]

    async with _client() as client:
        challenge_id, signature = await _sign_challenge(client, signed_stack, pid)
        first = await _complete(client, pid, challenge_id, signature)
        changed = {**proposal, "quantity": "99"}
        with pytest.raises(ExecutionConflictError):
            service.register_proposal(changed)
        second = await _complete(client, pid, challenge_id, signature)

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["execution_details"]["idempotent_replay"] is True
    assert signed_stack.ledger.get_order(pid).intent["quantity"] == "10.5"
    assert len(dispatcher.intents) == 1


@pytest.mark.asyncio
async def test_success_without_broker_order_id_becomes_unknown(signed_stack):
    """Former test_success_without_broker_order_id_becomes_unknown.

    A dispatcher reports a missing order id as BrokerOutcomeUnknownError.
    """
    dispatcher = ScriptedDispatcher(
        BrokerOutcomeUnknownError("Broker acknowledgement omitted an order id")
    )
    signed_stack.install(dispatcher)
    proposal = signed_stack.add_admitted_proposal()
    pid = proposal["proposal_id"]

    async with _client() as client:
        challenge_id, signature = await _sign_challenge(client, signed_stack, pid)
        response = await _complete(client, pid, challenge_id, signature)

    assert response.status_code == 502
    assert signed_stack.ledger.get_order(pid).state == "UNKNOWN"
    assert len(dispatcher.intents) == 1


@pytest.mark.asyncio
async def test_legacy_approve_route_never_reaches_a_dispatcher_even_when_admitted(signed_stack):
    dispatcher = ScriptedDispatcher(_ack("T212-LEGACY"))
    signed_stack.install(dispatcher)
    proposal = signed_stack.add_admitted_proposal()

    response = await post_approval(proposal["proposal_id"])

    assert response.status_code == 503
    assert response.json()["detail"] == "Broker execution is currently disabled"
    assert dispatcher.intents == []
    assert signed_stack.ledger.get_order(proposal["proposal_id"]).state == "PENDING"


@pytest.mark.asyncio
async def test_approve_trade_rejects_invalid_proposal_side():
    proposal = add_proposal(action="REBALANCE")
    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock()
    state.execution_service = ExecutionService(dispatcher, **gated())

    response = await post_approval(proposal["proposal_id"])

    assert response.status_code == 503
    assert response.json()["detail"] == "Broker execution is currently disabled"
    dispatcher.dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_approve_trade_not_found():
    response = await post_approval("non-existent")

    assert response.status_code == 404
    assert "not found" in response.json()["detail"]


@pytest.mark.asyncio
async def test_approve_trade_already_processed_without_ack_conflicts():
    proposal = add_proposal(status="APPROVED")
    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock()
    state.execution_service = ExecutionService(dispatcher, **gated())

    response = await post_approval(proposal["proposal_id"])

    assert response.status_code == 503
    assert response.json()["detail"] == "Broker execution is currently disabled"
    dispatcher.dispatch.assert_not_awaited()


@pytest.mark.asyncio
async def test_approval_endpoint_requires_approved_decision():
    proposal = add_proposal()

    response = await post_approval(proposal["proposal_id"], decision="REJECTED")

    assert response.status_code == 400
    assert proposal["status"] == "PENDING"


@pytest.mark.asyncio
async def test_reject_trade_without_open_ledger_is_503():
    """D2: with no ledger open a rejection is refused, never held only in memory."""
    assert state._execution_ledger is None
    proposal = add_proposal(quantity=20.0)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/ai/trade/reject",
            json={
                "proposal_id": proposal["proposal_id"],
                "decision": "REJECTED",
                "workspace": "uk",
                "notes": "Too risky right now",
            },
        )
        unknown = await client.post(
            "/api/ai/trade/reject",
            json={
                "proposal_id": "does-not-exist",
                "decision": "REJECTED",
                "workspace": "uk",
            },
        )

    assert response.status_code == 503
    assert response.json()["detail"] == (
        "Trade rejection is unavailable: no execution ledger is open"
    )
    assert proposal["status"] == "PENDING"
    assert "rejection_notes" not in proposal
    assert "rejected_at" not in proposal
    assert unknown.status_code == 503
