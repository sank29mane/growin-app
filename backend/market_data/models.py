"""Immutable normalized market-data value objects."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from hashlib import sha256
import json
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator


class IndiaInstrument(BaseModel):
    """Explicit India/NSE instrument identity for the first delivery slice."""

    model_config = ConfigDict(str_strip_whitespace=True, frozen=True, extra="forbid")

    workspace: Literal["india"] = "india"
    venue: Literal["NSE"] = "NSE"
    segment: Literal["CASH"] = "CASH"
    symbol: str = Field(..., min_length=1, max_length=32, pattern=r"^[A-Z0-9&-]+$")
    currency: Literal["INR"] = "INR"

    @property
    def key(self) -> str:
        return f"{self.workspace}:{self.venue}:{self.segment}:{self.symbol}"

    @property
    def execution_ticker(self) -> str:
        return f"{self.venue}:{self.segment}:{self.symbol}"


class MarketDataSubscription(BaseModel):
    """Explicit, capability-limited request passed to a provider."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace: Literal["india"] = "india"
    instruments: tuple[IndiaInstrument, ...] = Field(..., min_length=1)
    channels: tuple[Literal["quote", "trade"], ...] = ("quote", "trade")
    read_only: Literal[True] = True

    @model_validator(mode="after")
    def validate_subscription(self) -> "MarketDataSubscription":
        keys = [instrument.key for instrument in self.instruments]
        if len(keys) != len(set(keys)):
            raise ValueError("subscription instruments must be unique")
        if not self.channels or len(self.channels) != len(set(self.channels)):
            raise ValueError("subscription channels must be unique and non-empty")
        return self


class _MarketEvent(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, frozen=True, extra="forbid")

    instrument: IndiaInstrument
    source: str = Field(..., min_length=1, max_length=64)
    observed_at: datetime
    received_at: datetime
    sequence: int = Field(..., ge=0)

    @model_validator(mode="after")
    def validate_clocks(self) -> "_MarketEvent":
        if self.observed_at.tzinfo is None or self.received_at.tzinfo is None:
            raise ValueError("market-data timestamps must be timezone-aware")
        if self.received_at < self.observed_at:
            raise ValueError("market-data receive time cannot precede observation time")
        return self


class TopOfBook(_MarketEvent):
    kind: Literal["quote"] = "quote"
    bid: Decimal = Field(
        ..., gt=0, allow_inf_nan=False, max_digits=20, decimal_places=8
    )
    ask: Decimal = Field(
        ..., gt=0, allow_inf_nan=False, max_digits=20, decimal_places=8
    )
    bid_size: Decimal | None = Field(
        default=None, ge=0, allow_inf_nan=False, max_digits=20, decimal_places=8
    )
    ask_size: Decimal | None = Field(
        default=None, ge=0, allow_inf_nan=False, max_digits=20, decimal_places=8
    )

    @model_validator(mode="after")
    def validate_book(self) -> "TopOfBook":
        if self.bid > self.ask:
            raise ValueError("best bid cannot exceed best ask")
        return self


class TradeTick(_MarketEvent):
    kind: Literal["trade"] = "trade"
    price: Decimal = Field(
        ..., gt=0, allow_inf_nan=False, max_digits=20, decimal_places=8
    )
    quantity: Decimal = Field(
        ..., gt=0, allow_inf_nan=False, max_digits=20, decimal_places=8
    )
    trade_id: str | None = Field(default=None, min_length=1, max_length=128)


MarketDataEvent: TypeAlias = Annotated[
    TopOfBook | TradeTick,
    Field(discriminator="kind"),
]


class MarketSnapshot(BaseModel):
    """Immutable quote/trade state with explicit source lineage."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    instrument: IndiaInstrument
    source: str
    bid: Decimal = Field(
        ..., gt=0, allow_inf_nan=False, max_digits=20, decimal_places=8
    )
    ask: Decimal = Field(
        ..., gt=0, allow_inf_nan=False, max_digits=20, decimal_places=8
    )
    bid_size: Decimal | None = Field(
        default=None, ge=0, allow_inf_nan=False, max_digits=20, decimal_places=8
    )
    ask_size: Decimal | None = Field(
        default=None, ge=0, allow_inf_nan=False, max_digits=20, decimal_places=8
    )
    quote_observed_at: datetime
    quote_received_at: datetime
    quote_sequence: int = Field(..., ge=0)
    last_trade_price: Decimal | None = Field(
        default=None, gt=0, allow_inf_nan=False, max_digits=20, decimal_places=8
    )
    last_trade_quantity: Decimal | None = Field(
        default=None, gt=0, allow_inf_nan=False, max_digits=20, decimal_places=8
    )
    trade_observed_at: datetime | None = None
    trade_received_at: datetime | None = None
    trade_sequence: int | None = Field(default=None, ge=0)
    trade_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def validate_snapshot(self) -> "MarketSnapshot":
        if self.bid > self.ask:
            raise ValueError("best bid cannot exceed best ask")
        if self.quote_observed_at.tzinfo is None or self.quote_received_at.tzinfo is None:
            raise ValueError("snapshot quote timestamp must be timezone-aware")
        if self.quote_received_at < self.quote_observed_at:
            raise ValueError("snapshot quote receive time cannot precede observation time")
        trade_fields = (
            self.last_trade_price,
            self.last_trade_quantity,
            self.trade_observed_at,
            self.trade_received_at,
            self.trade_sequence,
        )
        if any(value is not None for value in trade_fields) and not all(
            value is not None for value in trade_fields
        ):
            raise ValueError("snapshot trade lineage must be complete")
        if (
            self.trade_observed_at is not None
            and (
                self.trade_observed_at.tzinfo is None
                or self.trade_received_at is None
                or self.trade_received_at.tzinfo is None
            )
        ):
            raise ValueError("snapshot trade timestamp must be timezone-aware")
        if (
            self.trade_observed_at is not None
            and self.trade_received_at is not None
            and self.trade_received_at < self.trade_observed_at
        ):
            raise ValueError("snapshot trade receive time cannot precede observation time")
        return self

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / Decimal("2")

    @property
    def spread_pct(self) -> Decimal:
        return (self.ask - self.bid) / self.mid

    @property
    def snapshot_id(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return sha256(payload).hexdigest()
