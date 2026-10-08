"""PR #557 fix 2: a first-seen terminal reconcile snapshot still applies its fill.

A practice order can partly fill and then be cancelled before the reconciler ever
looks at it. The first snapshot it sees is already CANCELLED with a positive
cumulative quantity. The reservation consumes that fill, so the position must
move with it, for BUY and for SELL, and for UNKNOWN snapshots too.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from execution import ReconciliationSnapshot
from execution.ledger import InvalidTransition
from regime_testkit import calm_probabilities
from t212_practice_testkit import (
    PRACTICE_ACCOUNT,
    place,
    sign_and_approve,
    start_practice_stack,
)
from t212_testkit import install_no_real_network
from test_practice_sell import held_position, position, prepare_sell
from venue_seam_testkit import practice_execution_payload


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    install_no_real_network(monkeypatch)


@pytest.fixture(autouse=True)
def regime_zero(monkeypatch):
    import market_data.regime as regime_module

    monkeypatch.setattr(
        regime_module, "fast_gmm_predict_proba", lambda feature, **params: calm_probabilities()
    )


def snapshot(pid, broker_order_id, status, quantity, notional, fingerprint):
    return ReconciliationSnapshot(
        proposal_id=pid,
        broker_order_id=broker_order_id,
        source="test",
        cumulative_quantity=Decimal(quantity),
        cumulative_notional=Decimal(notional),
        status=status,
        evidence_fingerprint=fingerprint,
        observed_at=datetime.now(timezone.utc),
    )


def budget_row(stack):
    raw = sqlite3.connect(stack.ledger.path)
    raw.row_factory = sqlite3.Row
    try:
        return dict(raw.execute("SELECT * FROM paper_budgets").fetchone())
    finally:
        raw.close()


@pytest.mark.asyncio
async def test_a_buy_that_partly_filled_then_cancelled_updates_the_position_on_first_sight(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        ack = await place(stack, "b-1", quantity="4")
        stack.broker.fill(int(ack.broker_order_id), price=70.0, quantity=1)
        stack.broker.expire(int(ack.broker_order_id))
        result = await stack.adapter.reconciler.reconcile("b-1")
        assert result.state == "CANCELLED"

        assert position(stack) == (Decimal("1"), Decimal("0.70")), "the position matches the broker fill"
        reservation = stack.ledger.get_reservation("b-1")
        assert reservation.consumed == Decimal("0.70")
        assert reservation.state == "SETTLED"
        assert reservation.consumed + reservation.released == reservation.reserved
        budget = budget_row(stack)
        assert Decimal(budget["consumed"]) == Decimal("0.70")
        assert Decimal(budget["reserved"]) == 0

        # Idempotent: reconciling the same evidence again changes nothing.
        before = (position(stack), budget_row(stack))
        again = await stack.adapter.reconciler.reconcile("b-1")
        assert again.state == "CANCELLED"
        assert (position(stack), budget_row(stack)) == before
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_sell_that_partly_filled_then_cancelled_updates_the_position_on_first_sight(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await held_position(tmp_path, private_config_dir, monkeypatch, quantity=3, price=49.0)
    try:
        sell = await prepare_sell(stack, monkeypatch, quantity=3)
        pid = sell["proposal_id"]
        ack = await sign_and_approve(stack, pid)
        stack.broker.fill(int(ack.broker_order_id), price=72.0, quantity=1)
        stack.broker.expire(int(ack.broker_order_id))
        result = await stack.adapter.reconciler.reconcile(pid)
        assert result.state == "CANCELLED"

        assert position(stack) == (Decimal("2"), Decimal("0.98")), "1.47 x 2/3 and the broker holds 2"
        reservation = stack.ledger.get_reservation(pid)
        assert reservation.consumed == Decimal("1")
        assert reservation.released == Decimal("2")
        assert reservation.state == "SETTLED"

        before = position(stack)
        again = await stack.adapter.reconciler.reconcile(pid)
        assert again.state == "CANCELLED"
        assert position(stack) == before
        assert stack.ledger.get_reservation(pid).consumed == Decimal("1")
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_an_unknown_snapshot_carrying_a_buy_fill_updates_the_position_and_keeps_the_reservation(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        ack = await place(stack, "u-1", quantity="4")
        stack.ledger.reconcile(snapshot("u-1", ack.broker_order_id, "UNKNOWN", "1", "0.70", "fp-1"))
        assert position(stack) == (Decimal("1"), Decimal("0.70"))
        reservation = stack.ledger.get_reservation("u-1")
        assert reservation.consumed == Decimal("0.70") and reservation.state == "ACTIVE"
        # A later cumulative snapshot adds only the new delta.
        stack.ledger.reconcile(snapshot("u-1", ack.broker_order_id, "CANCELLED", "2", "1.40", "fp-2"))
        assert position(stack) == (Decimal("2"), Decimal("1.40"))
        assert stack.ledger.get_reservation("u-1").state == "SETTLED"
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_an_unknown_snapshot_carrying_a_sell_fill_reduces_the_position(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await held_position(tmp_path, private_config_dir, monkeypatch, quantity=3, price=49.0)
    try:
        sell = await prepare_sell(stack, monkeypatch, quantity=3)
        pid = sell["proposal_id"]
        ack = await sign_and_approve(stack, pid)
        stack.ledger.reconcile(snapshot(pid, ack.broker_order_id, "UNKNOWN", "1", "0.72", "fp-1"))
        assert position(stack) == (Decimal("2"), Decimal("0.98"))
        reservation = stack.ledger.get_reservation(pid)
        assert reservation.consumed == Decimal("1") and reservation.state == "ACTIVE"
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_quantity_advance_without_a_notional_advance_is_refused_and_changes_nothing(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await start_practice_stack(tmp_path, private_config_dir, monkeypatch)
    try:
        ack = await place(stack, "x-1", quantity="4")
        stack.ledger.reconcile(snapshot("x-1", ack.broker_order_id, "PARTIALLY_FILLED", "1", "0.70", "fp-1"))
        with pytest.raises(InvalidTransition):
            stack.ledger.reconcile(
                snapshot("x-1", ack.broker_order_id, "CANCELLED", "2", "0.70", "fp-2")
            )
        assert position(stack) == (Decimal("1"), Decimal("0.70"))
        assert stack.ledger.get_reservation("x-1").state == "ACTIVE"
    finally:
        stack.close()
