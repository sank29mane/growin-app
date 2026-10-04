"""Prepaid-credit cash view and account-overhead disclosure (decision D-11).

The prepaid brokerage credit is a pilot cash-reporting view only. It is never
an input to the round-trip hurdle. The credit balance is a runtime input read
from private config by the caller; it must never be written into the repo, a
test fixture or a log.

"Return after incremental account overhead" never includes the one-time plan
fees or the shares-as-margin interest. The full AMC is always disclosed; it
enters the metric only when ``pilot_keeps_account_open`` is True (D13).
"""

from __future__ import annotations

import decimal
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from .charges import ContractNoteEstimate
from .core import (
    COST_CONTEXT,
    CostModelError,
    InputError,
    require_date,
    require_text,
    round_money,
    strict_decimal,
)
from .schedule import ChargeSchedule, PlanFee, ScheduleSet

METRIC_NAME = "return after incremental account overhead"
_DAYS_PER_YEAR = 365


@dataclass(frozen=True)
class PrepaidCredit:
    balance: Decimal
    expires_on: date
    source: str

    def __post_init__(self) -> None:
        balance = strict_decimal(self.balance, "credit.balance")
        if balance < 0:
            raise InputError("credit.balance must not be negative")
        object.__setattr__(self, "balance", balance)
        require_date(self.expires_on, "credit.expires_on")
        require_text(self.source, "credit.source")


@dataclass(frozen=True)
class CreditedDay:
    trade_date: date
    full_total: Decimal
    credit_applied: Decimal
    with_credit_total: Decimal


@dataclass(frozen=True)
class CreditedCashView:
    credit_source: str
    full_total: Decimal
    credit_applied: Decimal
    with_credit_total: Decimal
    credit_remaining: Decimal
    days: tuple[CreditedDay, ...]


def _check_estimate_matches(estimate: ContractNoteEstimate, schedule: ChargeSchedule) -> None:
    if estimate.schedule_version != schedule.version or estimate.schedule_hash != schedule.schedule_hash:
        raise CostModelError(
            f"estimate for {estimate.trade_date.isoformat()} does not match schedule {schedule.version}"
        )


def brokerage_gst_attributable(estimate: ContractNoteEstimate, schedule: ChargeSchedule) -> Decimal:
    """GST that rides on the day's brokerage line, rounded half-up to paise."""
    with decimal.localcontext(COST_CONTEXT):
        _check_estimate_matches(estimate, schedule)
        if "brokerage" not in schedule.gst.applies_to:
            return round_money(Decimal(0))
        return round_money(estimate.line("brokerage") * schedule.gst.rate)


def apply_prepaid_credit(
    days: Sequence[ContractNoteEstimate], credit: PrepaidCredit, *, schedules: ScheduleSet
) -> CreditedCashView:
    """Report cash charges with prepaid credit, rejecting duplicate date/exchange estimates."""
    with decimal.localcontext(COST_CONTEXT):
        seen: set[tuple[date, str]] = set()
        for estimate in days:
            key = (estimate.trade_date, estimate.exchange)
            if key in seen:
                raise InputError(f"duplicate estimate for {key[0].isoformat()} {key[1]}")
            seen.add(key)
        remaining = credit.balance
        rows: list[CreditedDay] = []
        for estimate in sorted(days, key=lambda d: (d.trade_date, d.exchange)):
            schedule = schedules.get(estimate.schedule_version)
            _check_estimate_matches(estimate, schedule)
            applied = Decimal(0)
            if estimate.trade_date <= credit.expires_on:
                eligible = estimate.line("brokerage") + brokerage_gst_attributable(estimate, schedule)
                applied = min(remaining, eligible)
            remaining -= applied
            rows.append(
                CreditedDay(
                    estimate.trade_date,
                    estimate.total,
                    round_money(applied),
                    round_money(estimate.total - applied),
                )
            )
        full_total = sum((row.full_total for row in rows), Decimal(0))
        applied_total = sum((row.credit_applied for row in rows), Decimal(0))
        return CreditedCashView(
            credit_source=credit.source,
            full_total=round_money(full_total),
            credit_applied=round_money(applied_total),
            with_credit_total=round_money(full_total - applied_total),
            credit_remaining=round_money(remaining),
            days=tuple(rows),
        )


@dataclass(frozen=True)
class OverheadDisclosure:
    metric_name: str
    schedule_version: str
    schedule_hash: str
    amc_annual_ex_gst: Decimal
    amc_gst_rate: Decimal
    amc_annual_incl_gst: Decimal
    pilot_keeps_account_open: bool
    period_start: date
    period_end: date
    incremental_allocation: Decimal
    plan_fees_excluded: tuple[PlanFee, ...]
    excluded_from_delivery_model: tuple[str, ...]


def incremental_account_overhead(
    *,
    schedule: ChargeSchedule,
    period_start: date,
    period_end: date,
    pilot_keeps_account_open: bool = False,
) -> OverheadDisclosure:
    """AMC disclosure for a period.

    ``pilot_keeps_account_open`` is the only defaulted flag in this package, by
    operator decision D13: the pilot carries zero AMC unless the operator says
    the pilot is the reason the account stays open.
    """
    with decimal.localcontext(COST_CONTEXT):
        require_date(period_start, "period_start")
        require_date(period_end, "period_end")
        if period_end < period_start:
            raise InputError("period_end must not precede period_start")
        if not isinstance(pilot_keeps_account_open, bool):
            raise InputError("pilot_keeps_account_open must be a bool")
        overhead = schedule.account_overhead
        annual = round_money(overhead.amc_annual_ex_gst * (1 + overhead.amc_gst_rate))
        if pilot_keeps_account_open:
            inclusive_days = (period_end - period_start).days + 1
            allocation = round_money(annual * inclusive_days / _DAYS_PER_YEAR)
        else:
            allocation = round_money(Decimal(0))
        return OverheadDisclosure(
            metric_name=METRIC_NAME,
            schedule_version=schedule.version,
            schedule_hash=schedule.schedule_hash,
            amc_annual_ex_gst=overhead.amc_annual_ex_gst,
            amc_gst_rate=overhead.amc_gst_rate,
            amc_annual_incl_gst=annual,
            pilot_keeps_account_open=pilot_keeps_account_open,
            period_start=period_start,
            period_end=period_end,
            incremental_allocation=allocation,
            plan_fees_excluded=overhead.plan_fees,
            excluded_from_delivery_model=schedule.excluded_from_delivery_model,
        )


def return_after_incremental_account_overhead(
    *, net_pnl_after_charges: Decimal, capital: Decimal, overhead: OverheadDisclosure
) -> Decimal:
    """(net P&L after charges minus incremental AMC allocation) over capital, to 6 places."""
    with decimal.localcontext(COST_CONTEXT):
        pnl = strict_decimal(net_pnl_after_charges, "net_pnl_after_charges")
        base = strict_decimal(capital, "capital")
        if base <= 0:
            raise InputError("capital must be greater than zero")
        return round_money((pnl - overhead.incremental_allocation) / base, Decimal("0.000001"))
