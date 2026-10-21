"""The VM's own reads behind the order guard (63-05): account, quote, bars.

Every read is a GET to Breeze through BreezeClient, charged to the relay budget
before it leaves and counted by the client. Nothing here can place, change or
withdraw an order: the three account endpoints (dematholdings, trades, order) are
the read-only ones, and the transport only ever sends a GET.

Every response is parsed strictly. A missing key, a wrong type, a bool where a
number belongs, NaN, a negative quantity, a stock code the security master cannot
bind to one ISIN, a capped bars chunk, or a duplicate row is a ReadFailed, and the
guard (63-01) turns any exception from a port into account_read_failed or
quote_unavailable. A read that fails is NEVER an empty account.

The ICICI response shapes below come from the docs and the 61 smoke, not from a
live NSE cash order book. They are UNVERIFIED until the 63-06 probe, so every
shape the docs leave open fails closed (see gateway/ORDERS-API.md).
"""

from __future__ import annotations

import json
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Iterator, Mapping

from ..breeze_client import BreezeClient, BreezeError, classify_payload
from ..contract import (
    BARS_EXCHANGE,
    BARS_REQUEST_KNOBS,
    SECURITY_MASTER_MAX_BYTES,
    SECURITY_MASTER_TOTAL_S,
    ZIP_MAGIC,
)
from ..events import EventLog
from ..session import BudgetGate, Credentials, SessionManager, charge_budget
from ..transport import Timeouts
from .limits import IST, ZERO, AccountSnapshot, Holding, OpenOrder, Quote, TickTable, Trade, load_tick_table
from .master import MasterHolder, MasterMap
from .pipeline import DatedTickReference, previous_month

BARS_ROWS_CAP = 1000  # a response this long may have been cut: it is never treated as complete
BARS_CHUNK_DAYS = 365
TRADE_LOOKBACK_DAYS = 365
_ISIN = re.compile(r"IN[A-Z0-9]{9}[0-9]")
_ORDER_ID = re.compile(r"[A-Za-z0-9_-]{1,40}")
_WHOLE = re.compile(r"[0-9]{1,9}")
_DECIMAL = re.compile(r"[0-9]{1,12}(\.[0-9]{1,6})?")
_BAR_TIME = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})[ T]00:00:00(\.0+)?Z?")
TERMINAL_STATUSES = frozenset({"executed", "cancelled", "canceled", "rejected", "expired"})


class ReadFailed(Exception):
    """A Breeze read could not be trusted. The text names a reason, never a value."""

    def __init__(self, reason: str = "read_failed") -> None:
        super().__init__(reason)
        self.reason = reason


# ------------------------------------------------------------ request context


class ReadContext:
    """The relay bearer and the Breeze session token of the request being served.

    The handler binds both for the length of one pipeline call; the ports read them
    from here. Per thread, so two requests never see each other's token. Reading an
    unbound context is a ReadFailed: a port can never run outside a request.
    """

    def __init__(self) -> None:
        self._local = threading.local()

    @contextmanager
    def bound(self, bearer: str | None, session_token: str) -> Iterator[None]:
        previous = getattr(self._local, "value", None)
        self._local.value = (bearer, session_token)
        try:
            yield
        finally:
            self._local.value = previous

    def values(self) -> tuple[str | None, str]:
        value = getattr(self._local, "value", None)
        if value is None:
            raise ReadFailed("no_request")
        return value


# ----------------------------------------------------------------- the reader


def _no_constant(_text: str) -> object:
    raise ValueError("bad constant")


