"""Fill-and-cost run with a lineage hash.

``simulate_and_price`` takes every input explicitly (no defaults), simulates
each order against its session bar, prices the resulting fills per contract
note day, and returns a ``SimulationRun`` whose ``run_hash`` covers the model
version, scope, scenario, pricing basis, inputs and outputs.
"""

from __future__ import annotations

import decimal
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timezone
from decimal import Decimal

from .charges import ContractNoteEstimate, price_trade_day
from .core import (
    COST_CONTEXT,
    MODEL_VERSION,
    CostModelError,
    InputError,
    TradeFill,
    seal,
)
from .fills import FillResult, FillScenario, LimitOrder, SessionBar, simulate_session
from .schedule import PricingBasis, ScheduleSet

UTC = timezone.utc


@dataclass(frozen=True)
class SimulationRun:
    model_version: str
    workspace: str
    currency: str
    scenario_id: str
    scenarios_version: str
    scenarios_hash: str
    fill_assumption_note: str
    pricing_basis: PricingBasis
    schedule_refs: tuple[tuple[date, str, str], ...]
    fills: tuple[FillResult, ...]
    days: tuple[ContractNoteEstimate, ...]
    total_charges: Decimal
    run_hash: str


def simulate_and_price(
    *,
    workspace: str,
    currency: str,
    orders: Sequence[LimitOrder],
    bars: Sequence[SessionBar],
    scenario: FillScenario,
    schedules: ScheduleSet,
    pricing_basis: PricingBasis,
) -> SimulationRun:
    with decimal.localcontext(COST_CONTEXT):
        return _simulate_and_price(workspace, currency, orders, bars, scenario, schedules, pricing_basis)


def _simulate_and_price(
    workspace: str,
    currency: str,
    orders: Sequence[LimitOrder],
    bars: Sequence[SessionBar],
    scenario: FillScenario,
    schedules: ScheduleSet,
    pricing_basis: PricingBasis,
) -> SimulationRun:
    for version in schedules.versions:
        if workspace != version.workspace:
            raise CostModelError(f"workspace {workspace!r} does not match schedule workspace {version.workspace!r}")
        if currency != version.currency:
            raise CostModelError(f"currency {currency!r} does not match schedule currency {version.currency!r}")
    if not orders:
        raise InputError("simulate_and_price needs at least one order")

    bar_index: dict[tuple[str, str, date], SessionBar] = {}
    for bar in bars:
        key = (bar.isin, bar.exchange, bar.session_date)
        if key in bar_index:
            raise InputError(f"duplicate bar for {key[0]} {key[1]} {key[2].isoformat()}")
        bar_index[key] = bar

    order_ids = [order.order_id for order in orders]
    if len(set(order_ids)) != len(order_ids):
        raise InputError("duplicate order_id in run")

    groups: dict[tuple[str, str, date], list[LimitOrder]] = {}
    for order in orders:
        key = (order.isin, order.exchange, order.session_date)
        if key not in bar_index:
            raise InputError(f"order {order.order_id!r} has no bar for {key[0]} {key[1]} {key[2].isoformat()}")
        groups.setdefault(key, []).append(order)

    results: list[FillResult] = []
    for key in sorted(groups):
        results.extend(simulate_session(groups[key], bar_index[key], scenario))
    submitted = {order.order_id: order.submitted_at.astimezone(UTC) for order in orders}
    results.sort(key=lambda r: (r.session_date, r.isin, submitted[r.order_id], r.order_id))

    by_day: dict[tuple[date, str], list[TradeFill]] = {}
    for result in results:
        trade_fill = result.to_trade_fill()
        if trade_fill is not None:
            by_day.setdefault((trade_fill.trade_date, trade_fill.exchange), []).append(trade_fill)
    days: list[ContractNoteEstimate] = []
    for trade_date, exchange in sorted(by_day):
        schedule = schedules.resolve(trade_date, pricing_basis)
        days.append(
            price_trade_day(
                by_day[(trade_date, exchange)],
                schedule,
                workspace=workspace,
                currency=currency,
                pricing_basis=pricing_basis,
            )
        )
    total = sum((day.total for day in days), Decimal("0.00"))

    sorted_orders = sorted(
        orders, key=lambda o: (o.session_date, o.isin, o.submitted_at.astimezone(UTC), o.order_id)
    )
    used_bars = [bar_index[key] for key in sorted(groups)]
    run = SimulationRun(
        model_version=MODEL_VERSION,
        workspace=workspace,
        currency=currency,
        scenario_id=scenario.scenario_id,
        scenarios_version=scenario.scenarios_version,
        scenarios_hash=scenario.scenarios_hash,
        fill_assumption_note=scenario.assumption_note,
        pricing_basis=pricing_basis,
        schedule_refs=tuple((day.trade_date, day.schedule_version, day.schedule_hash) for day in days),
        fills=tuple(results),
        days=tuple(days),
        total_charges=total,
        run_hash="",
    )
    return seal(
        run,
        "run_hash",
        extra={"scenario": scenario, "orders": tuple(sorted_orders), "bars": tuple(used_bars)},
    )

