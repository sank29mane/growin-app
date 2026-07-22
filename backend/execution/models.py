"""Canonical execution models shared by routes and broker dispatchers."""

from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Mapping

from pydantic import BaseModel, ConfigDict, Field, model_validator


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderMode(str, Enum):
    PAPER = "PAPER"
    LIVE = "LIVE"


class OrderState(str, Enum):
    PENDING = "PENDING"
    SUBMITTING = "SUBMITTING"
    APPROVED = "APPROVED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    EXPIRED = "EXPIRED"


class OrderIntent(BaseModel):
    """Minimum broker-neutral order intent supported by the legacy HITL flow."""

    model_config = ConfigDict(str_strip_whitespace=True, frozen=True)

    proposal_id: str = Field(..., min_length=1)
    client_order_id: str = Field(default="", max_length=96)
    intent_version: int = Field(default=1, ge=1)
    workspace: str = Field(default="uk", min_length=1)
    account: str = Field(default="invest", min_length=1)
    broker: str = Field(default="trading212", min_length=1)
    mode: OrderMode = OrderMode.PAPER
    ticker: str = Field(..., min_length=1)
    side: OrderSide
    quantity: Decimal = Field(..., gt=0)

    @model_validator(mode="after")
    def ensure_client_order_id(self) -> "OrderIntent":
        if not self.client_order_id:
            object.__setattr__(self, "client_order_id", f"growin-{self.proposal_id}")
        return self


class AdmissionDecision(str, Enum):
    ADMITTED = "ADMITTED"
    DENIED = "DENIED"


class ExecutionAdmissionInput(BaseModel):
    """Immutable evidence supplied by deterministic simulation/risk adapters."""

    model_config = ConfigDict(str_strip_whitespace=True, frozen=True)

    workspace: str = Field(..., min_length=1)
    account: str = Field(..., min_length=1)
    currency: str = Field(..., min_length=1, max_length=12)
    ticker: str = Field(..., min_length=1)
    side: OrderSide
    quantity: Decimal = Field(..., gt=0)
    price: Decimal = Field(..., gt=0)
    simulator_fill_price: Decimal = Field(..., gt=0)
    simulator_drawdown_pct: Decimal = Field(default=Decimal("0"), ge=0)
    risk_quantity: Decimal = Field(..., ge=0)
    current_spread_pct: Decimal = Field(default=Decimal("0"), ge=0)
    evidence_at: datetime
    max_age_seconds: int = Field(default=30, ge=0, le=86_400)
    simulator_evidence: Mapping[str, Any] = Field(default_factory=dict)
    risk_evidence: Mapping[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_evidence_clock(self) -> "ExecutionAdmissionInput":
        if self.evidence_at.tzinfo is None:
            raise ValueError("admission evidence timestamp must be timezone-aware")
        return self


class ExecutionAdmission(BaseModel):
    """Persisted decision binding immutable intent and deterministic evidence."""

    model_config = ConfigDict(str_strip_whitespace=True, frozen=True)

    proposal_id: str
    intent_hash: str
    workspace: str
    account: str
    currency: str
    ticker: str
    side: OrderSide
    original_quantity: Decimal = Field(..., gt=0)
    final_quantity: Decimal = Field(..., ge=0)
    price: Decimal = Field(..., ge=0)
    notional: Decimal = Field(..., ge=0)
    simulator_fill_price: Decimal = Field(..., ge=0)
    simulator_drawdown_pct: Decimal = Field(..., ge=0)
    risk_quantity: Decimal = Field(..., ge=0)
    current_spread_pct: Decimal = Field(..., ge=0)
    evidence_at: datetime
    evidence_hash: str
    decision: AdmissionDecision
    reason_code: str = Field(..., min_length=1, max_length=96)
    created_at: datetime

    @model_validator(mode="after")
    def validate_decision(self) -> "ExecutionAdmission":
        if self.evidence_at.tzinfo is None or self.created_at.tzinfo is None:
            raise ValueError("admission timestamps must be timezone-aware")
        if self.decision is AdmissionDecision.ADMITTED and (
            self.final_quantity <= 0 or self.price <= 0 or self.notional <= 0
        ):
            raise ValueError("admitted evidence must contain a positive quantity and notional")
        return self


class ReconciliationStatus(str, Enum):
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


class ReconciliationSnapshot(BaseModel):
    """Broker-neutral, monotonic evidence for an acknowledged paper order."""

    model_config = ConfigDict(str_strip_whitespace=True, frozen=True)

    proposal_id: str = Field(..., min_length=1)
    broker_order_id: str = Field(..., min_length=1)
    source: str = Field(..., min_length=1, max_length=96)
    cumulative_quantity: Decimal = Field(..., ge=0)
    cumulative_notional: Decimal = Field(..., ge=0)
    status: ReconciliationStatus
    evidence_fingerprint: str = Field(..., min_length=1, max_length=128)
    observed_at: datetime

    @model_validator(mode="after")
    def validate_snapshot(self) -> "ReconciliationSnapshot":
        if self.observed_at.tzinfo is None:
            raise ValueError("reconciliation timestamp must be timezone-aware")
        if self.status is ReconciliationStatus.FILLED and self.cumulative_quantity <= 0:
            raise ValueError("filled reconciliation requires a positive quantity")
        return self


class PaperBudget(BaseModel):
    model_config = ConfigDict(frozen=True)

    workspace: str
    account: str
    currency: str
    amount: Decimal = Field(..., gt=0)
    reserved: Decimal = Field(default=Decimal("0"), ge=0)
    consumed: Decimal = Field(default=Decimal("0"), ge=0)
    released: Decimal = Field(default=Decimal("0"), ge=0)

    @property
    def available(self) -> Decimal:
        return self.amount - self.consumed - self.reserved


class PaperReservation(BaseModel):
    model_config = ConfigDict(frozen=True)

    proposal_id: str
    workspace: str
    account: str
    currency: str
    intent_hash: str
    reserved: Decimal = Field(..., gt=0)
    consumed: Decimal = Field(default=Decimal("0"), ge=0)
    released: Decimal = Field(default=Decimal("0"), ge=0)
    state: str

    @property
    def outstanding(self) -> Decimal:
        return self.reserved - self.consumed - self.released


class WorkspaceControl(BaseModel):
    model_config = ConfigDict(frozen=True)

    workspace: str
    engaged: bool
    version: int = Field(..., ge=0)
    reason_code: str = ""
    updated_at: datetime

    @model_validator(mode="after")
    def validate_timestamp(self) -> "WorkspaceControl":
        if self.updated_at.tzinfo is None:
            raise ValueError("workspace control timestamp must be timezone-aware")
        return self


class OrderAck(BaseModel):
    """Typed acknowledgement accepted by the execution service."""

    proposal_id: str
    broker: str
    broker_order_id: str = Field(..., min_length=1)
    status: str = Field(default="ACKNOWLEDGED", min_length=1)
    raw: Dict[str, Any] = Field(default_factory=dict)
    idempotent_replay: bool = False

    def as_replay(self) -> "OrderAck":
        return self.model_copy(update={"idempotent_replay": True})
