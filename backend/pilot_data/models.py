"""Frozen value objects shared by the pilot data modules."""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .core import canonical_sha256, dec_str


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _d(value: Decimal | None) -> str | None:
    return None if value is None else dec_str(value)


class BhavcopyBar(_Frozen):
    file_kind: Literal["udiff", "cm_legacy", "pr_pd"]
    trade_date: date
    nse_symbol: str = Field(..., min_length=1)
    series: str = Field(..., min_length=1)
    isin: str | None = None
    token: int | None = None
    open: Decimal = Field(..., allow_inf_nan=False)
    high: Decimal = Field(..., allow_inf_nan=False)
    low: Decimal = Field(..., allow_inf_nan=False)
    close: Decimal = Field(..., allow_inf_nan=False)
    prev_close: Decimal = Field(..., allow_inf_nan=False, ge=0)
    last: Decimal | None = Field(default=None, allow_inf_nan=False)
    volume: int = Field(..., ge=0)
    traded_value: Decimal = Field(..., allow_inf_nan=False, ge=0)
    trades: int | None = None
    adjustment_basis: Literal["as_traded"] = "as_traded"

    @model_validator(mode="after")
    def _isin_required_except_pd(self) -> "BhavcopyBar":
        if self.isin is None and self.file_kind != "pr_pd":
            raise ValueError("isin is required for udiff and cm_legacy bars")
        return self

    def row_sha256(self) -> str:
        return canonical_sha256(
            {
                "file_kind": self.file_kind,
                "trade_date": self.trade_date.isoformat(),
                "nse_symbol": self.nse_symbol,
                "series": self.series,
                "isin": self.isin,
                "token": self.token,
                "open": _d(self.open),
                "high": _d(self.high),
                "low": _d(self.low),
                "close": _d(self.close),
                "prev_close": _d(self.prev_close),
                "last": _d(self.last),
                "volume": self.volume,
                "traded_value": _d(self.traded_value),
                "trades": self.trades,
                "adjustment_basis": self.adjustment_basis,
            }
        )


