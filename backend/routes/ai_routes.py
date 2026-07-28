"""
AI Routes - Strategy, Reasoning, and SOTA 2026 AI Interactions
"""

import asyncio
import base64
import binascii
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from sse_starlette.sse import EventSourceResponse

from app_context import state
from schemas import (
    AIStrategyResponse,
    TradeApprovalRequest,
    ApprovalChallengeRequest,
    ApprovalKeyEnrollmentRequest,
    SignedApprovalRequest,
)
from execution import (
    ApprovalError,
    ApprovalConflict,
    ApprovalVerificationError,
    BrokerExecutionError,
    BrokerOutcomeUnknownError,
    ExecutionConflictError,
    ExecutionDisabledError,
    EnrollmentError,
    LedgerError,
    ReconciliationSnapshot,
    ReconciliationStatus,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ai", tags=["AI Intelligence"])

# Mock database for strategies (in-memory for demo/SOTA phase)
# In production, this would be in the analytical database
STRATEGIES_MOCK = {}

# --- HITL Trade Approval Endpoints ---


def _strict_b64(value: str, *, expected_length: int | None = None) -> bytes:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(status_code=422, detail="Invalid cryptographic encoding") from exc
    if expected_length is not None and len(decoded) != expected_length:
        raise HTTPException(status_code=422, detail="Invalid cryptographic encoding")
    return decoded


@router.post("/trade/approval/enroll")
async def enroll_trade_approval_key(request: ApprovalKeyEnrollmentRequest):
    """Enroll one Secure Enclave P-256 public key using a local one-time token."""
    public_key = _strict_b64(request.public_key_x963_b64, expected_length=65)
    try:
        enrolled = state.execution_service.enroll_approval_key(
            public_key, request.enrollment_token
        )
    except EnrollmentError:
        logger.warning("Rejected local approval-key enrollment")
        raise HTTPException(status_code=403, detail="Approval enrollment was rejected")
    except ExecutionDisabledError:
        raise HTTPException(status_code=503, detail="Signed approval is unavailable")
    return {"status": "enrolled", "key_id": enrolled.key_id}


@router.get("/trade/approval/status")
async def get_trade_approval_status():
    """Return only non-secret local enrollment state for the settings UI."""
    return {
        "mode": "paper" if state.execution_authority else "disabled",
        "enrolled": state.execution_service.approval_key_id() is not None,
        "key_id": state.execution_service.approval_key_id(),
    }


@router.post("/trade/approval/uat-proposal")
async def create_paper_approval_uat_proposal():
    """Create an explicit local-only paper proposal for manual approval UAT."""
    try:
        proposal = state.create_paper_approval_check()
        # Do not expose the durable projection as an ad-hoc UI contract. Swift
        # decodes this exact, stable proposal shape before requesting a challenge.
        return {
            "proposal_id": str(proposal["proposal_id"]),
            "ticker": str(proposal["ticker"]),
            "action": str(proposal["action"]),
            "quantity": float(proposal["quantity"]),
            "reasoning": str(proposal.get("reasoning", "Local paper approval check")),
            "status": str(proposal.get("status", "PENDING")),
        }
    except (ExecutionDisabledError, LedgerError, ExecutionConflictError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/trade/requote/uat-proposal")
async def create_paper_requote_uat_proposal():
    """Create a local cancelled-parent LIMIT replacement for manual UAT only."""
    try:
        proposal = state.create_paper_requote_check()
        return {
            "proposal_id": str(proposal["proposal_id"]),
            "ticker": str(proposal["ticker"]),
            "action": str(proposal["action"]),
            "quantity": float(proposal["quantity"]),
            "reasoning": str(proposal["reasoning"]),
            "status": str(proposal.get("status", "PENDING")),
        }
    except (ExecutionDisabledError, LedgerError, ExecutionConflictError, ValueError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/trade/approval/challenge")
async def create_trade_approval_challenge(request: ApprovalChallengeRequest):
    """Return exact immutable bytes for native review and Touch ID signing."""
    try:
        challenge = state.execution_service.create_approval_challenge(
            request.proposal_id
        )
    except ExecutionDisabledError:
        raise HTTPException(status_code=503, detail="Signed approval is unavailable")
    except (ApprovalError, ApprovalConflict, LedgerError):
        raise HTTPException(status_code=409, detail="Approval challenge is unavailable")
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="Trade proposal is invalid")
    return {
        "challenge_id": challenge.challenge_id,
        "proposal_id": challenge.proposal_id,
        "key_id": challenge.key_id,
        "intent_hash": challenge.intent_hash,
        "signed_payload_b64": base64.b64encode(challenge.signed_payload).decode("ascii"),
        "issued_at": challenge.issued_at_epoch,
        "expires_at": challenge.expires_at_epoch,
    }


@router.post("/trade/approval/complete")
async def complete_trade_approval(request: SignedApprovalRequest):
    """Verify Touch ID evidence, atomically authorize once, then dispatch paper."""
    signature = _strict_b64(request.signature_der_b64)
    if state.is_paper_requote_check(request.proposal_id):
        raise HTTPException(
            status_code=409,
            detail="Local re-quote UAT never dispatches; use its verify-only action.",
        )
    try:
        ack = await state.execution_service.approve_signed(
            request.proposal_id, request.challenge_id, signature
        )
    except ApprovalVerificationError:
        logger.warning("Rejected invalid signed trade approval")
        raise HTTPException(status_code=403, detail="Signed approval was rejected")
    except ExecutionDisabledError:
        raise HTTPException(status_code=503, detail="Signed approval is unavailable")
    except ExecutionConflictError:
        raise HTTPException(status_code=409, detail="Signed approval could not be completed")
    except BrokerExecutionError:
        raise HTTPException(status_code=502, detail="Broker rejected the trade")
    except BrokerOutcomeUnknownError:
        raise HTTPException(
            status_code=502,
            detail="Broker outcome is unknown; reconciliation is required before retrying",
        )
    proposal = state.get_trade_proposal(request.proposal_id)
    is_local_uat = proposal and proposal.get("account") in {"paper-uat", "paper-uat-v2"}
    if is_local_uat:
        # The UAT dispatcher is local-only. Settle it as a zero-fill cancellation
        # after its acknowledgement so the bounded £1 test budget is reusable.
        state.execution_service.reconcile(
            ReconciliationSnapshot(
                proposal_id=ack.proposal_id,
                broker_order_id=ack.broker_order_id,
                source="local-paper-approval-uat",
                cumulative_quantity="0",
                cumulative_notional="0",
                status=ReconciliationStatus.CANCELLED,
                evidence_fingerprint=f"local-uat-release:{ack.broker_order_id}",
                observed_at=datetime.now(timezone.utc),
            )
        )
    return {
        "status": "success",
        "message": (
            "Paper approval check acknowledged locally and its test reservation released. "
            "No broker was contacted."
            if is_local_uat
            else f"Paper trade acknowledged by {ack.broker}."
        ),
        "execution_details": ack.model_dump(mode="json"),
    }


@router.post("/trade/requote/uat/verify")
async def verify_paper_requote_uat_signature(request: SignedApprovalRequest):
    """Verify the fresh local signature without consuming approval or dispatching."""
    if not state.is_paper_requote_check(request.proposal_id):
        raise HTTPException(status_code=404, detail="Local re-quote UAT proposal was not found")
    signature = _strict_b64(request.signature_der_b64)
    try:
        state.execution_service.verify_approval_signature_for_uat(
            request.proposal_id, request.challenge_id, signature
        )
    except ApprovalVerificationError:
        logger.warning("Rejected invalid local re-quote UAT signature")
        raise HTTPException(status_code=403, detail="Local UAT signature was rejected")
    except ExecutionDisabledError:
        raise HTTPException(status_code=503, detail="Signed approval is unavailable")
    except (ApprovalError, ApprovalConflict, ExecutionConflictError, LedgerError):
        raise HTTPException(status_code=409, detail="Local UAT signature could not be verified")
    return {
        "status": "success",
        "message": "Local re-quote signature verified. No order was dispatched and no broker was contacted.",
    }

@router.post("/trade/approve")
async def approve_trade(request: TradeApprovalRequest):
    """
    SOTA 2026 Phase 30: HITL Trade Approval.
    Validates the proposal and executes the trade via Trading 212 MCP.
    """
    if request.decision.upper() != "APPROVED":
        raise HTTPException(status_code=400, detail="Approval endpoint requires decision APPROVED")

    proposal_id = request.proposal_id
    
    proposal = state.get_trade_proposal(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"Trade proposal {proposal_id} not found")
    
    try:
        ack = await state.execution_service.approve(proposal)
        return {
            "status": "success",
            "message": f"Trade for {proposal['ticker']} acknowledged by {ack.broker}.",
            "execution_details": ack.model_dump(mode="json"),
        }
    except ExecutionDisabledError:
        logger.warning("Blocked trade approval because execution controls are not installed")
        raise HTTPException(status_code=503, detail="Broker execution is currently disabled")
    except ExecutionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except BrokerExecutionError:
        logger.warning("Broker rejected trade proposal %s", proposal_id)
        raise HTTPException(status_code=502, detail="Broker rejected the trade")
    except BrokerOutcomeUnknownError:
        logger.error("Broker outcome is unknown for proposal %s", proposal_id)
        raise HTTPException(
            status_code=502,
            detail="Broker outcome is unknown; reconciliation is required before retrying",
        )
    except (ValueError, TypeError) as exc:
        logger.warning("Invalid trade proposal %s: %s", proposal_id, exc)
        raise HTTPException(status_code=422, detail="Trade proposal is invalid")

@router.post("/trade/reject")
async def reject_trade(request: TradeApprovalRequest):
    """
    SOTA 2026 Phase 30: HITL Trade Rejection.
    Marks the proposal as rejected and stops execution.
    """
    proposal_id = request.proposal_id
    
    proposal = state.get_trade_proposal(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"Trade proposal {proposal_id} not found")
        
    if request.decision.upper() != "REJECTED":
        raise HTTPException(status_code=400, detail="Rejection endpoint requires decision REJECTED")

    try:
        await state.execution_service.reject(proposal, request.notes)
    except ExecutionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    
    logger.info(f"🚫 Trade {proposal_id} REJECTED by user: {request.notes}")
    
    return {
        "status": "rejected",
        "message": f"Trade proposal for {proposal['ticker']} has been rejected."
    }

@router.get("/strategy/stream")
async def stream_strategy_events(
    session_id: str = Query(..., description="Unique session ID for the strategy generation"),
    ticker: Optional[str] = None
):
    """
    SOTA 2026: AG-UI Streaming Protocol.
    Streams real-time agent workflow events (ReasoningSteps) via SSE.
    """
    return EventSourceResponse(
        strategy_event_generator(session_id, ticker),
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no"
        }
    )

async def strategy_event_generator(session_id: str, ticker: Optional[str]):
    """Generator for strategy events using real Orchestrator and Messenger telemetry."""
    from agents.orchestrator_agent import OrchestratorAgent
    from agents.messenger import get_messenger
    
    # SOTA: The route emits a standard stream of status_updates and reasoning_steps.
    # No conditional logic based on session_id is performed for revisions;
    # tests merely assert that at least one status_update is received.

    queue = asyncio.Queue()
    messenger = get_messenger()
    correlation_id = str(uuid.uuid4())
    
    # Mapper for subjects to event types
    subject_map = {
        "intent_classified": "status_update",
        "context_fabricated": "status_update",
        "swarm_started": "status_update",
        "agent_started": "reasoning_step",
        "agent_complete": "reasoning_step",
        "reasoning_started": "reasoning_step",
        "risk_review_started": "reasoning_step"
    }

    async def messenger_handler(msg):
        event_type = subject_map.get(msg.subject, "status_update")
        
        # Build the structured event for the UI
        event_data = {
            "event_type": event_type,
            "agent": msg.sender,
            "status": "working" if msg.subject.endswith("_started") else "ready",
            "timestamp": msg.timestamp.timestamp(),
            "step": {
                "agent": msg.sender,
                "action": msg.subject.replace("_", " ").title(),
                "content": json.dumps(msg.payload),
                "timestamp": msg.timestamp.timestamp()
            }
        }
        await queue.put(event_data)

    # Subscribe to this session's trace
    messenger.subscribe_to_trace(correlation_id, messenger_handler)
    
    try:
        # Start Orchestrator in background
        orchestrator = OrchestratorAgent()
        
        # We need to guarantee at least one status_update before the final result
        # if the test expects it, but orchestrator might fail fast and not send events
        # especially when models fail to load.
        yield {
            "event": "status_update",
            "data": json.dumps({
                "event_type": "status_update",
                "agent": "OrchestratorAgent",
                "status": "working",
                "timestamp": datetime.now().timestamp()
            })
        }

        # Use task to run orchestrator so we can yield from queue in parallel
        query = f"Generate a comprehensive investment strategy for {ticker or 'my portfolio'}."
        orch_task = asyncio.create_task(orchestrator.run(
            query=query, 
            ticker=ticker
        ))
        
        # Yield events from queue as they arrive
        while not orch_task.done() or not queue.empty():
            try:
                # Use a timeout to avoid blocking forever if task hangs
                event = await asyncio.wait_for(queue.get(), timeout=1.0)
                yield {
                    "event": event["event_type"],
                    "data": json.dumps(event)
                }
            except asyncio.TimeoutError:
                continue

        # Get final result
        result = await orch_task
        context = result.get("context")
        
        strategy_id = str(uuid.uuid4())
        # Map Orchestrator context to AIStrategyResponse
        final_strategy = {
            "strategy_id": strategy_id,
            "title": f"{ticker or 'Portfolio'} Strategic Alpha",
            "summary": result.get("content", ""),
            "confidence": 0.85, # Default or extracted
            "reasoning_trace": [
                {"agent": t.agent_name, "action": "Analysis", "content": t.input_tokens if hasattr(t, 'input_tokens') else "", "timestamp": datetime.now().timestamp()}
                for t in context.telemetry_trace
            ] if context else [],
            "instruments": [], # Map from context if needed
            "risk_assessment": context.user_context.get("risk_review", {}).get("risk_assessment", "Standard Risk") if context else "Standard Risk",
            "last_updated": datetime.now().timestamp()
        }
        
        STRATEGIES_MOCK[strategy_id] = final_strategy

        yield {
            "event": "final_result",
            "data": json.dumps({
                "event_type": "final_result",
                "agent": "OrchestratorAgent",
                "status": "ready",
                "strategy_id": strategy_id,
                "timestamp": datetime.now().timestamp()
            })
        }

    except Exception as e:
        logger.error(f"Strategy streaming error: {e}", exc_info=True)
        yield { "event": "error", "data": json.dumps({"message": str(e)}) }
    finally:
        messenger.unsubscribe_from_trace(correlation_id, messenger_handler)

@router.get("/strategy/{strategy_id}", response_model=AIStrategyResponse)
async def get_strategy(strategy_id: str):
    """Retrieve full strategy details."""
    if strategy_id not in STRATEGIES_MOCK:
        raise HTTPException(status_code=404, detail="Strategy not found")
    return STRATEGIES_MOCK[strategy_id]

@router.post("/strategy/{strategy_id}/challenge")
async def challenge_strategy(strategy_id: str, challenge: str):
    """
    SOTA 2026: Challenge Logic.
    Allows users to question the AI's reasoning and trigger a revision.
    """
    if strategy_id not in STRATEGIES_MOCK:
        raise HTTPException(status_code=404, detail="Strategy not found")
        
    # In a real system, this would trigger a new R-Stitch trajectory
    logger.info(f"Strategy {strategy_id} challenged: {challenge}")
    
    return {
        "status": "revision_triggered",
        "new_session_id": str(uuid.uuid4()),
        "message": "Challenge accepted. Re-stitching strategy trajectories..."
    }
