"""Option B latch machine for the Mac (D-05 to D-08, P-08, RISK-01, RISK-02).

State is a frozen value; every step returns a new one. Persistence is 63-04.

Rules, each pinned by a test:

- Peak starts at ``capital_cap`` and rises only when a session close is evaluated.
- halt: equity <= peak x (1 + drawdown_halt), inclusive. Buys stop, sells stay.
  The first transition pre-builds a pro-rata halve batch (floor(q / 2)).
- ended: equity <= peak x (1 + drawdown_flatten), inclusive. Terminal for the pilot.
  Sets halt too and builds a flatten batch for every position, and only that batch,
  including on a gap straight from above -8% to below -15%.
- stop, per ISIN: close x quantity <= cost x (1 + stop), where stop is the fixed
  ``position_stop`` (-12%) or a tighter vol-scaled value. The close raises it; the
  exit is placed next session. A vol-scaled stop looser than the fixed one is
  refused as a config error and the fixed stop applies (D-08).
- Latches never clear on recovery. Only ``reset`` clears halt or a stop, by an
  explicit actor (the admin window). ``ended`` cannot be reset. A stop also clears
  when the exit fill leaves the position at zero (``apply_exit_fill``), which is
  fill evidence, not a reset. A partial fill does not clear it.
- A halt reset does not rebase the peak, so a reset while equity is still at or
  below -8% of the old peak latches again at the next evaluated close (the VM does
  the same; the operator decides in 63-06 whether to rebase).
- An exit that misses (a Phase 60 limit fill that does not fill) stays in
  ``open_exits`` with its latch until a fill reduces it. ``pending_batches`` rebuilds
  it for the next session. It is never dropped on a miss.

This differs from Phase 62's backtest on purpose: 62 releases a halt automatically
when drawdown recovers past ``halt_release``; live release is admin-only (D-05).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping, Sequence

from .exits import (
    FLATTEN,
    HALVE,
    PRIORITY,
    STOP,
    ExitBatch,
    ExitError,
    Position,
    build_batch,
    halve_quantity,
)
from .rules import ONE, ZERO, Limits, RiskFlags

RESETTABLE = ("halt", "stop")
_DD_QUANT = Decimal("0.000001")


class MarkMissing(ValueError):
    """A held ISIN has no close for the session being evaluated. Fail closed."""


class ResetRefused(ValueError):
    """The reset is not allowed (ended is terminal, an actor is required, nothing to clear)."""


class StateInconsistent(ValueError):
    """An open exit has no held position. The caller must reconcile; nothing is guessed."""


def _frozen(mapping: Mapping) -> Mapping:
    return MappingProxyType(dict(mapping))


@dataclass(frozen=True)
class StopLatch:
    session: date  # the close that raised it
    quantity: int


@dataclass(frozen=True)
class OpenExit:
    reason: str  # halve | stop | flatten
    quantity: int  # halve: shares still to sell. stop and flatten: the whole position
    stock_code: str
    since: date


@dataclass(frozen=True)
class ResetRecord:
    latch: str
    actor: str
    isin: str | None


@dataclass(frozen=True)
class RiskState:
    peak: Decimal
    last_session: date | None = None
    drawdown: Decimal = ZERO
    halt: bool = False
    ended: bool = False
    stops: Mapping[str, StopLatch] = field(default_factory=lambda: _frozen({}))
    open_exits: Mapping[str, OpenExit] = field(default_factory=lambda: _frozen({}))
    resets: tuple[ResetRecord, ...] = ()

    def latch_names(self) -> tuple[str, ...]:
        names = [n for n in ("halt", "ended") if getattr(self, n)]
        if self.stops:
            names.append("stop")
        return tuple(names)

    def flags(
        self,
        *,
        ledger_cost: Mapping[str, Decimal] | None = None,
        mac_halt: bool = False,
        account_mismatch: bool = False,
    ) -> RiskFlags:
        """The view ``rules.evaluate`` takes. mac_halt and account_mismatch come from the VM."""
        return RiskFlags(
            halt=self.halt,
            ended=self.ended,
            mac_halt=mac_halt,
            account_mismatch=account_mismatch,
            stops=frozenset(self.stops),
            ledger_cost=dict(ledger_cost or {}),
        )


@dataclass(frozen=True)
class SessionResult:
    state: RiskState
    batches: tuple[ExitBatch, ...] = ()  # newly raised this session only
    config_errors: tuple[str, ...] = ()


def initial_state(limits: Limits) -> RiskState:
    """Pilot start: the peak is the capital cap (P-07)."""
    return RiskState(peak=limits.capital_cap)


def effective_stop(limits: Limits, requested: Decimal | None) -> tuple[Decimal, str | None]:
    """D-08: the fixed ``position_stop`` unless a vol-scaled stop is tighter.

    Returns (stop to use, config error or None). A value that is not a Decimal in
    (-1, 0), or looser than the fixed stop, is refused and the fixed stop applies.
    """
    if requested is None:
        return limits.position_stop, None
    if (
        not isinstance(requested, Decimal)
        or not requested.is_finite()
        or not -ONE < requested < ZERO
    ):
        return limits.position_stop, "vol_stop_invalid"
    if requested < limits.position_stop:
        return limits.position_stop, "vol_stop_looser_than_position_stop"
    return requested, None


def _merge(
    exits: dict[str, OpenExit], position: Position, reason: str, quantity: int, session: date
) -> bool:
    """Set the exit for a position unless a stronger one is already open. True if it changed."""
    current = exits.get(position.isin)
    if current is not None and PRIORITY[current.reason] >= PRIORITY[reason]:
        return False
    exits[position.isin] = OpenExit(
        reason, quantity, position.stock_code, current.since if current else session
    )
    return True


def evaluate_session(
    state: RiskState,
    limits: Limits,
    session: date,
    *,
    cash: Decimal,
    positions: Sequence[Position],
    closes: Mapping[str, Decimal],
    vol_stops: Mapping[str, Decimal] | None = None,
) -> SessionResult:
    """Apply one session close. A session at or before the last evaluated is a no-op.

    Equity = cash + sum(quantity x close). ``cash`` already carries Phase 60 charges.
    """
    if state.last_session is not None and session <= state.last_session:
        return SessionResult(state)
    if len({p.isin for p in positions}) != len(positions):
        raise ExitError("one position per ISIN")
    for position in positions:
        if position.isin not in closes:
            raise MarkMissing(position.isin)
    equity = cash + sum((p.quantity * closes[p.isin] for p in positions), ZERO)
    peak = max(state.peak, equity)
    flatten_hit = equity <= peak * (ONE + limits.drawdown_flatten)
    halt_hit = equity <= peak * (ONE + limits.drawdown_halt)

    config_errors: list[str] = []
    stops = dict(state.stops)
    open_exits = dict(state.open_exits)
    newly_stopped: list[Position] = []
    for position in positions:
        if position.isin in stops:
            continue
        stop, error = effective_stop(limits, (vol_stops or {}).get(position.isin))
        if error is not None:
            config_errors.append(f"{error}:{position.isin}")
        if position.quantity * closes[position.isin] <= position.cost * (ONE + stop):
            stops[position.isin] = StopLatch(session, position.quantity)
            newly_stopped.append(position)

    new_ended = flatten_hit and not state.ended
    new_halt = (halt_hit or flatten_hit) and not state.halt
    batches: list[ExitBatch] = []

    if new_ended:
        changed = [p for p in positions if _merge(open_exits, p, FLATTEN, p.quantity, session)]
        batch = build_batch(FLATTEN, session, [(p.isin, p.stock_code, p.quantity) for p in changed])
        if batch:
            batches.append(batch)
    else:
        stopped = [p for p in newly_stopped if _merge(open_exits, p, STOP, p.quantity, session)]
        batch = build_batch(STOP, session, [(p.isin, p.stock_code, p.quantity) for p in stopped])
        if batch:
            batches.append(batch)
        if new_halt:
            halved = [
                p
                for p in positions
                if halve_quantity(p.quantity) >= 1
                and _merge(open_exits, p, HALVE, halve_quantity(p.quantity), session)
            ]
            batch = build_batch(
                HALVE, session, [(p.isin, p.stock_code, halve_quantity(p.quantity)) for p in halved]
            )
            if batch:
                batches.append(batch)

    batches.sort(key=lambda b: (-PRIORITY[b.reason], b.batch_id))
    new_state = replace(
        state,
        peak=peak,
        last_session=session,
        drawdown=(equity / peak - ONE).quantize(_DD_QUANT),
        halt=state.halt or halt_hit or flatten_hit,
        ended=state.ended or flatten_hit,
        stops=_frozen(stops),
        open_exits=_frozen(open_exits),
    )
    return SessionResult(new_state, tuple(batches), tuple(config_errors))


def apply_exit_fill(
    state: RiskState, isin: str, *, sold_quantity: int, remaining_quantity: int
) -> RiskState:
    """Record exit fill evidence for one ISIN. Only a fill reduces an open exit.

    ``remaining_quantity`` is what is still held after the fill. A stop latch clears
    only when it reaches zero; a partial fill leaves it set. halt and ended are
    never touched by a fill.
    """
    for name, value in (("sold_quantity", sold_quantity), ("remaining_quantity", remaining_quantity)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ExitError(f"{name} must be a non-negative integer")
    open_exits = dict(state.open_exits)
    stops = dict(state.stops)
    current = open_exits.get(isin)
    if current is not None:
        if remaining_quantity == 0:
            del open_exits[isin]
        elif current.reason == HALVE:
            left = current.quantity - sold_quantity
            if left <= 0:
                del open_exits[isin]
            else:
                open_exits[isin] = replace(current, quantity=left)
        else:
            open_exits[isin] = replace(current, quantity=remaining_quantity)
    if remaining_quantity == 0:
        stops.pop(isin, None)
    return replace(state, stops=_frozen(stops), open_exits=_frozen(open_exits))


def pending_batches(
    state: RiskState, positions: Sequence[Position], decided_on: date
) -> tuple[ExitBatch, ...]:
    """Rebuild every open exit for the next session (re-issue after a miss).

    flatten and stop sell what is still held; halve sells what is left of its
    target, never more than held. An open exit with no held position is an
    inconsistency and raises rather than vanishing.
    """
    held = {p.isin: p for p in positions}
    legs: dict[str, list[tuple[str, str, int]]] = {}
    for isin, exit_ in sorted(state.open_exits.items()):
        position = held.get(isin)
        if position is None:
            raise StateInconsistent(isin)
        quantity = position.quantity if exit_.reason != HALVE else min(exit_.quantity, position.quantity)
        legs.setdefault(exit_.reason, []).append((isin, exit_.stock_code, quantity))
    batches = [
        batch
        for reason in sorted(legs, key=lambda r: -PRIORITY[r])
        if (batch := build_batch(reason, decided_on, legs[reason])) is not None
    ]
    return tuple(batches)


def reset(state: RiskState, latch: str, actor: str, *, isin: str | None = None) -> RiskState:
    """Admin release (D-05). The only way a halt or a stop clears without a fill.

    ``ended`` is terminal and refused. The peak is not rebased, and open exits are
    left in place (a reset releases the buy block, it does not cancel a sell).
    """
    if not isinstance(actor, str) or not actor.strip():
        raise ResetRefused("a reset needs a named actor")
    if latch == "ended":
        raise ResetRefused("the pilot-ended latch is terminal")
    if latch not in RESETTABLE:
        raise ResetRefused("unknown latch")
    if latch == "halt":
        if state.ended:
            raise ResetRefused("halt cannot be released while the pilot is ended")
        if not state.halt:
            raise ResetRefused("halt is not set")
        new = replace(state, halt=False)
    else:
        stops = dict(state.stops)
        if isin is None:
            if not stops:
                raise ResetRefused("no stop latch is set")
            stops.clear()
        elif isin in stops:
            del stops[isin]
        else:
            raise ResetRefused("no stop latch for that ISIN")
        new = replace(state, stops=_frozen(stops))
    return replace(new, resets=state.resets + (ResetRecord(latch, actor.strip(), isin),))
