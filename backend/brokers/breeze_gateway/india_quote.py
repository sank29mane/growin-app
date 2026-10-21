"""India quote evidence from the relay, for the Mac's own order limits (63-05).

Everything here is a GET through ``RelayClient`` (quote, daily bars, security master). The
parsers mirror the VM's: strict shapes, a finite Decimal for every number, no repeated bar
date, a bars response that may have been cut is never complete. A failure is a
``QuoteUnavailable`` with a short closed reason, never a guess, and the guard answers it as
``quote_unavailable``.

The ISIN map is the Mac's convenience only. The VM binds every ISIN again from its own master
(P-21), so a wrong ISIN here is refused there as ``isin_mismatch``.
"""

from __future__ import annotations

import csv
import io
import json
import re
import threading
import zipfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping

from execution.india_guard import IndiaQuoteEvidence
from risk_india import rules

from .orders import RelayLink

IST = rules.IST
CASH_SERIES = ("EQ", "BE")
BARS_ROWS_CAP = 1000
BARS_CHUNK_DAYS = 365
MASTER_MEMBER = "NSEScripMaster.txt"
MASTER_COLUMNS = ("Token", "ShortName", "Series", "ISINCode")
MASTER_MAX_MEMBERS = 8
MASTER_MAX_BYTES = 64 * 1024 * 1024
_ISIN = re.compile(r"IN[A-Z0-9]{9}[0-9]")
_STOCK_CODE = re.compile(r"[A-Z0-9]{1,10}")
_BAR_TIME = re.compile(r"([0-9]{4}-[0-9]{2}-[0-9]{2})[ T]00:00:00(\.0+)?Z?")
_DECIMAL = re.compile(r"[0-9]{1,12}(\.[0-9]{1,6})?")
_ZERO = Decimal("0")

Clock = Callable[[], datetime]


