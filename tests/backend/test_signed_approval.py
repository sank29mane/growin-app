import asyncio
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from execution import (
    ApprovalConflict,
    ApprovalService,
    ApprovalVerificationError,
    EnrollmentError,
    ExecutionConflictError,
    ExecutionDisabledError,
    ExecutionLedger,
    ExecutionService,
    PaperDispatcher,
)
from execution.ledger import IntentConflict
from execution.models import OrderIntent


class MutableClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


def private_key():
    return ec.generate_private_key(ec.SECP256R1())


def public_x963(key) -> bytes:
    return key.public_key().public_bytes(
        serialization.Encoding.X962,
        serialization.PublicFormat.UncompressedPoint,
    )


def make_intent(proposal_id: str = "signed-1", **overrides) -> OrderIntent:
    values = {
        "proposal_id": proposal_id,
        "workspace": "uk",
        "account": "invest",
        "broker": "paper",
        "mode": "PAPER",
        "ticker": "VUSA",
        "side": "BUY",
        "quantity": Decimal("2.5"),
        **overrides,
    }
    return OrderIntent(**values)


def admit_and_reserve(ledger: ExecutionLedger, intent: OrderIntent) -> None:
    service = ExecutionService(PaperDispatcher(), ledger, require_approval=True)
    service.admit(
        intent,
        currency="GBP",
        price="100",
        simulator_evidence={"simulated_fill_price": "100"},
        risk_evidence={"scaled_size": str(intent.quantity)},
    )
    ledger.configure_paper_budget(intent.account, "GBP", "10000")
    service.reserve(intent.proposal_id)


def enroll(approval: ApprovalService, key, token: bytes = b"one-time-secret"):
    token_path = approval.enrollment_token_path
    token_path.write_bytes(token)
    os.chmod(token_path, 0o600)
    enrolled = approval.enroll_key(public_x963(key), token)
    assert not token_path.exists()
    return enrolled


def sign(key, payload: bytes) -> bytes:
    return key.sign(payload, ec.ECDSA(hashes.SHA256()))


def test_first_key_enrollment_requires_private_one_time_token_and_is_idempotent(tmp_path):
    with ExecutionLedger(tmp_path / "execution.sqlite3", require_approval=True) as ledger:
        approval = ApprovalService(ledger)
        key = private_key()
        token_path = approval.enrollment_token_path
        generated = token_path.read_bytes()
        assert len(generated) >= 43
        assert generated.decode("ascii")
        assert token_path.stat().st_mode & 0o777 == 0o600
        assert approval.ensure_enrollment_token_file() == token_path
        assert token_path.read_bytes() == generated
        token_path.write_bytes(b"correct")
        os.chmod(token_path, 0o644)
        with pytest.raises(EnrollmentError, match="0600"):
            approval.enroll_key(public_x963(key), b"correct")
        os.chmod(token_path, 0o600)
        with pytest.raises(EnrollmentError, match="does not match"):
            approval.enroll_key(public_x963(key), b"wrong")

        first = approval.enroll_key(public_x963(key), b"correct")
        replay = approval.enroll_key(public_x963(key), b"token-is-gone")
        assert replay == first
        assert not token_path.exists()
        ApprovalService(ledger)
        assert not token_path.exists()
        assert b"correct" not in ledger.path.read_bytes()
        with pytest.raises(EnrollmentError, match="rotation"):
            approval.enroll_key(public_x963(private_key()), b"anything")


@pytest.mark.asyncio
async def test_signed_payload_is_exact_and_success_replays_only_same_evidence(tmp_path):
    clock = MutableClock()
    with ExecutionLedger(tmp_path / "execution.sqlite3", require_approval=True) as ledger:
        approval = ApprovalService(ledger, clock=clock)
        key = private_key()
        enrolled = enroll(approval, key)
        service = ExecutionService(
            PaperDispatcher(),
            ledger,
            require_approval=True,
            approval_service=approval,
        )
        admit_and_reserve(ledger, make_intent())
        challenge = service.create_approval_challenge("signed-1", ttl_seconds=60)
        payload = json.loads(challenge.signed_payload)

        assert set(payload) == {
            "version",
            "purpose",
            "challenge_id",
            "proposal_id",
            "client_order_id",
            "intent_hash",
            "workspace",
            "account",
            "broker",
            "mode",
            "ticker",
            "side",
            "quantity",
            "admitted_quantity",
            "currency",
            "price",
            "notional",
            "evidence_hash",
            "nonce",
            "issued_at",
            "expires_at",
            "key_id",
        }
        assert payload["version"] == 1
        assert payload["purpose"] == "growin.execution.dispatch"
        assert payload["key_id"] == enrolled.key_id
        assert payload["workspace"] == "uk"
        assert payload["quantity"] == "2.5"
        assert json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8") == challenge.signed_payload

        signature = sign(key, challenge.signed_payload)
        first = await service.approve_signed("signed-1", challenge.challenge_id, signature)
        replay = await service.approve_signed("signed-1", challenge.challenge_id, signature)

        assert first.idempotent_replay is False
        assert replay.idempotent_replay is True
        assert ledger.approval_evidence_count("signed-1") == 1
        assert len(ledger.list_attempts("signed-1")) == 1
        assert all(
            "signature" not in json.dumps(event.payload)
            for event in ledger.list_events("signed-1")
        )
        assert [event.event_type for event in ledger.list_events("signed-1")] == [
            "INTENT_CREATED",
            "ADMISSION_DECIDED",
            "BUYING_POWER_RESERVED",
            "APPROVAL_CHALLENGE_CREATED",
            "HUMAN_APPROVAL_VERIFIED",
            "DISPATCH_CLAIMED",
            "BROKER_ACKNOWLEDGED",
        ]


