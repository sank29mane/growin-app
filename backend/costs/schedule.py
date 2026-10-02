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
    LINE_ORDER,
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
_BROKERAGE_KEYS = ("delivery_rate", "delivery_min_per_order", "intraday_rate", "intraday_cap_per_order")
_STATUTORY_KEYS = (
    "stt_delivery_rate",
    "stt_intraday_sell_rate",
    "stamp_delivery_buy_rate",
    "stamp_intraday_buy_rate",
    "exchange_transaction_rate",
    "sebi_fee_rate",
    "ipft_rate",
)
_GST_LINES = ("brokerage", "exchange_transaction", "sebi_fee", "ipft")
_GST_BASES = ("rounded_lines", "unrounded_lines")
_DP_BASES = ("per_sell_order", "per_isin_per_day")
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


def _exact(raw: Mapping[str, Any], keys: tuple[str, ...], path: str) -> None:
    """Closed allow-list: the mapping must hold exactly ``keys``."""
    for key in raw:
        if key not in keys:
            raise ScheduleError(f"{path}.{key}: unknown key")
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


def _rate(raw: Mapping[str, Any], key: str, path: str) -> decimal.Decimal:
    value = _dec(raw, key, path)
    if value < 0 or value >= 1:
        raise ScheduleError(f"{path}.{key}: a rate must be at least 0 and below 1, got {value}")
    return value


def _money(raw: Mapping[str, Any], key: str, path: str) -> decimal.Decimal:
    value = _dec(raw, key, path)
    if value < 0:
        raise ScheduleError(f"{path}.{key}: an amount must not be negative, got {value}")
    return value


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


def _parse_brokerage(raw: Any, path: str) -> BrokerageRates:
    top = _mapping(raw, path)
    _exact(top, _BROKERAGE_KEYS, path)
    return BrokerageRates(
        _rate(top, "delivery_rate", path),
        _money(top, "delivery_min_per_order", path),
        _rate(top, "intraday_rate", path),
        _money(top, "intraday_cap_per_order", path),
    )


def _parse_statutory(raw: Any, path: str) -> StatutoryRates:
    top = _mapping(raw, path)
    _exact(top, _STATUTORY_KEYS, path)
    return StatutoryRates(*(_rate(top, key, path) for key in _STATUTORY_KEYS))


def _parse_gst(raw: Any, path: str) -> GstRule:
    top = _mapping(raw, path)
    _exact(top, ("rate", "applies_to", "base"), path)
    applies_to = _texts(top, "applies_to", path)
    for name in applies_to:
        if name not in _GST_LINES:
            raise ScheduleError(f"{path}.applies_to: {name!r} is not a GST-bearing line {_GST_LINES}")
    if len(set(applies_to)) != len(applies_to):
        raise ScheduleError(f"{path}.applies_to: duplicate entry")
    base = _text(top, "base", path)
    if base not in _GST_BASES:
        raise ScheduleError(f"{path}.base: must be one of {_GST_BASES}, got {base!r}")
    return GstRule(_rate(top, "rate", path), applies_to, base)


def _parse_dp(raw: Any, path: str) -> DpRule:
    top = _mapping(raw, path)
    _exact(top, ("charge_per_debit", "gst_applies", "basis"), path)
    if not isinstance(top["gst_applies"], bool):
        raise ScheduleError(f"{path}.gst_applies: expected a boolean")
    basis = _text(top, "basis", path)
    if basis not in _DP_BASES:
        raise ScheduleError(f"{path}.basis: must be one of {_DP_BASES}, got {basis!r}")
    return DpRule(_money(top, "charge_per_debit", path), top["gst_applies"], basis)


def _parse_rounding(raw: Any, path: str) -> RoundingRule:
    top = _mapping(raw, path)
    _exact(top, ("default_quantum", "line_quantum"), path)
    default = _dec(top, "default_quantum", path)
    if default <= 0:
        raise ScheduleError(f"{path}.default_quantum: must be greater than zero")
    quantum_raw = _mapping(top["line_quantum"], f"{path}.line_quantum")
    line_quantum: dict[str, decimal.Decimal] = {}
    for name in quantum_raw:
        if name not in LINE_ORDER:
            raise ScheduleError(f"{path}.line_quantum.{name}: unknown charge line")
        value = _dec(quantum_raw, name, f"{path}.line_quantum")
        if value <= 0:
            raise ScheduleError(f"{path}.line_quantum.{name}: must be greater than zero")
        line_quantum[name] = value
    return RoundingRule(default, MappingProxyType(line_quantum))


