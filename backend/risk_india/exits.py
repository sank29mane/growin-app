"""Exit batches for the Option B ladder (D-06, D-07, P-08): halve, flatten and stop.

A batch is one SELL intent per position, all sharing a ``batch_id``. These are plain
frozen data the execution layer registers later (63-04). Nothing here calls a ledger,
a broker or HTTP, and an intent carries no limit price: the price is chosen at
registration, inside the collar and the circuit band, so it is never stale.

``batch_id`` matches the O3 pattern ``^[a-z0-9-]{8,64}$`` and is deterministic in
(reason, decision session, legs), so a retry of the same decision is the same batch
and a re-issue on a later session is a different one.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Sequence

HALVE = "halve"
FLATTEN = "flatten"
STOP = "stop"
REASONS = (FLATTEN, STOP, HALVE)  # also the order batches are emitted in
# Same ranking as Phase 62's PRIORITY for these three reasons: a stronger exit
# supersedes a weaker one on the same position, never the other way round.
PRIORITY = {HALVE: 1, STOP: 4, FLATTEN: 5}

BATCH_ID_RE = re.compile(r"[a-z0-9-]{8,64}")


class ExitError(ValueError):
    """An exit input is invalid. Nothing is built from it."""


@dataclass(frozen=True)
class Position:
    isin: str
    stock_code: str
    quantity: int
    cost: Decimal  # total cost basis, charges excluded (the VM ledger's measure)

    def __post_init__(self) -> None:
        if not isinstance(self.isin, str) or not self.isin:
            raise ExitError("isin must be a non-empty string")
        if not isinstance(self.stock_code, str) or not self.stock_code:
            raise ExitError("stock_code must be a non-empty string")
        if isinstance(self.quantity, bool) or not isinstance(self.quantity, int) or self.quantity <= 0:
            raise ExitError("quantity must be a positive integer")
        if not isinstance(self.cost, Decimal) or not self.cost.is_finite() or self.cost < 0:
            raise ExitError("cost must be a non-negative Decimal")


@dataclass(frozen=True)
class ExitIntent:
    batch_id: str
    reason: str  # halve | flatten | stop
    isin: str
    stock_code: str
    quantity: int
    decided_on: date  # the session whose close raised it; it is placed next session
    side: str = "sell"


@dataclass(frozen=True)
class ExitBatch:
    batch_id: str
    reason: str
    decided_on: date
    intents: tuple[ExitIntent, ...]


def halve_quantity(quantity: int) -> int:
    """D-07: floor(quantity / 2), whole shares. 7 -> 3, 1 -> 0 (no halve sell)."""
    if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
        raise ExitError("quantity must be a non-negative integer")
    return quantity // 2


def make_batch_id(reason: str, decided_on: date, legs: Sequence[tuple[str, int]]) -> str:
    material = "|".join([reason, decided_on.isoformat(), *(f"{isin}:{qty}" for isin, qty in sorted(legs))])
    digest = hashlib.sha256(material.encode("ascii")).hexdigest()[:12]
    batch_id = f"{reason}-{decided_on.strftime('%Y%m%d')}-{digest}"
    if BATCH_ID_RE.fullmatch(batch_id) is None:  # pragma: no cover - the parts are constrained
        raise ExitError("batch_id does not match the contract pattern")
    return batch_id


def build_batch(
    reason: str, decided_on: date, legs: Sequence[tuple[str, str, int]]
) -> ExitBatch | None:
    """One SELL intent per position. ``legs`` is (isin, stock_code, quantity).

    A leg with quantity below 1 is not a sell and is dropped (a one-share position
    has no halve leg). Returns None when nothing is left. Two legs for one ISIN are
    refused: the contract is one intent per position.
    """
    if reason not in PRIORITY:
        raise ExitError(f"unknown exit reason {reason!r}")
    seen: set[str] = set()
    kept: list[tuple[str, str, int]] = []
    for isin, stock_code, quantity in legs:
        if isin in seen:
            raise ExitError("one intent per position")
        seen.add(isin)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 0:
            raise ExitError("quantity must be a non-negative integer")
        if quantity >= 1:
            kept.append((isin, stock_code, quantity))
    if not kept:
        return None
    kept.sort()
    batch_id = make_batch_id(reason, decided_on, [(isin, qty) for isin, _, qty in kept])
    intents = tuple(
        ExitIntent(batch_id, reason, isin, stock_code, qty, decided_on) for isin, stock_code, qty in kept
    )
    return ExitBatch(batch_id, reason, decided_on, intents)


def halve_batch(
    positions: Sequence[Position], decided_on: date, *, exclude: frozenset[str] = frozenset()
) -> ExitBatch | None:
    """D-07: floor(q / 2) of every position, pro rata, whole shares."""
    legs = [
        (p.isin, p.stock_code, halve_quantity(p.quantity)) for p in positions if p.isin not in exclude
    ]
    return build_batch(HALVE, decided_on, legs)


def flatten_batch(positions: Sequence[Position], decided_on: date) -> ExitBatch | None:
    return build_batch(FLATTEN, decided_on, [(p.isin, p.stock_code, p.quantity) for p in positions])


def stop_batch(positions: Sequence[Position], decided_on: date) -> ExitBatch | None:
    return build_batch(STOP, decided_on, [(p.isin, p.stock_code, p.quantity) for p in positions])
