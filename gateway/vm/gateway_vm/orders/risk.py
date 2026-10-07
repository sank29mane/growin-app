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

Latches never clear on recovery. The true drawdown peak is never rebased, so the
-15% end is always measured from the real high-water mark. `reset halt` refuses
while drawdown is at or below the halt threshold, because it would re-latch at
the next evaluated close. With the explicit rebase flag the operator sets a
separate halt anchor (the equity at the last evaluated close) that only the -8%
test reads: the halt fires again at 8% below the anchor, or below the anchor's
own running high, and the anchor drops away once equity regains the true peak.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
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
    # Chronology, persisted explicitly: never inferred from dict or JSON key
    # order, and never from trade-id spelling. executed_at is the exchange time
    # as a fixed-width UTC string, so string order is time order; seq is the
    # arrival number and breaks ties. A trade with no time inherits the latest
    # time already in the ledger ("" if none), so it orders by arrival.
    executed_at: str = ""
    seq: int = 0


_STAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z")


def _stamp(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def chronological_fills(state: "OrderState") -> list[tuple[str, Fill]]:
    """Every fill as (trade_id, Fill) in execution order: exchange time, then arrival."""
    return sorted(state.fills.items(), key=lambda item: (item[1].executed_at, item[1].seq))


@dataclass
class OrderState:
    start_equity: Decimal
    cash: Decimal
    peak: Decimal
    peak_date: str | None = None
    last_evaluated_session: str | None = None
    drawdown: Decimal = ZERO
    # Equity at the last evaluated close (exact, unlike the rounded drawdown).
    last_equity: Decimal | None = None
    # Operator-set reference for the -8% halt test only (see reset_latch). None
    # means the halt test measures from the true peak. Never read by the -15% end.
    halt_anchor: Decimal | None = None
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
            "last_equity": None if self.last_equity is None else _d(self.last_equity),
            "halt_anchor": None if self.halt_anchor is None else _d(self.halt_anchor),
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
                    "executed_at": f.executed_at,
                    "seq": f.seq,
                }
                for tid, f in chronological_fills(self)
            },
            "consumed_intents": list(self.consumed_intents),
            "alerts_sent": list(self.alerts_sent),
        }

    @classmethod
    def from_json(cls, raw: Any) -> "OrderState":
        keys = {
            "schema_version", "workspace", "start_equity", "cash", "peak", "peak_date",
            "last_evaluated_session", "drawdown", "last_equity", "halt_anchor", "halt",
            "ended", "mac_halt", "account_mismatch", "stops", "fills", "consumed_intents",
            "alerts_sent",
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
            seen_seq: set[int] = set()
            for tid, rec in raw["fills"].items():
                if (
                    not isinstance(rec, dict)
                    or set(rec) != {"isin", "side", "quantity", "price", "charges", "executed_at", "seq"}
                    or rec["side"] not in ("buy", "sell")
                    or isinstance(rec["quantity"], bool)
                    or not isinstance(rec["quantity"], int)
                    or rec["quantity"] <= 0
                    or isinstance(rec["seq"], bool)
                    or not isinstance(rec["seq"], int)
                    or rec["seq"] < 0
                    or rec["seq"] in seen_seq
                    or not isinstance(rec["executed_at"], str)
                    or (rec["executed_at"] != "" and _STAMP.fullmatch(rec["executed_at"]) is None)
                ):
                    raise StateInvalid("fill record")
                seen_seq.add(rec["seq"])
                fills[tid] = Fill(
                    isin=str(rec["isin"]),
                    side=rec["side"],
                    quantity=rec["quantity"],
                    price=_parse_d(rec["price"]),
                    charges=_parse_d(rec["charges"]),
                    executed_at=rec["executed_at"],
                    seq=rec["seq"],
                )
            # Whatever order the JSON listed them in, the ledger is chronological.
            fills = dict(
                sorted(fills.items(), key=lambda item: (item[1].executed_at, item[1].seq))
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
                last_equity=None if raw["last_equity"] is None else _parse_d(raw["last_equity"]),
                halt_anchor=None if raw["halt_anchor"] is None else _parse_d(raw["halt_anchor"]),
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
    for _, fill in chronological_fills(state):
        delta = fill.quantity if fill.side == "buy" else -fill.quantity
        net[fill.isin] = net.get(fill.isin, 0) + delta
    return net


def ledger_cost(state: OrderState) -> dict[str, Decimal]:
    """Average-cost basis per ISIN from the fill ledger (charges excluded, P-09).

    Fills are replayed in execution order (exchange time, then arrival): a sell
    shrinks the cost of what was held at that moment, so order changes the answer.
    """
    qty: dict[str, int] = {}
    cost: dict[str, Decimal] = {}
    for _, fill in chronological_fills(state):
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
    # Unseen trades only (idempotent by trade_id, also within one batch), applied
    # in exchange-time order so a snapshot that lists a later fill first cannot
    # make a sell look larger than the holding. Untimed trades go last, in
    # arrival order.
    batch: dict[str, tuple[int, Trade]] = {}
    for index, trade in enumerate(trades):
        if trade.trade_id not in state.fills and trade.trade_id not in batch:
            batch[trade.trade_id] = (index, trade)
    ordered = sorted(
        batch.values(),
        key=lambda item: (
            item[1].executed_at is None,
            _stamp(item[1].executed_at) if item[1].executed_at is not None else "",
            item[0],
        ),
    )
    for _, trade in ordered:
        value = trade.quantity * trade.price
        charges = trade.charges if trade.charges is not None else charge_bound(trade.side, value)
        latest = max((f.executed_at for f in state.fills.values()), default="")
        state.fills[trade.trade_id] = Fill(
            isin=trade.isin,
            side=trade.side,
            quantity=trade.quantity,
            price=trade.price,
            charges=charges,
            executed_at=_stamp(trade.executed_at) if trade.executed_at is not None else latest,
            seq=max((f.seq for f in state.fills.values()), default=-1) + 1,
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

    The -15% end is always measured from the true peak. The -8% halt is measured
    from the true peak too, unless the operator rebased the halt anchor (see
    reset_latch): then it is measured from the anchor, which follows equity up
    and is dropped once it reaches the true peak.
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
    if state.halt_anchor is not None:
        if equity > state.halt_anchor:
            state.halt_anchor = equity
        if state.halt_anchor >= state.peak:
            state.halt_anchor = None  # back at the true high-water mark: ordinary rules
    halt_reference = state.peak if state.halt_anchor is None else state.halt_anchor
    if equity <= state.peak * (ONE + limits.drawdown_flatten):
        state.halt = True
        state.ended = True
    elif equity <= halt_reference * (ONE + limits.drawdown_halt):
        state.halt = True
    cost = ledger_cost(state)
    for isin, quantity in net.items():
        if isin in state.stops:
            continue
        if quantity * closes[isin] <= cost.get(isin, ZERO) * (ONE + limits.position_stop):
            state.stops[isin] = {"session": iso, "quantity": quantity}
    state.last_evaluated_session = iso
    state.last_equity = equity


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


def reset_latch(
    state: OrderState,
    latch: str,
    isin: str | None = None,
    *,
    limits: Limits | None = None,
    rebase_halt_anchor: bool = False,
) -> bool:
    """Admin reset (VM shell, admin window). ended is terminal and refused.

    Resetting halt is refused while drawdown is at or below the halt threshold:
    the next evaluated close would only latch it again. The operator may pass
    ``rebase_halt_anchor`` to accept the loss and restart the -8% test from the
    current equity. That sets ``state.halt_anchor``; it never touches the peak,
    so the -15% end is still measured from the real high-water mark. Returns True
    when the halt anchor was rebased (the admin CLI audits it).
    """
    if latch == "ended":
        raise ResetRefused("the pilot-ended latch is terminal")
    if latch not in RESETTABLE:
        raise ResetRefused("unknown latch")
    if rebase_halt_anchor and latch != "halt":
        raise ResetRefused("the halt anchor applies to the halt latch only")
    rebased = False
    if latch == "halt":
        if limits is None:
            raise ResetRefused("the limits are required to reset the halt latch")
        if state.drawdown <= limits.drawdown_halt:
            if not rebase_halt_anchor:
                raise ResetRefused(
                    "drawdown is still at or below the halt threshold: the halt would latch "
                    "again at the next close; pass --rebase-halt-anchor to restart the -8% "
                    "test from the current equity"
                )
            if state.last_equity is None or not state.last_equity > ZERO:
                raise ResetRefused("no evaluated close to anchor the halt test to")
            state.halt_anchor = state.last_equity
            rebased = True
    if latch == "stop":
        if isin is None:
            state.stops.clear()
        elif isin in state.stops:
            del state.stops[isin]
        else:
            raise ResetRefused("no stop latch for that ISIN")
    else:
        setattr(state, latch, False)
    return rebased


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