def _parse_overhead(raw: Any, path: str) -> AccountOverhead:
    top = _mapping(raw, path)
    _exact(top, ("amc_annual_ex_gst", "amc_gst_rate", "amc_note", "plan_fees"), path)
    fees_raw = top["plan_fees"]
    if not isinstance(fees_raw, list):
        raise ScheduleError(f"{path}.plan_fees: expected a list")
    fees: list[PlanFee] = []
    for n, fee_raw in enumerate(fees_raw):
        fee_path = f"{path}.plan_fees[{n}]"
        fee = _mapping(fee_raw, fee_path)
        _exact(fee, ("item", "amount_ex_gst", "gst_rate", "treatment"), fee_path)
        fees.append(
            PlanFee(
                _text(fee, "item", fee_path),
                None if fee["amount_ex_gst"] is None else _money(fee, "amount_ex_gst", fee_path),
                None if fee["gst_rate"] is None else _rate(fee, "gst_rate", fee_path),
                _text(fee, "treatment", fee_path),
            )
        )
    if len({fee.item for fee in fees}) != len(fees):
        raise ScheduleError(f"{path}.plan_fees: duplicate item")
    return AccountOverhead(
        _money(top, "amc_annual_ex_gst", path),
        _rate(top, "amc_gst_rate", path),
        _text(top, "amc_note", path),
        tuple(fees),
    )


def _parse_version(raw: Any, index: int) -> ChargeSchedule:
    path = f"versions[{index}]"
    top = _mapping(raw, path)
    _exact(top, _VERSION_KEYS, path)
    for key, expected in _EXPECTED_SCOPE:
        if top[key] != expected:
            raise ScheduleError(f"{path}.{key}: expected {expected!r}, got {top[key]!r}")
    effective_from = _date(top, "effective_from", path)
    effective_to = None if top["effective_to"] is None else _date(top, "effective_to", path)
    if effective_to is not None and effective_to < effective_from:
        raise ScheduleError(f"{path}.effective_to: precedes effective_from")
    return ChargeSchedule(
        version=_text(top, "version", path),
        effective_from=effective_from,
        effective_to=effective_to,
        workspace=top["workspace"],
        exchange=top["exchange"],
        segment=top["segment"],
        currency=top["currency"],
        plan=_text(top, "plan", path),
        status=_text(top, "status", path),
        sources=_texts(top, "sources", path),
        brokerage=_parse_brokerage(top["brokerage"], f"{path}.brokerage"),
        statutory=_parse_statutory(top["statutory"], f"{path}.statutory"),
        gst=_parse_gst(top["gst"], f"{path}.gst"),
        dp=_parse_dp(top["dp"], f"{path}.dp"),
        rounding=_parse_rounding(top["rounding"], f"{path}.rounding"),
        account_overhead=_parse_overhead(top["account_overhead"], f"{path}.account_overhead"),
        excluded_from_delivery_model=_texts(top, "excluded_from_delivery_model", path),
        schedule_hash=sha256_hex(canonical_json(top)),
    )


def _check_version_set(versions: list[ChargeSchedule]) -> tuple[ChargeSchedule, ...]:
    ids = [version.version for version in versions]
    if len(set(ids)) != len(ids):
        raise ScheduleError("$.versions: duplicate version id")
    ordered = sorted(versions, key=lambda version: version.effective_from)
    for earlier, later in zip(ordered, ordered[1:]):
        if earlier.effective_to is None:
            raise ScheduleError(
                f"$.versions: {earlier.version} is open-ended but is not the latest version"
            )
        if earlier.effective_to >= later.effective_from:
            raise ScheduleError(f"$.versions: {earlier.version} overlaps {later.version}")
    return tuple(ordered)


def load_schedule_set(path: Path | None = None) -> ScheduleSet:
    source = DEFAULT_SCHEDULE_PATH if path is None else Path(path)
    with decimal.localcontext(COST_CONTEXT):
        raw = _mapping(load_strict_json(source.read_text(encoding="utf-8"), ScheduleError, str(source.name)), "$")
        _exact(raw, ("schema", "versions"), "$")
        if raw["schema"] != SCHEDULE_SCHEMA:
            raise ScheduleError(f"$.schema: expected {SCHEDULE_SCHEMA!r}")
        versions_raw = raw["versions"]
        if not isinstance(versions_raw, list) or not versions_raw:
            raise ScheduleError("$.versions: expected a non-empty list")
        versions = _check_version_set([_parse_version(item, n) for n, item in enumerate(versions_raw)])
    return ScheduleSet(raw["schema"], versions)
