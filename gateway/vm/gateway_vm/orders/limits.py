"""The VM's own limits file and rule evaluator (D-08, D-09, D-11, P-05 to P-10).

``evaluate`` is a pure function of (limits, risk flags, account view, quote,
IST clock, intent). It never reads a file or the network; the tick table is
passed in or loaded once from the vendored copy. It returns every applicable
reason code in a fixed precedence order, so the first one is the answer and the
rest are audit detail. The Mac sends none of these numbers: they come from
/etc/growin-gateway/limits.json, root-owned and changed only in an admin window.

All money and ratios are Decimal built from strings. Comparisons are inclusive
at the trigger edges.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from .challenge import canonical_bytes
from .intent import Intent

IST = timezone(timedelta(hours=5, minutes=30))  # no DST: a fixed offset is exact
SESSION_OPEN = time(9, 15)
ORDER_CUTOFF = time(15, 10)  # D-11: closing auction, no new orders at or after
TICK_TABLE_PATH = Path(__file__).parent / "data" / "nse_cash_tick_sizes.json"
SUPPORTED_SERIES = frozenset({"EQ", "BE"})

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
_MAX_FILE = 4096
ZERO = Decimal(0)
ONE = Decimal(1)


class LimitsError(Exception):
    """The limits file is missing, unsafe or invalid. Order routes answer 503 config_invalid."""


class TickUnavailable(Exception):
    pass


def _strict_loads(text: str) -> Any:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in items:
            if key in out:
                raise ValueError("duplicate key")
            out[key] = value
        return out

    def no_float(_: str) -> Any:
        raise ValueError("float")

    return json.loads(
        text, object_pairs_hook=pairs, parse_float=no_float, parse_constant=no_float
    )


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
        if not isinstance(fields, Mapping):
            raise LimitsError("limits must be an object")
        if set(fields) != set(LIMIT_KEYS):
            raise LimitsError("limits keys are not exactly the contract keys")
        version = fields["schema_version"]
        if isinstance(version, bool) or not isinstance(version, int) or version != 1:
            raise LimitsError("schema_version must be the integer 1")
        if fields["workspace"] != "india":
            raise LimitsError("workspace must be india")
        if fields["currency"] != "INR":
            raise LimitsError("currency must be INR")
        values: dict[str, Decimal] = {}
        for key in _DECIMAL_KEYS:
            raw = fields[key]
            if not isinstance(raw, str) or _DECIMAL_RE.fullmatch(raw) is None:
                raise LimitsError(f"{key} must be a decimal string")
            try:
                values[key] = Decimal(raw)
            except InvalidOperation as exc:  # pragma: no cover - regex already guards
                raise LimitsError(f"{key} is not a number") from exc
        cap, per = values["capital_cap"], values["per_position_cap"]
        halt, flatten = values["drawdown_halt"], values["drawdown_flatten"]
        stop, collar = values["position_stop"], values["fat_finger_collar"]
        # Same ordering rules as backend _check_india_limits, plus the collar.
        if not cap > ZERO:
            raise LimitsError("capital_cap")
        if not ZERO < per <= cap:
            raise LimitsError("per_position_cap")
        if not halt < ZERO:
            raise LimitsError("drawdown_halt")
        if not -ONE < flatten < halt:
            raise LimitsError("drawdown_flatten")
        if not -ONE < stop < ZERO:
            raise LimitsError("position_stop")
        if not ZERO < collar < ONE:
            raise LimitsError("fat_finger_collar")
        return cls(
            capital_cap=cap,
            per_position_cap=per,
            drawdown_halt=halt,
            drawdown_flatten=flatten,
            position_stop=stop,
            fat_finger_collar=collar,
            sha256=hashlib.sha256(canonical_bytes(dict(fields))).hexdigest(),
        )


def limits_sha256(fields: Mapping[str, Any]) -> str:
    """P-06: sha256 of canonical JSON of the nine contract keys."""
    return Limits.from_fields(fields).sha256


def load_limits(path: str | Path, *, expected_owner_uid: int | None = 0) -> Limits:
    target = Path(path)
    try:
        st = os.lstat(target)
    except OSError as exc:
        raise LimitsError("limits file is unreadable") from exc
    if not stat.S_ISREG(st.st_mode):
        raise LimitsError("limits file must be a regular file")
    if st.st_mode & 0o022:
        raise LimitsError("limits file is writable by group or other")
    if expected_owner_uid is not None and st.st_uid != expected_owner_uid:
        raise LimitsError("limits file has the wrong owner")
    if st.st_size > _MAX_FILE:
        raise LimitsError("limits file is too large")
    try:
        parsed = _strict_loads(target.read_text(encoding="ascii"))
    except (OSError, ValueError) as exc:
        raise LimitsError("limits file is not valid strict JSON") from exc
    return Limits.from_fields(parsed)


# --------------------------------------------------------------- tick table


@dataclass(frozen=True)
class _Band:
    lower: Decimal
    upper: Decimal | None
    tick: Decimal
    upper_inclusive: bool


@dataclass(frozen=True)
class _Version:
    effective_from: date
    effective_to: date | None
    series: tuple[str, ...]
    bands: tuple[_Band, ...]

    def covers(self, day: date) -> bool:
        return day >= self.effective_from and (
            self.effective_to is None or day <= self.effective_to
        )


@dataclass(frozen=True)
class TickTable:
    versions: tuple[_Version, ...]

    def resolve(self, session_date: date, series: str, reference: Decimal) -> Decimal:
        """Same version and band rules as backend resolve_nse_cash_tick (equity)."""
        for version in self.versions:
            if not version.covers(session_date):
                continue
            if series not in version.series:
                raise TickUnavailable("series not covered")
            for band in version.bands:
                if (
                    band.upper is None
                    or reference < band.upper
                    or (band.upper_inclusive and reference == band.upper)
                ):
                    return band.tick
            raise TickUnavailable("no band")
        raise TickUnavailable("no version covers the date")


def load_tick_table(path: str | Path = TICK_TABLE_PATH) -> TickTable:
    try:
        raw = _strict_loads(Path(path).read_text(encoding="utf-8"))
        versions: list[_Version] = []
        for item in raw["versions"]:
            bands = tuple(
                _Band(
                    lower=Decimal(b["from"]),
                    upper=None if b["to"] is None else Decimal(b["to"]),
                    tick=Decimal(b["tick"]),
                    upper_inclusive=bool(b.get("upper_inclusive", False)),
                )
                for b in item["bands"]
            )
            if not bands or any(b.tick <= 0 for b in bands):
                raise ValueError("bad bands")
            versions.append(
                _Version(
                    effective_from=date.fromisoformat(item["effective_from"]),
                    effective_to=None
                    if item["effective_to"] is None
                    else date.fromisoformat(item["effective_to"]),
                    series=tuple(item["series"]),
                    bands=bands,
                )
            )
        if not versions:
            raise ValueError("no versions")
    except (OSError, ValueError, KeyError, TypeError, InvalidOperation) as exc:
        raise LimitsError("tick table is unreadable") from exc
    return TickTable(tuple(versions))


# --------------------------------------------------------- read-model types


@dataclass(frozen=True)
class Holding:
    isin: str
    quantity: int
    cost: Decimal  # total cost basis of the position


@dataclass(frozen=True)
class OpenOrder:
    isin: str
    side: str  # "buy" | "sell"
    quantity: int  # pending (unfilled) quantity
    limit_price: Decimal


@dataclass(frozen=True)
class Trade:
    trade_id: str
    isin: str
    side: str
    quantity: int
    price: Decimal
    charges: Decimal | None = None  # None: trade detail not populated yet


@dataclass(frozen=True)
class AccountSnapshot:
    holdings: tuple[Holding, ...] = ()
    open_orders: tuple[OpenOrder, ...] = ()
    trades: tuple[Trade, ...] = ()


@dataclass(frozen=True)
class Quote:
    stock_code: str
    isin: str  # from the security master row for stock_code (P-21)
    series: str
    ltp: Decimal
    lower_circuit: Decimal
    upper_circuit: Decimal
    previous_close: Decimal  # tick band reference, and the A2 mark check
    session_date: date  # IST session the quote belongs to


@dataclass(frozen=True)
class RiskFlags:
    halt: bool = False
    ended: bool = False
    mac_halt: bool = False
    account_mismatch: bool = False
    stops: frozenset[str] = frozenset()
    ledger_cost: Mapping[str, Decimal] = field(default_factory=dict)


# ---------------------------------------------------------------- evaluator


def session_open(now_ist: datetime) -> bool:
    """09:15 <= t < 15:10 IST, Monday to Friday. 15:09:59 is open, 15:10:00 is not."""
    if now_ist.weekday() >= 5:
        return False
    return SESSION_OPEN <= now_ist.time() < ORDER_CUTOFF


def position_costs(
    account: AccountSnapshot, ledger_cost: Mapping[str, Decimal]
) -> dict[str, Decimal]:
    """Per-ISIN cost basis: the larger of the broker's holding and the VM ledger.

    Holdings lag fills (T+1), so a position bought today may be missing from
    holdings but is already in the ledger. Taking the larger never under-counts
    deployed capital.
    """
    costs: dict[str, Decimal] = {}
    for holding in account.holdings:
        costs[holding.isin] = max(costs.get(holding.isin, ZERO), holding.cost)
    for isin, cost in ledger_cost.items():
        costs[isin] = max(costs.get(isin, ZERO), cost)
    return costs


def evaluate(
    limits: Limits,
    flags: RiskFlags,
    account: AccountSnapshot,
    quote: Quote | None,
    now_ist: datetime,
    intent: Intent,
    *,
    kill_enabled: bool,
    tick_table: TickTable | None = None,
) -> tuple[str, ...]:
    """Ordered reason codes; empty means every rule passes.

    Precedence: hard blocks (kill, mac_halt, account_mismatch, pilot_ended),
    then stop_open, halt_latch, session_closed, then data and price rules, then
    holding and cap rules. stop_open therefore beats every non-hard-block code
    for a buy, on any ISIN, not only the stopped one.
    """
    codes: list[str] = []
    buy = intent.side == "buy"
    price = Decimal(intent.limit_price)
    quantity = intent.quantity
    notional = price * quantity

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
    if not intent.isin.startswith("INE"):
        codes.append("instrument_unsupported")
    elif fresh:
        assert quote is not None
        if quote.isin != intent.isin or quote.stock_code != intent.stock_code:
            codes.append("isin_mismatch")
        elif quote.series not in SUPPORTED_SERIES:
            codes.append("instrument_unsupported")
        else:
            try:
                tick = (tick_table or load_tick_table()).resolve(
                    now_ist.date(), quote.series, quote.previous_close
                )
                if price % tick != ZERO:
                    codes.append("off_tick")
            except (TickUnavailable, LimitsError):
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
            (o.quantity * o.limit_price for o in open_buys if o.isin == intent.isin),
            ZERO,
        )
        if costs.get(intent.isin, ZERO) + isin_pending + notional > limits.per_position_cap:
            codes.append("per_position_cap")
    else:
        held = sum((h.quantity for h in account.holdings if h.isin == intent.isin), 0)
        open_sells = sum(
            (o.quantity for o in account.open_orders if o.isin == intent.isin and o.side == "sell"),
            0,
        )
        if quantity > held - open_sells:
            codes.append("sell_exceeds_holding")
    return tuple(codes)


def to_ist(now: datetime) -> datetime:
    if now.tzinfo is None:
        raise ValueError("clock must be timezone-aware")
    return now.astimezone(IST)

