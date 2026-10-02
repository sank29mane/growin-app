"""Dated, versioned charge schedule: dataclasses, loader and date lookup.

The committed JSON file is the only source of rates. Every numeric in it is a
string. ``schedule_hash`` is the sha256 of the canonical JSON of one version's
raw object, so adding a version never changes an existing version's hash.
"""

from __future__ import annotations

import decimal
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .core import (
    COST_CONTEXT,
    ScheduleError,
    ScheduleNotEffective,
    canonical_json,
    load_strict_json,
    sha256_hex,
    strict_decimal,
)

SCHEDULE_SCHEMA = "growin.costs.charge_schedule/1"
DEFAULT_SCHEDULE_PATH = Path(__file__).parent / "schedules" / "icici_nse_cash_charges.json"

_VERSION_KEYS = (
    "version",
    "effective_from",
    "effective_to",
    "workspace",
    "exchange",
    "segment",
    "currency",
    "plan",
    "status",
    "sources",
    "brokerage",
    "statutory",
    "gst",
    "dp",
    "rounding",
    "account_overhead",
    "excluded_from_delivery_model",
)
_EXPECTED_SCOPE = (
    ("workspace", "india"),
    ("exchange", "NSE"),
    ("segment", "cash"),
    ("currency", "INR"),
)


@dataclass(frozen=True)
class BrokerageRates:
    delivery_rate: decimal.Decimal
    delivery_min_per_order: decimal.Decimal
    intraday_rate: decimal.Decimal
    intraday_cap_per_order: decimal.Decimal


@dataclass(frozen=True)
class StatutoryRates:
    stt_delivery_rate: decimal.Decimal
    stt_intraday_sell_rate: decimal.Decimal
    stamp_delivery_buy_rate: decimal.Decimal
    stamp_intraday_buy_rate: decimal.Decimal
    exchange_transaction_rate: decimal.Decimal
    sebi_fee_rate: decimal.Decimal
    ipft_rate: decimal.Decimal


@dataclass(frozen=True)
class GstRule:
    rate: decimal.Decimal
    applies_to: tuple[str, ...]
    base: str


@dataclass(frozen=True)
class DpRule:
    charge_per_debit: decimal.Decimal
    gst_applies: bool
    basis: str


@dataclass(frozen=True)
class RoundingRule:
    default_quantum: decimal.Decimal
    line_quantum: Mapping[str, decimal.Decimal]


@dataclass(frozen=True)
class PlanFee:
    item: str
    amount_ex_gst: decimal.Decimal | None
    gst_rate: decimal.Decimal | None
    treatment: str


@dataclass(frozen=True)
class AccountOverhead:
    amc_annual_ex_gst: decimal.Decimal
    amc_gst_rate: decimal.Decimal
    amc_note: str
    plan_fees: tuple[PlanFee, ...]


@dataclass(frozen=True)
class ChargeSchedule:
    version: str
    effective_from: date
    effective_to: date | None
    workspace: str
    exchange: str
    segment: str
    currency: str
    plan: str
    status: str
    sources: tuple[str, ...]
    brokerage: BrokerageRates
    statutory: StatutoryRates
    gst: GstRule
    dp: DpRule
    rounding: RoundingRule
    account_overhead: AccountOverhead
    excluded_from_delivery_model: tuple[str, ...]
    schedule_hash: str

    def covers(self, d: date) -> bool:
        if d < self.effective_from:
            return False
        return self.effective_to is None or d <= self.effective_to


@dataclass(frozen=True)
class PricingBasis:
    mode: str
    pinned_version: str | None

    def __post_init__(self) -> None:
        if self.mode not in ("trade_date", "pinned"):
            raise ScheduleError(f"pricing basis mode must be trade_date or pinned, got {self.mode!r}")
        if self.mode == "pinned" and not self.pinned_version:
            raise ScheduleError("a pinned pricing basis needs a version id")
        if self.mode == "trade_date" and self.pinned_version is not None:
            raise ScheduleError("a trade_date pricing basis takes no pinned version")

    @classmethod
    def trade_date(cls) -> "PricingBasis":
        return cls("trade_date", None)

    @classmethod
    def pinned(cls, version: str) -> "PricingBasis":
        return cls("pinned", version)


