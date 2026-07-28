import os
import sqlite3
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

import pytest

from execution.ledger import (
    ClaimStatus,
    ExecutionLedger,
    IntentConflict,
    InvalidTransition,
    LedgerWriterUnavailable,
)
from execution.models import OrderAck, OrderIntent


def make_intent(proposal_id: str = "proposal-1", **overrides) -> OrderIntent:
    values = {
        "proposal_id": proposal_id,
        "workspace": "uk",
        "broker": "paper",
        "ticker": "TQQQ",
        "side": "BUY",
        "quantity": Decimal("10.5"),
        **overrides,
    }
    return OrderIntent(**values)


def make_ack(proposal_id: str = "proposal-1", **overrides) -> OrderAck:
    return OrderAck(
        proposal_id=proposal_id,
        broker="paper",
        broker_order_id="paper-order-1",
        status="ACKNOWLEDGED",
        **overrides,
    )


def test_real_file_pragmas_and_private_permissions(tmp_path):
    db_path = tmp_path / "private" / "execution.sqlite3"

    with ExecutionLedger(db_path, busy_timeout_ms=2_500) as ledger:
        pragmas = ledger.pragmas()

        assert pragmas == {
            "journal_mode": "wal",
            "synchronous": 2,
            "foreign_keys": 1,
            "busy_timeout": 2_500,
            "user_version": 5,
        }
        assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(ledger.lock_path.stat().st_mode) == 0o600


def test_intent_and_acknowledgement_persist_across_reopen_without_raw_payload(tmp_path):
    db_path = tmp_path / "execution.sqlite3"
    intent = make_intent()

    with ExecutionLedger(db_path) as ledger:
        created = ledger.register_intent(intent)
        assert created.state == "PENDING"
        assert ledger.claim(intent.proposal_id, intent).status is ClaimStatus.CLAIMED
        ack = ledger.finalize(
            intent.proposal_id,
            make_ack(raw={"access_token": "must-not-survive"}),
        )
        assert ack.raw == {}

    assert b"must-not-survive" not in db_path.read_bytes()
    with ExecutionLedger(db_path) as reopened:
        order = reopened.get_order(intent.proposal_id)
        assert order is not None
        assert order.intent == intent.model_dump(mode="json")
        assert order.state == "ACKNOWLEDGED"
        assert order.acknowledgment is not None
        assert order.acknowledgment.broker_order_id == "paper-order-1"
        assert order.acknowledgment.raw == {}


def test_changed_intent_conflicts_after_restart(tmp_path):
    db_path = tmp_path / "execution.sqlite3"
    with ExecutionLedger(db_path) as ledger:
        ledger.register_intent(make_intent())

    with ExecutionLedger(db_path) as reopened:
        with pytest.raises(IntentConflict, match="changed intent"):
            reopened.register_intent(make_intent(quantity=Decimal("99")))


def test_client_order_identity_cannot_be_reused_by_another_proposal(tmp_path):
    db_path = tmp_path / "execution.sqlite3"
    with ExecutionLedger(db_path) as ledger:
        ledger.register_intent(make_intent(client_order_id="stable-client-id"))

        with pytest.raises(IntentConflict, match="identity was reused"):
            ledger.register_intent(
                make_intent("proposal-2", client_order_id="stable-client-id")
            )


def test_events_and_intents_are_immutable_by_database_trigger(tmp_path):
    db_path = tmp_path / "execution.sqlite3"
    with ExecutionLedger(db_path) as ledger:
        ledger.register_intent(make_intent())

        external = sqlite3.connect(db_path)
        try:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                external.execute("UPDATE execution_events SET event_type = 'TAMPERED'")
            external.rollback()
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                external.execute("DELETE FROM execution_events")
            external.rollback()
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                external.execute("UPDATE order_intents SET intent_hash = 'tampered'")
        finally:
            external.close()


def test_concurrent_claims_create_exactly_one_dispatch_attempt(tmp_path):
    db_path = tmp_path / "execution.sqlite3"
    with ExecutionLedger(db_path) as ledger:
        intent = make_intent()
        ledger.register_intent(intent)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(
                pool.map(
                    lambda _index: ledger.claim(intent.proposal_id, intent),
                    range(2),
                )
            )

        assert {result.status for result in results} == {
            ClaimStatus.CLAIMED,
            ClaimStatus.IN_PROGRESS,
        }
        assert len(ledger.list_attempts(intent.proposal_id)) == 1
        assert [event.event_type for event in ledger.list_events(intent.proposal_id)] == [
            "INTENT_CREATED",
            "DISPATCH_CLAIMED",
        ]