@pytest.mark.asyncio
async def test_required_approval_blocks_legacy_and_invalid_signature(tmp_path):
    with ExecutionLedger(tmp_path / "execution.sqlite3", require_approval=True) as ledger:
        clock = MutableClock()
        approval = ApprovalService(ledger, clock=clock)
        key = private_key()
        enroll(approval, key)
        service = ExecutionService(
            PaperDispatcher(), ledger, require_approval=True, approval_service=approval
        )
        intent = make_intent()
        admit_and_reserve(ledger, intent)
        with pytest.raises(ExecutionDisabledError, match="Signed approval"):
            await service.approve(intent.proposal_id)

        challenge = service.create_approval_challenge(intent.proposal_id)
        wrong_signature = sign(private_key(), challenge.signed_payload)
        with pytest.raises(ApprovalVerificationError, match="invalid"):
            await service.approve_signed(
                intent.proposal_id, challenge.challenge_id, wrong_signature
            )
        assert ledger.get_order(intent.proposal_id).state == "PENDING"
        assert ledger.approval_evidence_count(intent.proposal_id) == 0
        assert ledger.list_attempts(intent.proposal_id) == []


@pytest.mark.asyncio
async def test_concurrent_services_create_one_approval_and_dispatch(tmp_path):
    class BlockingDispatcher:
        def __init__(self) -> None:
            self.calls = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def dispatch(self, intent):
            self.calls += 1
            self.started.set()
            await self.release.wait()
            return await PaperDispatcher().dispatch(intent)

    with ExecutionLedger(tmp_path / "execution.sqlite3", require_approval=True) as ledger:
        approval = ApprovalService(ledger, clock=MutableClock())
        key = private_key()
        enroll(approval, key)
        admit_and_reserve(ledger, make_intent())
        challenge = approval.create_challenge("signed-1")
        signature = sign(key, challenge.signed_payload)
        dispatcher = BlockingDispatcher()
        first_service = ExecutionService(
            dispatcher, ledger, require_approval=True, approval_service=approval
        )
        second_service = ExecutionService(
            dispatcher, ledger, require_approval=True, approval_service=approval
        )

        first = asyncio.create_task(
            first_service.approve_signed("signed-1", challenge.challenge_id, signature)
        )
        await dispatcher.started.wait()
        with pytest.raises(ExecutionConflictError, match="before acknowledgement"):
            await second_service.approve_signed(
                "signed-1", challenge.challenge_id, signature
            )
        dispatcher.release.set()
        await first

        assert dispatcher.calls == 1
        assert ledger.approval_evidence_count("signed-1") == 1
        assert len(ledger.list_attempts("signed-1")) == 1


def test_expired_and_replay_before_ack_fail_without_duplicate_evidence(tmp_path):
    clock = MutableClock()
    with ExecutionLedger(tmp_path / "execution.sqlite3", require_approval=True) as ledger:
        approval = ApprovalService(ledger, clock=clock)
        key = private_key()
        enroll(approval, key)
        admit_and_reserve(ledger, make_intent("expired"))
        expired = approval.create_challenge("expired", ttl_seconds=5)
        expired_signature = sign(key, expired.signed_payload)
        clock.advance(seconds=6)
        with pytest.raises(ApprovalConflict, match="expired"):
            approval.approve_signed("expired", expired.challenge_id, expired_signature)
        assert ledger.approval_evidence_count("expired") == 0

        admit_and_reserve(ledger, make_intent("in-flight"))
        current = approval.create_challenge("in-flight", ttl_seconds=60)
        current_signature = sign(key, current.signed_payload)
        first = approval.approve_signed(
            "in-flight", current.challenge_id, current_signature
        )
        assert first.claimed
        with pytest.raises(ApprovalConflict, match="before acknowledgement"):
            approval.approve_signed(
                "in-flight", current.challenge_id, current_signature
            )
        assert ledger.approval_evidence_count("in-flight") == 1
        assert len(ledger.list_attempts("in-flight")) == 1


