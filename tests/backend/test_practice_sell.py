"""Phase 66-04 Task 2: the practice SELL path (D-19), and where SELL stays denied.

A SELL reserves held quantity, is cross-checked against the broker's own
``quantityAvailableForTrading``, consumes on fill and releases on cancel or
failure. Paper ledgers (UK paper, India) keep denying every SELL.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import numpy as np
import pytest

from execution import (
    ApprovalConflict,
    ExecutionLedger,
    ExecutionService,
    VenueBinding,
)
from execution.service import BrokerExecutionError, BrokerOutcomeUnknownError
from t212_practice_testkit import (
    PRACTICE_ACCOUNT,
    FakeDemoBroker,
    place,
    route_client,
    sign_and_approve,
    start_practice_stack,
)
from t212_testkit import FakeClock, install_no_real_network
from venue_seam_testkit import SYNTH_LIMITS, practice_execution_payload

PREPARE = "/api/t212-practice/preparations"
TICKER = "VODl_EQ"


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    install_no_real_network(monkeypatch)


@pytest.fixture(autouse=True)
def regime_zero(monkeypatch):
    import market_data.regime as regime_module

    monkeypatch.setattr(
        regime_module, "fast_gmm_predict_proba", lambda feature, **params: np.array([1.0, 0.0, 0.0, 0.0])
    )


def readings(bid="71.2", ask="71.3"):
    now = datetime.now(timezone.utc)
    return [
        {"bid": bid, "ask": ask, "observed_at": (now - timedelta(seconds=4 - 2 * i)).isoformat()}
        for i in range(3)
    ]


async def prepare_sell(stack, monkeypatch, *, quantity=3, limit="71.2", ticker=TICKER, bid="71.2"):
    async with route_client(stack, monkeypatch) as client:
        response = await client.post(
            PREPARE,
            json={
                "confirmation": "PREPARE_T212_PRACTICE", "ticker": ticker, "side": "SELL",
                "quantity": quantity, "limit_price": limit, "readings": readings(bid=bid),
            },
        )
    assert response.status_code == 201, response.text
    return response.json()


async def held_position(tmp_path, private_dir, monkeypatch, *, quantity=3, price=49.0, **kwargs):
    """A practice ledger that really holds ``quantity`` shares: BUY, fill, reconcile."""

    kwargs.setdefault("execution", practice_execution_payload(account_id=PRACTICE_ACCOUNT, max_slippage_bps=25))
    stack = await start_practice_stack(tmp_path, private_dir, monkeypatch, **kwargs)
    assert stack.started, stack.app.execution_startup_error
    await place(stack, "seed-buy", quantity=str(quantity))
    stack.broker.fill(7000001, price=price)
    assert (await stack.adapter.reconciler.reconcile("seed-buy")).state == "FILLED"
    return stack


def position(stack, ticker=TICKER):
    row = stack.ledger.get_paper_position(PRACTICE_ACCOUNT, "GBP", ticker, workspace="uk")
    return None if row is None else (Decimal(row["quantity"]), Decimal(row["notional"]))


def quantity_rows(stack):
    raw = sqlite3.connect(stack.ledger.path)
    try:
        return raw.execute("SELECT COUNT(*) FROM ledger_quantity_reservations").fetchone()[0]
    finally:
        raw.close()


# --- denied -----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_sell_with_no_reconciled_position_is_denied(tmp_path, private_config_dir, monkeypatch):
    stack = await start_practice_stack(
        tmp_path, private_config_dir, monkeypatch,
        execution=practice_execution_payload(account_id=PRACTICE_ACCOUNT, max_slippage_bps=25),
    )
    try:
        result = await prepare_sell(stack, monkeypatch, quantity=1)
        assert result["admitted"] is False
        assert result["admission"]["reason_code"] == "POSITION_UNAVAILABLE"
        assert quantity_rows(stack) == 0
        assert stack.broker.mutations == []
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_sell_above_held_minus_open_sell_reservations_is_denied(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await held_position(tmp_path, private_config_dir, monkeypatch, quantity=3)
    try:
        too_many = await prepare_sell(stack, monkeypatch, quantity=4)
        assert too_many["admission"]["reason_code"] == "POSITION_UNAVAILABLE"
        first = await prepare_sell(stack, monkeypatch, quantity=2)
        assert first["admitted"] is True, first
        second = await prepare_sell(stack, monkeypatch, quantity=2)  # only 1 left unreserved
        assert second["admission"]["reason_code"] == "POSITION_UNAVAILABLE"
        third = await prepare_sell(stack, monkeypatch, quantity=1)
        assert third["admitted"] is True, third
        assert quantity_rows(stack) == 2
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_broker_available_quantity_below_the_sell_denies_and_a_failed_read_denies(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await held_position(tmp_path, private_config_dir, monkeypatch, quantity=3)
    try:
        stack.broker.available[TICKER] = Decimal("1")  # e.g. 2 are tied up elsewhere at the broker
        short = await prepare_sell(stack, monkeypatch, quantity=2)
        assert short["admission"]["reason_code"] == "BROKER_QUANTITY_INSUFFICIENT"
        fits = await prepare_sell(stack, monkeypatch, quantity=1)
        assert fits["admitted"] is True, fits

        stack.broker.available.pop(TICKER)
        stack.broker.get_override = lambda request: (
            httpx.Response(500, text="boom") if request.url.path.endswith("/positions") else None
        )
        failed = await prepare_sell(stack, monkeypatch, quantity=1)
        assert failed["admission"]["reason_code"] == "BROKER_POSITION_UNAVAILABLE"
        stack.broker.get_override = lambda request: (
            httpx.Response(200, text="not json") if request.url.path.endswith("/positions") else None
        )
        garbage = await prepare_sell(stack, monkeypatch, quantity=1)
        assert garbage["admission"]["reason_code"] == "BROKER_POSITION_UNAVAILABLE"
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_sell_is_measured_against_the_recorded_bid_through_the_route(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await held_position(tmp_path, private_config_dir, monkeypatch, quantity=3)
    try:
        below = await prepare_sell(stack, monkeypatch, quantity=1, limit="71.0")  # 28 bp under the bid
        assert below["admission"]["reason_code"] == "SLIPPAGE_LIMIT"
        at_bid = await prepare_sell(stack, monkeypatch, quantity=1, limit="71.2")
        assert at_bid["admitted"] is True
        far = await prepare_sell(stack, monkeypatch, quantity=1, limit="90.0")  # far above the bid
        assert far["admitted"] is True, far
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_the_sell_request_cannot_choose_the_account_or_skip_the_checks(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await held_position(tmp_path, private_config_dir, monkeypatch, quantity=3)
    try:
        async with route_client(stack, monkeypatch) as client:
            response = await client.post(
                PREPARE,
                json={
                    "confirmation": "PREPARE_T212_PRACTICE", "ticker": TICKER, "side": "SELL",
                    "quantity": 1, "limit_price": "71.2", "readings": readings(),
                    "account": "other", "broker_available_quantity": 99,
                },
            )
        assert response.status_code == 422
    finally:
        stack.close()


# --- fill, cancel, failure, unknown ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_sell_fill_reduces_the_position_consumes_the_reservation_and_returns_headroom(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await held_position(tmp_path, private_config_dir, monkeypatch, quantity=3, price=49.0)
    try:
        assert position(stack) == (Decimal("3"), Decimal("1.47"))
        headroom_before = stack.ledger.practice_headroom(PRACTICE_ACCOUNT, "GBP", TICKER)
        sell = await prepare_sell(stack, monkeypatch, quantity=3, limit="71.2")
        pid = sell["proposal_id"]
        assert stack.ledger.get_reservation(pid).reserved == Decimal("3")
        assert stack.ledger.get_reservation(pid).state == "ACTIVE"
        ack = await sign_and_approve(stack, pid)

        post = stack.broker.posts[-1]
        assert json.loads(post.content)["quantity"] == -3
        stack.broker.fill(int(ack.broker_order_id), price=72.0)  # price improvement on a sell
        result = await stack.adapter.reconciler.reconcile(pid)
        assert result.state == "FILLED"

        reservation = stack.ledger.get_reservation(pid)
        assert reservation.consumed == Decimal("3") and reservation.state == "SETTLED"
        assert position(stack) == (Decimal("0"), Decimal("0"))
        headroom_after = stack.ledger.practice_headroom(PRACTICE_ACCOUNT, "GBP", TICKER)
        assert headroom_after["held_notional"] == headroom_before["held_notional"] - Decimal("1.47")
        assert stack.ledger.get_order(pid).intent["quantity"] == "3", "the ledger keeps the positive quantity"
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_partial_sell_fill_returns_the_cost_basis_in_proportion(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await held_position(tmp_path, private_config_dir, monkeypatch, quantity=3, price=49.0)
    try:
        sell = await prepare_sell(stack, monkeypatch, quantity=3)
        ack = await sign_and_approve(stack, sell["proposal_id"])
        stack.broker.fill(int(ack.broker_order_id), price=72.0, quantity=1)
        result = await stack.adapter.reconciler.reconcile(sell["proposal_id"])
        assert result.state == "PARTIALLY_FILLED"
        assert position(stack) == (Decimal("2"), Decimal("0.98"))  # 1.47 x 2/3
        reservation = stack.ledger.get_reservation(sell["proposal_id"])
        assert reservation.consumed == Decimal("1") and reservation.outstanding == Decimal("2")
        assert reservation.state == "ACTIVE"
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_cancelling_a_sell_releases_the_quantity_and_leaves_the_position(
    tmp_path, private_config_dir, monkeypatch
):
    stack = await held_position(tmp_path, private_config_dir, monkeypatch, quantity=3)
    try:
        sell = await prepare_sell(stack, monkeypatch, quantity=3)
        pid = sell["proposal_id"]
        ack = await sign_and_approve(stack, pid)
        assert (await stack.service.cancel_order(pid)).requested
        assert (await stack.adapter.reconciler.reconcile(pid)).state == "CANCELLED"
        reservation = stack.ledger.get_reservation(pid)
        assert reservation.outstanding == 0 and reservation.state == "SETTLED" and reservation.consumed == 0
        assert position(stack)[0] == Decimal("3")
        # The released quantity can be sold again.
        again = await prepare_sell(stack, monkeypatch, quantity=3)
        assert again["admitted"] is True, again
        assert ack.broker_order_id
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_failed_sell_dispatch_releases_the_quantity_and_an_unknown_one_keeps_it(
    tmp_path, private_config_dir, monkeypatch
):
    clock = FakeClock(start=__import__("time").time())
    broker = FakeDemoBroker(clock)
    mode = {"post": None}

    def handle(request):
        if request.method == "POST" and mode["post"] is not None:
            broker.requests.append(request)
            return httpx.Response(mode["post"], text="x")
        return broker(request)

    stack = await held_position(
        tmp_path, private_config_dir, monkeypatch, quantity=3, broker=broker, clock=clock, handler=handle
    )
    try:
        mode["post"] = 400
        failed = await prepare_sell(stack, monkeypatch, quantity=3)
        with pytest.raises(BrokerExecutionError):
            await sign_and_approve(stack, failed["proposal_id"])
        assert stack.ledger.get_order(failed["proposal_id"]).state == "FAILED"
        released = stack.ledger.get_reservation(failed["proposal_id"])
        assert released.state == "SETTLED" and released.outstanding == 0

        mode["post"] = 408
        unknown = await prepare_sell(stack, monkeypatch, quantity=3)
        with pytest.raises(BrokerOutcomeUnknownError):
            await sign_and_approve(stack, unknown["proposal_id"])
        kept = stack.ledger.get_reservation(unknown["proposal_id"])
        assert stack.ledger.get_order(unknown["proposal_id"]).state == "UNKNOWN"
        assert kept.state == "ACTIVE" and kept.outstanding == Decimal("3"), "UNKNOWN keeps the reservation"
        # While it is held, the same shares cannot be reserved for another SELL.
        blocked = await prepare_sell(stack, monkeypatch, quantity=1)
        assert blocked["admission"]["reason_code"] == "POSITION_UNAVAILABLE"
    finally:
        stack.close()


@pytest.mark.asyncio
async def test_a_sell_reservation_is_checked_in_the_ledger_even_without_the_admission_pre_check(
    tmp_path, private_config_dir, monkeypatch
):
    """The reservation transaction is the authority: admission pre-checks are a convenience."""

    stack = await held_position(tmp_path, private_config_dir, monkeypatch, quantity=2)
    try:
        sell = await prepare_sell(stack, monkeypatch, quantity=2)
        assert sell["admitted"] is True
        # A second admitted SELL of the same shares, forced past the pre-check, cannot reserve.
        from t212_practice_testkit import practice_proposal_dict
        from execution.venue import PRICE_SOURCE_TEST_REPLAY

        proposal = practice_proposal_dict("forced", side="SELL", quantity="2", limit_price="71.2")
        admission = stack.service.admit(
            proposal, currency="GBP", price="0.712", price_source=PRICE_SOURCE_TEST_REPLAY,
            **stack.app._local_paper_preflight(),
        )
        assert admission.decision.value == "ADMITTED"
        with pytest.raises(ApprovalConflict):
            stack.ledger.reserve_sell_quantity("forced", broker_available_quantity="2")
        with pytest.raises(Exception):
            stack.service.reserve("forced")  # no broker number: fails closed
        with pytest.raises(ApprovalConflict):
            stack.ledger.reserve_sell_quantity("forced", broker_available_quantity="0")
    finally:
        stack.close()


# --- paper ledgers keep denying SELL (D-19, D-21) --------------------------------------------------------


@pytest.mark.parametrize("workspace", ["uk", "india"])
def test_paper_ledgers_deny_every_sell_with_todays_reason_and_have_no_quantity_table(
    workspace, tmp_path
):
    from app_context import AppState
    from execution import OrderIntent

    ticker = "VUSA" if workspace == "uk" else "NSE:CASH:RELIANCE"
    currency = "GBP" if workspace == "uk" else "INR"
    with ExecutionLedger(tmp_path / f"{workspace}.sqlite3", workspace=workspace) as ledger:
        service = ExecutionService(
            None, ledger, simulator=None, risk_gate=None
        )
        intent = OrderIntent(
            proposal_id="sell-1", workspace=workspace, account="paper", broker="paper", mode="PAPER",
            ticker=ticker, side="SELL", quantity=Decimal("1"),
        )
        admission = service.admit(
            intent, currency=currency, price="10",
            simulator_evidence={"simulated_fill_price": "10"}, risk_evidence={"scaled_size": "1"},
            price_source="local-replay",
        )
        assert admission.decision.value == "DENIED"
        assert admission.reason_code == "SELL_ADMISSION_REQUIRES_A_POSITION_RESERVATION"
        with pytest.raises(ApprovalConflict):
            ledger.reserve_sell_quantity("sell-1", broker_available_quantity="1")
        tables = {
            row[0] for row in sqlite3.connect(ledger.path).execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert "ledger_quantity_reservations" not in tables, "no new structure in a paper ledger"
        assert AppState  # imported to keep the module path honest


def test_a_practice_ledger_made_before_the_quantity_table_gains_it_idempotently(tmp_path):
    """66-01 code created practice ledgers without it: opening with the limits adds it, once."""

    binding = VenueBinding(venue="t212_practice", account_id=PRACTICE_ACCOUNT, currency="GBP")
    path = tmp_path / "old.sqlite3"
    with ExecutionLedger(path, workspace="uk", venue=binding) as ledger:
        ledger.configure_venue_limits(
            SYNTH_LIMITS["capital_cap"], SYNTH_LIMITS["per_position_cap"], workspace="uk"
        )
    raw = sqlite3.connect(path)
    raw.executescript(
        "DROP TRIGGER IF EXISTS ledger_quantity_reservations_no_delete;"
        "DROP TRIGGER IF EXISTS ledger_quantity_reservations_workspace_insert_guard;"
        "DROP TABLE ledger_quantity_reservations;"
    )
    raw.commit()
    raw.close()
    for _ in range(2):
        with ExecutionLedger(path, workspace="uk", venue=binding) as ledger:
            ledger.configure_venue_limits(
                SYNTH_LIMITS["capital_cap"], SYNTH_LIMITS["per_position_cap"], workspace="uk"
            )
    tables = {
        row[0] for row in sqlite3.connect(path).execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert "ledger_quantity_reservations" in tables