@dataclass(frozen=True)
class ScheduleSet:
    schema: str
    versions: tuple[ChargeSchedule, ...]

    def for_date(self, d: date) -> ChargeSchedule:
        for version in self.versions:
            if version.covers(d):
                return version
        raise ScheduleNotEffective(f"no charge schedule version is effective on {d.isoformat()}")

    def get(self, version: str) -> ChargeSchedule:
        for candidate in self.versions:
            if candidate.version == version:
                return candidate
        raise ScheduleError(f"unknown schedule version {version!r}")

    def resolve(self, trade_date: date, basis: PricingBasis) -> ChargeSchedule:
        if basis.mode == "pinned":
            assert basis.pinned_version is not None
            return self.get(basis.pinned_version)
        return self.for_date(trade_date)


def _mapping(raw: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(raw, dict):
        raise ScheduleError(f"{path}: expected an object")
    return raw


def _need(raw: Mapping[str, Any], keys: tuple[str, ...], path: str) -> None:
    for key in keys:
        if key not in raw:
            raise ScheduleError(f"{path}.{key}: missing required key")


def _text(raw: Mapping[str, Any], key: str, path: str) -> str:
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise ScheduleError(f"{path}.{key}: expected a non-empty string")
    return value


def _dec(raw: Mapping[str, Any], key: str, path: str) -> decimal.Decimal:
    value = raw[key]
    if not isinstance(value, str):
        raise ScheduleError(f"{path}.{key}: numerics must be JSON strings")
    try:
        return strict_decimal(value, f"{path}.{key}")
    except ValueError as exc:
        raise ScheduleError(str(exc)) from exc


def _opt_dec(raw: Mapping[str, Any], key: str, path: str) -> decimal.Decimal | None:
    if raw[key] is None:
        return None
    return _dec(raw, key, path)


def _date(raw: Mapping[str, Any], key: str, path: str) -> date:
    value = raw[key]
    if not isinstance(value, str):
        raise ScheduleError(f"{path}.{key}: expected an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ScheduleError(f"{path}.{key}: not an ISO date") from exc


def _texts(raw: Mapping[str, Any], key: str, path: str) -> tuple[str, ...]:
    value = raw[key]
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise ScheduleError(f"{path}.{key}: expected a list of non-empty strings")
    return tuple(value)


def _parse_version(raw: Any, index: int) -> ChargeSchedule:
    path = f"versions[{index}]"
    top = _mapping(raw, path)
    _need(top, _VERSION_KEYS, path)
    for key, expected in _EXPECTED_SCOPE:
        if top[key] != expected:
            raise ScheduleError(f"{path}.{key}: expected {expected!r}, got {top[key]!r}")

    brokerage = _mapping(top["brokerage"], f"{path}.brokerage")
    _need(brokerage, ("delivery_rate", "delivery_min_per_order", "intraday_rate", "intraday_cap_per_order"),
          f"{path}.brokerage")
    statutory = _mapping(top["statutory"], f"{path}.statutory")
    stat_keys = (
        "stt_delivery_rate", "stt_intraday_sell_rate", "stamp_delivery_buy_rate",
        "stamp_intraday_buy_rate", "exchange_transaction_rate", "sebi_fee_rate", "ipft_rate",
    )
    _need(statutory, stat_keys, f"{path}.statutory")
    gst = _mapping(top["gst"], f"{path}.gst")
    _need(gst, ("rate", "applies_to", "base"), f"{path}.gst")
    dp = _mapping(top["dp"], f"{path}.dp")
    _need(dp, ("charge_per_debit", "gst_applies", "basis"), f"{path}.dp")
    rounding = _mapping(top["rounding"], f"{path}.rounding")
    _need(rounding, ("default_quantum", "line_quantum"), f"{path}.rounding")
    overhead = _mapping(top["account_overhead"], f"{path}.account_overhead")
    _need(overhead, ("amc_annual_ex_gst", "amc_gst_rate", "amc_note", "plan_fees"), f"{path}.account_overhead")

    line_quantum_raw = _mapping(rounding["line_quantum"], f"{path}.rounding.line_quantum")
    line_quantum = {
        name: _dec(line_quantum_raw, name, f"{path}.rounding.line_quantum") for name in line_quantum_raw
    }
    if not isinstance(dp["gst_applies"], bool):
        raise ScheduleError(f"{path}.dp.gst_applies: expected a boolean")
    fees_raw = overhead["plan_fees"]
    if not isinstance(fees_raw, list):
        raise ScheduleError(f"{path}.account_overhead.plan_fees: expected a list")
    fees = []
    for n, fee_raw in enumerate(fees_raw):
        fee_path = f"{path}.account_overhead.plan_fees[{n}]"
        fee = _mapping(fee_raw, fee_path)
        _need(fee, ("item", "amount_ex_gst", "gst_rate", "treatment"), fee_path)
        fees.append(
            PlanFee(
                _text(fee, "item", fee_path),
                _opt_dec(fee, "amount_ex_gst", fee_path),
                _opt_dec(fee, "gst_rate", fee_path),
                _text(fee, "treatment", fee_path),
            )
        )
    effective_to = None if top["effective_to"] is None else _date(top, "effective_to", path)

    return ChargeSchedule(
        version=_text(top, "version", path),
        effective_from=_date(top, "effective_from", path),
        effective_to=effective_to,
        workspace=top["workspace"],
        exchange=top["exchange"],
        segment=top["segment"],
        currency=top["currency"],
        plan=_text(top, "plan", path),
        status=_text(top, "status", path),
        sources=_texts(top, "sources", path),
        brokerage=BrokerageRates(
            *(_dec(brokerage, k, f"{path}.brokerage") for k in (
                "delivery_rate", "delivery_min_per_order", "intraday_rate", "intraday_cap_per_order"))
        ),
        statutory=StatutoryRates(*(_dec(statutory, k, f"{path}.statutory") for k in stat_keys)),
        gst=GstRule(
            _dec(gst, "rate", f"{path}.gst"),
            _texts(gst, "applies_to", f"{path}.gst"),
            _text(gst, "base", f"{path}.gst"),
        ),
        dp=DpRule(_dec(dp, "charge_per_debit", f"{path}.dp"), dp["gst_applies"], _text(dp, "basis", f"{path}.dp")),
        rounding=RoundingRule(
            _dec(rounding, "default_quantum", f"{path}.rounding"), MappingProxyType(line_quantum)
        ),
        account_overhead=AccountOverhead(
            _dec(overhead, "amc_annual_ex_gst", f"{path}.account_overhead"),
            _dec(overhead, "amc_gst_rate", f"{path}.account_overhead"),
            _text(overhead, "amc_note", f"{path}.account_overhead"),
            tuple(fees),
        ),
        excluded_from_delivery_model=_texts(top, "excluded_from_delivery_model", path),
        schedule_hash=sha256_hex(canonical_json(top)),
    )


def load_schedule_set(path: Path | None = None) -> ScheduleSet:
    source = DEFAULT_SCHEDULE_PATH if path is None else Path(path)
    with decimal.localcontext(COST_CONTEXT):
        raw = _mapping(load_strict_json(source.read_text(encoding="utf-8"), ScheduleError, str(source.name)), "$")
        _need(raw, ("schema", "versions"), "$")
        if raw["schema"] != SCHEDULE_SCHEMA:
            raise ScheduleError(f"$.schema: expected {SCHEDULE_SCHEMA!r}")
        versions_raw = raw["versions"]
        if not isinstance(versions_raw, list) or not versions_raw:
            raise ScheduleError("$.versions: expected a non-empty list")
        versions = tuple(_parse_version(item, n) for n, item in enumerate(versions_raw))
    return ScheduleSet(raw["schema"], versions)
