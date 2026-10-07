"""Shared helpers for the Phase 63-04 India Mac-limits tests.

Every value here is synthetic. Nothing contacts a broker, the relay, or a real ledger or
private/ directory: the private config lives in a tmp dir and the clock is injected, so the
tests do not depend on the day or hour they run.
"""

from __future__ import annotations

import json
import os
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from regime_testkit import bound_admit, gated
from execution import ExecutionLedger, ExecutionService, OrderIntent, PaperDispatcher
from execution.india_guard import IndiaAdmissionGuard, IndiaQuoteEvidence
from private_config import load_workspace_config
from risk_india import rules

# Thursday 2026-10-08, 10:00 IST: inside the 09:15 to 15:10 order window.
NOW = datetime(2026, 10, 8, 10, 0, tzinfo=rules.IST)
SESSION = date(2026, 10, 8)
SYMBOL = "RELIANCE"
TICKER = f"NSE:CASH:{SYMBOL}"
ISIN = "INE002A01018"

# Synthetic caps. The collar and slippage cap are the D-09 and operator values.
CAPITAL_CAP = "1000.00"
PER_POSITION_CAP = "600.00"


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, 0o600)


def india_private_dir(
    root: Path,
    *,
    capital_cap: str = CAPITAL_CAP,
    per_position_cap: str = PER_POSITION_CAP,
    collar: str = "0.02",
    max_slippage_bps: str = "25",
) -> Path:
    """A synthetic private/ with India limits.json and execution.json set as asked."""

    from conftest import build_private_config

    private = build_private_config(root)
    write_json(
        private / "india" / "limits.json",
        {
            "schema_version": 1,
            "workspace": "india",
            "currency": "INR",
            "capital_cap": capital_cap,
            "per_position_cap": per_position_cap,
            "drawdown_halt": "-0.08",
            "drawdown_flatten": "-0.15",
            "position_stop": "-0.12",
        },
    )
    write_json(
        private / "india" / "execution.json",
        {
            "schema_version": 1,
            "workspace": "india",
            "venue": "paper",
            "fat_finger_collar": collar,
            "max_slippage_bps": max_slippage_bps,
        },
    )
    return private


class MutableClock:
    """An injectable admission clock a test can move (the guard reads it on every call)."""

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def make_guard(ledger: ExecutionLedger, private: Path, *, now: Any = NOW) -> IndiaAdmissionGuard:
    config = load_workspace_config(private, "india", require_india_execution=True)
    clock = now if callable(now) else (lambda: now)
    return IndiaAdmissionGuard.from_config(config, ledger, clock=clock)


def make_service(
    ledger: ExecutionLedger, guard: IndiaAdmissionGuard | None, **kwargs: Any
) -> ExecutionService:
    return ExecutionService(PaperDispatcher(), ledger, india_guard=guard, **gated(**kwargs))


def make_quote(**overrides: Any) -> rules.Quote:
    values: dict[str, Any] = {
        "stock_code": SYMBOL,
        "isin": ISIN,
        "series": "EQ",
        "ltp": Decimal("100.00"),
        "lower_circuit": Decimal("90.00"),
        "upper_circuit": Decimal("110.00"),
        "previous_close": Decimal("99.80"),
        "session_date": SESSION,
        "tick_reference": Decimal("99.80"),
        "tick_reference_month": date(2026, 9, 30),
        "bid": Decimal("99.90"),
        "ask": Decimal("100.00"),
    }
    values.update(overrides)
    return rules.Quote(**values)


def make_evidence(*, observed_at: datetime = NOW, **overrides: Any) -> IndiaQuoteEvidence:
    return IndiaQuoteEvidence(make_quote(**overrides), observed_at)


def make_intent(
    proposal_id: str,
    *,
    side: str = "BUY",
    quantity: int | str = 1,
    limit_price: str | None = "100.00",
    ticker: str = TICKER,
    **overrides: Any,
) -> OrderIntent:
    values: dict[str, Any] = {
        "proposal_id": proposal_id,
        "workspace": "india",
        "account": "paper",
        "broker": "paper",
        "mode": "PAPER",
        "ticker": ticker,
        "side": side,
        "quantity": Decimal(str(quantity)),
    }
    if limit_price is not None:
        values["order_type"] = "LIMIT"
        values["limit_price"] = Decimal(limit_price)
    values.update(overrides)
    return OrderIntent(**values)


NO_QUOTE = object()  # pass as ``evidence`` to admit with no India quote at all


def admit(
    service: ExecutionService,
    intent: OrderIntent,
    *,
    evidence: Any = None,
    fill: str | None = None,
    price: str | None = None,
):
    """Admit with explicit simulator and risk evidence. ``fill`` defaults to 100.00."""

    limit = str(intent.limit_price) if intent.limit_price is not None else "100.00"
    return service.admit(
        intent,
        currency="INR",
        price=price or limit,
        # The simulator fill defaults to the default quote's reference (ask 100.00), so a
        # limit price away from it does not trip the slippage gate by accident.
        simulator_evidence={"simulated_fill_price": fill or "100.00"},
        risk_evidence={"scaled_size": str(intent.quantity)}, **bound_admit(),
        india_quote=None if evidence is NO_QUOTE else (evidence or make_evidence()),
    )


def prepare(service: ExecutionService, intent: OrderIntent, **kwargs: Any):
    """Admit then reserve (what ``service.prepare`` does) with the same explicit evidence."""

    admission = admit(service, intent, **kwargs)
    if admission.decision.value == "ADMITTED":
        service.reserve(admission.proposal_id)
    return admission


def open_ledger(tmp_path: Path, *, name: str = "india.sqlite3", budget: str = "100000") -> ExecutionLedger:
    ledger = ExecutionLedger(tmp_path / name, workspace="india", require_approval=True)
    ledger.configure_paper_budget("paper", "INR", budget, workspace="india")
    return ledger


def seed_position(
    ledger: ExecutionLedger,
    ticker: str,
    quantity: int,
    cost: str,
    *,
    guard: IndiaAdmissionGuard | None = None,
) -> None:
    """Put a held position in the ledger the way a reconciled fill would leave it.

    Pass the ``guard`` when the pilot has not started yet: a fill with no latch file is
    exactly the "deleted file" case the Mac fails closed on, so the file is created first,
    as it would have been when the pilot began.
    """

    if guard is not None:
        guard.store.load()
    with ledger._transaction() as connection:  # noqa: SLF001 - test seam for a reconciled fill
        connection.execute(
            "INSERT INTO paper_positions (workspace, account, currency, ticker, quantity, notional, updated_at) "
            "VALUES ('india', 'paper', 'INR', ?, ?, ?, '2026-10-08T00:00:00+00:00') "
            "ON CONFLICT(workspace, account, currency, ticker) DO UPDATE SET "
            "quantity = excluded.quantity, notional = excluded.notional",
            (ticker, str(quantity), cost),
        )
