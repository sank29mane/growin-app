"""Shared fill replay for the drawdown vector harnesses (Phase 63-02 r1).

A vector's ``fills`` list is not guaranteed to be in execution order. The VM orders fills
by ``executed_at`` and then ``seq`` (its ledger never trusts list or dict order), so the
harnesses do too when a row carries either. A row with neither keeps its listed order,
because the sort is stable. The shared vectors carry neither today.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any, Sequence


def reference_month_for(session: date) -> date:
    """First day of the calendar month before ``session``: the month a valid tick reference carries.

    Mac-only harness default. The shared VM vectors carry a ``tick_reference`` but no month,
    so the Mac harnesses date it as the previous month of the row's session. A row that
    does carry ``tick_reference_month`` (ISO date) overrides this.
    """
    return date(session.year - 1, 12, 1) if session.month == 1 else date(session.year, session.month - 1, 1)


class FillOrderError(ValueError):
    """A sell with nothing (or too little) held at that point in the order."""


def in_execution_order(fills: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fills sorted by (executed_at, seq); a missing key sorts as "" and 0, list order breaks ties."""
    return sorted(fills, key=lambda f: (f.get("executed_at", ""), f.get("seq", 0)))


def replay_fills(
    fills: Sequence[dict[str, Any]], cash: Decimal
) -> tuple[Decimal, dict[str, tuple[int, Decimal]]]:
    """Cash and ``{isin: (quantity, cost)}`` after the fills, applied in execution order.

    A sell that would take a position below zero raises ``FillOrderError`` instead of
    dividing by a zero holding.
    """
    held: dict[str, list] = {}  # isin -> [quantity, cost]
    for fill in in_execution_order(fills):
        quantity, price, charges = fill["quantity"], Decimal(fill["price"]), Decimal(fill["charges"])
        entry = held.setdefault(fill["isin"], [0, Decimal(0)])
        if fill["side"] == "buy":
            entry[0] += quantity
            entry[1] += quantity * price
            cash -= quantity * price + charges
        else:
            if quantity > entry[0]:
                raise FillOrderError(
                    f"{fill.get('trade_id', '?')}: sell {quantity} of {fill['isin']} with {entry[0]} held"
                )
            entry[1] = entry[1] * (entry[0] - quantity) / entry[0]
            entry[0] -= quantity
            cash += quantity * price - charges
    return cash, {isin: (q, c) for isin, (q, c) in held.items()}