def test_approval_and_claim_roll_back_together_on_event_failure(tmp_path, monkeypatch):
    with ExecutionLedger(tmp_path / "execution.sqlite3", require_approval=True) as ledger:
        approval = ApprovalService(ledger, clock=MutableClock())
        key = private_key()
        enroll(approval, key)
        admit_and_reserve(ledger, make_intent())
        challenge = approval.create_challenge("signed-1")
        signature = sign(key, challenge.signed_payload)

        def fail_event(*_args, **_kwargs):
            raise RuntimeError("injected event failure")

        monkeypatch.setattr(ledger, "_append_event", fail_event)
        with pytest.raises(RuntimeError, match="injected"):
            approval.approve_signed("signed-1", challenge.challenge_id, signature)
        assert ledger.get_order("signed-1").state == "PENDING"
        assert ledger.approval_evidence_count("signed-1") == 0
        assert ledger.list_attempts("signed-1") == []


def test_workspace_and_live_intents_fail_closed(tmp_path):
    with ExecutionLedger(tmp_path / "execution.sqlite3", workspace="uk") as ledger:
        with pytest.raises(IntentConflict, match="workspace"):
            ledger.register_intent(make_intent(workspace="india"))

    with ExecutionLedger(tmp_path / "live.sqlite3", require_approval=True) as ledger:
        approval = ApprovalService(ledger)
        enroll(approval, private_key())
        ledger.register_intent(
            make_intent("live", broker="trading212", mode="LIVE")
        )
        with pytest.raises(ApprovalConflict, match="live execution"):
            approval.create_challenge("live")


def test_v1_database_migrates_without_losing_existing_order(tmp_path):
    db_path = tmp_path / "execution.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE order_intents (
            proposal_id TEXT PRIMARY KEY,
            client_order_id TEXT NOT NULL UNIQUE,
            intent_hash TEXT NOT NULL,
            canonical_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE order_projection (
            proposal_id TEXT PRIMARY KEY REFERENCES order_intents(proposal_id),
            state TEXT NOT NULL,
            acknowledgment_json TEXT,
            rejection_notes TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE dispatch_attempts (
            attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_id TEXT NOT NULL UNIQUE REFERENCES order_intents(proposal_id),
            state TEXT NOT NULL,
            claimed_at TEXT NOT NULL,
            completed_at TEXT,
            acknowledgment_json TEXT
        );
        CREATE TABLE execution_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_id TEXT NOT NULL REFERENCES order_intents(proposal_id),
            event_type TEXT NOT NULL,
            from_state TEXT,
            to_state TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        PRAGMA user_version = 1;
        """
    )
    intent = make_intent("legacy")
    from execution.ledger import canonical_json
    import hashlib

    snapshot = canonical_json(intent)
    digest = hashlib.sha256(snapshot.encode()).hexdigest()
    connection.execute(
        "INSERT INTO order_intents VALUES (?, ?, ?, ?, ?)",
        ("legacy", "growin-legacy", digest, snapshot, "before"),
    )
    connection.execute(
        "INSERT INTO order_projection VALUES (?, 'PENDING', NULL, NULL, ?, ?)",
        ("legacy", "before", "before"),
    )
    connection.commit()
    connection.close()

    with ExecutionLedger(db_path) as ledger:
        assert ledger.pragmas()["user_version"] == 3
        assert ledger.get_order("legacy").intent_hash == digest
        columns = {
            row[1]
            for row in sqlite3.connect(db_path).execute(
                "PRAGMA table_info(dispatch_attempts)"
            )
        }
        assert "approval_id" in columns


def test_approval_evidence_and_challenge_are_database_immutable(tmp_path):
    with ExecutionLedger(tmp_path / "execution.sqlite3", require_approval=True) as ledger:
        approval = ApprovalService(ledger, clock=MutableClock())
        key = private_key()
        enroll(approval, key)
        admit_and_reserve(ledger, make_intent())
        challenge = approval.create_challenge("signed-1")
        approval.approve_signed(
            "signed-1", challenge.challenge_id, sign(key, challenge.signed_payload)
        )
        observer = sqlite3.connect(ledger.path)
        try:
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                observer.execute("DELETE FROM approval_challenges")
            observer.rollback()
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                observer.execute("UPDATE execution_approvals SET key_id = 'changed'")
        finally:
            observer.close()