class QuoteUnavailable(Exception):
    """A read failed or its shape was untrusted. ``reason`` is a closed word, never a value."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _no_constant(_text: str) -> object:
    raise QuoteUnavailable("shape")


def _success(body: bytes) -> object:
    try:
        payload = json.loads(body, parse_float=Decimal, parse_constant=_no_constant)
    except (ValueError, RecursionError):
        raise QuoteUnavailable("shape") from None
    if not isinstance(payload, dict) or "Success" not in payload:
        raise QuoteUnavailable("shape")
    return payload["Success"]


def _number(value: object) -> Decimal:
    if isinstance(value, bool):
        raise QuoteUnavailable("shape")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, Decimal) and value.is_finite():
        return value
    if isinstance(value, str) and _DECIMAL.fullmatch(value):
        try:
            return Decimal(value)
        except InvalidOperation:  # pragma: no cover - the pattern already guards
            raise QuoteUnavailable("shape") from None
    raise QuoteUnavailable("shape")


def _optional_price(value: object) -> Decimal | None:
    """A best bid or offer: absent, zero or null means no book side, which is not a price."""
    if value is None or value == "":
        return None
    price = _number(value)
    return price if price > _ZERO else None


@dataclass(frozen=True)
class RelayQuote:
    stock_code: str
    ltp: Decimal
    lower_circuit: Decimal
    upper_circuit: Decimal
    previous_close: Decimal
    session_date: date
    bid: Decimal | None
    ask: Decimal | None


def parse_quote(body: bytes, stock_code: str) -> RelayQuote:
    success = _success(body)
    rows = success if isinstance(success, list) else [success] if isinstance(success, dict) else None
    if rows is None or len(rows) != 1 or not isinstance(rows[0], dict):
        raise QuoteUnavailable("shape")
    row: Mapping[str, Any] = rows[0]
    if row.get("stock_code") != stock_code:
        raise QuoteUnavailable("shape")
    ltt = row.get("ltt")
    if not isinstance(ltt, str):
        raise QuoteUnavailable("shape")
    try:
        stamp = datetime.strptime(ltt, "%d-%b-%Y %H:%M:%S")
    except ValueError:
        raise QuoteUnavailable("shape") from None
    values: dict[str, Decimal] = {}
    for key in ("ltp", "lower_circuit", "upper_circuit", "previous_close"):
        raw = row.get(key)
        if isinstance(raw, str):  # a quoted price is not the shape the smoke saw
            raise QuoteUnavailable("shape")
        values[key] = _number(raw)
    if values["ltp"] <= _ZERO or values["previous_close"] <= _ZERO:
        raise QuoteUnavailable("shape")
    return RelayQuote(
        stock_code=stock_code,
        ltp=values["ltp"],
        lower_circuit=values["lower_circuit"],
        upper_circuit=values["upper_circuit"],
        previous_close=values["previous_close"],
        session_date=stamp.date(),
        bid=_optional_price(row.get("best_bid_price")),
        ask=_optional_price(row.get("best_offer_price")),
    )


def parse_bar_closes(body: bytes) -> dict[date, Decimal]:
    success = _success(body)
    if not isinstance(success, list) or len(success) >= BARS_ROWS_CAP:
        raise QuoteUnavailable("bars_shape")
    closes: dict[date, Decimal] = {}
    for row in success:
        if not isinstance(row, dict):
            raise QuoteUnavailable("shape")
        stamp = row.get("datetime")
        match = _BAR_TIME.fullmatch(stamp) if isinstance(stamp, str) else None
        if match is None:
            raise QuoteUnavailable("shape")
        try:
            day = date.fromisoformat(match.group(1))
        except ValueError:
            raise QuoteUnavailable("shape") from None
        close = _number(row.get("close"))
        if close <= _ZERO or day in closes:
            raise QuoteUnavailable("shape")
        closes[day] = close
    return closes


@dataclass(frozen=True)
class MasterEntry:
    isin: str
    series: str


def parse_master(content: bytes) -> dict[str, MasterEntry]:
    """Live EQ and BE rows by stock code. An ambiguous code or a shared ISIN is left out.

    The same rules as the VM's master parser, so the Mac and the VM bind the same ISIN or the
    Mac refuses first: one named zip member under a size cap, four required columns, a live
    Token only, a cash series only, and a code with a bad ISIN or a second row is ambiguous.
    """

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            infos = archive.infolist()
            matches = [info for info in infos if info.filename == MASTER_MEMBER]
            if len(infos) > MASTER_MAX_MEMBERS or len(matches) != 1:
                raise QuoteUnavailable("master")
            if matches[0].file_size > MASTER_MAX_BYTES:
                raise QuoteUnavailable("master")
            with archive.open(matches[0]) as handle:
                raw = handle.read(MASTER_MAX_BYTES + 1)
        if len(raw) > MASTER_MAX_BYTES:
            raise QuoteUnavailable("master")
        reader = csv.reader(io.StringIO(raw.decode("utf-8-sig")), skipinitialspace=True)
        header = next(reader)
        if any(name not in header for name in MASTER_COLUMNS):
            raise QuoteUnavailable("master")
        index = {name: header.index(name) for name in MASTER_COLUMNS}
        seen: dict[str, MasterEntry] = {}
        ambiguous: set[str] = set()
        for row in reader:
            if not row:
                continue
            if len(row) != len(header):
                raise QuoteUnavailable("master")
            token = row[index["Token"]].strip()
            series = row[index["Series"]].strip()
            code = row[index["ShortName"]].strip()
            isin = row[index["ISINCode"]].strip()
            if not token.isdigit() or int(token) == 0:
                continue
            if series not in CASH_SERIES or _STOCK_CODE.fullmatch(code) is None:
                continue
            if _ISIN.fullmatch(isin) is None or code in seen:
                ambiguous.add(code)
                continue
            seen[code] = MasterEntry(isin, series)
    except QuoteUnavailable:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, RuntimeError, ValueError,
            EOFError, StopIteration, csv.Error):
        raise QuoteUnavailable("master") from None
    by_code = {code: entry for code, entry in seen.items() if code not in ambiguous}
    owners: dict[str, list[str]] = {}
    for code, entry in by_code.items():
        owners.setdefault(entry.isin, []).append(code)
    return {
        code: entry for code, entry in by_code.items() if len(owners[entry.isin]) == 1
    }


def aligned_limit(quote: rules.Quote) -> Decimal:
    """The quote's last traded price floored to its NSE tick (two decimals), never above it."""

    from costs.core import CostModelError
    from costs.ticks import InstrumentClass, resolve_nse_cash_tick

    if quote.tick_reference is None:
        raise QuoteUnavailable("tick_reference")
    try:
        resolution = resolve_nse_cash_tick(
            session_date=quote.session_date,
            band_reference_price=quote.tick_reference,
            instrument_class=InstrumentClass.EQUITY,
            series=quote.series,
        )
    except CostModelError:
        raise QuoteUnavailable("tick_unavailable") from None
    tick = resolution.tick.value
    price = (quote.ltp // tick) * tick
    if price <= _ZERO:
        raise QuoteUnavailable("tick_unavailable")
    return price.quantize(Decimal("0.01"))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class RelayQuoteSource:
    """Quote, ISIN, tick reference and daily closes, all read through one ``RelayLink``."""

    def __init__(self, link: RelayLink, *, clock: Clock = _utc_now) -> None:
        self._link = link
        self._clock = clock
        self._lock = threading.Lock()
        self._master_day: date | None = None
        self._master: dict[str, MasterEntry] = {}

    def _today(self) -> date:
        return self._clock().astimezone(IST).date()

    def master_entry(self, stock_code: str) -> MasterEntry | None:
        today = self._today()
        with self._lock:
            if self._master_day != today:
                payload = self._link.client().security_master()
                self._master = parse_master(payload.body)
                self._master_day = today
            return self._master.get(stock_code)

    def isin(self, stock_code: str) -> str:
        entry = self.master_entry(stock_code)
        if entry is None:
            raise QuoteUnavailable("isin_unbound")
        return entry.isin

    def closes(self, stock_code: str, first: date, last: date) -> dict[date, Decimal]:
        """Raw daily closes, in chunks that stay under the response cap."""
        out: dict[date, Decimal] = {}
        start = first
        while start <= last:
            end = min(last, start + timedelta(days=BARS_CHUNK_DAYS - 1))
            payload = self._link.client().daily_bars(stock_code, start, end)
            for day, close in parse_bar_closes(payload.body).items():
                if start <= day <= end:
                    out[day] = close
            start = end + timedelta(days=1)
        return out

    def tick_reference(self, stock_code: str, session: date) -> tuple[Decimal, date]:
        """The last close of the previous calendar month, with its date (D-09)."""
        year, month = rules.previous_month(session)
        first = date(year, month, 1)
        following = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
        closes = self.closes(stock_code, first, following - timedelta(days=1))
        if not closes:
            raise QuoteUnavailable("no_reference")
        day = max(closes)
        return closes[day], day

    def quote(self, stock_code: str) -> RelayQuote:
        payload = self._link.client().quote(stock_code)
        return parse_quote(payload.body, stock_code)

    def evidence(self, stock_code: str) -> IndiaQuoteEvidence:
        """One fresh ``IndiaQuoteEvidence``. Raises ``QuoteUnavailable`` or a relay error."""
        entry = self.master_entry(stock_code)
        if entry is None:
            raise QuoteUnavailable("isin_unbound")
        read = self.quote(stock_code)
        reference, reference_day = self.tick_reference(stock_code, read.session_date)
        quote = rules.Quote(
            stock_code=stock_code,
            isin=entry.isin,
            series=entry.series,
            ltp=read.ltp,
            lower_circuit=read.lower_circuit,
            upper_circuit=read.upper_circuit,
            previous_close=read.previous_close,
            session_date=read.session_date,
            tick_reference=reference,
            tick_reference_month=reference_day,
            bid=read.bid,
            ask=read.ask,
        )
        return IndiaQuoteEvidence(quote=quote, observed_at=self._clock())


__all__ = [
    "MasterEntry",
    "aligned_limit",
    "QuoteUnavailable",
    "RelayQuote",
    "RelayQuoteSource",
    "parse_bar_closes",
    "parse_master",
    "parse_quote",
]
