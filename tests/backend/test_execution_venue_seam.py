"""Phase 66-01: the dispatcher seam, OrderMode.PRACTICE and the practice ledger.

No test here contacts a broker. The practice dispatcher is a recording double
registered in an injected factory map; the production map holds only ``paper``.
All limit values are synthetic.
"""

from __future__ import annotations

import pytest

from app_context import AppState
from execution import ExecutionLedger, OrderMode
from execution.venue import (
    VENUE_PAPER,
    VENUE_T212_PRACTICE,
    production_dispatcher_factories,
)
from venue_seam_testkit import (
    SYNTH_ACCOUNT,
    RecordingDispatcher,
    enroll,
    practice_factories,
    practice_proposal,
    prepare,
    private_key,
    sign,
    write_practice_files,
)


@pytest.fixture
def uk_process(monkeypatch):
    monkeypatch.setenv("GROWIN_WORKSPACE", "uk")


def test_production_factory_map_holds_only_paper():
    assert set(production_dispatcher_factories()) == {VENUE_PAPER}


@pytest.mark.asyncio
async def test_tracer_signed_practice_intent_reaches_the_seam_double_once(
    tmp_path, private_config_dir, uk_process
):
    write_practice_files(private_config_dir)
    double = RecordingDispatcher()
    ledger_path = tmp_path / "practice" / "execution.sqlite3"
    app_state = AppState()

    assert app_state.start_execution(
        ledger_path,
        workspace="uk",
        private_dir=private_config_dir,
        dispatcher_factories=practice_factories(double),
    ), app_state.execution_startup_error
    try:
        service = app_state.execution_service
        ledger: ExecutionLedger = app_state._execution_ledger
        assert ledger.path == ledger_path
        assert ledger.venue_binding is not None
        assert ledger.venue_binding.account_id == SYNTH_ACCOUNT

        key = private_key()
        enroll(service._approval_service, key)
        proposal = practice_proposal()
        admission = prepare(app_state, proposal)
        assert admission.decision.value == "ADMITTED"
        assert ledger.get_reservation("practice-1").state == "ACTIVE"

        challenge = service.create_approval_challenge("practice-1", workspace="uk")
        ack = await service.approve_signed(
            "practice-1",
            challenge.challenge_id,
            sign(key, challenge.signed_payload),
            workspace="uk",
        )

        assert len(double.intents) == 1
        sent = double.intents[0]
        assert sent.mode is OrderMode.PRACTICE
        assert (sent.ticker, sent.side.value, str(sent.quantity), str(sent.limit_price)) == (
            "VODl_EQ",
            "BUY",
            "2",
            "50",
        )
        assert sent.account == SYNTH_ACCOUNT
        assert ack.broker == VENUE_T212_PRACTICE
        stored = ledger.get_order("practice-1")
        assert stored.state == "ACKNOWLEDGED"
        assert stored.acknowledgment.broker_order_id == "practice-order-1"
        assert app_state.execution_mode == "practice"
    finally:
        app_state.close_execution()
