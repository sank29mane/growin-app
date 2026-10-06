"""The Mac's own India order rules (RISK-03, P-06, P-08 to P-10, P-14, P-16; D-09, D-11).

Pure Decimal code over explicit inputs. It reads no file, no clock and no network,
and it imports nothing from ``gateway/``. The VM keeps its own implementation in
``gateway_vm.orders.limits``; the two are held together by the shared vectors in
``tests/backend/fixtures/relay_orders/limits_vectors.json``, not by shared code, so a
mistake in one cannot silently become a mistake in both.

``evaluate`` returns every applicable reason code in a fixed precedence, so the first
is the answer and the rest are detail. The names are the O6 reason names of
``growin-orders/1`` and compare as strings against the VM's codes.

Money, prices and ratios are ``Decimal`` built from strings. Trigger edges are
inclusive, as in Phase 62's ``update_risk``. Ticks come from the Mac's own resolver,
``costs.ticks.resolve_nse_cash_tick``, never from the VM's vendored copy. The band
reference is ``Quote.tick_reference`` (the previous month's last close), not the
quote's ``previous_close``. It must carry ``Quote.tick_reference_month`` in the calendar
month before the session date; a missing, zero, undated, stale or wrong-month one is
``tick_reference_unavailable``.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from costs.core import CostModelError
from costs.ticks import InstrumentClass, resolve_nse_cash_tick

IST = timezone(timedelta(hours=5, minutes=30))  # no DST: a fixed offset is exact
SESSION_OPEN = time(9, 15)
ORDER_CUTOFF = time(15, 10)  # D-11: closing auction, nothing new at or after
SUPPORTED_SERIES = frozenset({"EQ", "BE"})

ZERO = Decimal(0)
ONE = Decimal(1)
BPS = Decimal(10000)

LIMIT_KEYS = (
    "schema_version",
    "workspace",
    "currency",
    "capital_cap",
    "per_position_cap",
    "drawdown_halt",
    "drawdown_flatten",
    "position_stop",
    "fat_finger_collar",
)
_DECIMAL_KEYS = LIMIT_KEYS[3:]
_DECIMAL_RE = re.compile(r"-?(0|[1-9][0-9]*)(\.[0-9]+)?")

SLIPPAGE_LIMIT = "SLIPPAGE_LIMIT"
# The bid or ask for the order's side is absent or unusable: refused, never guessed from ltp.
SLIPPAGE_QUOTE_UNAVAILABLE = "SLIPPAGE_QUOTE_UNAVAILABLE"


class RiskConfigError(ValueError):
    """A limits value, cap or input is invalid. Fail closed: nothing is admitted on it."""


# ------------------------------------------------------------------- limits


@dataclass(frozen=True)
class Limits:
    capital_cap: Decimal
    per_position_cap: Decimal
    drawdown_halt: Decimal
    drawdown_flatten: Decimal
    position_stop: Decimal
    fat_finger_collar: Decimal
    sha256: str

    @classmethod
    def from_fields(cls, fields: Mapping[str, Any]) -> "Limits":
        """Validate the nine contract keys and compute ``limits_sha256`` (P-06)."""
        if not isinstance(fields, Mapping):
            raise RiskConfigError("limits must be an object")
        if set(fields) != set(LIMIT_KEYS):
            raise RiskConfigError("limits keys are not exactly the contract keys")
        version = fields["schema_version"]
        if isinstance(version, bool) or not isinstance(version, int) or version != 1:
            raise RiskConfigError("schema_version must be the integer 1")
        if fields["workspace"] != "india":
            raise RiskConfigError("workspace must be india")
        if fields["currency"] != "INR":
            raise RiskConfigError("currency must be INR")
        values: dict[str, Decimal] = {}
        for key in _DECIMAL_KEYS:
            raw = fields[key]
            if not isinstance(raw, str) or _DECIMAL_RE.fullmatch(raw) is None:
                raise RiskConfigError(f"{key} must be a decimal string")
            try:
                values[key] = Decimal(raw)
            except InvalidOperation as exc:  # pragma: no cover - the regex already guards
                raise RiskConfigError(f"{key} is not a number") from exc
        cap, per = values["capital_cap"], values["per_position_cap"]
        halt, flatten = values["drawdown_halt"], values["drawdown_flatten"]
        stop, collar = values["position_stop"], values["fat_finger_collar"]
        if not cap > ZERO:
            raise RiskConfigError("capital_cap must be positive")
        if not ZERO < per <= cap:
            raise RiskConfigError("per_position_cap must be positive and at most capital_cap")
        if not halt < ZERO:
            raise RiskConfigError("drawdown_halt must be negative")
        if not -ONE < flatten < halt:
            raise RiskConfigError("drawdown_flatten must lie between -1 and drawdown_halt")
        if not -ONE < stop < ZERO:
            raise RiskConfigError("position_stop must lie between -1 and 0")
        if not ZERO < collar < ONE:
            raise RiskConfigError("fat_finger_collar must lie between 0 and 1")
        return cls(
            capital_cap=cap,
            per_position_cap=per,
            drawdown_halt=halt,
            drawdown_flatten=flatten,
            position_stop=stop,
            fat_finger_collar=collar,
            sha256=limits_sha256(fields),
        )


def limits_sha256(fields: Mapping[str, Any]) -> str:
    """P-06: sha256 of canonical JSON (sorted keys, ``,`` and ``:``, ASCII) of the nine keys."""
    if not isinstance(fields, Mapping) or set(fields) != set(LIMIT_KEYS):
        raise RiskConfigError("limits keys are not exactly the contract keys")
    canonical = json.dumps(
        {key: fields[key] for key in LIMIT_KEYS},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("ascii")).hexdigest()


# --------------------------------------------------------- read-model types


@dataclass(frozen=True)
class Holding:
    isin: str
    quantity: int
    cost: Decimal  # total cost basis of the position, charges excluded


@dataclass(frozen=True)
class OpenOrder:
    isin: str
    side: str  # "buy" | "sell"
    quantity: int  # pending (unfilled) quantity
    limit_price: Decimal


@dataclass(frozen=True)
class Account:
    holdings: tuple[Holding, ...] = ()
    open_orders: tuple[OpenOrder, ...] = ()


@dataclass(frozen=True)
class Quote:
    stock_code: str
    isin: str  # from the security master row for stock_code
    series: str
    ltp: Decimal
    lower_circuit: Decimal
    upper_circuit: Decimal
    previous_close: Decimal  # the A2 mark check; NOT the tick band reference
    session_date: date  # the IST session the quote belongs to
    # The tick table's band reference: the close on the last trading day of the previous
    # calendar month (or the exchange's dated tick reference). It comes from a separate
    # source than the quote, and None means that source could not supply it (fail closed).
    tick_reference: Decimal | None = None
    # The date the reference is a close of: any date in its calendar month (the last
    # trading day is the natural value). It must fall in the calendar month BEFORE the
    # order's session date, which is how the VM keys the reference,
    # ``band_reference(isin, session_date)``. None, a stale cached value from an earlier
    # month, or a current-month value all fail closed as tick_reference_unavailable.
    tick_reference_month: date | None = None
    bid: Decimal | None = None  # best bid at the quote time; a sell is measured against it
    ask: Decimal | None = None  # best ask at the quote time; a buy is measured against it


@dataclass(frozen=True)
class RiskFlags:
    halt: bool = False
    ended: bool = False
    mac_halt: bool = False
    account_mismatch: bool = False
    stops: frozenset[str] = frozenset()
    ledger_cost: Mapping[str, Decimal] = field(default_factory=dict)


@dataclass(frozen=True)
class OrderRequest:
    side: str  # "buy" | "sell"
    stock_code: str
    isin: str
    quantity: int
    limit_price: Decimal

    def __post_init__(self) -> None:
        if self.side not in ("buy", "sell"):
            raise RiskConfigError("side must be buy or sell")
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int) or self.quantity <= 0:
            raise RiskConfigError("quantity must be a positive integer")
        if not isinstance(self.limit_price, Decimal) or not self.limit_price > ZERO:
            raise RiskConfigError("limit_price must be a positive Decimal")


@dataclass(frozen=True)
class Decision:
    codes: tuple[str, ...]

    @property
    def allowed(self) -> bool:
        return not self.codes

    @property
    def first(self) -> str | None:
        return self.codes[0] if self.codes else None


# ---------------------------------------------------------------- evaluator


def to_ist(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise RiskConfigError("clock must be timezone-aware")
    return now.astimezone(IST)


def session_open(now_ist: datetime) -> bool:
    """09:15 <= t < 15:10 IST, Monday to Friday. 15:09:59 is open, 15:10:00 is not."""
    if now_ist.weekday() >= 5:
        return False
    return SESSION_OPEN <= now_ist.time() < ORDER_CUTOFF


def position_costs(account: Account, ledger_cost: Mapping[str, Decimal]) -> dict[str, Decimal]:
    """Per-ISIN cost basis: the larger of the broker holding and the ledger.

    Holdings lag fills by a day, so a position bought today can be missing from
    holdings but is already in the ledger. Taking the larger never under-counts
    deployed capital.
    """
    costs: dict[str, Decimal] = {}
    for holding in account.holdings:
        costs[holding.isin] = max(costs.get(holding.isin, ZERO), holding.cost)
    for isin, cost in ledger_cost.items():
        costs[isin] = max(costs.get(isin, ZERO), cost)
    return costs


def previous_month(session: date) -> tuple[int, int]:
    """(year, month) of the calendar month before ``session``; January rolls to the prior December."""
    return (session.year - 1, 12) if session.month == 1 else (session.year, session.month - 1)


def _tick_reference_usable(quote: Quote, session: date) -> bool:
    """A positive finite reference that is dated, and dated to the previous calendar month."""
    reference, month = quote.tick_reference, quote.tick_reference_month
    if not (isinstance(reference, Decimal) and reference.is_finite() and reference > ZERO):
        return False
    if not isinstance(month, date):
        return False  # undated: nothing says which month this close is from
    return (month.year, month.month) == previous_month(session)


def _on_tick(quote: Quote, session: date, price: Decimal) -> bool:
    try:
        resolution = resolve_nse_cash_tick(
            session_date=session,
            band_reference_price=quote.tick_reference,
            instrument_class=InstrumentClass.EQUITY,
            series=quote.series,
        )
    except CostModelError:  # no table, series not covered, bad reference: all fail closed
        return False
    return price % resolution.tick.value == ZERO


def evaluate(
    limits: Limits,
    flags: RiskFlags,
    account: Account,
    quote: Quote | None,
    now_ist: datetime,
    order: OrderRequest,
    *,
    kill_enabled: bool,
) -> Decision:
    """Ordered reason codes; an empty tuple means every rule passes.

    Precedence: hard blocks (kill, mac_halt, account_mismatch, pilot_ended), then
    stop_open, halt_latch and session_closed, then data and price rules, then
    holding and cap rules. ``stop_open`` therefore beats every non-hard-block code
    for a buy, on any ISIN, not only the stopped one (operator 2026-10-07).
    """
    now_ist = to_ist(now_ist)
    codes: list[str] = []
    buy = order.side == "buy"
    price = order.limit_price
    notional = price * order.quantity

    if not kill_enabled:
        codes.append("kill_switch")
    if flags.mac_halt:
        codes.append("mac_halt")
    if flags.account_mismatch:
        codes.append("account_mismatch")
    if buy and flags.ended:
        codes.append("pilot_ended")
    if buy and flags.stops:
        codes.append("stop_open")
    if buy and flags.halt:
        codes.append("halt_latch")
    if not session_open(now_ist):
        codes.append("session_closed")

    fresh = (
        quote is not None
        and quote.session_date == now_ist.date()
        and quote.ltp > ZERO
        and quote.lower_circuit > ZERO
        and quote.upper_circuit >= quote.lower_circuit
        and quote.previous_close > ZERO
    )
    if not fresh:
        codes.append("quote_unavailable")
    if not order.isin.startswith("INE"):
        codes.append("instrument_unsupported")
    elif fresh:
        assert quote is not None
        if quote.isin != order.isin or quote.stock_code != order.stock_code:
            codes.append("isin_mismatch")
        elif quote.series not in SUPPORTED_SERIES:
            codes.append("instrument_unsupported")
        else:
            if not _tick_reference_usable(quote, now_ist.date()):
                codes.append("tick_reference_unavailable")  # never a guess from previous_close
            elif not _on_tick(quote, now_ist.date(), price):
                codes.append("off_tick")
            if not quote.lower_circuit <= price <= quote.upper_circuit:
                codes.append("circuit_band")
            if abs(price - quote.ltp) > limits.fat_finger_collar * quote.ltp:
                codes.append("collar")

    if buy:
        costs = position_costs(account, flags.ledger_cost)
        open_buys = [o for o in account.open_orders if o.side == "buy"]
        pending = sum((o.quantity * o.limit_price for o in open_buys), ZERO)
        deployed = sum(costs.values(), ZERO) + pending + notional
        if deployed > limits.capital_cap:
            codes.append("capital_cap")
        isin_pending = sum(
            (o.quantity * o.limit_price for o in open_buys if o.isin == order.isin), ZERO
        )
        if costs.get(order.isin, ZERO) + isin_pending + notional > limits.per_position_cap:
            codes.append("per_position_cap")
    else:
        held = sum((h.quantity for h in account.holdings if h.isin == order.isin), 0)
        open_sells = sum(
            (o.quantity for o in account.open_orders if o.isin == order.isin and o.side == "sell"),
            0,
        )
        if order.quantity > held - open_sells:
            codes.append("sell_exceeds_holding")
    return Decision(tuple(codes))


# ----------------------------------------------------------------- slippage


@dataclass(frozen=True)
class SlippageResult:
    ok: bool
    code: str | None  # SLIPPAGE_LIMIT or SLIPPAGE_QUOTE_UNAVAILABLE when not ok
    reason: str  # "within_cap", "over_cap" or why the check failed closed
    slippage_bps: Decimal | None  # side-adjusted, positive means worse than the reference


def _positive_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (Decimal, str)):
        return None
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        return None
    if not parsed.is_finite() or not parsed > ZERO:
        return None
    return parsed


def slippage_check(
    side: str,
    quote: Quote | None,
    fill_price: Decimal | str | None,
    max_slippage_bps: Decimal | str | None,
) -> SlippageResult:
    """P-16: deny ``SLIPPAGE_LIMIT`` when the side-adjusted slippage exceeds the cap.

    The reference is the side of the book the order would take: a BUY is measured
    against ``quote.ask`` and a SELL against ``quote.bid``. Neither the last traded
    price nor the other side is ever used, so a wide spread cannot hide a bad fill. A
    buy is worse the further the price is above the ask; a sell is worse the further it
    is below the bid. A price better than the reference is never a breach.

    A missing, zero, negative or non-finite bid or ask for the order's side, or no
    quote at all, fails closed with the typed code ``SLIPPAGE_QUOTE_UNAVAILABLE`` and
    reason ``ask_missing`` or ``bid_missing``. The other side may be absent, but a value
    that is present and zero, negative or non-finite is ``bid_unusable`` or
    ``ask_unusable``, and a crossed book (bid above ask) is ``book_crossed``; a locked
    book (bid equal to ask) is allowed. A missing fill price or cap fails closed
    as ``SLIPPAGE_LIMIT``. The cap is passed in (India: 25 bps, from ``IndiaExecution``);
    this function has no default. The comparison is done in cross-multiplied form, so
    the edge is exact: exactly the cap passes and a hundredth of a basis point more
    fails. A cap of zero or below is a configuration error and is refused, never read
    as "no limit".

    Quote freshness is the caller's to check before calling (63-04 admission).
    """
    if side not in ("buy", "sell"):
        raise RiskConfigError("side must be buy or sell")
    if max_slippage_bps is not None:
        cap = _positive_decimal(max_slippage_bps)
        if cap is None:
            raise RiskConfigError("max_slippage_bps must be a positive Decimal")
    else:
        cap = None
    book_side = "ask" if side == "buy" else "bid"
    other_side = "bid" if side == "buy" else "ask"
    reference = _positive_decimal(getattr(quote, book_side, None)) if isinstance(quote, Quote) else None
    # The other side is not needed, but a value that is PRESENT must be usable, and the
    # two must not cross: a crossed or garbage book is a bad snapshot, so neither side of
    # it is a trustworthy reference.
    other_raw = getattr(quote, other_side, None) if isinstance(quote, Quote) else None
    other = _positive_decimal(other_raw)
    other_unusable = other_raw is not None and other is None
    crossed = (
        reference is not None
        and other is not None
        and (other > reference if side == "buy" else reference > other)
    )
    fill = _positive_decimal(fill_price)
    if cap is None:
        return SlippageResult(False, SLIPPAGE_LIMIT, "cap_missing", None)
    if reference is None:
        return SlippageResult(False, SLIPPAGE_QUOTE_UNAVAILABLE, f"{book_side}_missing", None)
    if other_unusable:
        return SlippageResult(False, SLIPPAGE_QUOTE_UNAVAILABLE, f"{other_side}_unusable", None)
    if crossed:
        return SlippageResult(False, SLIPPAGE_QUOTE_UNAVAILABLE, "book_crossed", None)
    if fill is None:
        return SlippageResult(False, SLIPPAGE_LIMIT, "price_missing", None)
    adverse = fill - reference if side == "buy" else reference - fill
    slippage = adverse / reference * BPS
    if adverse * BPS > cap * reference:
        return SlippageResult(False, SLIPPAGE_LIMIT, "over_cap", slippage)
    return SlippageResult(True, None, "within_cap", slippage)
