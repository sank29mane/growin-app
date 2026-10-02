"""Contract-note-day pricing from a dated charge schedule.

``price_trade_day`` prices every fill that settled on one trade date and one
exchange. Charges depend only on traded value and the schedule: decision drift
and execution slippage never enter a line or a total (review decision D11).

Same-day handling (D-09), applied per ISIN:

1. Only buys: delivery.
2. Only sells: delivery, with DP debits.
3. Buy quantity equals sell quantity (net zero): every order is intraday.
4. Buys exceed sells: buy orders keep delivery brokerage, sell orders carry
   none (``squared_off_no_brokerage``).
5. Sells exceed buys: sell orders keep delivery brokerage, buy orders carry
   none.

Brokerage classification (``classify_same_day_brokerage``) and the matched
quantity statutory split (``split_same_day_statutory``) are separate code
paths that never call each other (review decision D12).
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
    InputError,
    ScheduleNotEffective,
    Side,
    TradeFill,
    round_money,
    seal,
)
from .schedule import ChargeSchedule, PricingBasis, ScheduleSet

SAME_DAY_PARTIAL_STATUTORY_STATUS = (
    "provisional, unvalidated: matched-quantity intraday statutory split for a partial same-day "
    "square-off; validate against a real broker contract note"
)
PROVISIONAL_SAME_DAY_PARTIAL_STATUTORY = "same_day_partial_statutory_split"

CLASS_DELIVERY = "delivery"
CLASS_INTRADAY = "intraday"
CLASS_SQUARED_OFF = "squared_off_no_brokerage"


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
    provisional_notes: tuple[str, ...]
    total: Decimal
    estimate_hash: str

    def line(self, name: str) -> Decimal:
        for item in self.lines:
            if item.name == name:
                return item.amount
        raise KeyError(name)


@dataclass(frozen=True)
class _Order:
    order_id: str
    isin: str
    side: Side
    quantity: int
    value: Decimal


def _quantum(schedule: ChargeSchedule, name: str) -> Decimal:
    return schedule.rounding.line_quantum.get(name, schedule.rounding.default_quantum)


def _orders(fills_for_isin: Sequence[TradeFill]) -> list[_Order]:
    """Aggregate one ISIN's fills into orders sorted by order id."""
    if not fills_for_isin:
        raise CostModelError("an ISIN group needs at least one fill")
    if len({fill.isin for fill in fills_for_isin}) != 1:
        raise CostModelError("an ISIN group holds more than one ISIN")
    per_order: dict[str, list[TradeFill]] = {}
    for fill in fills_for_isin:
        per_order.setdefault(fill.order_id, []).append(fill)
    orders = []
    for order_id in sorted(per_order):
        rows = per_order[order_id]
        if len({row.side for row in rows}) != 1:
            raise CostModelError(f"order {order_id!r} mixes sides")
        orders.append(
            _Order(
                order_id,
                rows[0].isin,
                rows[0].side,
                sum(row.quantity for row in rows),
                sum((row.quantity * row.price for row in rows), Decimal(0)),
            )
        )
    return orders


def _quantities(orders: Sequence[_Order]) -> tuple[int, int]:
    bought = sum(order.quantity for order in orders if order.side is Side.BUY)
    sold = sum(order.quantity for order in orders if order.side is Side.SELL)
    return bought, sold


def _classification(order: _Order, bought: int, sold: int) -> str:
    if bought == 0 or sold == 0:
        return CLASS_DELIVERY
    if bought == sold:
        return CLASS_INTRADAY
    if bought > sold:
        return CLASS_DELIVERY if order.side is Side.BUY else CLASS_SQUARED_OFF
    return CLASS_DELIVERY if order.side is Side.SELL else CLASS_SQUARED_OFF


def _exact_brokerage(classification: str, value: Decimal, schedule: ChargeSchedule) -> Decimal:
    rates = schedule.brokerage
    if classification == CLASS_DELIVERY:
        return max(rates.delivery_rate * value, rates.delivery_min_per_order)
    if classification == CLASS_INTRADAY:
        return min(rates.intraday_cap_per_order, rates.intraday_rate * value)
    return Decimal(0)


