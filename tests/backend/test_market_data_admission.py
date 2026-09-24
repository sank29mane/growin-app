from datetime import datetime, timedelta, timezone
from decimal import Decimal
import sqlite3
from unittest.mock import AsyncMock, MagicMock

import pytest

from execution import AdmissionDecision, ExecutionLedger, ExecutionService, OrderIntent
from app_context import AppState
from market_data import (
    IndiaInstrument,
    MarketDataError,
    MarketDataSession,
    RegimeEvidence,
    ReplayMarketDataProvider,
    TopOfBook,
    build_market_preflight_context,
)
from simulation import PreFlightSimulator, RiskSwarmGate


def get_now():
    return datetime.now(timezone.utc)

INSTRUMENT = IndiaInstrument(symbol="RELIANCE")


def quote(*, observed_at=None):
    now = observed_at or get_now()
    return TopOfBook(
        instrument=INSTRUMENT,
        source="local-replay",
        bid="99",
        ask="101",
        observed_at=now,
        received_at=now,
        sequence=1,
    )


async def session_with_quote(*, observed_at=None):
    now = observed_at or get_now()
    session = MarketDataSession(
        ReplayMarketDataProvider([quote(observed_at=now)]),
        max_age_seconds=30,
        clock=lambda: now,
    )
    await session.start((INSTRUMENT,))
    await session.poll_once()
    return session


def intent(**overrides):
    values = {
        "proposal_id": "india-admission",
        "workspace": "india",
        "account": "paper",
        "broker": "paper",
        "mode": "PAPER",
        "ticker": INSTRUMENT.execution_ticker,
        "side": "BUY",
        "quantity": Decimal("1"),
    }
    values.update(overrides)
    return OrderIntent(**values)


def policy_connection():
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE scaling_policies (regime_id INTEGER PRIMARY KEY, scale_multiplier REAL NOT NULL)"
    )
    connection.execute(
        "INSERT INTO scaling_policies (regime_id, scale_multiplier) VALUES (0, 1)"
    )
    return connection


@pytest.mark.asyncio
async def test_fresh_bound_snapshot_feeds_real_phase_54_controls(tmp_path):
    now = get_now()
    session = await session_with_quote(observed_at=now)
    snapshot = session.snapshot(INSTRUMENT, now=now)
    regime = RegimeEvidence(
        instrument=INSTRUMENT,
        regime_id=0,
        observed_at=now,
        model_version="phase50-test-v1",
        source_snapshot_id=snapshot.snapshot_id,
    )
    context = build_market_preflight_context(
        session,
        intent=intent(),
        instrument=INSTRUMENT,
        regime=regime,
        now=now,
    )
    dispatcher = MagicMock()
    dispatcher.dispatch = AsyncMock()
    connection = policy_connection()
    try:
        with ExecutionLedger(tmp_path / "execution.sqlite3", workspace="india") as ledger:
            service = ExecutionService(
                dispatcher,
                ledger,
                simulator=PreFlightSimulator(),
                risk_gate=RiskSwarmGate(),
                require_runtime_preflight=True,
            )
            admission = service.admit(
                intent(),
                currency="INR",
                portfolio_state={"equity": 1000.0, "peak_equity": 1000.0},
                risk_db_connection=connection,
                **context.execution_kwargs(),
            )
            assert admission.decision is AdmissionDecision.ADMITTED
            assert context.snapshot_id == snapshot.snapshot_id
            assert admission.price == Decimal("100")
            dispatcher.dispatch.assert_not_awaited()
    finally:
        connection.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "intent_overrides,error_code",
    [
        ({"workspace": "uk"}, "WORKSPACE_MISMATCH"),
        ({"ticker": "NSE:CASH:TCS"}, "INSTRUMENT_MISMATCH"),
    ],
)
async def test_cross_workspace_and_wrong_instrument_fail_before_simulation(
    intent_overrides,
    error_code,
):
    now = get_now()
    session = await session_with_quote(observed_at=now)
    snapshot = session.snapshot(INSTRUMENT, now=now)
    regime = RegimeEvidence(
        instrument=INSTRUMENT,
        regime_id=0,
        observed_at=now,
        model_version="phase50-test-v1",
        source_snapshot_id=snapshot.snapshot_id,
    )
    with pytest.raises(MarketDataError) as error:
        build_market_preflight_context(
            session,
            intent=intent(**intent_overrides),
            instrument=INSTRUMENT,
            regime=regime,
            now=now,
        )
    assert error.value.code == error_code


@pytest.mark.asyncio
async def test_stale_or_mismatched_regime_fails_closed():
    now = get_now()
    session = await session_with_quote(observed_at=now)
    snapshot = session.snapshot(INSTRUMENT, now=now)
    stale = RegimeEvidence(
        instrument=INSTRUMENT,
        regime_id=0,
        observed_at=now - timedelta(seconds=31),
        model_version="phase50-test-v1",
        source_snapshot_id=snapshot.snapshot_id,
    )
    with pytest.raises(MarketDataError) as error:
        build_market_preflight_context(
            session,
            intent=intent(),
            instrument=INSTRUMENT,
            regime=stale,
            now=now,
        )
    assert error.value.code == "STALE_REGIME"

    wrong_snapshot = stale.model_copy(
        update={"observed_at": now, "source_snapshot_id": "0" * 64}
    )
    with pytest.raises(MarketDataError) as error:
        build_market_preflight_context(
            session,
            intent=intent(),
            instrument=INSTRUMENT,
            regime=wrong_snapshot,
            now=now,
        )
    assert error.value.code == "REGIME_SNAPSHOT_MISMATCH"


@pytest.mark.asyncio
async def test_app_owned_india_entry_uses_market_session_and_durably_rejects_gaps(
    tmp_path,
):
    app_state = AppState()
    assert app_state.start_execution(
        tmp_path / "execution.sqlite3",
        workspace="india",
    )
    try:
        await app_state.start_market_data_replay(
            (INSTRUMENT,),
            tuple(
                quote(observed_at=datetime.now(timezone.utc)).model_copy(update={"sequence": sequence})
                for sequence in (1, 2, 3)
            ),
        )
        app_state._execution_ledger.configure_paper_budget("paper", "INR", "1000")
        proposal = intent(proposal_id="app-entry").model_dump(mode="json")
        admitted = app_state.admit_india_paper_proposal(
            proposal,
            instrument=INSTRUMENT,
            portfolio_state={"equity": 1000.0, "peak_equity": 1000.0},
        )
        assert admitted.decision is AdmissionDecision.ADMITTED
        assert app_state._execution_ledger.list_attempts("app-entry") == []

        await app_state.close_market_data()
        denied_proposal = intent(proposal_id="missing-market").model_dump(mode="json")
        denied = app_state.admit_india_paper_proposal(
            denied_proposal,
            instrument=INSTRUMENT,
            portfolio_state={"equity": 1000.0, "peak_equity": 1000.0},
        )
        assert denied.decision is AdmissionDecision.DENIED
        assert app_state._execution_ledger.get_order("missing-market").state == "REJECTED"
        assert app_state._execution_ledger.get_reservation("missing-market") is None
        assert app_state._execution_ledger.list_attempts("missing-market") == []
    finally:
        await app_state.close_market_data()
        app_state.close_execution()
