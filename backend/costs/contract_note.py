"""Contract-note fixture schema and per-line comparator.

The contract note is the only per-line truth. Trade endpoints return totals
only, and ``preview_order`` has no DP line, no plan or prepaid field and no
intraday mode, so it is a tripwire and never authoritative.

The fixture holds account documents, so loading is defensive on top of the
operator's redaction: a closed schema, no identifier-shaped values, and an
explicit redaction attestation. No contract-note number lives in this
repository; fixtures are supplied locally by the operator.
"""

from __future__ import annotations

import decimal
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .charges import ContractNoteEstimate, price_trade_day
from .core import (
    COST_CONTEXT,
    InputError,
    Side,
    TradeFill,
    canonical_json,
    load_strict_json,
    sha256_hex,
    strict_decimal,
)
from .overhead import brokerage_gst_attributable
from .schedule import PricingBasis, ScheduleSet

FIXTURE_SCHEMA = "growin.costs.contract_note_fixture/1"
TOLERANCE = Decimal("0.05")
REQUIRED_LINES = ("brokerage", "exchange_transaction", "stt", "gst")
REQUIRED_BUY_LINES = ("stamp_duty",)
FIXTURE_RELATIVE_PATH = "docs/icici-docs/_local/contract-notes/golden-delivery-roundtrip.json"

PRINTED_LINES = ("brokerage", "exchange_transaction", "sebi_fee", "ipft", "gst", "stt", "stamp_duty")
GST_PARTS = ("cgst", "sgst", "igst", "utgst")
DP_LINES = ("dp_charge", "dp_gst")
PREVIEW_ORDER_LINE_MAP = {
    "brokerage": "brokerage",
    "exchange_turnover_charges": "exchange_transaction",
    "sebi_charges": "sebi_fee",
    "stt": "stt",
    "stamp_duty": "stamp_duty",
    "gst": "gst",
}
PREVIEW_IGNORED_FIELDS = ("total_turnover_and_sebi_charges", "total_other_charges", "total_brokerage")

_FIXTURE_KEYS = ("schema", "redaction_attested", "provenance", "notes")
_NOTE_REQUIRED = (
    "label",
    "trade_date",
    "exchange",
    "segment",
    "brokerage_billing",
    "dp_source",
    "trades",
    "lines",
    "note_total_charges",
    "unmapped_lines",
)
_NOTE_OPTIONAL = ("gst_components", "dp_lines")
_TRADE_KEYS = ("order_ref", "isin", "side", "quantity", "price")
_BILLING = ("gross", "prepaid_credit_applied")
_DP_SOURCES = ("contract_note", "demat_statement", "not_supplied", "not_applicable")
_TOTAL_SLACK = Decimal("0.01")

_ORDER_REF = re.compile(r"[a-z0-9-]{1,16}", re.ASCII)
_ISIN = re.compile(r"IN[A-Z0-9]{9}[0-9]", re.ASCII)
_PAN_SHAPE = re.compile(r"(?<![A-Za-z0-9])[A-Z]{5}[0-9]{4}[A-Z](?![A-Za-z0-9])", re.ASCII)
_LONG_DIGITS = re.compile(r"[0-9]{8,}", re.ASCII)


class ContractNoteError(InputError):
    """A contract-note fixture or comparison input is malformed."""


@dataclass(frozen=True)
class ContractNoteTrade:
    order_ref: str
    isin: str
    side: Side
    quantity: int
    price: Decimal


@dataclass(frozen=True)
class ContractNote:
    label: str
    trade_date: date
    exchange: str
    segment: str
    brokerage_billing: str
    dp_source: str
    trades: tuple[ContractNoteTrade, ...]
    lines: Mapping[str, Decimal]
    gst_components: Mapping[str, Decimal] | None
    dp_lines: Mapping[str, Decimal] | None
    note_total_charges: Decimal | None
    unmapped_lines: tuple[str, ...]


@dataclass(frozen=True)
class ContractNoteFixture:
    schema: str
    redaction_attested: bool
    provenance: str
    notes: tuple[ContractNote, ...]
    fixture_hash: str


class ReferenceKind(str, Enum):
    CONTRACT_NOTE = "CONTRACT_NOTE"
    PREVIEW_ORDER = "PREVIEW_ORDER"


@dataclass(frozen=True)
class LineCheck:
    line: str
    reference: Decimal
    model: Decimal
    delta: Decimal
    within_tolerance: bool