def classify_same_day_brokerage(
    fills_for_isin: Sequence[TradeFill], schedule: ChargeSchedule
) -> tuple[OrderBrokerage, ...]:
    """Per-order brokerage classification and amount for one ISIN (D-09 rules 1-5)."""
    with decimal.localcontext(COST_CONTEXT):
        orders = _orders(fills_for_isin)
        bought, sold = _quantities(orders)
        rows = []
        for order in orders:
            kind = _classification(order, bought, sold)
            amount = round_money(_exact_brokerage(kind, order.value, schedule), _quantum(schedule, "brokerage"))
            rows.append(OrderBrokerage(order.order_id, order.isin, order.side, order.value, kind, amount))
        return tuple(rows)


def split_same_day_statutory(fills_for_isin: Sequence[TradeFill]) -> TurnoverBuckets:
    """Exact, unrounded turnover buckets for one ISIN.

    Review decision D12 status: provisional, unvalidated: matched-quantity
    intraday statutory split for a partial same-day square-off; validate
    against a real broker contract note (SAME_DAY_PARTIAL_STATUTORY_STATUS).
    The matched quantity is priced at the carried side's average price.
    """
    with decimal.localcontext(COST_CONTEXT):
        orders = _orders(fills_for_isin)
        bought, sold = _quantities(orders)
        buy_value = sum((o.value for o in orders if o.side is Side.BUY), Decimal(0))
        sell_value = sum((o.value for o in orders if o.side is Side.SELL), Decimal(0))
        zero = Decimal(0)
        if sold == 0:
            return TurnoverBuckets(buy_value, zero, zero, zero)
        if bought == 0:
            return TurnoverBuckets(zero, sell_value, zero, zero)
        if bought == sold:
            return TurnoverBuckets(zero, zero, buy_value, sell_value)
        if bought > sold:
            matched_buy = (sold * buy_value) / bought
            return TurnoverBuckets(buy_value - matched_buy, zero, matched_buy, sell_value)
        matched_sell = (bought * sell_value) / sold
        return TurnoverBuckets(zero, sell_value - matched_sell, buy_value, matched_sell)


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

    by_isin: dict[str, list[TradeFill]] = {}
    for fill in fills:
        by_isin.setdefault(fill.isin, []).append(fill)

    brokerage_rows: list[OrderBrokerage] = []
    exact_brokerage = Decimal(0)
    delivery_buy = delivery_sell = intraday_buy = intraday_sell = Decimal(0)
    dp_debits = 0
    partial = False
    for isin in sorted(by_isin):
        group = by_isin[isin]
        rows = classify_same_day_brokerage(group, schedule)
        buckets = split_same_day_statutory(group)
        brokerage_rows.extend(rows)
        for row in rows:
            exact_brokerage += _exact_brokerage(row.classification, row.traded_value, schedule)
            if row.classification == CLASS_SQUARED_OFF:
                partial = True
        delivery_buy += buckets.delivery_buy
        delivery_sell += buckets.delivery_sell
        intraday_buy += buckets.intraday_buy
        intraday_sell += buckets.intraday_sell
        sell_debits = [r for r in rows if r.side is Side.SELL and r.classification == CLASS_DELIVERY]
        if sell_debits:
            dp_debits += len(sell_debits) if schedule.dp.basis == "per_sell_order" else 1

    turnover = delivery_buy + delivery_sell + intraday_buy + intraday_sell
    stat = schedule.statutory
    exact = {
        "brokerage": exact_brokerage,
        "exchange_transaction": stat.exchange_transaction_rate * turnover,
        "sebi_fee": stat.sebi_fee_rate * turnover,
        "ipft": stat.ipft_rate * turnover,
        "stt": stat.stt_delivery_rate * (delivery_buy + delivery_sell) + stat.stt_intraday_sell_rate * intraday_sell,
        "stamp_duty": stat.stamp_delivery_buy_rate * delivery_buy + stat.stamp_intraday_buy_rate * intraday_buy,
    }
    amounts = {
        "brokerage": round_money(sum((row.amount for row in brokerage_rows), Decimal(0)), _quantum(schedule, "brokerage")),
    }
    for name in ("exchange_transaction", "sebi_fee", "ipft", "stt", "stamp_duty"):
        amounts[name] = round_money(exact[name], _quantum(schedule, name))
    source = amounts if schedule.gst.base == "rounded_lines" else exact
    gst_base = sum((source[name] for name in schedule.gst.applies_to), Decimal(0))
    amounts["gst"] = round_money(schedule.gst.rate * gst_base, _quantum(schedule, "gst"))
    amounts["dp_charge"] = round_money(schedule.dp.charge_per_debit * dp_debits, _quantum(schedule, "dp_charge"))
    if schedule.dp.gst_applies:
        amounts["dp_gst"] = round_money(schedule.gst.rate * amounts["dp_charge"], _quantum(schedule, "dp_gst"))
    else:
        amounts["dp_gst"] = round_money(Decimal(0), _quantum(schedule, "dp_gst"))
    lines = tuple(ChargeLine(name, amounts[name]) for name in LINE_ORDER)
    total = sum((line.amount for line in lines), Decimal(0))

    display = schedule.rounding.default_quantum
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
        buckets=TurnoverBuckets(
            round_money(delivery_buy, display),
            round_money(delivery_sell, display),
            round_money(intraday_buy, display),
            round_money(intraday_sell, display),
        ),
        dp_debits=dp_debits,
        provisional_flags=(PROVISIONAL_SAME_DAY_PARTIAL_STATUTORY,) if partial else (),
        provisional_notes=(SAME_DAY_PARTIAL_STATUTORY_STATUS,) if partial else (),
        total=total,
        estimate_hash="",
    )
    return seal(estimate, "estimate_hash")


