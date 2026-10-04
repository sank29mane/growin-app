"""Shared types, strict parsing and canonical hashing for backend/costs.

Everything here is pure: Decimal only, no clock, no RNG, no network. The
canonical JSON contract matches ``backend/execution/ledger.py`` ``canonical_json``
but is reimplemented here so this package never imports execution code.
"""

from __future__ import annotations

import dataclasses
import decimal
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import (
    ROUND_FLOOR,
    ROUND_HALF_UP,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
)
from enum import Enum
from typing import Any

MODEL_VERSION = "growin-costs-fills/1"
IST = timezone(timedelta(hours=5, minutes=30), "IST")
COST_CONTEXT = decimal.Context(
    prec=34,
    rounding=ROUND_HALF_UP,
    traps=[InvalidOperation, DivisionByZero, Overflow],
)
LINE_ORDER = (
    "brokerage",
    "exchange_transaction",
    "sebi_fee",
    "ipft",
    "gst",
    "stt",
    "stamp_duty",
    "dp_charge",
    "dp_gst",
)

_DECIMAL_TEXT = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", re.ASCII)
_SHA256_HEX = re.compile(r"[0-9a-f]{64}", re.ASCII)


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class CostModelError(ValueError):
    """Base class for every failure raised by backend/costs."""


class InputError(CostModelError):
    """A caller supplied a malformed or unsafe input."""


class ScheduleError(CostModelError):
    """A schedule file is malformed or cannot be applied."""


class ScheduleNotEffective(ScheduleError):
    """No schedule version covers the requested date."""


class TickSizeUnavailable(InputError):
    """No dated tick size is available. Nothing defaults."""


class LookaheadError(InputError):
    """An input would let a simulated fill use information from its own session."""


def strict_decimal(value: Any, name: str) -> Decimal:
    """Parse a finite Decimal from Decimal, int (not bool) or a plain numeric string."""
    if isinstance(value, bool) or value is None:
        raise InputError(f"{name} must be a Decimal, int or numeric string, got {type(value).__name__}")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise InputError(f"{name} must be finite")
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        if not _DECIMAL_TEXT.fullmatch(value):
            raise InputError(f"{name} is not a plain decimal string")
        return Decimal(value)
    raise InputError(f"{name} must be a Decimal, int or numeric string, got {type(value).__name__}")


def strict_int(value: Any, name: str, *, minimum: int) -> int:
    """Accept an int only (not bool, float, Decimal or str) at or above minimum."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise InputError(f"{name} must be an int, got {type(value).__name__}")
    if value < minimum:
        raise InputError(f"{name} must be at least {minimum}, got {value}")
    return value


def round_money(value: Decimal, quantum: Decimal = Decimal("0.01")) -> Decimal:
    """Quantize with explicit half-up rounding, independent of the global context."""
    with decimal.localcontext(COST_CONTEXT):
        return value.quantize(quantum, rounding=ROUND_HALF_UP)


def floor_to(value: Decimal, quantum: Decimal) -> Decimal:
    """Quantize with explicit round-floor (share counts, tick alignment)."""
    with decimal.localcontext(COST_CONTEXT):
        return value.quantize(quantum, rounding=ROUND_FLOOR)


def require_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InputError(f"{name} must be a non-empty string")
    return value


def require_date(value: Any, name: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise InputError(f"{name} must be a date")
    return value


def require_aware(value: Any, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InputError(f"{name} must be a timezone-aware datetime")
    return value


def require_time(value: Any, name: str) -> time:
    if not isinstance(value, time) or isinstance(value, datetime):
        raise InputError(f"{name} must be a time")
    return value


def require_sha256(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _SHA256_HEX.fullmatch(value):
        raise InputError(f"{name} must be 64 lowercase hex characters")
    return value


def positive_decimal(value: Any, name: str) -> Decimal:
    parsed = strict_decimal(value, name)
    if parsed <= 0:
        raise InputError(f"{name} must be greater than zero")
    return parsed


def set_field(obj: Any, name: str, value: Any) -> None:
    """Assign a normalised value inside a frozen dataclass __post_init__."""
    object.__setattr__(obj, name, value)


def canonical_value(obj: Any) -> Any:
    """Convert a value into JSON-safe primitives with a stable, lossless form."""
    if isinstance(obj, Enum):
        return canonical_value(obj.value)
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: canonical_value(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, Decimal):
        if not obj.is_finite():
            raise TypeError("non-finite Decimal is not canonical")
        return str(obj)
    if isinstance(obj, datetime):
        if obj.tzinfo is None or obj.utcoffset() is None:
            raise TypeError("naive datetime is not canonical")
        return obj.astimezone(timezone.utc).isoformat()
    if isinstance(obj, (date, time)):
        return obj.isoformat()
    if isinstance(obj, (tuple, list)):
        return [canonical_value(item) for item in obj]
    if isinstance(obj, Mapping):
        out: dict[str, Any] = {}
        for key, item in obj.items():
            if not isinstance(key, str):
                raise TypeError("canonical mapping keys must be str")
            out[key] = canonical_value(item)
        return out
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    raise TypeError(f"{type(obj).__name__} is not canonical")


def canonical_json(obj: Any) -> str:
    return json.dumps(
        canonical_value(obj),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def seal(obj: Any, hash_field: str, extra: Mapping[str, Any] | None = None) -> Any:
    """Return obj with hash_field set to the sha256 of every other field (plus extra)."""
    body = canonical_value(obj)
    body.pop(hash_field)
    if extra:
        body["__extra__"] = canonical_value(extra)
    return dataclasses.replace(obj, **{hash_field: sha256_hex(canonical_json(body))})


def load_strict_json(text: str, error: type[CostModelError], what: str) -> Any:
    """Parse JSON rejecting floats, NaN/Infinity constants and duplicate keys."""

    def reject_float(token: str) -> Any:
        raise error(f"{what}: bare JSON number {token} is not allowed; write numerics as strings")

    def reject_constant(token: str) -> Any:
        raise error(f"{what}: constant {token} is not allowed")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, item in pairs:
            if key in out:
                raise error(f"{what}: duplicate key {key!r}")
            out[key] = item
        return out

    try:
        return json.loads(
            text,
            parse_float=reject_float,
            parse_constant=reject_constant,
            object_pairs_hook=reject_duplicates,
        )
    except json.JSONDecodeError as exc:
        raise error(f"{what}: invalid JSON ({exc.msg})") from exc


@dataclass(frozen=True)
class TradeFill:
    order_id: str
    isin: str
    exchange: str
    side: Side
    quantity: int
    price: Decimal
    trade_date: date

    def __post_init__(self) -> None:
        require_text(self.order_id, "order_id")
        require_text(self.isin, "isin")
        require_text(self.exchange, "exchange")
        if not isinstance(self.side, Side):
            raise InputError("side must be a Side")
        strict_int(self.quantity, "quantity", minimum=1)
        set_field(self, "price", positive_decimal(self.price, "price"))
        require_date(self.trade_date, "trade_date")
