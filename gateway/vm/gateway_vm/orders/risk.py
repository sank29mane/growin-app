"""VM-derived risk state: the Option B latches, the fill ledger, the charge bound
and the session-end alert (P-07, P-08, D-04, D-05, D-07).

Pure functions over an OrderState value. Nothing here touches a file or the
network; store.py persists the state and the pipeline supplies fresh reads.

Latch rules (each pinned by a test):

- halt (drawdown <= -8%): buys refused, sells allowed. Cleared only by the
  admin reset. A verified sell, or a sell fill on the trade list, does not
  clear it.
- ended (drawdown <= -15%): sells only, terminal. The reset CLI refuses it.
- stop (close <= cost x (1 + position_stop)), per ISIN: that ISIN is sell-only
  and every buy on every ISIN is refused (stop_open) until the trade list shows
  the exit filled. A verified sell does not clear it; the fill does.
- mac_halt and account_mismatch: everything refused until the admin reset.

Latches never clear on recovery. A reset of halt does not rebase the peak, so a
reset while still at or below -8% re-latches at the next evaluated close.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Protocol, Sequence

from .audit import AuditBroken, AuditLog
from .limits import (
    ONE,
    ZERO,
    Holding,
    Limits,
    RiskFlags,
    Trade,
    to_ist,
)

SESSION_END = time(15, 30)

# P-07: conservative per-fill bound on charges, derived from the Phase 60
# schedule icici_nse_cash_charges.json (delivery, the costliest class). Worst
# rates per rupee of traded value, with GST on brokerage, exchange and SEBI
# lines: buy about 0.2012%, sell about 0.1862%. The rate below rounds up to
# 0.25%. A sell also pays one DP debit (20 plus 18% GST = 23.60), bounded by
# the fixed amount, which also covers per-line rounding on every fill.
# tests/backend/test_gateway_orders_state.py recomputes the schedule's real
# charges and fails if this is zero, below any row, or if the schedule file
# changes without the bound being re-derived.
CHARGE_BOUND_SCHEDULE_VERSION = "icici-prime9999-ivalue-nse-cash-2024-10-01.r1"
CHARGE_RATE_BOUND = Decimal("0.0025")
CHARGE_ROUNDING_BOUND = Decimal("0.10")
CHARGE_SELL_FIXED_BOUND = Decimal("25")

LATCH_NAMES = ("halt", "ended", "mac_halt", "account_mismatch", "stop")
RESETTABLE = ("halt", "stop", "mac_halt", "account_mismatch")

Clock = Callable[[], datetime]


class StateInvalid(Exception):
    """Persisted state does not validate. The store maps this to 503."""


class MarkMissing(Exception):
    """A held ISIN has no close for the session being evaluated."""


class MarkMismatch(Exception):
    """The latest bar close and the quote's previous_close disagree by more than a tick (A2)."""


class ResetRefused(Exception):
    pass


def charge_bound(side: str, traded_value: Decimal) -> Decimal:
    """Upper bound on one fill's charges. Used only when trade detail has none."""
    bound = traded_value * CHARGE_RATE_BOUND + CHARGE_ROUNDING_BOUND
    if side == "sell":
        bound += CHARGE_SELL_FIXED_BOUND
    return bound


@dataclass
class Fill:
    isin: str
    side: str
    quantity: int
    price: Decimal
    charges: Decimal