class BreezeReads:
    """One budget-charged GET and the Success payload, with decimals kept exact."""

    def __init__(
        self,
        *,
        breeze: BreezeClient,
        credentials: Credentials,
        budget: BudgetGate,
        events: EventLog,
        session: SessionManager,
        context: ReadContext,
    ) -> None:
        self._breeze = breeze
        self._credentials = credentials
        self._budget = budget
        self._events = events
        self._session = session
        self._context = context

    @property
    def breeze(self) -> BreezeClient:
        return self._breeze

    def success(self, call: str, **arguments: Any) -> object:
        """Run BreezeClient.<call> and return payload["Success"], or raise ReadFailed."""
        _bearer, session_token = self._context.values()
        charge_budget(self._budget, self._events)
        method = getattr(self._breeze, call)
        keys: dict[str, Any] = {"app_key": self._credentials.app_key, "session_token": session_token}
        if call != "daily_bars_v2_raw":
            keys["secret_key"] = self._credentials.secret_key
        try:
            raw = method(**keys, **arguments)
        except BreezeError:
            raise ReadFailed("upstream") from None
        verdict = classify_payload(raw.http_status, raw.body)
        if not verdict.ok:
            self._session.note_breeze_error(
                verdict.error_class, breeze_status=verdict.breeze_status, http_status=raw.http_status
            )
            raise ReadFailed("upstream")
        try:
            payload = json.loads(raw.body, parse_float=Decimal, parse_constant=_no_constant)
        except (ValueError, RecursionError):
            raise ReadFailed("shape") from None
        if not isinstance(payload, dict) or "Success" not in payload:
            raise ReadFailed("shape")
        return payload["Success"]

    def security_master_bytes(self, url: str) -> bytes:
        """The master zip, through the same budget kind and egress check as the data route."""
        bearer, _token = self._context.values()
        charge_budget(self._budget, self._events, "security_master")
        self._session.require_egress(bearer)
        try:
            raw = self._breeze.security_master_raw(
                url,
                timeouts=Timeouts(connect_s=5.0, read_s=30.0, total_s=SECURITY_MASTER_TOTAL_S),
                max_bytes=SECURITY_MASTER_MAX_BYTES,
            )
        except BreezeError:
            raise ReadFailed("upstream") from None
        if raw.http_status != 200 or not raw.body.startswith(ZIP_MAGIC):
            raise ReadFailed("shape")
        return raw.body


# -------------------------------------------------------------------- parsing


def number(value: object) -> Decimal:
    """A JSON number or a plain decimal string as a finite Decimal. Never a bool."""
    if isinstance(value, bool):
        raise ReadFailed("shape")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ReadFailed("shape")
        return value
    if isinstance(value, str) and _DECIMAL.fullmatch(value):
        try:
            return Decimal(value)
        except InvalidOperation:  # pragma: no cover - the pattern already guards
            raise ReadFailed("shape") from None
    raise ReadFailed("shape")


def whole(value: object) -> int:
    """A non-negative whole number from a digit string or a JSON integer."""
    if isinstance(value, bool):
        raise ReadFailed("shape")
    if isinstance(value, int):
        if value < 0:
            raise ReadFailed("shape")
        return value
    if isinstance(value, str) and _WHOLE.fullmatch(value):
        return int(value)
    raise ReadFailed("shape")


def _rows(success: object) -> list[Mapping[str, object]]:
    if not isinstance(success, list):
        raise ReadFailed("shape")
    out: list[Mapping[str, object]] = []
    for item in success:
        if not isinstance(item, dict):
            raise ReadFailed("shape")
        out.append(item)
    return out


def _text(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise ReadFailed("shape")
    return value


def _ist_midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, tzinfo=IST)


def parse_holdings(success: object) -> dict[str, int]:
    """ISIN to demat quantity. A repeated ISIN is summed (the larger number is the cautious one)."""
    held: dict[str, int] = {}
    for row in _rows(success):
        isin = _text(row, "stock_ISIN")
        if _ISIN.fullmatch(isin) is None:
            raise ReadFailed("shape")
        quantity = whole(row.get("quantity"))
        if quantity > 0:
            held[isin] = held.get(isin, 0) + quantity
    return held


def parse_trades(success: object, master: MasterMap) -> tuple[Trade, ...]:
    """One Trade per row, bound to an ISIN by the security master.

    The docs do not say whether a row is one order or one fill. The order id is used
    as the trade id, and a repeated order id (a per-fill shape) is a ReadFailed: it
    would otherwise be double counted or silently dropped. Rows carry a date and no
    time, so executed_at is the IST day; within one day buys sort before sells, so a
    same-day round trip can never look like a sale of shares the ledger does not hold.
    """
    seen: set[str] = set()
    parsed: list[tuple[date, int, str, Trade]] = []
    for row in _rows(success):
        order_id = _text(row, "order_id")
        if _ORDER_ID.fullmatch(order_id) is None or order_id in seen:
            raise ReadFailed("shape")
        seen.add(order_id)
        code = _text(row, "stock_code")
        bound = master.row(code)
        if bound is None:
            raise ReadFailed("unbound_stock_code")
        action = _text(row, "action").lower()
        if action not in ("buy", "sell"):
            raise ReadFailed("shape")
        quantity = whole(row.get("quantity"))
        price = number(row.get("average_cost"))
        if quantity <= 0 or price <= ZERO:
            raise ReadFailed("shape")
        try:
            day = datetime.strptime(_text(row, "trade_date"), "%d-%b-%Y").date()
        except ValueError:
            raise ReadFailed("shape") from None
        charges: Decimal | None
        try:
            charges = number(row.get("brokerage_amount")) + number(row.get("total_taxes"))
        except ReadFailed:
            charges = None  # no detail yet: the guard uses the P-07 bound
        trade = Trade(
            trade_id=order_id,
            isin=bound.isin,
            side=action,
            quantity=quantity,
            price=price,
            charges=charges,
            executed_at=_ist_midnight(day),
        )
        parsed.append((day, 0 if action == "buy" else 1, order_id, trade))
    parsed.sort(key=lambda item: item[:3])
    return tuple(item[3] for item in parsed)


