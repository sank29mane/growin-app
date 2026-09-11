"""Explicit, local-only market-data replay lifecycle and snapshot resources."""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app_context import state
from execution import LedgerError, OrderAck, ReconciliationSnapshot, ReconciliationStatus
from execution.service import ExecutionDisabledError
from market_data import (
    IndiaInstrument,
    MarketDataError,
    MarketDataEvent,
    RegimeClassifier,
)


router = APIRouter(prefix="/api/market-data", tags=["Market Data"])


class ReplaySessionRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: Literal["local-replay"] = "local-replay"
    confirmation: Literal["START_READ_ONLY_REPLAY"]
    instruments: tuple[IndiaInstrument, ...] = Field(..., min_length=1, max_length=100)
    events: tuple[Annotated[MarketDataEvent, Field(discriminator="kind")], ...] = Field(
        ...,
        min_length=1,
        max_length=10_000,
    )

    @model_validator(mode="after")
    def validate_event_scope(self) -> "ReplaySessionRequest":
        keys = {instrument.key for instrument in self.instruments}
        if any(event.instrument.key not in keys for event in self.events):
            raise ValueError("every replay event must belong to a subscribed instrument")
        if any(event.source != self.provider for event in self.events):
            raise ValueError("every replay event source must match local-replay")
        return self


class IndiaPaperPreparationRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    confirmation: Literal["PREPARE_INDIA_PAPER"]
    symbol: str = Field(..., min_length=1, max_length=64)
    quantity: str = Field(..., min_length=1, max_length=32)


class IndiaPaperReconcileRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    confirmation: Literal["RECONCILE_INDIA_PAPER"]
    proposal_id: str = Field(..., min_length=1, max_length=64)


def _market_error(error: MarketDataError) -> HTTPException:
    status = 409
    if error.code in {"INSTRUMENT_OUT_OF_SCOPE", "INSTRUMENT_MISMATCH"}:
        status = 404
    elif error.code in {"PROVIDER_START_FAILED", "PROVIDER_READ_FAILED"}:
        status = 503
    return HTTPException(
        status_code=status,
        detail={"code": error.code, "message": str(error)},
    )


def _require_loopback(request: Request) -> None:
    host = None if request.client is None else request.client.host
    if host not in {"127.0.0.1", "::1", "testclient"}:
        raise HTTPException(
            status_code=403,
            detail={
                "code": "LOCAL_ACCESS_REQUIRED",
                "message": "market-data session control is local-only",
            },
        )


def _optional_prepare_regime(symbol: str):
    session = state._market_data_session
    if session is None:
        return None
    try:
        classifier = state._regime_classifier or RegimeClassifier()
        state._regime_classifier = classifier
        return classifier.evidence(session, IndiaInstrument(symbol=symbol)).model_dump(
            mode="json"
        )
    except MarketDataError:
        return None


def _paper_reconcile_denied(exc: Exception) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={"code": "PAPER_RECONCILE_DENIED", "message": str(exc)},
    )


@router.post("/sessions", status_code=201)
async def create_market_data_session(payload: ReplaySessionRequest, request: Request):
    """Start one finite, normalized, read-only local replay explicitly."""

    _require_loopback(request)
    try:
        return await state.start_market_data_replay(payload.instruments, payload.events)
    except MarketDataError as exc:
        raise _market_error(exc) from exc


@router.get("/sessions/current")
async def get_market_data_session(request: Request):
    _require_loopback(request)
    return state.market_data_status()


@router.delete("/sessions/current")
async def delete_market_data_session(request: Request):
    _require_loopback(request)
    await state.close_market_data()
    return state.market_data_status()


@router.get("/snapshots/{symbol}")
async def get_market_data_snapshot(symbol: str, request: Request):
    _require_loopback(request)
    try:
        instrument = IndiaInstrument(symbol=symbol)
        snapshot = state.market_data_snapshot(instrument)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_INSTRUMENT", "message": "invalid India/NSE symbol"},
        ) from exc
    except MarketDataError as exc:
        raise _market_error(exc) from exc
    return {
        **snapshot.model_dump(mode="json"),
        "snapshot_id": snapshot.snapshot_id,
    }


@router.post("/paper-preparations", status_code=201)
async def prepare_india_paper(payload: IndiaPaperPreparationRequest, request: Request):
    """Prepare a server-owned local PAPER intent; it never approves or dispatches."""
    _require_loopback(request)
    try:
        proposal, admission = state.prepare_india_paper_local(
            symbol=payload.symbol, quantity=payload.quantity,
        )
    except MarketDataError as exc:
        raise _market_error(exc) from exc
    except (LedgerError, ValueError) as exc:
        raise HTTPException(status_code=409, detail={"code": "PAPER_PREPARATION_DENIED", "message": str(exc)}) from exc
    body = {
        "proposal_id": proposal["proposal_id"],
        "state": proposal["status"],
        "admission": admission.model_dump(mode="json"),
    }
    regime = _optional_prepare_regime(payload.symbol)
    if regime is not None:
        body["regime"] = regime
    return body


@router.post("/paper-reconciliations")
async def reconcile_india_paper(payload: IndiaPaperReconcileRequest, request: Request):
    """Reconcile a stored India paper ack locally. Never invents a fill or reads a broker."""
    _require_loopback(request)
    try:
        proposal = state.execution_service.get_proposal(payload.proposal_id)
    except ExecutionDisabledError as exc:
        raise _paper_reconcile_denied(exc) from exc
    ack_payload = None if proposal is None else proposal.get("execution_ack")
    if not ack_payload:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "PAPER_RECONCILE_DENIED",
                "message": "stored paper acknowledgement is required",
            },
        )
    ack = OrderAck.model_validate(ack_payload)
    snapshot = ReconciliationSnapshot(
        proposal_id=payload.proposal_id,
        broker_order_id=ack.broker_order_id,
        source="local-paper-operations",
        cumulative_quantity=Decimal("0"),
        cumulative_notional=Decimal("0"),
        status=ReconciliationStatus.ACKNOWLEDGED,
        evidence_fingerprint=f"local-paper-operations:{ack.broker_order_id}",
        observed_at=datetime.now(timezone.utc),
    )
    try:
        order = state.execution_service.reconcile(snapshot)
    except (LedgerError, ExecutionDisabledError) as exc:
        raise _paper_reconcile_denied(exc) from exc
    acknowledgment = None
    if order.acknowledgment is not None:
        acknowledgment = order.acknowledgment.model_dump(mode="json")
    return {
        "proposal_id": order.proposal_id,
        "state": order.state,
        "client_order_id": order.client_order_id,
        "acknowledgment": acknowledgment,
    }
