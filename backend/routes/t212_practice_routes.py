"""Loopback-only routes for the Trading 212 practice venue (Phase 66).

Every route answers loopback callers only. None of them signs, approves or
dispatches an order: dispatch needs the app's Touch ID signature through the
approval routes. These routes only prepare (admit and reserve), list, reconcile and cancel.
"""

from decimal import Decimal
from typing import Any, Literal, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app_context import state
from execution import AdmissionDecision, LedgerError
from execution.service import ExecutionConflictError, ExecutionDisabledError
from market_data.admission import RecordedQuoteReading

router = APIRouter(prefix="/api/t212-practice", tags=["Trading 212 Practice"])


class PracticeReconcileRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    confirmation: Literal["RECONCILE_T212_PRACTICE"]
    proposal_id: str = Field(..., min_length=1, max_length=96)


class PracticeCancelRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    confirmation: Literal["CANCEL_T212_PRACTICE"]
    proposal_id: str = Field(..., min_length=1, max_length=96)


class PracticePreparationRequest(BaseModel):
    """What a caller may say. Workspace, account, broker and mode are never accepted:
    the server stamps them from the open ledger's venue binding (decision 2)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    confirmation: Literal["PREPARE_T212_PRACTICE"]
    ticker: str = Field(..., min_length=1, max_length=32)
    side: Literal["BUY", "SELL"]
    # A whole number of shares. A JSON string or a fraction is refused by validation.
    quantity: int = Field(..., gt=0, le=1_000_000, strict=True)
    limit_price: Decimal = Field(..., gt=0, allow_inf_nan=False, max_digits=20, decimal_places=8)
    source: Literal["operator-recorded"] = "operator-recorded"
    readings: list[RecordedQuoteReading] = Field(..., min_length=1, max_length=20)


def _require_loopback(request: Request) -> None:
    host = None if request.client is None else request.client.host
    if host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "LOCAL_ACCESS_REQUIRED",
                "message": "practice execution control is local-only",
            },
        )


def _adapter() -> Any:
    """The live practice adapter, or a 503. No adapter means execution is off."""

    adapter = state.venue_adapter
    if (
        not state.execution_authority
        or state.execution_venue_binding is None
        or adapter is None
        or getattr(adapter, "reconciler", None) is None
    ):
        raise HTTPException(
            status_code=503,
            detail={"code": "PRACTICE_UNAVAILABLE", "message": "practice execution is not active"},
        )
    return adapter


@router.post("/reconciliations")
async def reconcile_practice_order(payload: PracticeReconcileRequest, request: Request):
    """Read the broker (pending, history, positions) and apply one ledger snapshot.

    Never places or cancels anything. A 404 by id is "not pending", never FAILED.
    """

    _require_loopback(request)
    from brokers.trading212.practice_reconcile import ReconcileRefused

    adapter = _adapter()
    try:
        result = await adapter.reconciler.reconcile(payload.proposal_id)
    except ReconcileRefused as exc:
        raise HTTPException(
            status_code=409, detail={"code": exc.code, "message": "reconcile refused"}
        ) from exc
    except LedgerError as exc:
        raise HTTPException(
            status_code=409, detail={"code": "LEDGER_REFUSED", "message": str(exc)}
        ) from exc
    return result.as_dict()


@router.post("/cancellations")
async def cancel_practice_order(payload: PracticeCancelRequest, request: Request):
    """Request one cancel for the ledger's own acknowledged order (D-22).

    The ledger event commits first, then one DELETE is sent and never resent.
    ``REQUESTED`` means the broker accepted the request, not that the order is
    cancelled: reconcile afterwards. Unsigned by design: a cancel only reduces
    exposure on virtual funds, and signed cancel is deferred.
    """

    _require_loopback(request)
    _adapter()
    try:
        outcome = await state.execution_service.cancel_order(payload.proposal_id)
    except ExecutionDisabledError as exc:
        raise HTTPException(
            status_code=503, detail={"code": "PRACTICE_UNAVAILABLE", "message": str(exc)}
        ) from exc
    except ExecutionConflictError as exc:
        raise HTTPException(
            status_code=409, detail={"code": "CANCEL_REFUSED", "message": str(exc)}
        ) from exc
    return {
        "proposal_id": payload.proposal_id,
        "cancel": {"outcome": outcome.outcome, "code": outcome.code},
    }


def _proposal_view(proposal_id: str) -> Optional[dict[str, Any]]:
    """The loopback view of one practice proposal. It never carries an account id."""

    ledger = state._execution_ledger
    if ledger is None:
        return None
    order = ledger.get_order(proposal_id)
    if order is None or order.intent.get("mode") != "PRACTICE":
        return None
    admission = ledger.get_admission(proposal_id)
    reservation = ledger.get_reservation(proposal_id)
    intent = order.intent
    return {
        "proposal_id": order.proposal_id,
        "ticker": intent["ticker"],
        "action": intent["side"],
        "quantity": intent["quantity"],
        "order_type": intent.get("order_type"),
        "limit_price": intent.get("limit_price"),
        "time_validity": "DAY",
        "mode": intent["mode"],
        "broker": intent["broker"],
        "reasoning": (
            "UK practice order from operator-recorded quotes. Approval with Touch ID "
            "sends one LIMIT DAY order to the Trading 212 demo account."
        ),
        "status": order.state,
        "currency": None if admission is None else admission.currency,
        "notional": None if admission is None else str(admission.notional),
        "admission": None if admission is None else admission.decision.value,
        "reason_code": None if admission is None else admission.reason_code,
        "reservation": None if reservation is None else reservation.state,
        "broker_order_id": (
            None if order.acknowledgment is None else order.acknowledgment.broker_order_id
        ),
    }


@router.post("/preparations", status_code=201)
async def prepare_practice_order(payload: PracticePreparationRequest, request: Request):
    """Admit one practice order from the operator's recorded quotes (D-02), or deny it.

    Prepare only: it never signs, approves or dispatches. A denial is returned as a
    201 body with ``admission.decision`` DENIED and a stable ``reason_code``, and
    leaves no reservation. Needs at least three distinct recorded readings.
    """

    _require_loopback(request)
    _adapter()
    try:
        proposal, admission = await state.admit_uk_practice_proposal(
            ticker=payload.ticker,
            side=payload.side,
            quantity=payload.quantity,
            limit_price=payload.limit_price,
            readings=payload.readings,
            price_source=payload.source,
        )
    except (LedgerError, ExecutionConflictError, ValueError) as exc:
        raise HTTPException(
            status_code=409, detail={"code": "PRACTICE_PREPARATION_DENIED", "message": str(exc)}
        ) from exc
    except ExecutionDisabledError as exc:
        raise HTTPException(
            status_code=503, detail={"code": "PRACTICE_UNAVAILABLE", "message": str(exc)}
        ) from exc
    view = _proposal_view(proposal["proposal_id"]) or {}
    return {
        "proposal_id": proposal["proposal_id"],
        "state": view.get("status"),
        "admitted": admission.decision is AdmissionDecision.ADMITTED,
        "admission": admission.model_dump(mode="json", exclude={"account"}),
        "proposal": view,
    }


@router.get("/proposals")
async def list_practice_proposals(request: Request):
    """Pending practice proposals the app may open for Touch ID: admitted, reserved, unsigned."""

    _require_loopback(request)
    _adapter()
    ledger = state._execution_ledger
    views = []
    for order in ledger.list_orders(states=["PENDING"]):
        view = _proposal_view(order.proposal_id)
        if view is not None and view["admission"] == "ADMITTED" and view["reservation"] == "ACTIVE":
            views.append(view)
    return {"proposals": views}


@router.get("/proposals/{proposal_id}")
async def get_practice_proposal(proposal_id: str, request: Request):
    _require_loopback(request)
    _adapter()
    view = _proposal_view(proposal_id)
    if view is None:
        raise HTTPException(
            status_code=404, detail={"code": "PROPOSAL_NOT_FOUND", "message": "no such practice proposal"}
        )
    return view