@dataclass(frozen=True)
class RoundTripEstimate:
    workspace: str
    currency: str
    isin: str
    quantity: int
    buy_day: ContractNoteEstimate
    sell_day: ContractNoteEstimate
    total: Decimal
    cost_bps_of_buy_value: Decimal
    estimate_hash: str


def estimate_delivery_round_trip(
    *,
    workspace: str,
    currency: str,
    isin: str,
    exchange: str,
    quantity: int,
    buy_price: Decimal,
    sell_price: Decimal,
    buy_date: date,
    sell_date: date,
    schedules: ScheduleSet,
    pricing_basis: PricingBasis,
) -> RoundTripEstimate:
    """Full-cost delivery round trip: the STRAT-03 hurdle.

    Takes no prepaid credit, no decision price, no drift and no slippage.
    Backtests and the hurdle always use full 0.07% plus GST, and returns come
    from fill prices that already contain the overnight move (D11).
    """
    with decimal.localcontext(COST_CONTEXT):
        if sell_date <= buy_date:
            raise InputError("sell_date must be after buy_date; a same-day round trip is an error path")
        buy = TradeFill("rt-buy", isin, exchange, Side.BUY, quantity, buy_price, buy_date)
        sell = TradeFill("rt-sell", isin, exchange, Side.SELL, quantity, sell_price, sell_date)
        buy_day = price_trade_day(
            [buy], schedules.resolve(buy_date, pricing_basis),
            workspace=workspace, currency=currency, pricing_basis=pricing_basis,
        )
        sell_day = price_trade_day(
            [sell], schedules.resolve(sell_date, pricing_basis),
            workspace=workspace, currency=currency, pricing_basis=pricing_basis,
        )
        total = buy_day.total + sell_day.total
        bps = round_money(total / (buy.quantity * buy.price) * 10000, Decimal("0.01"))
        estimate = RoundTripEstimate(
            workspace=workspace,
            currency=currency,
            isin=isin,
            quantity=quantity,
            buy_day=buy_day,
            sell_day=sell_day,
            total=total,
            cost_bps_of_buy_value=bps,
            estimate_hash="",
        )
        return seal(estimate, "estimate_hash")
