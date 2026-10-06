"""Strict parser for the O3 order intent.

The Mac's intent POST is untrusted input. Anything not exactly the contract is
refused with a short code: a missing or extra key, a duplicate key, a float, a
JSON boolean where a number belongs, a non-ASCII byte, a regex miss. Values are
never echoed back in an error.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from . import intent_invalid, refusal

MAX_BODY_BYTES = 4096

_INTENT_ID = re.compile(r"[A-Za-z0-9_-]{8,96}")
_PROPOSAL_ID = re.compile(r"[A-Za-z0-9_-]{1,96}")
_STOCK_CODE = re.compile(r"[A-Z0-9]{1,10}")
_ISIN = re.compile(r"IN[A-Z0-9]{9}[0-9]")
_LIMIT_PRICE = re.compile(r"[0-9]{1,7}(\.[0-9]{1,2})?")
_BATCH_ID = re.compile(r"[a-z0-9-]{8,64}")
_HEX64 = re.compile(r"[0-9a-f]{64}")

REASONS = frozenset({"entry", "exit", "halve", "flatten", "stop"})

FIELDS: tuple[str, ...] = (
    "intent_id",
    "proposal_id",
    "workspace",
    "broker",
    "mode",
    "exchange",
    "product",
    "order_type",
    "validity",
    "side",
    "stock_code",
    "isin",
    "quantity",
    "limit_price",
    "reason",
    "batch_id",
    "limits_sha256",
    "params_sha256",
    "key_id",
)

_CONSTANTS = {
    "workspace": "india",
    "broker": "icici-breeze",
    "exchange": "NSE",
    "product": "cash",
    "order_type": "limit",
    "validity": "day",
}

_REGEX_FIELDS = {
    "intent_id": _INTENT_ID,
    "proposal_id": _PROPOSAL_ID,
    "stock_code": _STOCK_CODE,
    "isin": _ISIN,
    "limit_price": _LIMIT_PRICE,
    "limits_sha256": _HEX64,
    "params_sha256": _HEX64,
    "key_id": _HEX64,
}


@dataclass(frozen=True)
class Intent:
    intent_id: str
    proposal_id: str
    workspace: str
    broker: str
    mode: str
    exchange: str
    product: str
    order_type: str
    validity: str
    side: str
    stock_code: str
    isin: str
    quantity: int
    limit_price: str
    reason: str
    batch_id: str | None
    limits_sha256: str
    params_sha256: str
    key_id: str

    def as_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in FIELDS}

    @property
    def price(self) -> Decimal:
        return Decimal(self.limit_price)

    @property
    def is_buy(self) -> bool:
        return self.side == "buy"


def _pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate_key")
        out[key] = value
    return out


def _no_float(_text: str) -> Any:
    raise ValueError("float_not_allowed")


def _no_constant(_text: str) -> Any:
    raise ValueError("float_not_allowed")


def parse_intent(raw: bytes | str) -> Intent:
    """Parse one O3 body. Raises OrderRefusal (422, or 423 live_disabled)."""
    if isinstance(raw, str):
        if not raw.isascii():
            raise intent_invalid("non_ascii")
        data = raw.encode("ascii")
    else:
        data = bytes(raw)
    if len(data) > MAX_BODY_BYTES:
        raise intent_invalid("too_large")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise intent_invalid("non_ascii") from None
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_pairs_hook,
            parse_float=_no_float,
            parse_constant=_no_constant,
        )
    except ValueError as exc:
        reason = str(exc)
        if reason in ("duplicate_key", "float_not_allowed"):
            raise intent_invalid(reason) from None
        raise intent_invalid("bad_json") from None
    except RecursionError:
        raise intent_invalid("bad_json") from None
    if not isinstance(parsed, dict):
        raise intent_invalid("not_object")

    missing = [name for name in FIELDS if name not in parsed]
    if missing:
        raise intent_invalid("missing_key")
    if len(parsed) != len(FIELDS):
        raise intent_invalid("extra_key")

    for name in FIELDS:
        value = parsed[name]
        if name == "quantity":
            # bool is an int subclass: refuse it before the range check.
            if isinstance(value, bool) or not isinstance(value, int):
                raise intent_invalid("bad_type")
        elif name == "batch_id":
            if value is not None and not isinstance(value, str):
                raise intent_invalid("bad_type")
        elif not isinstance(value, str):
            raise intent_invalid("bad_type")

    if parsed["mode"] == "LIVE":
        raise refusal("live_disabled")
    if parsed["mode"] != "SHADOW":
        raise intent_invalid("bad_field:mode")

    for name, expected in _CONSTANTS.items():
        if parsed[name] != expected:
            raise intent_invalid(f"bad_field:{name}")
    if parsed["side"] not in ("buy", "sell"):
        raise intent_invalid("bad_field:side")
    if parsed["reason"] not in REASONS:
        raise intent_invalid("bad_field:reason")
    for name, pattern in _REGEX_FIELDS.items():
        if pattern.fullmatch(parsed[name]) is None:
            raise intent_invalid(f"bad_field:{name}")
    batch = parsed["batch_id"]
    if batch is not None and _BATCH_ID.fullmatch(batch) is None:
        raise intent_invalid("bad_field:batch_id")
    if not 1 <= parsed["quantity"] <= 100000:
        raise intent_invalid("bad_field:quantity")
    if Decimal(parsed["limit_price"]) <= 0:
        raise intent_invalid("bad_field:limit_price")

    return Intent(**{name: parsed[name] for name in FIELDS})
