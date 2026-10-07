"""Phase 66-04 Task 2: one contract suite over every execution venue (UKT-02, D-24).

The same test functions run for ``paper-uk``, ``paper-india`` and
``t212-practice``. "Same tests as India" means the same functions: each test
talks to a ``Harness`` and never to a venue directly. Adding a venue is one
entry in ``VENUES``. Where a case cannot apply (cancel on paper) the venue
declares ``can_cancel = False`` and the same test asserts the refusal instead
of being skipped. Phase 63 adds ``breeze-relay`` as another entry.

The practice venue runs over ``FakeDemoBroker`` (``httpx.MockTransport``); no
test contacts a broker. The real smoke never runs in CI.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio

from app_context import AppState
from execution import (
    ApprovalService,
    ExecutionDisabledError,
    ExecutionLedger,
    ExecutionService,
    OrderAck,
    PaperDispatcher,
    ReconciliationSnapshot,
    ReconciliationStatus,
)
import india_limits_support as ils
from market_data.admission import RecordedQuoteReading
from regime_testkit import calm_probabilities, shipped_map
from t212_practice_testkit import (
    PRACTICE_ACCOUNT,
    start_practice_stack,
)
from t212_testkit import install_no_real_network
from venue_seam_testkit import enroll, practice_execution_payload, private_key, sign

QUANTITY = 2


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    install_no_real_network(monkeypatch)


# --- the harness interface ---------------------------------------------------------------------------


class Harness:
    """What every venue entry must provide. Tests use only these members."""

    id: str
    can_cancel: bool

    async def open(self) -> None: ...
    async def close(self) -> None: ...
    async def admit(self, kind: str, proposal_id: str) -> Any:
        """``kind`` is fresh, stale or missing. Returns an admission-like object."""
    def reservation(self, proposal_id: str): ...
    def reserve_again(self, proposal_id: str): ...
    async def approve(self, proposal_id: str) -> OrderAck: ...
    async def approve_again(self, proposal_id: str) -> OrderAck:
        """Present the very same signed approval a second time."""
    def dispatch_count(self) -> int: ...
    def order_state(self, proposal_id: str) -> str: ...
    async def partial_fill(self, proposal_id: str, quantity: int) -> str: ...
    async def fill_rest(self, proposal_id: str) -> str: ...
    async def request_cancel(self, proposal_id: str) -> None: ...
    async def reconcile_cancelled(self, proposal_id: str) -> str: ...
    def position_quantity(self) -> Decimal: ...
    def budget_reserved(self) -> Decimal: ...
    async def cancel_through_service(self, proposal_id: str) -> Any: ...


class CountingPaper(PaperDispatcher):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def dispatch(self, intent):
        self.calls += 1
        return await super().dispatch(intent)


class PaperHarness(Harness):
    """A paper ledger (UK or India) with a counting paper dispatcher."""

    can_cancel = False

    def __init__(self, workspace: str, tmp_path, private_dir=None) -> None:
        self.workspace = workspace
        self.private_dir = private_dir
        self.id = f"paper-{workspace}"
        self.tmp_path = tmp_path
        if workspace == "uk":
            self.account, self.broker, self.ticker, self.currency = "invest", "paper", "VUSA", "GBP"
        else:
            self.account, self.broker, self.ticker, self.currency = "paper", "paper", "NSE:CASH:RELIANCE", "INR"

    async def open(self) -> None:
        self.ledger = ExecutionLedger(
            self.tmp_path / f"{self.id}.sqlite3", workspace=self.workspace, require_approval=True
        )
        self.approval = ApprovalService(self.ledger)
        self.key = private_key()
        enroll(self.approval, self.key, workspace=self.workspace)
        self.dispatcher = CountingPaper()
        # Phase 63-04: an India runtime service carries the Mac's India limits. UK is unchanged.
        guard = (
            ils.make_guard(self.ledger, self.private_dir) if self.workspace == "india" else None
        )
        self.service = ExecutionService(
            self.dispatcher, self.ledger, require_approval=True, approval_service=self.approval,
            simulator=__import__("simulation").PreFlightSimulator(),
            risk_gate=__import__("simulation").RiskSwarmGate(),
            require_runtime_preflight=True,
            india_guard=guard,
        )
        self.policy = AppState._local_preflight_policy_connection()
        self.ledger.configure_paper_budget(self.account, self.currency, "1000", workspace=self.workspace)
        self._signed: dict[str, tuple] = {}
        self._filled: dict[str, int] = {}

    async def close(self) -> None:
        self.policy.close()
        self.ledger.close()

    def _proposal(self, proposal_id: str) -> dict[str, Any]:
        proposal = {
            "proposal_id": proposal_id, "client_order_id": f"growin-{proposal_id}",
            "workspace": self.workspace, "account": self.account, "broker": self.broker,
            "mode": "PAPER", "ticker": self.ticker, "action": "BUY", "quantity": str(QUANTITY),
            "status": "PENDING",
        }
        if self.workspace == "india":
            # The India limits need a LIMIT order (the price the suite already uses).
            proposal.update({"order_type": "LIMIT", "limit_price": "10.00"})
        return proposal

    def _india_quote(self):
        return ils.make_evidence(
            ltp=Decimal("10.00"), lower_circuit=Decimal("9.00"), upper_circuit=Decimal("11.00"),
            previous_close=Decimal("10.00"), tick_reference=Decimal("10.00"),
            bid=Decimal("9.99"), ask=Decimal("10.01"),
        )

    async def admit(self, kind: str, proposal_id: str):
        now = datetime.now(timezone.utc)
        kwargs: dict[str, Any] = {
            "price": "10",
            "tick_window": {"bid": [9.99], "ask": [10.01], "spread": [0.002]},
            "portfolio_state": {"equity": 1000.0, "peak_equity": 1000.0},
            "regime_id": shipped_map().calm_id, "current_spread_pct": 0.002, "risk_db_connection": self.policy,
            "evidence_at": now,
        }
        if self.workspace == "india":
            kwargs["india_quote"] = self._india_quote()
        if kind == "stale":
            kwargs["evidence_at"] = now - timedelta(seconds=120)
        if kind == "missing":
            for name in ("tick_window", "portfolio_state", "regime_id", "current_spread_pct"):
                kwargs.pop(name)
        return self.service.prepare(self._proposal(proposal_id), currency=self.currency, **kwargs)

    def reservation(self, proposal_id):
        return self.ledger.get_reservation(proposal_id)

    def reserve_again(self, proposal_id):
        return self.service.reserve(proposal_id)

    async def approve(self, proposal_id):
        # 63-04: an India BUY is rechecked against a fresh quote at challenge and at claim.
        extra = {"india_quote": self._india_quote()} if self.workspace == "india" else {}
        challenge = self.service.create_approval_challenge(
            proposal_id, workspace=self.workspace, **extra
        )
        signature = sign(self.key, challenge.signed_payload)
        self._signed[proposal_id] = (challenge.challenge_id, signature)
        return await self.service.approve_signed(
            proposal_id, challenge.challenge_id, signature, workspace=self.workspace, **extra
        )

    async def approve_again(self, proposal_id):
        challenge_id, signature = self._signed[proposal_id]
        return await self.service.approve_signed(
            proposal_id, challenge_id, signature, workspace=self.workspace
        )

    def dispatch_count(self):
        return self.dispatcher.calls

    def order_state(self, proposal_id):
        return self.ledger.get_order(proposal_id).state

    def _snapshot(self, proposal_id, quantity, status):
        ack = self.ledger.get_order(proposal_id).acknowledgment
        notional = Decimal("10") * quantity
        return ReconciliationSnapshot(
            proposal_id=proposal_id, broker_order_id=ack.broker_order_id, source="contract-suite",
            cumulative_quantity=Decimal(quantity), cumulative_notional=notional, status=status,
            evidence_fingerprint=f"contract:{proposal_id}:{quantity}:{status.value}",
            observed_at=datetime.now(timezone.utc),
        )

    async def partial_fill(self, proposal_id, quantity):
        self._filled[proposal_id] = quantity
        return self.service.reconcile(
            self._snapshot(proposal_id, quantity, ReconciliationStatus.PARTIALLY_FILLED)
        ).state

    async def fill_rest(self, proposal_id):
        self._filled[proposal_id] = QUANTITY
        return self.service.reconcile(
            self._snapshot(proposal_id, QUANTITY, ReconciliationStatus.FILLED)
        ).state

    async def request_cancel(self, proposal_id):
        raise AssertionError("paper venues declare can_cancel = False")

    async def reconcile_cancelled(self, proposal_id):
        done = self._filled.get(proposal_id, 0)
        return self.service.reconcile(
            self._snapshot(proposal_id, done, ReconciliationStatus.CANCELLED)
        ).state

    def position_quantity(self):
        row = self.ledger.get_paper_position(
            self.account, self.currency, self.ticker, workspace=self.workspace
        )
        return Decimal("0") if row is None else Decimal(row["quantity"])

    def budget_reserved(self):
        return self.ledger.get_paper_budget(self.account, self.currency, workspace=self.workspace).reserved

    async def cancel_through_service(self, proposal_id):
        return await self.service.cancel_order(proposal_id)


class PracticeHarness(Harness):
    """The Trading 212 practice venue over a mock demo host."""

    can_cancel = True
    id = "t212-practice"

    def __init__(self, tmp_path, private_dir, monkeypatch) -> None:
        self.tmp_path, self.private_dir, self.monkeypatch = tmp_path, private_dir, monkeypatch
        self.ticker = "VODl_EQ"
        self._signed: dict[str, tuple] = {}
        self._broker_ids: dict[str, int] = {}

    async def open(self) -> None:
        import market_data.regime as regime_module

        self.monkeypatch.setattr(
            regime_module, "fast_gmm_predict_proba",
            lambda feature, **params: calm_probabilities(),
        )
        self.stack = await start_practice_stack(
            self.tmp_path, self.private_dir, self.monkeypatch,
            execution=practice_execution_payload(account_id=PRACTICE_ACCOUNT, max_slippage_bps=25),
        )
        assert self.stack.started, self.stack.app.execution_startup_error

    async def close(self) -> None:
        self.stack.close()

    async def admit(self, kind: str, proposal_id: str):
        end = datetime.now(timezone.utc)
        if kind == "stale":
            end -= timedelta(seconds=45)
        count = 0 if kind == "missing" else 3
        quotes = [
            RecordedQuoteReading(
                bid=Decimal("71.2"), ask=Decimal("71.3"),
                observed_at=end - timedelta(seconds=2 * (count - 1 - index)),
            )
            for index in range(count)
        ]
        proposal, admission = await self.stack.app.admit_uk_practice_proposal(
            ticker=self.ticker, side="BUY", quantity=QUANTITY, limit_price=Decimal("71.3"), readings=quotes,
        )
        self._by_requested = getattr(self, "_by_requested", {})
        self._by_requested[proposal_id] = proposal["proposal_id"]
        return admission

    def _real(self, proposal_id: str) -> str:
        return self._by_requested[proposal_id]

    def reservation(self, proposal_id):
        return self.stack.ledger.get_reservation(self._real(proposal_id))

    def reserve_again(self, proposal_id):
        return self.stack.service.reserve(self._real(proposal_id))

    async def approve(self, proposal_id):
        real = self._real(proposal_id)
        challenge = self.stack.service.create_approval_challenge(real, workspace="uk")
        signature = sign(self.stack.key, challenge.signed_payload)
        self._signed[proposal_id] = (challenge.challenge_id, signature)
        ack = await self.stack.service.approve_signed(
            real, challenge.challenge_id, signature, workspace="uk"
        )
        self._broker_ids[proposal_id] = int(ack.broker_order_id)
        return ack

    async def approve_again(self, proposal_id):
        challenge_id, signature = self._signed[proposal_id]
        return await self.stack.service.approve_signed(
            self._real(proposal_id), challenge_id, signature, workspace="uk"
        )

    def dispatch_count(self):
        return len(self.stack.broker.posts)

    def order_state(self, proposal_id):
        return self.stack.ledger.get_order(self._real(proposal_id)).state

    async def partial_fill(self, proposal_id, quantity):
        self.stack.broker.fill(self._broker_ids[proposal_id], price=70.0, quantity=quantity)
        return (await self.stack.adapter.reconciler.reconcile(self._real(proposal_id))).state

    async def fill_rest(self, proposal_id):
        self.stack.broker.fill(self._broker_ids[proposal_id], price=70.0)
        return (await self.stack.adapter.reconciler.reconcile(self._real(proposal_id))).state

    async def request_cancel(self, proposal_id):
        assert (await self.stack.service.cancel_order(self._real(proposal_id))).requested

    async def reconcile_cancelled(self, proposal_id):
        broker_id = self._broker_ids[proposal_id]
        if broker_id in self.stack.broker.pending:
            self.stack.broker.expire(broker_id)
        return (await self.stack.adapter.reconciler.reconcile(self._real(proposal_id))).state

    def position_quantity(self):
        row = self.stack.ledger.get_paper_position(
            PRACTICE_ACCOUNT, "GBP", self.ticker, workspace="uk"
        )
        return Decimal("0") if row is None else Decimal(row["quantity"])

    def budget_reserved(self):
        return self.stack.ledger.get_paper_budget(PRACTICE_ACCOUNT, "GBP", workspace="uk").reserved

    async def cancel_through_service(self, proposal_id):
        return await self.stack.service.cancel_order(self._real(proposal_id))


# --- the fixture table: adding a venue is one entry --------------------------------------------------

VENUES = {
    "paper-uk": lambda tmp_path, private_dir, monkeypatch: PaperHarness("uk", tmp_path),
    "paper-india": lambda tmp_path, private_dir, monkeypatch: PaperHarness("india", tmp_path, private_dir),
    "t212-practice": lambda tmp_path, private_dir, monkeypatch: PracticeHarness(
        tmp_path, private_dir, monkeypatch
    ),
}


@pytest_asyncio.fixture(params=sorted(VENUES))
async def venue(request, tmp_path, private_config_dir, monkeypatch):
    harness = VENUES[request.param](tmp_path, private_config_dir, monkeypatch)
    await harness.open()
    try:
        yield harness
    finally:
        await harness.close()


# --- the contract: the same functions for every venue ------------------------------------------------


@pytest.mark.asyncio
async def test_a_fresh_admission_is_admitted_and_reserves(venue):
    admission = await venue.admit("fresh", "c-fresh")
    assert admission.decision.value == "ADMITTED", admission.reason_code
    reservation = venue.reservation("c-fresh")
    assert reservation is not None and reservation.state == "ACTIVE"
    assert reservation.reserved > 0
    assert venue.dispatch_count() == 0, "admission and reservation never dispatch"


@pytest.mark.asyncio
async def test_stale_evidence_is_denied_and_reserves_nothing(venue):
    admission = await venue.admit("stale", "c-stale")
    assert admission.decision.value == "DENIED"
    assert "STALE" in admission.reason_code
    assert venue.reservation("c-stale") is None
    assert venue.budget_reserved() == 0
    assert venue.dispatch_count() == 0


@pytest.mark.asyncio
async def test_missing_evidence_is_denied_and_reserves_nothing(venue):
    admission = await venue.admit("missing", "c-missing")
    assert admission.decision.value == "DENIED"
    assert venue.reservation("c-missing") is None
    assert venue.budget_reserved() == 0
    assert venue.dispatch_count() == 0


@pytest.mark.asyncio
async def test_a_reservation_is_made_once_and_repeating_it_changes_nothing(venue):
    await venue.admit("fresh", "c-once")
    reserved = venue.budget_reserved()
    again = venue.reserve_again("c-once")
    assert again.proposal_id == venue.reservation("c-once").proposal_id
    assert venue.budget_reserved() == reserved, "the budget is charged once"


@pytest.mark.asyncio
async def test_a_signed_approval_dispatches_exactly_once(venue):
    await venue.admit("fresh", "c-sign")
    ack = await venue.approve("c-sign")
    assert venue.dispatch_count() == 1
    assert ack.broker_order_id
    assert venue.order_state("c-sign") == "ACKNOWLEDGED"


@pytest.mark.asyncio
async def test_presenting_the_same_signed_approval_again_returns_the_stored_ack_and_sends_nothing(venue):
    await venue.admit("fresh", "c-replay")
    first = await venue.approve("c-replay")
    replay = await venue.approve_again("c-replay")
    assert replay.idempotent_replay is True
    assert replay.broker_order_id == first.broker_order_id
    assert venue.dispatch_count() == 1


@pytest.mark.asyncio
async def test_reconcile_partial_then_fill_is_monotonic_and_moves_the_position(venue):
    await venue.admit("fresh", "c-fill")
    await venue.approve("c-fill")
    assert await venue.partial_fill("c-fill", 1) == "PARTIALLY_FILLED"
    assert venue.position_quantity() == Decimal("1")
    assert await venue.fill_rest("c-fill") == "FILLED"
    assert venue.position_quantity() == Decimal(QUANTITY)
    reservation = venue.reservation("c-fill")
    assert reservation.state == "SETTLED" and reservation.outstanding == 0
    assert venue.dispatch_count() == 1


@pytest.mark.asyncio
async def test_a_cancel_releases_the_reservation_where_the_venue_can_cancel_and_is_refused_where_it_cannot(venue):
    await venue.admit("fresh", "c-cancel")
    await venue.approve("c-cancel")
    assert venue.reservation("c-cancel").state == "ACTIVE"
    if venue.can_cancel:
        await venue.request_cancel("c-cancel")
    else:
        with pytest.raises(ExecutionDisabledError):
            await venue.cancel_through_service("c-cancel")
        assert venue.reservation("c-cancel").state == "ACTIVE", "a refused cancel releases nothing"
    assert await venue.reconcile_cancelled("c-cancel") == "CANCELLED"
    reservation = venue.reservation("c-cancel")
    assert reservation.state == "SETTLED" and reservation.outstanding == 0
    assert venue.budget_reserved() == 0
    assert venue.position_quantity() == 0


def test_every_registered_venue_has_exactly_one_fixture_entry():
    assert sorted(VENUES) == ["paper-india", "paper-uk", "t212-practice"]