def parse_open_orders(success: object, master: MasterMap) -> tuple[OpenOrder, ...]:
    """The orders still working. An unknown status counts as working (the cautious reading)."""
    orders: list[OpenOrder] = []
    for row in _rows(success):
        _text(row, "order_id")
        code = _text(row, "stock_code")
        bound = master.row(code)
        if bound is None:
            raise ReadFailed("unbound_stock_code")
        action = _text(row, "action").lower()
        if action not in ("buy", "sell"):
            raise ReadFailed("shape")
        status = _text(row, "status").strip().lower()
        pending = whole(row.get("pending_quantity"))
        price = number(row.get("price"))
        if status in TERMINAL_STATUSES or pending == 0:
            continue
        if action == "buy" and price <= ZERO:
            raise ReadFailed("shape")  # a working market buy has no notional to cap
        orders.append(OpenOrder(bound.isin, action, pending, price))
    return tuple(orders)


@dataclass(frozen=True)
class QuoteRead:
    stock_code: str
    ltp: Decimal
    lower_circuit: Decimal
    upper_circuit: Decimal
    previous_close: Decimal
    session_date: date


def parse_quote(success: object, stock_code: str) -> QuoteRead:
    rows = _rows(success) if isinstance(success, list) else None
    if rows is None:
        if not isinstance(success, dict):
            raise ReadFailed("shape")
        rows = [success]
    if len(rows) != 1:
        raise ReadFailed("shape")
    row = rows[0]
    if row.get("stock_code") != stock_code:
        raise ReadFailed("shape")
    try:
        stamp = datetime.strptime(_text(row, "ltt"), "%d-%b-%Y %H:%M:%S")
    except ValueError:
        raise ReadFailed("shape") from None
    values = {}
    for key in ("ltp", "lower_circuit", "upper_circuit", "previous_close"):
        raw = row.get(key)
        if isinstance(raw, str):  # numbers only: a quoted price is not the shape the smoke saw
            raise ReadFailed("shape")
        values[key] = number(raw)
    return QuoteRead(
        stock_code=stock_code,
        ltp=values["ltp"],
        lower_circuit=values["lower_circuit"],
        upper_circuit=values["upper_circuit"],
        previous_close=values["previous_close"],
        session_date=stamp.date(),
    )


def parse_bars(success: object) -> dict[date, Decimal]:
    """Raw daily closes by date. A cut response or a repeated date is a ReadFailed."""
    rows = _rows(success)
    if len(rows) >= BARS_ROWS_CAP:
        raise ReadFailed("bars_capped")
    closes: dict[date, Decimal] = {}
    for row in rows:
        match = _BAR_TIME.fullmatch(_text(row, "datetime"))
        if match is None:
            raise ReadFailed("shape")
        try:
            day = date.fromisoformat(match.group(1))
        except ValueError:
            raise ReadFailed("shape") from None
        close = number(row.get("close"))
        if close <= ZERO or day in closes:
            raise ReadFailed("shape")
        closes[day] = close
    return closes


# ---------------------------------------------------------------------- ports


def _ist_today(clock: Any) -> date:
    return clock.now_utc().astimezone(IST).date()


def _previous_weekday(day: date) -> date:
    prior = day - timedelta(days=1)
    while prior.weekday() >= 5:
        prior -= timedelta(days=1)
    return prior


class BreezeAccount:
    """AccountPort: fresh holdings, working orders and the trade list, from three GETs.

    Holdings lag fills by a settlement day, so a sale made today (or on the previous
    weekday) still shows in the demat quantity. Those sales are netted off the holding
    before the guard compares it with the ledger; otherwise every sale would latch
    account_mismatch. Netting can only lower a holding, so it never hides a buy and
    never lets a sell exceed what the ledger holds. A holding has no price in this
    response, so cost is ZERO and the cap rules read the VM ledger's cost instead.
    """

    def __init__(self, *, reads: BreezeReads, master: MasterHolder, clock: Any) -> None:
        self._reads = reads
        self._master = master
        self._clock = clock

    def snapshot(self) -> AccountSnapshot:
        master = self._master.current()
        today = _ist_today(self._clock)
        held = parse_holdings(self._reads.success("demat_holdings_raw"))
        first = today - timedelta(days=TRADE_LOOKBACK_DAYS)
        trades = parse_trades(
            self._reads.success(
                "trade_list_raw",
                exchange_code=BARS_EXCHANGE,
                from_iso=first.isoformat() + "T00:00:00.000Z",
                to_iso=today.isoformat() + "T23:59:59.000Z",
            ),
            master,
        )
        orders = parse_open_orders(
            self._reads.success(
                "order_list_raw",
                exchange_code=BARS_EXCHANGE,
                from_iso=today.isoformat() + "T00:00:00.000Z",
                to_iso=today.isoformat() + "T23:59:59.000Z",
            ),
            master,
        )
        unsettled = _previous_weekday(today)
        for trade in trades:
            if trade.side == "sell" and trade.executed_at is not None:
                if trade.executed_at.astimezone(IST).date() >= unsettled:
                    held[trade.isin] = max(0, held.get(trade.isin, 0) - trade.quantity)
        holdings = tuple(Holding(isin, quantity, ZERO) for isin, quantity in sorted(held.items()) if quantity > 0)
        return AccountSnapshot(holdings=holdings, open_orders=orders, trades=trades)


