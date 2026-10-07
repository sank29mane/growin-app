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
    WorkspaceMismatch,
)
from execution.ledger import IntentConflict, LedgerUnpinned
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
    ledger.configure_paper_budget(intent.account, "GBP", "10000", workspace="uk")
    service.reserve(intent.proposal_id)


def enroll(approval: ApprovalService, key, token: bytes = b"one-time-secret"):
    token_path = approval.enrollment_token_path
    token_path.write_bytes(token)
    os.chmod(token_path, 0o600)
    enrolled = approval.enroll_key(public_x963(key), token, workspace="uk")
    assert not token_path.exists()
    return enrolled


def sign(key, payload: bytes) -> bytes:
    return key.sign(payload, ec.ECDSA(hashes.SHA256()))


def test_first_key_enrollment_requires_private_one_time_token_and_is_idempotent(tmp_path):
    with ExecutionLedger(tmp_path / "execution.sqlite3", require_approval=True, workspace="uk") as ledger:
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
            approval.enroll_key(public_x963(key), b"correct", workspace="uk")
        os.chmod(token_path, 0o600)
        with pytest.raises(EnrollmentError, match="does not match"):
            approval.enroll_key(public_x963(key), b"wrong", workspace="uk")

        first = approval.enroll_key(public_x963(key), b"correct", workspace="uk")
        replay = approval.enroll_key(public_x963(key), b"token-is-gone", workspace="uk")
        assert replay == first
        assert not token_path.exists()
        ApprovalService(ledger)
        assert not token_path.exists()
        assert b"correct" not in ledger.path.read_bytes()
        with pytest.raises(EnrollmentError, match="rotation"):
            approval.enroll_key(public_x963(private_key()), b"anything", workspace="uk")