def test_acknowledgement_finalize_is_replay_safe(tmp_path):
    db_path = tmp_path / "execution.sqlite3"
    with ExecutionLedger(db_path) as ledger:
        intent = make_intent()
        ledger.claim_intent(intent)
        first = ledger.finalize(intent.proposal_id, make_ack())
        replay = ledger.finalize(intent.proposal_id, make_ack())
        claim_replay = ledger.claim(intent.proposal_id, intent)

        assert first.idempotent_replay is False
        assert replay.idempotent_replay is True
        assert claim_replay.status is ClaimStatus.REPLAY
        assert claim_replay.order.acknowledgment is not None
        assert len(ledger.list_attempts(intent.proposal_id)) == 1
        assert [event.event_type for event in ledger.list_events(intent.proposal_id)] == [
            "INTENT_CREATED",
            "DISPATCH_CLAIMED",
            "BROKER_ACKNOWLEDGED",
        ]


def test_startup_recovers_abandoned_submission_to_unknown_without_retry(tmp_path):
    db_path = tmp_path / "execution.sqlite3"
    intent = make_intent()
    with ExecutionLedger(db_path) as ledger:
        ledger.claim_intent(intent)
        assert ledger.get_order(intent.proposal_id).state == "SUBMITTING"

    with ExecutionLedger(db_path) as reopened:
        order = reopened.get_order(intent.proposal_id)
        assert order is not None
        assert order.state == "UNKNOWN"
        assert reopened.list_attempts(intent.proposal_id)[0].state == "UNKNOWN"
        assert reopened.list_events(intent.proposal_id)[-1].event_type == "STARTUP_RECOVERY"
        with pytest.raises(InvalidTransition, match="already UNKNOWN"):
            reopened.claim(intent.proposal_id, intent)
        assert len(reopened.list_attempts(intent.proposal_id)) == 1


def test_reject_and_mark_unknown_are_atomic_terminal_transitions(tmp_path):
    db_path = tmp_path / "execution.sqlite3"
    with ExecutionLedger(db_path) as ledger:
        rejected = make_intent("rejected")
        ledger.register_intent(rejected)
        assert ledger.reject(rejected.proposal_id, "user declined").state == "REJECTED"
        assert ledger.reject(rejected.proposal_id, "ignored replay").state == "REJECTED"

        unknown = make_intent("unknown")
        ledger.claim_intent(unknown)
        assert ledger.mark_unknown(unknown.proposal_id, "BROKER_TIMEOUT").state == "UNKNOWN"
        assert ledger.mark_unknown(unknown.proposal_id).state == "UNKNOWN"

        assert len(ledger.list_events(rejected.proposal_id)) == 2
        assert len(ledger.list_events(unknown.proposal_id)) == 3


def test_second_writer_fails_closed_until_first_releases_lock(tmp_path):
    db_path = tmp_path / "execution.sqlite3"
    first = ExecutionLedger(db_path)
    try:
        with pytest.raises(LedgerWriterUnavailable, match="already active"):
            ExecutionLedger(db_path)
    finally:
        first.close()

    with ExecutionLedger(db_path) as replacement:
        assert replacement.pragmas()["journal_mode"] == "wal"


def test_close_is_idempotent_and_releases_resources(tmp_path):
    db_path = tmp_path / "execution.sqlite3"
    ledger = ExecutionLedger(db_path)
    ledger.close()
    ledger.close()

    assert os.path.exists(db_path)
    with ExecutionLedger(db_path):
        pass


def test_writer_authority_releases_when_owner_process_dies(tmp_path):
    db_path = tmp_path / "execution.sqlite3"
    backend_path = str((Path(__file__).resolve().parents[2] / "backend"))
    environment = os.environ.copy()
    environment["PYTHONPATH"] = backend_path
    code = (
        "from execution.ledger import ExecutionLedger; import sys; "
        f"ledger=ExecutionLedger({str(db_path)!r}); "
        "print('READY', flush=True); sys.stdin.read()"
    )
    owner = subprocess.Popen(
        [sys.executable, "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=environment,
    )
    try:
        assert owner.stdout is not None
        assert owner.stdout.readline().strip() == "READY"
        with pytest.raises(LedgerWriterUnavailable):
            ExecutionLedger(db_path)
    finally:
        owner.terminate()
        owner.wait(timeout=5)

    with ExecutionLedger(db_path) as replacement:
        assert replacement.pragmas()["journal_mode"] == "wal"
