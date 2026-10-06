"""Loopback-only routes for the Trading 212 practice venue (Phase 66).

Every route answers loopback callers only. None of them signs, approves or
dispatches an order: dispatch needs the app's Touch ID signature through the
approval routes. These routes only reconcile, cancel and (66-04) prepare and list.
"""

from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from app_context import state
from execution import LedgerError
from execution.service import ExecutionConflictError, ExecutionDisabledError

router = APIRouter(prefix="/api/t212-practice", tags=["Trading 212 Practice"])


class PracticeReconcileRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    confirmation: Literal["RECONCILE_T212_PRACTICE"]
    proposal_id: str = Field(..., min_length=1, max_length=96)


class PracticeCancelRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    confirmation: Literal["CANCEL_T212_PRACTICE"]
    proposal_id: str = Field(..., min_length=1, max_length=96)


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