@pytest.mark.asyncio
async def test_signed_payload_is_exact_and_success_replays_only_same_evidence(tmp_path):
    clock = MutableClock()
    with ExecutionLedger(tmp_path / "execution.sqlite3", require_approval=True, workspace="uk") as ledger:
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
        challenge = service.create_approval_challenge("signed-1", workspace="uk", ttl_seconds=60)
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
            "replaces_proposal_id",
            "limit_price",
            "requote_id",
            "order_type",
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
        first = await service.approve_signed("signed-1", challenge.challenge_id, signature, workspace="uk")
        replay = await service.approve_signed("signed-1", challenge.challenge_id, signature, workspace="uk")

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
    with ExecutionLedger(tmp_path / "execution.sqlite3", require_approval=True, workspace="uk") as ledger:
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

        challenge = service.create_approval_challenge(intent.proposal_id, workspace="uk")
        wrong_signature = sign(private_key(), challenge.signed_payload)
        with pytest.raises(ApprovalVerificationError, match="invalid"):
            await service.approve_signed(
                intent.proposal_id,
                challenge.challenge_id,
                wrong_signature,
                workspace="uk",
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

    with ExecutionLedger(tmp_path / "execution.sqlite3", require_approval=True, workspace="uk") as ledger:
        approval = ApprovalService(ledger, clock=MutableClock())
        key = private_key()
        enroll(approval, key)
        admit_and_reserve(ledger, make_intent())
        challenge = approval.create_challenge("signed-1", workspace="uk")
        signature = sign(key, challenge.signed_payload)
        dispatcher = BlockingDispatcher()
        first_service = ExecutionService(
            dispatcher, ledger, require_approval=True, approval_service=approval
        )
        second_service = ExecutionService(
            dispatcher, ledger, require_approval=True, approval_service=approval
        )

        first = asyncio.create_task(
            first_service.approve_signed("signed-1", challenge.challenge_id, signature, workspace="uk")
        )
        await dispatcher.started.wait()
        with pytest.raises(ExecutionConflictError, match="before acknowledgement"):
            await second_service.approve_signed(
                "signed-1", challenge.challenge_id, signature, workspace="uk"
            )
        dispatcher.release.set()
        await first

        assert dispatcher.calls == 1
        assert ledger.approval_evidence_count("signed-1") == 1
        assert len(ledger.list_attempts("signed-1")) == 1


def test_expired_and_replay_before_ack_fail_without_duplicate_evidence(tmp_path):
    clock = MutableClock()
    with ExecutionLedger(tmp_path / "execution.sqlite3", require_approval=True, workspace="uk") as ledger:
        approval = ApprovalService(ledger, clock=clock)
        key = private_key()
        enroll(approval, key)
        admit_and_reserve(ledger, make_intent("expired"))
        expired = approval.create_challenge("expired", workspace="uk", ttl_seconds=5)
        expired_signature = sign(key, expired.signed_payload)
        clock.advance(seconds=6)
        with pytest.raises(ApprovalConflict, match="expired"):
            approval.approve_signed("expired", expired.challenge_id, expired_signature, workspace="uk")
        assert ledger.approval_evidence_count("expired") == 0

        admit_and_reserve(ledger, make_intent("in-flight"))
        current = approval.create_challenge("in-flight", workspace="uk", ttl_seconds=60)
        current_signature = sign(key, current.signed_payload)
        first = approval.approve_signed(
            "in-flight", current.challenge_id, current_signature, workspace="uk"
        )
        assert first.claimed
        with pytest.raises(ApprovalConflict, match="before acknowledgement"):
            approval.approve_signed(
                "in-flight", current.challenge_id, current_signature, workspace="uk"
            )
        assert ledger.approval_evidence_count("in-flight") == 1
        assert len(ledger.list_attempts("in-flight")) == 1


def test_approval_and_claim_roll_back_together_on_event_failure(tmp_path, monkeypatch):
    with ExecutionLedger(tmp_path / "execution.sqlite3", require_approval=True, workspace="uk") as ledger:
        approval = ApprovalService(ledger, clock=MutableClock())
        key = private_key()
        enroll(approval, key)
        admit_and_reserve(ledger, make_intent())
        challenge = approval.create_challenge("signed-1", workspace="uk")
        signature = sign(key, challenge.signed_payload)

        def fail_event(*_args, **_kwargs):
            raise RuntimeError("injected event failure")

        monkeypatch.setattr(ledger, "_append_event", fail_event)
        with pytest.raises(RuntimeError, match="injected"):
            approval.approve_signed("signed-1", challenge.challenge_id, signature, workspace="uk")
        assert ledger.get_order("signed-1").state == "PENDING"
        assert ledger.approval_evidence_count("signed-1") == 0
        assert ledger.list_attempts("signed-1") == []


def test_workspace_and_live_intents_fail_closed(tmp_path):
    with ExecutionLedger(tmp_path / "execution.sqlite3", workspace="uk") as ledger:
        with pytest.raises(IntentConflict, match="workspace"):
            ledger.register_intent(make_intent(workspace="india"))

    with ExecutionLedger(tmp_path / "live.sqlite3", require_approval=True, workspace="uk") as ledger:
        approval = ApprovalService(ledger)
        enroll(approval, private_key())
        ledger.register_intent(
            make_intent("live", broker="trading212", mode="LIVE")
        )
        with pytest.raises(ApprovalConflict, match="live execution"):
            approval.create_challenge("live", workspace="uk")


def test_v1_database_is_refused_until_operator_confirms_ownership(tmp_path):
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

    # Decision 4: ownership is never taken on open. The positive migration path
    # (order preserved, approval_id added, user_version 6) is covered through the
    # operator-confirmed tool in test_ledger_migration_v6.py.
    with pytest.raises(LedgerUnpinned):
        ExecutionLedger(db_path, workspace="uk")

    check = sqlite3.connect(db_path)
    try:
        assert check.execute("SELECT * FROM order_intents").fetchall() == [
            ("legacy", "growin-legacy", digest, snapshot, "before")
        ]
        assert check.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        check.close()


def test_approval_evidence_and_challenge_are_database_immutable(tmp_path):
    with ExecutionLedger(tmp_path / "execution.sqlite3", require_approval=True, workspace="uk") as ledger:
        approval = ApprovalService(ledger, clock=MutableClock())
        key = private_key()
        enroll(approval, key)
        admit_and_reserve(ledger, make_intent())
        challenge = approval.create_challenge("signed-1", workspace="uk")
        approval.approve_signed(
            "signed-1",
            challenge.challenge_id,
            sign(key, challenge.signed_payload),
            workspace="uk",
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


# --- cross-workspace approvals (ISO-02), real P-256 signatures throughout ---


def _workspace_stack(tmp_path, workspace: str, key):
    """A pinned ledger with its own enrolled key and one admitted, reserved order."""

    currency = "GBP" if workspace == "uk" else "INR"
    ledger = ExecutionLedger(
        tmp_path / f"{workspace}.sqlite3", require_approval=True, workspace=workspace
    )
    approval = ApprovalService(ledger, clock=MutableClock())
    token_path = approval.enrollment_token_path
    token_path.write_bytes(b"one-time-secret")
    os.chmod(token_path, 0o600)
    approval.enroll_key(public_x963(key), b"one-time-secret", workspace=workspace)
    guard = None
    overrides: dict = {}
    quote = None
    if workspace == "india":
        # 63-04: an India admission needs the Mac's India limits, a LIMIT order and a quote.
        import india_limits_support as ils

        (tmp_path / "india-config").mkdir()
        guard = ils.make_guard(ledger, ils.india_private_dir(tmp_path / "india-config"))
        overrides = {
            "ticker": ils.TICKER,
            "quantity": Decimal("2"),
            "order_type": "LIMIT",
            "limit_price": Decimal("100.00"),
        }
        quote = ils.make_evidence()
    service = ExecutionService(
        PaperDispatcher(),
        ledger,
        require_approval=True,
        approval_service=approval,
        india_guard=guard,
    )
    intent = make_intent(workspace=workspace, **overrides)
    service.admit(
        intent,
        currency=currency,
        price="100",
        simulator_evidence={"simulated_fill_price": "100"},
        risk_evidence={"scaled_size": str(intent.quantity)},
        india_quote=quote,
    )
    ledger.configure_paper_budget(intent.account, currency, "10000", workspace=workspace)
    service.reserve(intent.proposal_id)
    return ledger, approval, service


@pytest.mark.asyncio
async def test_challenge_and_signature_from_india_cannot_approve_on_uk(tmp_path):
    uk_key, india_key = private_key(), private_key()
    uk, uk_approval, uk_service = _workspace_stack(tmp_path, "uk", uk_key)
    india, india_approval, _ = _workspace_stack(tmp_path, "india", india_key)
    try:
        india_challenge = india_approval.create_challenge("signed-1", workspace="india")
        india_signature = sign(india_key, india_challenge.signed_payload)

        with pytest.raises(ExecutionConflictError):
            await uk_service.approve_signed(
                "signed-1", india_challenge.challenge_id, india_signature, workspace="uk"
            )

        assert uk.list_attempts("signed-1") == []
        assert uk.approval_evidence_count("signed-1") == 0
        assert uk.get_order("signed-1").state == "PENDING"
        assert india.list_attempts("signed-1") == []
    finally:
        uk.close()
        india.close()


def test_uk_key_signature_over_an_india_challenge_fails_india_verification(tmp_path):
    uk_key, india_key = private_key(), private_key()
    uk, _, _ = _workspace_stack(tmp_path, "uk", uk_key)
    india, india_approval, _ = _workspace_stack(tmp_path, "india", india_key)
    try:
        india_challenge = india_approval.create_challenge("signed-1", workspace="india")
        wrong_key_signature = sign(uk_key, india_challenge.signed_payload)

        with pytest.raises(ApprovalVerificationError, match="invalid"):
            india_approval.verify_signature(
                "signed-1",
                india_challenge.challenge_id,
                wrong_key_signature,
                workspace="india",
            )
        with pytest.raises(ApprovalVerificationError):
            india_approval.approve_signed(
                "signed-1",
                india_challenge.challenge_id,
                wrong_key_signature,
                workspace="india",
            )
        assert india.list_attempts("signed-1") == []
        assert india.approval_evidence_count("signed-1") == 0
    finally:
        uk.close()
        india.close()


def test_payload_with_altered_workspace_bytes_fails_even_when_correctly_signed(tmp_path):
    uk_key = private_key()
    uk, uk_approval, _ = _workspace_stack(tmp_path, "uk", uk_key)
    try:
        challenge = uk_approval.create_challenge("signed-1", workspace="uk")
        original = challenge.signed_payload
        assert b'"workspace":"uk"' in original
        altered = original.replace(b'"workspace":"uk"', b'"workspace":"india"')
        assert altered != original
        # The right UK key signs the altered bytes; the stored challenge still
        # holds the original bytes, so verification against it must fail.
        signature = sign(uk_key, altered)

        with pytest.raises(ApprovalVerificationError, match="invalid"):
            uk_approval.approve_signed(
                "signed-1", challenge.challenge_id, signature, workspace="uk"
            )

        assert uk.approval_evidence_count("signed-1") == 0
        assert uk.list_attempts("signed-1") == []
        assert uk.get_order("signed-1").state == "PENDING"
    finally:
        uk.close()


def test_key_calls_for_another_workspace_raise_and_enroll_nothing(tmp_path):
    with ExecutionLedger(
        tmp_path / "execution.sqlite3", require_approval=True, workspace="uk"
    ) as ledger:
        approval = ApprovalService(ledger)
        token = approval.enrollment_token_path.read_bytes()
        key = private_key()

        with pytest.raises(WorkspaceMismatch):
            ledger.get_approval_key(workspace="india")
        with pytest.raises(WorkspaceMismatch):
            ledger.register_approval_key("k", public_x963(key), workspace="india")
        with pytest.raises(WorkspaceMismatch):
            approval.enroll_key(public_x963(key), token, workspace="india")
        with pytest.raises(TypeError):
            ledger.get_approval_key()
        with pytest.raises(TypeError):
            ledger.register_approval_key("k", public_x963(key))
        with pytest.raises(TypeError):
            approval.enroll_key(public_x963(key), token)

        assert ledger.get_approval_key(workspace="uk") is None
        assert approval.enrollment_token_path.read_bytes() == token