@dataclass
class OrderState:
    start_equity: Decimal
    cash: Decimal
    peak: Decimal
    peak_date: str | None = None
    last_evaluated_session: str | None = None
    drawdown: Decimal = ZERO
    halt: bool = False
    ended: bool = False
    mac_halt: bool = False
    account_mismatch: bool = False
    stops: dict[str, dict[str, Any]] = field(default_factory=dict)
    fills: dict[str, Fill] = field(default_factory=dict)
    consumed_intents: list[str] = field(default_factory=list)
    alerts_sent: list[str] = field(default_factory=list)

    # -- views -------------------------------------------------------------

    def latch_names(self) -> tuple[str, ...]:
        names = [n for n in ("halt", "ended", "mac_halt", "account_mismatch") if getattr(self, n)]
        if self.stops:
            names.append("stop")
        return tuple(names)

    def flags(self) -> RiskFlags:
        return RiskFlags(
            halt=self.halt,
            ended=self.ended,
            mac_halt=self.mac_halt,
            account_mismatch=self.account_mismatch,
            stops=frozenset(self.stops),
            ledger_cost=ledger_cost(self),
        )

    def copy(self) -> "OrderState":
        return copy.deepcopy(self)

    # -- persistence shape ---------------------------------------------------

    def to_json(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "workspace": "india",
            "start_equity": _d(self.start_equity),
            "cash": _d(self.cash),
            "peak": _d(self.peak),
            "peak_date": self.peak_date,
            "last_evaluated_session": self.last_evaluated_session,
            "drawdown": _d(self.drawdown),
            "halt": self.halt,
            "ended": self.ended,
            "mac_halt": self.mac_halt,
            "account_mismatch": self.account_mismatch,
            "stops": {k: dict(v) for k, v in sorted(self.stops.items())},
            "fills": {
                tid: {
                    "isin": f.isin,
                    "side": f.side,
                    "quantity": f.quantity,
                    "price": _d(f.price),
                    "charges": _d(f.charges),
                }
                for tid, f in self.fills.items()
            },
            "consumed_intents": list(self.consumed_intents),
            "alerts_sent": list(self.alerts_sent),
        }

    @classmethod
    def from_json(cls, raw: Any) -> "OrderState":
        keys = {
            "schema_version", "workspace", "start_equity", "cash", "peak", "peak_date",
            "last_evaluated_session", "drawdown", "halt", "ended", "mac_halt",
            "account_mismatch", "stops", "fills", "consumed_intents", "alerts_sent",
        }
        try:
            if not isinstance(raw, dict) or set(raw) != keys:
                raise StateInvalid("state keys")
            if raw["schema_version"] != 1 or isinstance(raw["schema_version"], bool):
                raise StateInvalid("schema_version")
            if raw["workspace"] != "india":
                raise StateInvalid("workspace")
            for name in ("halt", "ended", "mac_halt", "account_mismatch"):
                if not isinstance(raw[name], bool):
                    raise StateInvalid(name)
            for name in ("peak_date", "last_evaluated_session"):
                if raw[name] is not None:
                    date.fromisoformat(raw[name])
            stops = raw["stops"]
            if not isinstance(stops, dict):
                raise StateInvalid("stops")
            for isin, rec in stops.items():
                if (
                    not isinstance(isin, str)
                    or not isinstance(rec, dict)
                    or set(rec) != {"session", "quantity"}
                    or isinstance(rec["quantity"], bool)
                    or not isinstance(rec["quantity"], int)
                ):
                    raise StateInvalid("stops record")
                date.fromisoformat(rec["session"])
            fills: dict[str, Fill] = {}
            if not isinstance(raw["fills"], dict):
                raise StateInvalid("fills")
            for tid, rec in raw["fills"].items():
                if (
                    not isinstance(rec, dict)
                    or set(rec) != {"isin", "side", "quantity", "price", "charges"}
                    or rec["side"] not in ("buy", "sell")
                    or isinstance(rec["quantity"], bool)
                    or not isinstance(rec["quantity"], int)
                    or rec["quantity"] <= 0
                ):
                    raise StateInvalid("fill record")
                fills[tid] = Fill(
                    isin=str(rec["isin"]),
                    side=rec["side"],
                    quantity=rec["quantity"],
                    price=_parse_d(rec["price"]),
                    charges=_parse_d(rec["charges"]),
                )
            for name in ("consumed_intents", "alerts_sent"):
                if not isinstance(raw[name], list) or not all(isinstance(x, str) for x in raw[name]):
                    raise StateInvalid(name)
            return cls(
                start_equity=_parse_d(raw["start_equity"]),
                cash=_parse_d(raw["cash"]),
                peak=_parse_d(raw["peak"]),
                peak_date=raw["peak_date"],
                last_evaluated_session=raw["last_evaluated_session"],
                drawdown=_parse_d(raw["drawdown"]),
                halt=raw["halt"],
                ended=raw["ended"],
                mac_halt=raw["mac_halt"],
                account_mismatch=raw["account_mismatch"],
                stops={k: dict(v) for k, v in stops.items()},
                fills=fills,
                consumed_intents=list(raw["consumed_intents"]),
                alerts_sent=list(raw["alerts_sent"]),
            )
        except (ValueError, TypeError, InvalidOperation) as exc:
            raise StateInvalid("state value") from exc


def _d(value: Decimal) -> str:
    return format(value, "f")