class SecurityMasterRow(_Frozen):
    token: int = Field(..., ge=0)
    stock_code: str = Field(..., pattern=r"^[A-Z0-9]{1,10}$")
    series: str
    company_name: str
    tick_size_raw: str
    isin: str
    nse_symbol: str
    listing_raw: str
    delisting_raw: str
    delete_flag_raw: str

    def row_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class DailyBarConvention(_Frozen):
    """How a daily bar's timestamp maps to a trade date. Always explicit, never defaulted."""

    allowed_times: tuple[str, ...] = Field(..., min_length=1)
    datetime_format: Literal["%Y-%m-%d %H:%M:%S"] = "%Y-%m-%d %H:%M:%S"
    date_basis: Literal["date_component"] = "date_component"
    source: str = Field(..., min_length=1)

    @field_validator("allowed_times")
    @classmethod
    def _times_well_formed(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if not re.fullmatch(r"\d\d:\d\d:\d\d", item):
                raise ValueError("allowed_times entries must look like HH:MM:SS")
        return value


class BreezeDailyBar(_Frozen):
    stock_code: str = Field(..., min_length=1)
    trade_date: date
    raw_datetime: str
    open: Decimal = Field(..., gt=0, allow_inf_nan=False)
    high: Decimal = Field(..., gt=0, allow_inf_nan=False)
    low: Decimal = Field(..., gt=0, allow_inf_nan=False)
    close: Decimal = Field(..., gt=0, allow_inf_nan=False)
    volume: int = Field(..., ge=0)
    api: Literal["breeze_v2_historical"] = "breeze_v2_historical"
    interval: Literal["1day"] = "1day"
    adjustment_basis: Literal["as_traded_claimed"] = "as_traded_claimed"

    def row_sha256(self) -> str:
        return canonical_sha256(
            {
                "stock_code": self.stock_code,
                "trade_date": self.trade_date.isoformat(),
                "raw_datetime": self.raw_datetime,
                "open": dec_str(self.open),
                "high": dec_str(self.high),
                "low": dec_str(self.low),
                "close": dec_str(self.close),
                "volume": self.volume,
                "api": self.api,
                "interval": self.interval,
                "adjustment_basis": self.adjustment_basis,
            }
        )


class IsinSegment(_Frozen):
    isin: str
    nse_symbol: str
    valid_from: date
    valid_to: date
    link: Literal["anchor", "isin_continuity", "isin_change_split", "master_snapshot"]
    evidence: str | None = None
    token: int | None = None

    @model_validator(mode="after")
    def _ordered(self) -> "IsinSegment":
        if self.valid_from > self.valid_to:
            raise ValueError("segment valid_from must not be after valid_to")
        return self


class Lineage(_Frozen):
    workspace: Literal["india"]
    anchor_isin: str
    anchor_series: str
    stock_code: str | None = None
    segments: tuple[IsinSegment, ...] = Field(..., min_length=1)
    resolved_from: date
    unresolved_before: date | None = None
    unresolved_reason: str | None = None
    built_as_of: date
    basis: Literal["master_snapshot_single_segment", "bhavcopy_walk"]

    @model_validator(mode="after")
    def _segments_ascending(self) -> "Lineage":
        previous: IsinSegment | None = None
        for segment in self.segments:
            if previous is not None and segment.valid_from <= previous.valid_to:
                raise ValueError("lineage segments must be ascending and non-overlapping")
            previous = segment
        return self

    def _segment_on(self, day: date) -> IsinSegment | None:
        for segment in self.segments:
            if segment.valid_from <= day <= segment.valid_to:
                return segment
        return None

    def isin_on(self, day: date) -> str | None:
        segment = self._segment_on(day)
        return None if segment is None else segment.isin

    def symbol_on(self, day: date) -> str | None:
        segment = self._segment_on(day)
        return None if segment is None else segment.nse_symbol

    def content_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class RawDailyBar(_Frozen):
    trade_date: date
    isin: str
    series: str
    nse_symbol: str
    open: Decimal = Field(..., allow_inf_nan=False)
    high: Decimal = Field(..., allow_inf_nan=False)
    low: Decimal = Field(..., allow_inf_nan=False)
    close: Decimal = Field(..., allow_inf_nan=False)
    volume: int = Field(..., ge=0)
    traded_value: Decimal | None = Field(default=None, allow_inf_nan=False)
    source_kind: Literal["bhavcopy", "breeze"]
    source_sha256: str


class QuarantineRecord(_Frozen):
    workspace: Literal["india"]
    check: str = Field(..., pattern=r"^[a-z_]+$")
    reason_code: str = Field(..., pattern=r"^[a-z0-9_]+$")
    scope: Literal["raw", "adjusted", "both"]
    isin: str | None = None
    nse_symbol: str | None = None
    stock_code: str | None = None
    series: str | None = None
    date_from: date | None = None
    date_to: date | None = None
    detail: dict[str, str] = Field(default_factory=dict)
    evidence_sha256s: tuple[str, ...] = ()
    run_id: str | None = None

    @field_validator("evidence_sha256s")
    @classmethod
    def _sorted(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(set(value)))

    @model_validator(mode="after")
    def _date_order(self) -> "QuarantineRecord":
        if self.date_from is not None and self.date_to is not None and self.date_from > self.date_to:
            raise ValueError("date_from must not be after date_to")
        return self

    @property
    def quarantine_id(self) -> str:
        return canonical_sha256(self.model_dump(mode="json"))


class CrossCheckTolerances(_Frozen):
    """Operator-locked D-06 tolerances: close 0.5 percent, open/high/low 1 percent."""

    version: Literal["pilot-xcheck/1"] = "pilot-xcheck/1"
    close_max_rel: Decimal = Field(default=Decimal("0.005"), gt=0, allow_inf_nan=False)
    ohl_max_rel: Decimal = Field(default=Decimal("0.01"), gt=0, allow_inf_nan=False)


class ParseQuarantineInput(_Frozen):
    """A parser's request to quarantine a row; the ingest function turns it into a record."""

    reason_code: str = Field(..., pattern=r"^[a-z0-9_]+$")
    trade_date: date | None = None
    nse_symbol: str | None = None
    series: str | None = None
    isin: str | None = None
    stock_code: str | None = None
    detail: dict[str, str] = Field(default_factory=dict)