class BreezeMarket:
    """MarketPort: one fresh quote, bound to its security master row (P-21).

    A stock code the master cannot bind to exactly one live EQ or BE row gets an empty
    ISIN and series, which the evaluator answers as isin_mismatch. The quote's own
    numbers are only used if they parse strictly.
    """

    def __init__(self, *, reads: BreezeReads, master: MasterHolder) -> None:
        self._reads = reads
        self._master = master

    def quote(self, stock_code: str) -> Quote:
        row = self._master.current().row(stock_code)
        read = parse_quote(
            self._reads.success("quote_raw", stock_code=stock_code, exchange_code=BARS_EXCHANGE),
            stock_code,
        )
        return Quote(
            stock_code=stock_code,
            isin=row.isin if row else "",
            series=row.series if row else "",
            ltp=read.ltp,
            lower_circuit=read.lower_circuit,
            upper_circuit=read.upper_circuit,
            previous_close=read.previous_close,
            session_date=read.session_date,
        )


class BreezeBars:
    """Raw daily closes by ISIN, in chunks that stay under the response cap."""

    def __init__(self, *, reads: BreezeReads, master: MasterHolder) -> None:
        self._reads = reads
        self._master = master

    def closes(self, isin: str, first: date, last: date) -> dict[date, Decimal]:
        code = self._master.current().code_for_isin(isin)
        if code is None:
            raise ReadFailed("unbound_isin")
        out: dict[date, Decimal] = {}
        start = first
        while start <= last:
            end = min(last, start + timedelta(days=BARS_CHUNK_DAYS - 1))
            success = self._reads.success(
                "daily_bars_v2_raw",
                stock_code=code,
                exch_code=BARS_EXCHANGE,
                from_iso=start.isoformat() + "T00:00:00.000Z",
                to_iso=end.isoformat() + "T23:59:59.000Z",
                interval="1day",
                **BARS_REQUEST_KNOBS,
            )
            for day, close in parse_bars(success).items():
                if start <= day <= end:
                    out[day] = close
            start = end + timedelta(days=1)
        return out


class BarsTickReference:
    """TickReferencePort: the last close of the previous calendar month, from daily bars (D-09)."""

    def __init__(self, *, bars: BreezeBars) -> None:
        self._bars = bars

    def band_reference(self, isin: str, session_date: date) -> DatedTickReference:
        year, month = previous_month(session_date)
        first = date(year, month, 1)
        following = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        closes = self._bars.closes(isin, first, following - timedelta(days=1))
        if not closes:
            raise ReadFailed("no_reference")
        day = max(closes)
        return DatedTickReference(price=closes[day], month=day)


class BarsMarks:
    """MarksPort: closes for the Option B evaluation, and the A2 reference pair."""

    def __init__(
        self,
        *,
        bars: BreezeBars,
        market: BreezeMarket,
        tick_reference: BarsTickReference,
        master: MasterHolder,
        tick_table: TickTable | None = None,
    ) -> None:
        self._bars = bars
        self._market = market
        self._tick_reference = tick_reference
        self._master = master
        self._tick_table = tick_table

    def closes(self, isin: str, first: date, last: date) -> dict[date, Decimal]:
        return self._bars.closes(isin, first, last)

    def reference(self, isin: str) -> tuple[Decimal, Decimal]:
        """(the quote's previous_close, one tick) for one held ISIN, for the A2 mark check."""
        code = self._master.current().code_for_isin(isin)
        if code is None:
            raise ReadFailed("unbound_isin")
        quote = self._market.quote(code)
        band = self._tick_reference.band_reference(isin, quote.session_date)
        table = self._tick_table or load_tick_table()
        tick = table.resolve(quote.session_date, quote.series, band.price)
        return quote.previous_close, tick