def _parse_d(value: Any) -> Decimal:
    if not isinstance(value, str):
        raise StateInvalid("decimal must be a string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise StateInvalid("decimal") from exc
    if not parsed.is_finite():
        raise StateInvalid("decimal")
    return parsed


def initial_state(limits: Limits) -> OrderState:
    """P-07: pilot start equity and the initial peak are the capital cap."""
    return OrderState(
        start_equity=limits.capital_cap, cash=limits.capital_cap, peak=limits.capital_cap
    )


# ------------------------------------------------------------------- ledger


def net_quantities(state: OrderState) -> dict[str, int]:
    net: dict[str, int] = {}
    for fill in state.fills.values():
        delta = fill.quantity if fill.side == "buy" else -fill.quantity
        net[fill.isin] = net.get(fill.isin, 0) + delta
    return net


def ledger_cost(state: OrderState) -> dict[str, Decimal]:
    """Average-cost basis per ISIN from the fill ledger (charges excluded, P-09)."""
    qty: dict[str, int] = {}
    cost: dict[str, Decimal] = {}
    for fill in state.fills.values():
        held = qty.get(fill.isin, 0)
        if fill.side == "buy":
            qty[fill.isin] = held + fill.quantity
            cost[fill.isin] = cost.get(fill.isin, ZERO) + fill.quantity * fill.price
        else:
            if held <= 0:
                continue
            remaining = max(held - fill.quantity, 0)
            cost[fill.isin] = cost[fill.isin] * remaining / held
            qty[fill.isin] = remaining
    return {isin: c for isin, c in cost.items() if qty.get(isin, 0) > 0}


def apply_trades(state: OrderState, trades: Sequence[Trade]) -> bool:
    """Ingest new fills (idempotent by trade_id). Returns True if state changed.

    Cash falls by buy value and rises by sell proceeds, less charges: the
    trade-detail charges when populated, else the P-07 bound. A sell fill that
    brings an ISIN's net quantity to zero is the fill evidence that clears its
    stop latch. It never touches halt. A sell that would take net quantity
    below zero cannot have come from this ledger, so it latches account_mismatch.
    """
    changed = False
    for trade in trades:
        if trade.trade_id in state.fills:
            continue
        value = trade.quantity * trade.price
        charges = trade.charges if trade.charges is not None else charge_bound(trade.side, value)
        state.fills[trade.trade_id] = Fill(
            isin=trade.isin,
            side=trade.side,
            quantity=trade.quantity,
            price=trade.price,
            charges=charges,
        )
        if trade.side == "buy":
            state.cash -= value + charges
        else:
            state.cash += value - charges
        changed = True
        net = net_quantities(state).get(trade.isin, 0)
        if trade.side == "sell":
            if net < 0:
                state.account_mismatch = True
            elif net == 0 and trade.isin in state.stops:
                del state.stops[trade.isin]
    return changed


def detect_mismatch(state: OrderState, holdings: Sequence[Holding]) -> bool:
    """D-04: an ISIN the ledger never bought, or more shares than it explains."""
    net = net_quantities(state)
    if any(q < 0 for q in net.values()):
        return True
    for holding in holdings:
        if holding.quantity > net.get(holding.isin, 0):
            return True
    return False


# ----------------------------------------------------------------- Option B


def evaluate_session(
    state: OrderState,
    limits: Limits,
    session: date,
    closes: Mapping[str, Decimal],
) -> None:
    """Apply the close of one session. Mutates state; the caller persists it.

    Sessions must arrive in order; one at or before the last evaluated is a
    no-op. Equity = cash + net quantity x close. Peak starts at the capital cap
    and only rises. Triggers are inclusive: equity <= peak x (1 + threshold).
    A gap straight through -15% sets halt and ended together.
    """
    iso = session.isoformat()
    if state.last_evaluated_session is not None and iso <= state.last_evaluated_session:
        return
    net = {isin: q for isin, q in net_quantities(state).items() if q > 0}
    for isin in net:
        if isin not in closes:
            raise MarkMissing(isin)
    equity = state.cash + sum((q * closes[isin] for isin, q in net.items()), ZERO)
    if equity > state.peak:
        state.peak = equity
        state.peak_date = iso
    state.drawdown = (equity / state.peak - ONE).quantize(Decimal("0.000001"))
    if equity <= state.peak * (ONE + limits.drawdown_flatten):
        state.halt = True
        state.ended = True
    elif equity <= state.peak * (ONE + limits.drawdown_halt):
        state.halt = True
    cost = ledger_cost(state)
    for isin, quantity in net.items():
        if isin in state.stops:
            continue
        if quantity * closes[isin] <= cost.get(isin, ZERO) * (ONE + limits.position_stop):
            state.stops[isin] = {"session": iso, "quantity": quantity}
    state.last_evaluated_session = iso


def assert_marks_agree(
    closes: Mapping[str, Decimal],
    previous_closes: Mapping[str, Decimal],
    ticks: Mapping[str, Decimal],
) -> None:
    """A2: the latest bar close must match the quote's previous_close within one tick."""
    for isin, close in closes.items():
        if isin not in previous_closes or isin not in ticks:
            raise MarkMismatch(isin)
        if abs(close - previous_closes[isin]) > ticks[isin]:
            raise MarkMismatch(isin)


def reset_latch(state: OrderState, latch: str, isin: str | None = None) -> None:
    """Admin reset (VM shell, admin window). ended is terminal and refused."""
    if latch == "ended":
        raise ResetRefused("the pilot-ended latch is terminal")
    if latch not in RESETTABLE:
        raise ResetRefused("unknown latch")
    if latch == "stop":
        if isin is None:
            state.stops.clear()
        elif isin in state.stops:
            del state.stops[isin]
        else:
            raise ResetRefused("no stop latch for that ISIN")
    else:
        setattr(state, latch, False)


# ----------------------------------------------------------- session-end alert


@dataclass(frozen=True)
class StopExitAlert:
    """The alert body: ISIN, session date, quantity. No account values."""

    isin: str
    session_date: date
    quantity: int


class AlertPort(Protocol):
    """Interface to the existing halt and kill-switch alert channel (bound in 63-05)."""

    def send(self, alert: StopExitAlert) -> None: ...


class AlertUnavailable(Exception):
    pass


class UnboundAlertPort:
    """Default until 63-05 binds the channel: fails loudly, never silently drops."""

    def send(self, alert: StopExitAlert) -> None:
        raise AlertUnavailable("the operator alert channel is not bound yet")


@dataclass(frozen=True)
class SessionEndResult:
    sent: tuple[StopExitAlert, ...] = ()
    failed: tuple[StopExitAlert, ...] = ()
    audit_failed: bool = False

    @property
    def ok(self) -> bool:
        return not self.failed and not self.audit_failed


def session_end_check(
    state: OrderState,
    clock: Clock,
    alert_port: AlertPort,
    *,
    audit: AuditLog | None = None,
    limits_sha256: str | None = None,
) -> SessionEndResult:
    """At or after 15:30:00 IST on a weekday, alert once per (session, ISIN) for
    every stop exit still open.

    Mutates state.alerts_sent for each alert that went out; the caller persists
    it. A port that raises records `alert_failed` in the audit and is retried on
    the next call. Nothing here blocks or unblocks an order. Reads persisted
    state only, so a stale ledger can over-alert but never suppresses one.
    """
    now = to_ist(clock())
    if now.weekday() >= 5 or now.time() < SESSION_END or not state.stops:
        return SessionEndResult()
    net = net_quantities(state)
    sent: list[StopExitAlert] = []
    failed: list[StopExitAlert] = []
    audit_failed = False
    for isin in sorted(state.stops):
        key = f"{now.date().isoformat()}:{isin}"
        if key in state.alerts_sent:
            continue
        quantity = net.get(isin, 0)
        if quantity <= 0:
            quantity = int(state.stops[isin]["quantity"])
        alert = StopExitAlert(isin=isin, session_date=now.date(), quantity=quantity)
        code = "stop_exit_open"
        try:
            alert_port.send(alert)
        except Exception:
            failed.append(alert)
            code = "alert_failed"
        else:
            sent.append(alert)
            state.alerts_sent.append(key)
        if audit is not None:
            try:
                audit.append(
                    {
                        "route": "session_end",
                        "decision": "EVALUATED",
                        "codes": [code],
                        "isin": isin,
                        "quantity": quantity,
                        "limits_sha256": limits_sha256,
                        "latches": list(state.latch_names()),
                    }
                )
            except AuditBroken:
                audit_failed = True
    return SessionEndResult(sent=tuple(sent), failed=tuple(failed), audit_failed=audit_failed)