@dataclass(frozen=True)
class LineComparison:
    kind: ReferenceKind
    authoritative: bool
    rows: tuple[LineCheck, ...]
    unverified: tuple[str, ...]
    unmapped: tuple[str, ...]
    missing_required: tuple[str, ...]
    warnings: tuple[str, ...]
    passed: bool


# ---- loading -----------------------------------------------------------------


def _fail(path: str, message: str) -> ContractNoteError:
    return ContractNoteError(f"{path}: {message}")


def _exact(raw: Any, required: tuple[str, ...], optional: tuple[str, ...], path: str) -> Mapping[str, Any]:
    if not isinstance(raw, dict):
        raise _fail(path, "expected an object")
    for key in raw:
        if key not in required and key not in optional:
            raise _fail(f"{path}.{key}", "unknown key")
    for key in required:
        if key not in raw:
            raise _fail(f"{path}.{key}", "missing required key")
    return raw


def _scan_strings(value: Any, path: str) -> None:
    """Defence in depth: reject identifier-shaped strings anywhere in the document."""
    if isinstance(value, str):
        if _PAN_SHAPE.search(value):
            raise _fail(path, "contains a PAN-shaped value")
        if _LONG_DIGITS.search(value):
            raise _fail(path, "contains a run of 8 or more digits")
        if "@" in value:
            raise _fail(path, "contains an email-like value")
    elif isinstance(value, dict):
        for key, item in value.items():
            _scan_strings(key, f"{path}.<key>")
            _scan_strings(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _scan_strings(item, f"{path}[{index}]")


def _text(raw: Mapping[str, Any], key: str, path: str) -> str:
    value = raw[key]
    if not isinstance(value, str) or not value.strip():
        raise _fail(f"{path}.{key}", "expected a non-empty string")
    return value


def _amount(raw: Mapping[str, Any], key: str, path: str, *, positive: bool = False) -> Decimal:
    value = raw[key]
    if not isinstance(value, str):
        raise _fail(f"{path}.{key}", "amounts must be JSON strings")
    try:
        parsed = strict_decimal(value, f"{path}.{key}")
    except InputError as exc:
        raise _fail(f"{path}.{key}", str(exc)) from exc
    if parsed < 0 or (positive and parsed == 0):
        raise _fail(f"{path}.{key}", "amount must be positive" if positive else "amount must not be negative")
    return parsed


def _amount_map(raw: Any, allowed: tuple[str, ...], path: str) -> Mapping[str, Decimal]:
    if not isinstance(raw, dict) or not raw:
        raise _fail(path, "expected a non-empty object")
    for key in raw:
        if key not in allowed:
            raise _fail(f"{path}.{key}", "unknown key")
    return MappingProxyType({key: _amount(raw, key, path) for key in raw})


def _parse_trade(raw: Any, path: str) -> ContractNoteTrade:
    trade = _exact(raw, _TRADE_KEYS, (), path)
    order_ref = _text(trade, "order_ref", path)
    if not _ORDER_REF.fullmatch(order_ref):
        raise _fail(f"{path}.order_ref", "must match ^[a-z0-9-]{1,16}$")
    isin = _text(trade, "isin", path)
    if not _ISIN.fullmatch(isin):
        raise _fail(f"{path}.isin", "must look like an ISIN")
    side = _text(trade, "side", path)
    if side not in ("BUY", "SELL"):
        raise _fail(f"{path}.side", "must be BUY or SELL")
    quantity = trade["quantity"]
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
        raise _fail(f"{path}.quantity", "expected an integer of at least 1")
    return ContractNoteTrade(order_ref, isin, Side(side), quantity, _amount(trade, "price", path, positive=True))


def _parse_note(raw: Any, index: int) -> ContractNote:
    path = f"notes[{index}]"
    note = _exact(raw, _NOTE_REQUIRED, _NOTE_OPTIONAL, path)
    label = _text(note, "label", path)
    try:
        trade_date = date.fromisoformat(_text(note, "trade_date", path))
    except ValueError as exc:
        raise _fail(f"{path}.trade_date", "not an ISO date") from exc
    if note["exchange"] != "NSE":
        raise _fail(f"{path}.exchange", "expected 'NSE'")
    if note["segment"] != "cash":
        raise _fail(f"{path}.segment", "expected 'cash'")
    billing = _text(note, "brokerage_billing", path)
    if billing not in _BILLING:
        raise _fail(f"{path}.brokerage_billing", f"must be one of {_BILLING}")
    dp_source = _text(note, "dp_source", path)
    if dp_source not in _DP_SOURCES:
        raise _fail(f"{path}.dp_source", f"must be one of {_DP_SOURCES}")
    trades_raw = note["trades"]
    if not isinstance(trades_raw, list) or not trades_raw:
        raise _fail(f"{path}.trades", "expected a non-empty list")
    trades = tuple(_parse_trade(item, f"{path}.trades[{n}]") for n, item in enumerate(trades_raw))
    lines_raw = note["lines"]
    if not isinstance(lines_raw, dict) or not lines_raw:
        raise _fail(f"{path}.lines", "a contract note must print its charge lines; the mapping is empty")
    lines = _amount_map(lines_raw, PRINTED_LINES, f"{path}.lines")
    required = REQUIRED_LINES + (REQUIRED_BUY_LINES if any(t.side is Side.BUY for t in trades) else ())
    for name in required:
        if name not in lines:
            raise _fail(f"{path}.lines.{name}", "a contract note must print this line")

    gst_components = None
    if note.get("gst_components") is not None:
        gst_components = _amount_map(note["gst_components"], GST_PARTS, f"{path}.gst_components")
        if sum(gst_components.values(), Decimal(0)) != lines["gst"]:
            raise _fail(f"{path}.gst_components", "components must sum exactly to lines.gst")

    dp_lines = None
    if dp_source in ("contract_note", "demat_statement"):
        if note.get("dp_lines") is None:
            raise _fail(f"{path}.dp_lines", f"required when dp_source is {dp_source}")
        dp_lines = _amount_map(note["dp_lines"], DP_LINES, f"{path}.dp_lines")
    elif note.get("dp_lines") is not None:
        raise _fail(f"{path}.dp_lines", f"must be absent when dp_source is {dp_source}")

    total = None
    if note["note_total_charges"] is not None:
        total = _amount(note, "note_total_charges", path)
        printed = sum(lines.values(), Decimal(0))
        if dp_source == "contract_note" and dp_lines is not None:
            printed += sum(dp_lines.values(), Decimal(0))
        if abs(printed - total) > _TOTAL_SLACK:
            raise _fail(f"{path}.note_total_charges", "differs from the sum of the printed lines by more than 0.01")

    unmapped = note["unmapped_lines"]
    if not isinstance(unmapped, list) or not all(isinstance(item, str) and item for item in unmapped):
        raise _fail(f"{path}.unmapped_lines", "expected a list of non-empty strings")
    return ContractNote(
        label=label,
        trade_date=trade_date,
        exchange="NSE",
        segment="cash",
        brokerage_billing=billing,
        dp_source=dp_source,
        trades=trades,
        lines=lines,
        gst_components=gst_components,
        dp_lines=dp_lines,
        note_total_charges=total,
        unmapped_lines=tuple(unmapped),
    )


def load_fixture_text(text: str) -> ContractNoteFixture:
    with decimal.localcontext(COST_CONTEXT):
        raw = load_strict_json(text, ContractNoteError, "contract-note fixture")
        document = _exact(raw, _FIXTURE_KEYS, (), "$")
        _scan_strings(raw, "$")
        if document["schema"] != FIXTURE_SCHEMA:
            raise _fail("$.schema", f"expected {FIXTURE_SCHEMA!r}")
        if document["redaction_attested"] is not True:
            raise _fail("$.redaction_attested", "must be true; the fixture must be redacted by the operator")
        provenance = _text(document, "provenance", "$")
        notes_raw = document["notes"]
        if not isinstance(notes_raw, list) or not notes_raw:
            raise _fail("$.notes", "expected a non-empty list")
        notes = tuple(_parse_note(item, n) for n, item in enumerate(notes_raw))
        return ContractNoteFixture(
            schema=FIXTURE_SCHEMA,
            redaction_attested=True,
            provenance=provenance,
            notes=notes,
            fixture_hash=sha256_hex(canonical_json(raw)),
        )


def load_fixture(path: Path) -> ContractNoteFixture:
    return load_fixture_text(Path(path).read_text(encoding="utf-8"))


# ---- pricing and comparison --------------------------------------------------


def price_note(note: ContractNote, schedules: ScheduleSet, *, workspace: str, currency: str) -> ContractNoteEstimate:
    """Price a note's trades under the trade-date basis (ScheduleNotEffective before coverage)."""
    basis = PricingBasis.trade_date()
    fills = [
        TradeFill(trade.order_ref, trade.isin, note.exchange, trade.side, trade.quantity, trade.price, note.trade_date)
        for trade in note.trades
    ]
    return price_trade_day(
        fills, schedules.resolve(note.trade_date, basis), workspace=workspace, currency=currency, pricing_basis=basis
    )


def _checked_tolerance(tolerance: Decimal) -> Decimal:
    value = strict_decimal(tolerance, "tolerance")
    if value < 0 or value > TOLERANCE:
        raise InputError(f"tolerance must be between 0 and {TOLERANCE}; it cannot be loosened")
    return value


def _row(line: str, reference: Decimal, model: Decimal, tolerance: Decimal) -> LineCheck:
    delta = reference - model
    return LineCheck(line, reference, model, delta, abs(delta) <= tolerance)


def _unverified(estimate: ContractNoteEstimate, covered: set[str]) -> tuple[str, ...]:
    return tuple(
        line.name for line in estimate.lines if line.name not in covered and line.amount != 0
    )


def compare_note(
    note: ContractNote,
    estimate: ContractNoteEstimate,
    *,
    schedules: ScheduleSet,
    tolerance: Decimal = TOLERANCE,
) -> LineComparison:
    with decimal.localcontext(COST_CONTEXT):
        tol = _checked_tolerance(tolerance)
        model = {line.name: line.amount for line in estimate.lines}
        if note.brokerage_billing == "prepaid_credit_applied":
            schedule = schedules.get(estimate.schedule_version)
            model["gst"] = model["gst"] - brokerage_gst_attributable(estimate, schedule)
            model["brokerage"] = Decimal("0.00")
        rows = [_row(name, amount, model[name], tol) for name, amount in note.lines.items()]
        covered = set(note.lines)
        for name, amount in (note.dp_lines or {}).items():
            rows.append(_row(name, amount, model[name], tol))
            covered.add(name)
        required = REQUIRED_LINES + (REQUIRED_BUY_LINES if any(t.side is Side.BUY for t in note.trades) else ())
        missing = tuple(name for name in required if name not in note.lines)
        unmapped = tuple(note.unmapped_lines)
        return LineComparison(
            kind=ReferenceKind.CONTRACT_NOTE,
            authoritative=True,
            rows=tuple(rows),
            unverified=_unverified(estimate, covered),
            unmapped=unmapped,
            missing_required=missing,
            warnings=(),
            passed=bool(rows) and all(r.within_tolerance for r in rows) and not unmapped and not missing,
        )


def compare_preview(
    preview_fields: Mapping[str, Any],
    estimate: ContractNoteEstimate,
    *,
    tolerance: Decimal = TOLERANCE,
) -> LineComparison:
    """Advisory tripwire against preview_order output. Never authoritative."""
    with decimal.localcontext(COST_CONTEXT):
        tol = _checked_tolerance(tolerance)
        if len(estimate.order_brokerage) != 1:
            raise InputError("preview_order describes one order; compare against a single-order estimate")
        model = {line.name: line.amount for line in estimate.lines}
        rows: list[LineCheck] = []
        warnings: list[str] = []
        unmapped: list[str] = []
        covered: set[str] = set()
        for field in sorted(preview_fields):
            if field in PREVIEW_IGNORED_FIELDS:
                continue
            target = PREVIEW_ORDER_LINE_MAP.get(field)
            if target is None:
                unmapped.append(field)
                continue
            row = _row(target, strict_decimal(preview_fields[field], f"preview.{field}"), model[target], tol)
            rows.append(row)
            covered.add(target)
            if not row.within_tolerance:
                warnings.append(
                    f"preview {field} {row.reference} differs from the model {row.model} by {row.delta}"
                )
        return LineComparison(
            kind=ReferenceKind.PREVIEW_ORDER,
            authoritative=False,
            rows=tuple(rows),
            unverified=_unverified(estimate, covered),
            unmapped=tuple(unmapped),
            missing_required=(),
            warnings=tuple(warnings),
            passed=bool(rows) and all(r.within_tolerance for r in rows),
        )
