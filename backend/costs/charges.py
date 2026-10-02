"""Contract-note-day pricing from a dated charge schedule.

``price_trade_day`` prices every fill that settled on one trade date and one
exchange. Charges depend only on traded value and the schedule: decision drift
and execution slippage never enter a line or a total (review decision D11).
"""

from __future__ import annotations

import decimal
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .core import (
    COST_CONTEXT,
    LINE_ORDER,
    CostModelError,
    ScheduleNotEffective,
    Side,
    TradeFill,
    round_money,
    seal,
)
from .schedule import ChargeSchedule, PricingBasis


@dataclass(frozen=True)
class OrderBrokerage:
    order_id: str
    isin: str
    side: Side
    traded_value: Decimal
    classification: str
    amount: Decimal


@dataclass(frozen=True)
class ChargeLine:
    name: str
    amount: Decimal


@dataclass(frozen=True)
class TurnoverBuckets:
    delivery_buy: Decimal
    delivery_sell: Decimal
    intraday_buy: Decimal
    intraday_sell: Decimal


@dataclass(frozen=True)
class ContractNoteEstimate:
    workspace: str
    currency: str
    trade_date: date
    exchange: str
    schedule_version: str
    schedule_hash: str
    pricing_basis: PricingBasis
    order_brokerage: tuple[OrderBrokerage, ...]
    lines: tuple[ChargeLine, ...]
    buckets: TurnoverBuckets
    dp_debits: int
    provisional_flags: tuple[str, ...]
    total: Decimal
    estimate_hash: str

    def line(self, name: str) -> Decimal:
        for item in self.lines:
            if item.name == name:
                return item.amount
        raise KeyError(name)


def price_trade_day(
    fills: Sequence[TradeFill],
    schedule: ChargeSchedule,
    *,
    workspace: str,
    currency: str,
    pricing_basis: PricingBasis,
) -> ContractNoteEstimate:
    with decimal.localcontext(COST_CONTEXT):
        return _price_trade_day(fills, schedule, workspace, currency, pricing_basis)


def _price_trade_day(
    fills: Sequence[TradeFill],
    schedule: ChargeSchedule,
    workspace: str,
    currency: str,
    pricing_basis: PricingBasis,
) -> ContractNoteEstimate:
    if workspace != schedule.workspace:
        raise CostModelError(f"workspace {workspace!r} does not match schedule workspace {schedule.workspace!r}")
    if currency != schedule.currency:
        raise CostModelError(f"currency {currency!r} does not match schedule currency {schedule.currency!r}")
    if not fills:
        raise CostModelError("price_trade_day needs at least one fill")
    trade_dates = {fill.trade_date for fill in fills}
    exchanges = {fill.exchange for fill in fills}
    if len(trade_dates) != 1:
        raise CostModelError("fills span more than one trade date")
    if len(exchanges) != 1:
        raise CostModelError("fills span more than one exchange")
    trade_date = next(iter(trade_dates))
    exchange = next(iter(exchanges))
    if exchange != schedule.exchange:
        raise CostModelError(f"exchange {exchange!r} does not match schedule exchange {schedule.exchange!r}")
    if pricing_basis.mode == "pinned":
        if pricing_basis.pinned_version != schedule.version:
            raise CostModelError("schedule version does not match the pinned pricing basis")
    elif not schedule.covers(trade_date):
        raise ScheduleNotEffective(
            f"schedule {schedule.version} is not effective on {trade_date.isoformat()}"
        )

    sides_by_isin: dict[str, set[Side]] = {}
    for fill in fills:
        sides_by_isin.setdefault(fill.isin, set()).add(fill.side)
    if any(len(sides) > 1 for sides in sides_by_isin.values()):
        raise CostModelError(
            "an ISIN has both buys and sells on one day; the D-09 same-day classification lands in Plan 02 Task 2"
        )

    per_order: dict[str, list[TradeFill]] = {}
    for fill in fills:
        per_order.setdefault(fill.order_id, []).append(fill)
    brokerage_rows: list[OrderBrokerage] = []
    buy_value = Decimal(0)
    sell_value = Decimal(0)
    dp_debits = 0
    for order_id in sorted(per_order):
        rows = per_order[order_id]
        if len({row.side for row in rows}) != 1 or len({row.isin for row in rows}) != 1:
            raise CostModelError(f"order {order_id!r} mixes sides or ISINs")
        value = sum((row.quantity * row.price for row in rows), Decimal(0))
        side = rows[0].side
        raw = max(schedule.brokerage.delivery_rate * value, schedule.brokerage.delivery_min_per_order)
        brokerage_rows.append(
            OrderBrokerage(order_id, rows[0].isin, side, value, "delivery", round_money(raw, schedule.rounding.default_quantum))
        )
        if side is Side.BUY:
            buy_value += value
        else:
            sell_value += value
            dp_debits += 1
    turnover = buy_value + sell_value
    stat = schedule.statutory
    quantum = schedule.rounding.default_quantum

    amounts = {
        "brokerage": sum((row.amount for row in brokerage_rows), Decimal(0)),
        "exchange_transaction": round_money(stat.exchange_transaction_rate * turnover, quantum),
        "sebi_fee": round_money(stat.sebi_fee_rate * turnover, quantum),
        "ipft": round_money(stat.ipft_rate * turnover, quantum),
        "stt": round_money(stat.stt_delivery_rate * (buy_value + sell_value), quantum),
        "stamp_duty": round_money(stat.stamp_delivery_buy_rate * buy_value, quantum),
    }
    gst_base = sum((amounts[name] for name in schedule.gst.applies_to), Decimal(0))
    amounts["gst"] = round_money(schedule.gst.rate * gst_base, quantum)
    dp_charge = schedule.dp.charge_per_debit * dp_debits
    amounts["dp_charge"] = round_money(dp_charge, quantum)
    amounts["dp_gst"] = (
        round_money(schedule.gst.rate * amounts["dp_charge"], quantum) if schedule.dp.gst_applies else round_money(Decimal(0), quantum)
    )
    lines = tuple(ChargeLine(name, round_money(amounts[name], quantum)) for name in LINE_ORDER)
    total = sum((line.amount for line in lines), Decimal(0))

    estimate = ContractNoteEstimate(
        workspace=workspace,
        currency=currency,
        trade_date=trade_date,
        exchange=exchange,
        schedule_version=schedule.version,
        schedule_hash=schedule.schedule_hash,
        pricing_basis=pricing_basis,
        order_brokerage=tuple(brokerage_rows),
        lines=lines,
        buckets=TurnoverBuckets(buy_value, sell_value, Decimal(0), Decimal(0)),
        dp_debits=dp_debits,
        provisional_flags=(),
        total=total,
        estimate_hash="",
    )
    return seal(estimate, "estimate_hash")
