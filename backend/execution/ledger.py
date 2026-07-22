"""Restart-safe local execution ledger with single-writer authority.

The ledger deliberately owns persistence only.  Broker I/O must happen after
``claim`` returns, because that method commits the ``SUBMITTING`` transition
before returning to its caller.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

from .models import (
    AdmissionDecision,
    ExecutionAdmission,
    ExecutionAdmissionInput,
    OrderAck,
    OrderIntent,
    OrderSide,
    PaperBudget,
    PaperReservation,
    ReconciliationSnapshot,
    ReconciliationStatus,
    WorkspaceControl,
)


SCHEMA_VERSION = 3
_WORKSPACE_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_REASON_CODE_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class LedgerError(RuntimeError):
    """Base class for durable execution ledger failures."""


class LedgerWriterUnavailable(LedgerError):
    """Raised when another process already owns execution authority."""


class IntentConflict(LedgerError):
    """Raised when an identity is reused with a different immutable intent."""


class InvalidTransition(LedgerError):
    """Raised when an order cannot legally move from its current state."""


class OrderNotFound(LedgerError):
    """Raised when an order identity is absent from the ledger."""


class ApprovalConflict(LedgerError):
    """Raised when approval evidence is absent, stale, or inconsistent."""


class ApprovalKeyConflict(LedgerError):
    """Raised when approval-key enrollment would replace an active key."""


class ClaimStatus(str, Enum):
    CLAIMED = "CLAIMED"
    IN_PROGRESS = "IN_PROGRESS"
    REPLAY = "REPLAY"


@dataclass(frozen=True)
class LedgerOrder:
    proposal_id: str
    client_order_id: str
    intent_hash: str
    intent: Mapping[str, Any]
    state: str
    acknowledgment: Optional[OrderAck]
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class ClaimResult:
    status: ClaimStatus
    order: LedgerOrder
    attempt_id: Optional[int] = None

    @property
    def claimed(self) -> bool:
        return self.status is ClaimStatus.CLAIMED

    @property
    def is_replay(self) -> bool:
        return self.status is ClaimStatus.REPLAY


@dataclass(frozen=True)
class DispatchAttempt:
    attempt_id: int
    proposal_id: str
    state: str
    claimed_at: str
    completed_at: Optional[str]
    acknowledgment: Optional[OrderAck]


@dataclass(frozen=True)
class ExecutionEvent:
    event_id: int
    proposal_id: str
    event_type: str
    from_state: Optional[str]
    to_state: str
    payload: Mapping[str, Any]
    created_at: str


@dataclass(frozen=True)
class LedgerApprovalKey:
    workspace: str
    key_id: str
    public_key_x963: bytes
    created_at: str


@dataclass(frozen=True)
class LedgerApprovalChallenge:
    challenge_id: str
    proposal_id: str
    key_id: str
    intent_hash: str
    signed_payload: bytes
    issued_at_epoch: int
    expires_at_epoch: int


def default_ledger_path(workspace: str = "uk") -> Path:
    """Return the local macOS ledger path without creating it."""

    if not _WORKSPACE_PATTERN.fullmatch(workspace):
        raise ValueError("workspace must contain only letters, digits, '_' or '-'")
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "Growin"
        / "workspaces"
        / workspace
        / "execution.sqlite3"
    )


def canonical_json(value: Any) -> str:
    """Serialize a model or mapping deterministically for identity hashing."""

    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if not isinstance(value, Mapping):
        raise TypeError("canonical values must be mappings or Pydantic models")
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def intent_hash(intent: OrderIntent) -> str:
    return hashlib.sha256(canonical_json(intent).encode("utf-8")).hexdigest()


class ExecutionLedger:
    """SQLite-backed execution authority for one local workspace."""

    def __init__(
        self,
        path: os.PathLike[str] | str | None = None,
        *,
        workspace: str = "uk",
        busy_timeout_ms: int = 5_000,
        require_approval: bool = False,
    ) -> None:
        if not 1 <= busy_timeout_ms <= 60_000:
            raise ValueError("busy_timeout_ms must be between 1 and 60000")

        if not _WORKSPACE_PATTERN.fullmatch(workspace):
            raise ValueError("workspace must contain only letters, digits, '_' or '-'")
        self.workspace = workspace
        self.require_approval = require_approval
        self.path = Path(path) if path is not None else default_ledger_path(workspace)
        if self.path.exists() and self.path.is_symlink():
            raise LedgerError("ledger path must not be a symbolic link")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")
        if self.lock_path.exists() and self.lock_path.is_symlink():
            raise LedgerError("ledger lock path must not be a symbolic link")

        self._mutex = threading.RLock()
        self._connection: Optional[sqlite3.Connection] = None
        self._lock_fd: Optional[int] = None
        try:
            self._acquire_writer_lock()
            self._connection = sqlite3.connect(
                self.path,
                timeout=busy_timeout_ms / 1_000,
                isolation_level=None,
                check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
            self._configure(busy_timeout_ms)
            self._create_schema()
            os.chmod(self.path, 0o600)
            self.recover_abandoned_submissions()
        except Exception:
            self.close()
            raise

    def __enter__(self) -> "ExecutionLedger":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _acquire_writer_lock(self) -> None:
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        fd = os.open(self.lock_path, flags, 0o600)
        os.chmod(self.lock_path, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EACCES, errno.EAGAIN):
                raise LedgerWriterUnavailable(
                    f"execution writer is already active for {self.path}"
                ) from None
            raise
        self._lock_fd = fd

    def _configure(self, busy_timeout_ms: int) -> None:
        connection = self._require_connection()
        connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms:d}")
        mode = connection.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise LedgerError("SQLite WAL mode is unavailable")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA foreign_keys = ON")

    def _create_schema(self) -> None:
        current_version = int(
            self._require_connection().execute("PRAGMA user_version").fetchone()[0]
        )
        if current_version > SCHEMA_VERSION:
            raise LedgerError(
                f"ledger schema {current_version} is newer than supported version "
                f"{SCHEMA_VERSION}"
            )
        statements = (
            """
            CREATE TABLE IF NOT EXISTS order_intents (
                proposal_id TEXT PRIMARY KEY,
                client_order_id TEXT NOT NULL UNIQUE,
                intent_hash TEXT NOT NULL,
                canonical_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS order_projection (
                proposal_id TEXT PRIMARY KEY REFERENCES order_intents(proposal_id),
                state TEXT NOT NULL,
                acknowledgment_json TEXT,
                rejection_notes TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS dispatch_attempts (
                attempt_id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposal_id TEXT NOT NULL UNIQUE REFERENCES order_intents(proposal_id),
                approval_id TEXT REFERENCES execution_approvals(approval_id),
                state TEXT NOT NULL,
                claimed_at TEXT NOT NULL,
                completed_at TEXT,
                acknowledgment_json TEXT
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS approval_keys (
                workspace TEXT NOT NULL,
                key_id TEXT NOT NULL,
                public_key_x963 BLOB NOT NULL CHECK(length(public_key_x963) = 65),
                created_at TEXT NOT NULL,
                PRIMARY KEY (workspace, key_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS approval_challenges (
                challenge_id TEXT PRIMARY KEY,
                proposal_id TEXT NOT NULL REFERENCES order_intents(proposal_id),
                workspace TEXT NOT NULL,
                key_id TEXT NOT NULL,
                intent_hash TEXT NOT NULL,
                signed_payload BLOB NOT NULL,
                issued_at_epoch INTEGER NOT NULL,
                expires_at_epoch INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (workspace, key_id)
                    REFERENCES approval_keys(workspace, key_id)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS execution_approvals (
                approval_id TEXT PRIMARY KEY,
                challenge_id TEXT NOT NULL UNIQUE
                    REFERENCES approval_challenges(challenge_id),
                proposal_id TEXT NOT NULL UNIQUE
                    REFERENCES order_intents(proposal_id),
                workspace TEXT NOT NULL,
                key_id TEXT NOT NULL,
                intent_hash TEXT NOT NULL,
                signed_payload_hash TEXT NOT NULL,
                signature_der BLOB NOT NULL,
                approved_at TEXT NOT NULL,
                FOREIGN KEY (workspace, key_id)
                    REFERENCES approval_keys(workspace, key_id)
            )
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS dispatch_attempt_approval_unique
            ON dispatch_attempts(approval_id) WHERE approval_id IS NOT NULL
            """,
            """
            CREATE TABLE IF NOT EXISTS execution_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposal_id TEXT NOT NULL REFERENCES order_intents(proposal_id),
                event_type TEXT NOT NULL,
                from_state TEXT,
                to_state TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS execution_admissions (
                proposal_id TEXT PRIMARY KEY REFERENCES order_intents(proposal_id),
                intent_hash TEXT NOT NULL,
                workspace TEXT NOT NULL,
                account TEXT NOT NULL,
                currency TEXT NOT NULL,
                ticker TEXT NOT NULL,
                side TEXT NOT NULL,
                original_quantity TEXT NOT NULL,
                final_quantity TEXT NOT NULL,
                price TEXT NOT NULL,
                notional TEXT NOT NULL,
                simulator_fill_price TEXT NOT NULL,
                simulator_drawdown_pct TEXT NOT NULL,
                risk_quantity TEXT NOT NULL,
                current_spread_pct TEXT NOT NULL,
                evidence_at TEXT NOT NULL,
                evidence_hash TEXT NOT NULL,
                decision TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                created_at TEXT NOT NULL,
                CHECK (decision IN ('ADMITTED', 'DENIED'))
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS paper_budgets (
                workspace TEXT NOT NULL,
                account TEXT NOT NULL,
                currency TEXT NOT NULL,
                amount TEXT NOT NULL,
                reserved TEXT NOT NULL DEFAULT '0',
                consumed TEXT NOT NULL DEFAULT '0',
                released TEXT NOT NULL DEFAULT '0',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (workspace, account, currency)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS buying_power_reservations (
                proposal_id TEXT PRIMARY KEY REFERENCES order_intents(proposal_id),
                workspace TEXT NOT NULL,
                account TEXT NOT NULL,
                currency TEXT NOT NULL,
                intent_hash TEXT NOT NULL,
                reserved TEXT NOT NULL,
                consumed TEXT NOT NULL DEFAULT '0',
                released TEXT NOT NULL DEFAULT '0',
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS workspace_controls (
                workspace TEXT PRIMARY KEY,
                engaged INTEGER NOT NULL DEFAULT 0 CHECK (engaged IN (0, 1)),
                version INTEGER NOT NULL DEFAULT 0,
                reason_code TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS workspace_control_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace TEXT NOT NULL,
                version INTEGER NOT NULL,
                engaged INTEGER NOT NULL CHECK (engaged IN (0, 1)),
                purpose TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                evidence_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (workspace, version)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS reconciliation_evidence (
                evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
                proposal_id TEXT NOT NULL REFERENCES order_intents(proposal_id),
                broker_order_id TEXT NOT NULL,
                source TEXT NOT NULL,
                cumulative_quantity TEXT NOT NULL,
                cumulative_notional TEXT NOT NULL,
                status TEXT NOT NULL,
                evidence_fingerprint TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (proposal_id, evidence_fingerprint)
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS paper_positions (
                workspace TEXT NOT NULL,
                account TEXT NOT NULL,
                currency TEXT NOT NULL,
                ticker TEXT NOT NULL,
                quantity TEXT NOT NULL DEFAULT '0',
                notional TEXT NOT NULL DEFAULT '0',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (workspace, account, currency, ticker)
            )
            """,
            """
            CREATE TRIGGER IF NOT EXISTS order_intents_no_update
            BEFORE UPDATE ON order_intents
            BEGIN
                SELECT RAISE(ABORT, 'order_intents are immutable');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS order_intents_no_delete
            BEFORE DELETE ON order_intents
            BEGIN
                SELECT RAISE(ABORT, 'order_intents are immutable');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS execution_events_no_update
            BEFORE UPDATE ON execution_events
            BEGIN
                SELECT RAISE(ABORT, 'execution_events are append-only');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS execution_events_no_delete
            BEFORE DELETE ON execution_events
            BEGIN
                SELECT RAISE(ABORT, 'execution_events are append-only');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS approval_keys_no_update
            BEFORE UPDATE ON approval_keys
            BEGIN
                SELECT RAISE(ABORT, 'approval_keys are immutable');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS approval_keys_no_delete
            BEFORE DELETE ON approval_keys
            BEGIN
                SELECT RAISE(ABORT, 'approval_keys are immutable');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS approval_challenges_no_update
            BEFORE UPDATE ON approval_challenges
            BEGIN
                SELECT RAISE(ABORT, 'approval_challenges are immutable');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS approval_challenges_no_delete
            BEFORE DELETE ON approval_challenges
            BEGIN
                SELECT RAISE(ABORT, 'approval_challenges are immutable');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS execution_approvals_no_update
            BEFORE UPDATE ON execution_approvals
            BEGIN
                SELECT RAISE(ABORT, 'execution_approvals are immutable');
            END
            """,
            """
            CREATE TRIGGER IF NOT EXISTS execution_approvals_no_delete
            BEFORE DELETE ON execution_approvals
            BEGIN
                SELECT RAISE(ABORT, 'execution_approvals are immutable');
            END
            """,
        )
        with self._transaction() as connection:
            if current_version == 1:
                columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(dispatch_attempts)"
                    ).fetchall()
                }
                if "approval_id" not in columns:
                    connection.execute(
                        "ALTER TABLE dispatch_attempts ADD COLUMN approval_id TEXT "
                        "REFERENCES execution_approvals(approval_id)"
                    )
            for statement in statements:
                connection.execute(statement)
            if current_version < 3:
                budget_columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(paper_budgets)"
                    ).fetchall()
                }
                if budget_columns and "reserved" not in budget_columns:
                    connection.execute(
                        "ALTER TABLE paper_budgets ADD COLUMN reserved TEXT NOT NULL DEFAULT '0'"
                    )
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION:d}")

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._mutex:
            connection = self._require_connection()
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            else:
                connection.commit()

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise LedgerError("execution ledger is closed")
        return self._connection

    def close(self) -> None:
        with self._mutex:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
            if self._lock_fd is not None:
                try:
                    fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(self._lock_fd)
                    self._lock_fd = None

    def register_intent(self, intent: OrderIntent) -> LedgerOrder:
        """Persist an immutable intent, or return its exact prior registration."""

        if intent.workspace != self.workspace:
            raise IntentConflict("intent workspace does not match ledger workspace")
        snapshot, digest, proposal_id, client_order_id = _intent_identity(intent)
        now = _now()
        with self._transaction() as connection:
            row = self._find_identity(connection, proposal_id, client_order_id)
            if row is not None:
                self._assert_same_intent(row, proposal_id, client_order_id, digest)
                return self._order_from_row(row)

            connection.execute(
                """
                INSERT INTO order_intents
                    (proposal_id, client_order_id, intent_hash, canonical_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (proposal_id, client_order_id, digest, snapshot, now),
            )
            connection.execute(
                """
                INSERT INTO order_projection
                    (proposal_id, state, created_at, updated_at)
                VALUES (?, 'PENDING', ?, ?)
                """,
                (proposal_id, now, now),
            )
            self._append_event(
                connection, proposal_id, "INTENT_CREATED", None, "PENDING", {}, now
            )
            return self._get_order_locked(connection, proposal_id)

    create_intent = register_intent

    def record_admission(
        self, intent: OrderIntent, admission: ExecutionAdmission
    ) -> ExecutionAdmission:
        """Persist one immutable admission decision for an exact intent hash."""

        if intent.workspace != self.workspace:
            raise IntentConflict("intent workspace does not match ledger workspace")
        _, digest, proposal_id, _ = _intent_identity(intent)
        if admission.proposal_id != proposal_id or admission.intent_hash != digest:
            raise IntentConflict("admission does not match the immutable intent")
        now = _now()
        payload = admission.model_dump(mode="json")
        with self._transaction() as connection:
            order = self._select_order(connection, proposal_id)
            if order is None:
                raise OrderNotFound(f"order {proposal_id!r} was not found")
            row = connection.execute(
                "SELECT * FROM execution_admissions WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            if row is not None:
                existing = self._admission_from_row(row)
                if existing.model_dump(mode="json") != payload:
                    raise ApprovalConflict("admission evidence is immutable")
                return existing
            connection.execute(
                """
                INSERT INTO execution_admissions
                    (proposal_id, intent_hash, workspace, account, currency, ticker,
                     side, original_quantity, final_quantity, price, notional,
                     simulator_fill_price, simulator_drawdown_pct, risk_quantity,
                     current_spread_pct, evidence_at, evidence_hash, decision,
                     reason_code, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    proposal_id,
                    digest,
                    admission.workspace,
                    admission.account,
                    admission.currency,
                    admission.ticker,
                    admission.side.value,
                    _decimal_str(admission.original_quantity),
                    _decimal_str(admission.final_quantity),
                    _decimal_str(admission.price),
                    _decimal_str(admission.notional),
                    _decimal_str(admission.simulator_fill_price),
                    _decimal_str(admission.simulator_drawdown_pct),
                    _decimal_str(admission.risk_quantity),
                    _decimal_str(admission.current_spread_pct),
                    admission.evidence_at.isoformat(),
                    admission.evidence_hash,
                    admission.decision.value,
                    admission.reason_code,
                    now,
                ),
            )
            self._append_event(
                connection,
                proposal_id,
                "ADMISSION_DECIDED",
                str(order["state"]),
                str(order["state"]),
                {
                    "decision": admission.decision.value,
                    "reason_code": admission.reason_code,
                    "intent_hash": digest,
                    "evidence_hash": admission.evidence_hash,
                    "final_quantity": _decimal_str(admission.final_quantity),
                    "notional": _decimal_str(admission.notional),
                },
                now,
            )
            row = connection.execute(
                "SELECT * FROM execution_admissions WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            if row is None:
                raise LedgerError("admission did not persist")
            return self._admission_from_row(row)

    def get_admission(self, proposal_id: str) -> Optional[ExecutionAdmission]:
        with self._mutex:
            row = self._require_connection().execute(
                "SELECT * FROM execution_admissions WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
        return self._admission_from_row(row) if row is not None else None

    def configure_paper_budget(
        self, account: str, currency: str, amount: Decimal | str | int | float
    ) -> PaperBudget:
        amount_decimal = _positive_decimal(amount, "budget amount")
        now = _now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM paper_budgets WHERE workspace = ? AND account = ? AND currency = ?",
                (self.workspace, account, currency),
            ).fetchone()
            if row is not None:
                if _decimal(row["amount"]) != amount_decimal:
                    raise ApprovalConflict("paper budget is immutable once configured")
                return self._budget_from_row(row)
            connection.execute(
                """
                INSERT INTO paper_budgets
                    (workspace, account, currency, amount, reserved, consumed, released, created_at, updated_at)
                VALUES (?, ?, ?, ?, '0', '0', '0', ?, ?)
                """,
                (self.workspace, account, currency, _decimal_str(amount_decimal), now, now),
            )
            row = connection.execute(
                "SELECT * FROM paper_budgets WHERE workspace = ? AND account = ? AND currency = ?",
                (self.workspace, account, currency),
            ).fetchone()
            if row is None:
                raise LedgerError("paper budget did not persist")
            return self._budget_from_row(row)

    def get_paper_budget(self, account: str, currency: str) -> Optional[PaperBudget]:
        with self._mutex:
            row = self._require_connection().execute(
                "SELECT * FROM paper_budgets WHERE workspace = ? AND account = ? AND currency = ?",
                (self.workspace, account, currency),
            ).fetchone()
        return self._budget_from_row(row) if row is not None else None

    def reserve_buying_power(self, proposal_id: str) -> PaperReservation:
        """Atomically check budget and reserve an admitted BUY notional."""

        now = _now()
        with self._transaction() as connection:
            order = self._select_order(connection, proposal_id)
            if order is None:
                raise OrderNotFound(f"order {proposal_id!r} was not found")
            intent = json.loads(str(order["canonical_json"]))
            admission_row = connection.execute(
                "SELECT * FROM execution_admissions WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            if admission_row is None:
                raise ApprovalConflict("execution admission is required before reservation")
            admission = self._admission_from_row(admission_row)
            if admission.decision is not AdmissionDecision.ADMITTED:
                raise ApprovalConflict("execution admission denied the intent")
            if admission.intent_hash != str(order["intent_hash"]):
                raise IntentConflict("admission intent hash does not match stored intent")
            if admission.workspace != self.workspace or admission.workspace != str(intent["workspace"]):
                raise IntentConflict("reservation workspace does not match ledger workspace")
            if admission.side is not OrderSide.BUY:
                raise ApprovalConflict("SELL reservation is not supported")
            if self._workspace_engaged_locked(connection):
                raise ApprovalConflict("workspace execution control is engaged")
            existing_row = connection.execute(
                "SELECT * FROM buying_power_reservations WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            if existing_row is not None:
                existing = self._reservation_from_row(existing_row)
                if existing.intent_hash != admission.intent_hash:
                    raise IntentConflict("reservation intent hash does not match")
                return existing
            budget_row = connection.execute(
                "SELECT * FROM paper_budgets WHERE workspace = ? AND account = ? AND currency = ?",
                (self.workspace, admission.account, admission.currency),
            ).fetchone()
            if budget_row is None:
                raise ApprovalConflict("explicit paper budget is required")
            budget = self._budget_from_row(budget_row)
            if budget.available < admission.notional:
                raise ApprovalConflict("paper budget is insufficient")
            connection.execute(
                """
                INSERT INTO buying_power_reservations
                    (proposal_id, workspace, account, currency, intent_hash, reserved,
                     state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
                """,
                (
                    proposal_id,
                    self.workspace,
                    admission.account,
                    admission.currency,
                    admission.intent_hash,
                    _decimal_str(admission.notional),
                    now,
                    now,
                ),
            )
            connection.execute(
                "UPDATE paper_budgets SET reserved = ?, updated_at = ? WHERE workspace = ? AND account = ? AND currency = ?",
                (
                    _decimal_str(_decimal(budget_row["reserved"]) + admission.notional),
                    now,
                    self.workspace,
                    admission.account,
                    admission.currency,
                ),
            )
            self._append_event(
                connection,
                proposal_id,
                "BUYING_POWER_RESERVED",
                str(order["state"]),
                str(order["state"]),
                {
                    "workspace": self.workspace,
                    "account": admission.account,
                    "currency": admission.currency,
                    "reserved": _decimal_str(admission.notional),
                    "intent_hash": admission.intent_hash,
                },
                now,
            )
            row = connection.execute(
                "SELECT * FROM buying_power_reservations WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            if row is None:
                raise LedgerError("reservation did not persist")
            return self._reservation_from_row(row)

    def get_reservation(self, proposal_id: str) -> Optional[PaperReservation]:
        with self._mutex:
            row = self._require_connection().execute(
                "SELECT * FROM buying_power_reservations WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
        return self._reservation_from_row(row) if row is not None else None

    def engage_workspace_control(
        self, reason_code: str = "MANUAL_KILL", *, actor: str = "local", evidence_id: str = ""
    ) -> WorkspaceControl:
        if not _REASON_CODE_PATTERN.fullmatch(reason_code):
            raise ValueError("reason_code must be a short, non-sensitive code")
        now = _now()
        evidence_id = evidence_id or str(uuid.uuid4())
        with self._transaction() as connection:
            current = self._workspace_control_locked(connection)
            if current.engaged:
                return current
            version = current.version + 1
            connection.execute(
                """
                INSERT INTO workspace_controls(workspace, engaged, version, reason_code, updated_at)
                VALUES (?, 1, ?, ?, ?)
                ON CONFLICT(workspace) DO UPDATE SET engaged=1, version=excluded.version,
                    reason_code=excluded.reason_code, updated_at=excluded.updated_at
                """,
                (self.workspace, version, reason_code, now),
            )
            connection.execute(
                """
                INSERT INTO workspace_control_events
                    (workspace, version, engaged, purpose, reason_code, evidence_id, created_at)
                VALUES (?, ?, 1, ?, ?, ?, ?)
                """,
                (self.workspace, version, "ENGAGE", reason_code, evidence_id, now),
            )
            return self._workspace_control_locked(connection)

    def get_workspace_control(self) -> WorkspaceControl:
        with self._mutex:
            return self._workspace_control_locked(self._require_connection())

    def clear_workspace_control(
        self, *, version: int, evidence_id: str, purpose: str = "growin.execution.control.clear"
    ) -> WorkspaceControl:
        if purpose != "growin.execution.control.clear":
            raise ApprovalConflict("control-clear purpose is invalid")
        now = _now()
        with self._transaction() as connection:
            current = self._workspace_control_locked(connection)
            if not current.engaged:
                return current
            if current.version != version:
                raise ApprovalConflict("workspace control version is stale")
            next_version = current.version + 1
            connection.execute(
                """
                INSERT INTO workspace_controls(workspace, engaged, version, reason_code, updated_at)
                VALUES (?, 0, ?, '', ?)
                ON CONFLICT(workspace) DO UPDATE SET engaged=0, version=excluded.version,
                    reason_code='', updated_at=excluded.updated_at
                """,
                (self.workspace, next_version, now),
            )
            connection.execute(
                """
                INSERT INTO workspace_control_events
                    (workspace, version, engaged, purpose, reason_code, evidence_id, created_at)
                VALUES (?, ?, 0, ?, '', ?, ?)
                """,
                (self.workspace, next_version, purpose, evidence_id, now),
            )
            return self._workspace_control_locked(connection)

    def register_approval_key(
        self, key_id: str, public_key_x963: bytes
    ) -> LedgerApprovalKey:
        """Enroll the first workspace key; replacement requires a later phase."""

        if not key_id or len(public_key_x963) != 65:
            raise ValueError("a key id and 65-byte X9.63 public key are required")
        now = _now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM approval_keys WHERE workspace = ?",
                (self.workspace,),
            ).fetchone()
            if row is not None:
                if str(row["key_id"]) != key_id or bytes(row["public_key_x963"]) != bytes(
                    public_key_x963
                ):
                    raise ApprovalKeyConflict("approval key rotation is not enabled")
                return self._approval_key_from_row(row)
            connection.execute(
                """
                INSERT INTO approval_keys
                    (workspace, key_id, public_key_x963, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (self.workspace, key_id, bytes(public_key_x963), now),
            )
            row = connection.execute(
                "SELECT * FROM approval_keys WHERE workspace = ? AND key_id = ?",
                (self.workspace, key_id),
            ).fetchone()
            if row is None:
                raise LedgerError("approval key enrollment did not persist")
            return self._approval_key_from_row(row)

    def get_approval_key(self) -> Optional[LedgerApprovalKey]:
        with self._mutex:
            row = self._require_connection().execute(
                "SELECT * FROM approval_keys WHERE workspace = ?",
                (self.workspace,),
            ).fetchone()
        return self._approval_key_from_row(row) if row is not None else None

    def store_approval_challenge(
        self,
        *,
        challenge_id: str,
        proposal_id: str,
        key_id: str,
        intent_hash: str,
        signed_payload: bytes,
        issued_at_epoch: int,
        expires_at_epoch: int,
    ) -> LedgerApprovalChallenge:
        if expires_at_epoch <= issued_at_epoch:
            raise ValueError("approval challenge expiry must follow issuance")
        now = _now()
        with self._transaction() as connection:
            order = self._select_order(connection, proposal_id)
            if order is None:
                raise OrderNotFound(f"order {proposal_id!r} was not found")
            intent = json.loads(str(order["canonical_json"]))
            if str(intent.get("workspace")) != self.workspace:
                raise ApprovalConflict("order workspace does not match ledger workspace")
            if str(order["state"]) != "PENDING":
                raise InvalidTransition(f"order is already {order['state']}")
            if str(order["intent_hash"]) != intent_hash:
                raise ApprovalConflict("challenge intent hash does not match stored intent")
            admission = connection.execute(
                "SELECT decision, intent_hash FROM execution_admissions WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            if admission is None or str(admission["decision"]) != AdmissionDecision.ADMITTED.value:
                raise ApprovalConflict("admitted evidence is required before approval")
            if str(admission["intent_hash"]) != str(order["intent_hash"]):
                raise ApprovalConflict("approval admission does not match the immutable intent")
            reservation = connection.execute(
                "SELECT state, intent_hash FROM buying_power_reservations WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            if reservation is None or str(reservation["state"]) != "ACTIVE":
                raise ApprovalConflict("active paper reservation is required before approval")
            if str(reservation["intent_hash"]) != str(order["intent_hash"]):
                raise ApprovalConflict("approval reservation does not match the immutable intent")
            if self._workspace_engaged_locked(connection):
                raise ApprovalConflict("workspace execution control is engaged")
            key = connection.execute(
                "SELECT 1 FROM approval_keys WHERE workspace = ? AND key_id = ?",
                (self.workspace, key_id),
            ).fetchone()
            if key is None:
                raise ApprovalConflict("approval signer is not enrolled")
            connection.execute(
                """
                INSERT INTO approval_challenges
                    (challenge_id, proposal_id, workspace, key_id, intent_hash,
                     signed_payload, issued_at_epoch, expires_at_epoch, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    challenge_id,
                    proposal_id,
                    self.workspace,
                    key_id,
                    intent_hash,
                    bytes(signed_payload),
                    issued_at_epoch,
                    expires_at_epoch,
                    now,
                ),
            )
            self._append_event(
                connection,
                proposal_id,
                "APPROVAL_CHALLENGE_CREATED",
                "PENDING",
                "PENDING",
                {
                    "challenge_id": challenge_id,
                    "key_id": key_id,
                    "expires_at_epoch": expires_at_epoch,
                },
                now,
            )
        challenge = self.get_approval_challenge(challenge_id)
        if challenge is None:
            raise LedgerError("approval challenge did not persist")
        return challenge

    def get_approval_challenge(
        self, challenge_id: str
    ) -> Optional[LedgerApprovalChallenge]:
        with self._mutex:
            row = self._require_connection().execute(
                "SELECT * FROM approval_challenges WHERE challenge_id = ?",
                (challenge_id,),
            ).fetchone()
        if row is None:
            return None
        return LedgerApprovalChallenge(
            challenge_id=str(row["challenge_id"]),
            proposal_id=str(row["proposal_id"]),
            key_id=str(row["key_id"]),
            intent_hash=str(row["intent_hash"]),
            signed_payload=bytes(row["signed_payload"]),
            issued_at_epoch=int(row["issued_at_epoch"]),
            expires_at_epoch=int(row["expires_at_epoch"]),
        )

    def claim_with_approval(
        self,
        *,
        proposal_id: str,
        challenge_id: str,
        key_id: str,
        signature_der: bytes,
        verified_payload_hash: str,
        now_epoch: int,
    ) -> ClaimResult:
        """Atomically consume verified evidence and establish dispatch authority."""

        now = _now()
        with self._transaction() as connection:
            row = self._select_order(connection, proposal_id)
            if row is None:
                raise OrderNotFound(f"order {proposal_id!r} was not found")
            evidence = connection.execute(
                "SELECT * FROM execution_approvals WHERE challenge_id = ?",
                (challenge_id,),
            ).fetchone()
            if evidence is not None:
                same_evidence = (
                    str(evidence["proposal_id"]) == proposal_id
                    and str(evidence["key_id"]) == key_id
                    and str(evidence["signed_payload_hash"]) == verified_payload_hash
                    and bytes(evidence["signature_der"]) == bytes(signature_der)
                )
                if not same_evidence:
                    raise ApprovalConflict("approval challenge was already consumed")
                if str(row["state"]) in {"ACKNOWLEDGED", "APPROVED"} and row[
                    "acknowledgment_json"
                ]:
                    return ClaimResult(ClaimStatus.REPLAY, self._order_from_row(row))
                raise ApprovalConflict("approval was consumed before acknowledgement")

            challenge = connection.execute(
                "SELECT * FROM approval_challenges WHERE challenge_id = ?",
                (challenge_id,),
            ).fetchone()
            if challenge is None:
                raise ApprovalConflict("approval challenge was not found")
            payload_hash = hashlib.sha256(bytes(challenge["signed_payload"])).hexdigest()
            intent_bytes = str(row["canonical_json"]).encode("utf-8")
            stored_intent_hash = hashlib.sha256(intent_bytes).hexdigest()
            intent = json.loads(intent_bytes)
            try:
                signed_payload = json.loads(bytes(challenge["signed_payload"]))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ApprovalConflict("approval payload is not canonical JSON") from exc
            expected_payload = {
                "version": 1,
                "purpose": "growin.execution.dispatch",
                "challenge_id": challenge_id,
                "proposal_id": proposal_id,
                "client_order_id": str(row["client_order_id"]),
                "intent_hash": stored_intent_hash,
                "workspace": intent.get("workspace"),
                "account": intent.get("account"),
                "broker": intent.get("broker"),
                "mode": intent.get("mode"),
                "ticker": intent.get("ticker"),
                "side": intent.get("side"),
                "quantity": intent.get("quantity"),
                "issued_at": int(challenge["issued_at_epoch"]),
                "expires_at": int(challenge["expires_at_epoch"]),
                "key_id": key_id,
            }
            if not isinstance(signed_payload, Mapping) or any(
                signed_payload.get(field) != value
                for field, value in expected_payload.items()
            ):
                raise ApprovalConflict("approval payload does not match stored intent")
            if not isinstance(signed_payload.get("nonce"), str) or not signed_payload[
                "nonce"
            ]:
                raise ApprovalConflict("approval payload nonce is invalid")
            if canonical_json(signed_payload).encode("utf-8") != bytes(
                challenge["signed_payload"]
            ):
                raise ApprovalConflict("approval payload bytes are not canonical")
            if (
                str(challenge["proposal_id"]) != proposal_id
                or str(challenge["workspace"]) != self.workspace
                or str(challenge["key_id"]) != key_id
                or str(challenge["intent_hash"]) != stored_intent_hash
                or str(row["intent_hash"]) != stored_intent_hash
                or payload_hash != verified_payload_hash
            ):
                raise ApprovalConflict("approval does not match the immutable intent")
            if str(intent.get("workspace")) != self.workspace:
                raise ApprovalConflict("approval workspace does not match ledger workspace")
            if str(intent.get("mode", "")).upper() != "PAPER":
                raise ApprovalConflict("live execution remains disabled")
            admission = connection.execute(
                "SELECT * FROM execution_admissions WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            if admission is None or str(admission["decision"]) != AdmissionDecision.ADMITTED.value:
                raise ApprovalConflict("admitted evidence is required before signed claim")
            if str(admission["intent_hash"]) != stored_intent_hash:
                raise ApprovalConflict("signed claim admission does not match the immutable intent")
            if (
                str(signed_payload.get("admitted_quantity", "")) != str(admission["final_quantity"])
                or str(signed_payload.get("currency", "")) != str(admission["currency"])
                or str(signed_payload.get("price", "")) != str(admission["price"])
                or str(signed_payload.get("notional", "")) != str(admission["notional"])
                or str(signed_payload.get("evidence_hash", "")) != str(admission["evidence_hash"])
            ):
                raise ApprovalConflict("approval evidence does not match admitted quantity")
            reservation = connection.execute(
                "SELECT * FROM buying_power_reservations WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            if reservation is None or str(reservation["state"]) != "ACTIVE":
                raise ApprovalConflict("active paper reservation is required before signed claim")
            if str(reservation["intent_hash"]) != stored_intent_hash:
                raise ApprovalConflict("signed claim reservation does not match the immutable intent")
            if self._workspace_engaged_locked(connection):
                raise ApprovalConflict("workspace execution control is engaged")
            if now_epoch >= int(challenge["expires_at_epoch"]):
                raise ApprovalConflict("approval challenge has expired")
            signer = connection.execute(
                "SELECT 1 FROM approval_keys WHERE workspace = ? AND key_id = ?",
                (self.workspace, key_id),
            ).fetchone()
            if signer is None:
                raise ApprovalConflict("approval signer is not enrolled")
            state = str(row["state"])
            if state != "PENDING":
                raise InvalidTransition(f"order is already {state}")

            approval_id = str(uuid.uuid4())
            try:
                connection.execute(
                    """
                    INSERT INTO execution_approvals
                        (approval_id, challenge_id, proposal_id, workspace, key_id,
                         intent_hash, signed_payload_hash, signature_der, approved_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        approval_id,
                        challenge_id,
                        proposal_id,
                        self.workspace,
                        key_id,
                        stored_intent_hash,
                        verified_payload_hash,
                        bytes(signature_der),
                        now,
                    ),
                )
                cursor = connection.execute(
                    """
                    INSERT INTO dispatch_attempts
                        (proposal_id, approval_id, state, claimed_at)
                    VALUES (?, ?, 'SUBMITTING', ?)
                    """,
                    (proposal_id, approval_id, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ApprovalConflict("approval or dispatch was already claimed") from exc
            updated = connection.execute(
                """
                UPDATE order_projection
                SET state = 'SUBMITTING', updated_at = ?
                WHERE proposal_id = ? AND state = 'PENDING'
                """,
                (now, proposal_id),
            )
            if updated.rowcount != 1:
                raise ApprovalConflict("order dispatch claim was lost")
            self._append_event(
                connection,
                proposal_id,
                "HUMAN_APPROVAL_VERIFIED",
                "PENDING",
                "PENDING",
                {
                    "approval_id": approval_id,
                    "challenge_id": challenge_id,
                    "key_id": key_id,
                    "intent_hash": stored_intent_hash,
                },
                now,
            )
            self._append_event(
                connection,
                proposal_id,
                "DISPATCH_CLAIMED",
                "PENDING",
                "SUBMITTING",
                {"approval_id": approval_id},
                now,
            )
            return ClaimResult(
                ClaimStatus.CLAIMED,
                self._get_order_locked(connection, proposal_id),
                int(cursor.lastrowid),
            )

    def claim(
        self, proposal_id: str, intent: Optional[OrderIntent] = None
    ) -> ClaimResult:
        """Atomically claim one order and commit before broker dispatch."""

        if self.require_approval:
            raise ApprovalConflict("signed approval is required before dispatch")
        if not proposal_id:
            raise ValueError("proposal_id is required")
        if intent is not None and intent.workspace != self.workspace:
            raise IntentConflict("intent workspace does not match ledger workspace")
        supplied_identity = _intent_identity(intent) if intent is not None else None
        if supplied_identity is not None and supplied_identity[2] != proposal_id:
            raise IntentConflict("proposal_id does not match the supplied intent")

        now = _now()
        with self._transaction() as connection:
            row = self._select_order(connection, proposal_id)
            if row is None:
                if supplied_identity is None:
                    raise OrderNotFound(f"order {proposal_id!r} was not found")
                snapshot, digest, _, client_order_id = supplied_identity
                identity_row = self._find_identity(
                    connection, proposal_id, client_order_id
                )
                if identity_row is not None:
                    self._assert_same_intent(
                        identity_row, proposal_id, client_order_id, digest
                    )
                    row = identity_row
                else:
                    connection.execute(
                        """
                        INSERT INTO order_intents
                            (proposal_id, client_order_id, intent_hash,
                             canonical_json, created_at)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (proposal_id, client_order_id, digest, snapshot, now),
                    )
                    connection.execute(
                        """
                        INSERT INTO order_projection
                            (proposal_id, state, created_at, updated_at)
                        VALUES (?, 'PENDING', ?, ?)
                        """,
                        (proposal_id, now, now),
                    )
                    self._append_event(
                        connection,
                        proposal_id,
                        "INTENT_CREATED",
                        None,
                        "PENDING",
                        {},
                        now,
                    )
                    row = self._select_order(connection, proposal_id)
            elif supplied_identity is not None:
                _, digest, _, client_order_id = supplied_identity
                self._assert_same_intent(row, proposal_id, client_order_id, digest)

            if row is None:  # Defensive: all branches above establish a row.
                raise LedgerError("failed to establish order intent")
            state = str(row["state"])
            if state in {"ACKNOWLEDGED", "APPROVED"} and row["acknowledgment_json"]:
                return ClaimResult(ClaimStatus.REPLAY, self._order_from_row(row))
            if state == "SUBMITTING":
                attempt = connection.execute(
                    "SELECT attempt_id FROM dispatch_attempts WHERE proposal_id = ?",
                    (proposal_id,),
                ).fetchone()
                return ClaimResult(
                    ClaimStatus.IN_PROGRESS,
                    self._order_from_row(row),
                    int(attempt["attempt_id"]) if attempt else None,
                )
            if state != "PENDING":
                raise InvalidTransition(f"order is already {state}")

            cursor = connection.execute(
                """
                INSERT INTO dispatch_attempts (proposal_id, state, claimed_at)
                VALUES (?, 'SUBMITTING', ?)
                """,
                (proposal_id, now),
            )
            connection.execute(
                """
                UPDATE order_projection
                SET state = 'SUBMITTING', updated_at = ?
                WHERE proposal_id = ? AND state = 'PENDING'
                """,
                (now, proposal_id),
            )
            self._append_event(
                connection,
                proposal_id,
                "DISPATCH_CLAIMED",
                "PENDING",
                "SUBMITTING",
                {},
                now,
            )
            return ClaimResult(
                ClaimStatus.CLAIMED,
                self._get_order_locked(connection, proposal_id),
                int(cursor.lastrowid),
            )

    def claim_intent(self, intent: OrderIntent) -> ClaimResult:
        return self.claim(str(intent.proposal_id), intent)

    def finalize(self, proposal_id: str, acknowledgment: OrderAck) -> OrderAck:
        """Persist one typed broker acknowledgement and make replays harmless."""

        if str(acknowledgment.proposal_id) != proposal_id:
            raise IntentConflict("acknowledgement proposal_id does not match the order")
        safe_ack = _safe_acknowledgment(acknowledgment)
        safe_json = canonical_json(safe_ack)
        now = _now()
        with self._transaction() as connection:
            row = self._select_order(connection, proposal_id)
            if row is None:
                raise OrderNotFound(f"order {proposal_id!r} was not found")
            state = str(row["state"])
            if state in {"ACKNOWLEDGED", "APPROVED"}:
                if row["acknowledgment_json"] != safe_json:
                    raise IntentConflict("order already has a different acknowledgement")
                return _ack_from_json(safe_json, replay=True)
            if state != "SUBMITTING":
                raise InvalidTransition(f"cannot acknowledge an order in {state}")

            connection.execute(
                """
                UPDATE dispatch_attempts
                SET state = 'ACKNOWLEDGED', completed_at = ?, acknowledgment_json = ?
                WHERE proposal_id = ? AND state = 'SUBMITTING'
                """,
                (now, safe_json, proposal_id),
            )
            connection.execute(
                """
                UPDATE order_projection
                SET state = 'ACKNOWLEDGED', acknowledgment_json = ?, updated_at = ?
                WHERE proposal_id = ? AND state = 'SUBMITTING'
                """,
                (safe_json, now, proposal_id),
            )
            self._append_event(
                connection,
                proposal_id,
                "BROKER_ACKNOWLEDGED",
                "SUBMITTING",
                "ACKNOWLEDGED",
                json.loads(safe_json),
                now,
            )
        return _ack_from_json(safe_json)

    finalize_ack = finalize

    def reconcile(self, snapshot: ReconciliationSnapshot) -> LedgerOrder:
        """Apply one monotonic typed reconciliation snapshot atomically."""

        now = _now()
        with self._transaction() as connection:
            row = self._select_order(connection, snapshot.proposal_id)
            if row is None:
                raise OrderNotFound(f"order {snapshot.proposal_id!r} was not found")
            admission_row = connection.execute(
                "SELECT * FROM execution_admissions WHERE proposal_id = ?",
                (snapshot.proposal_id,),
            ).fetchone()
            if admission_row is None:
                raise ApprovalConflict("execution admission is required before reconciliation")
            admission = self._admission_from_row(admission_row)
            reservation_row = connection.execute(
                "SELECT * FROM buying_power_reservations WHERE proposal_id = ?",
                (snapshot.proposal_id,),
            ).fetchone()
            if reservation_row is None:
                raise ApprovalConflict("active reservation is required before reconciliation")
            reservation = self._reservation_from_row(reservation_row)
            ack_id = str(row["acknowledgment_json"] or "")
            if row["acknowledgment_json"]:
                ack = _ack_from_json(str(row["acknowledgment_json"]))
                expected_broker_order_id = ack.broker_order_id
            else:
                expected_broker_order_id = snapshot.broker_order_id
            if snapshot.broker_order_id != expected_broker_order_id:
                raise InvalidTransition("reconciliation broker order id does not match")
            prior_row = connection.execute(
                "SELECT * FROM reconciliation_evidence WHERE proposal_id = ? ORDER BY evidence_id DESC LIMIT 1",
                (snapshot.proposal_id,),
            ).fetchone()
            if prior_row is not None and str(prior_row["evidence_fingerprint"]) == snapshot.evidence_fingerprint:
                return self._order_from_row(row)
            prior_quantity = _decimal(prior_row["cumulative_quantity"]) if prior_row else Decimal("0")
            prior_notional = _decimal(prior_row["cumulative_notional"]) if prior_row else Decimal("0")
            if snapshot.cumulative_quantity < prior_quantity or snapshot.cumulative_notional < prior_notional:
                raise InvalidTransition("reconciliation evidence is non-monotonic")
            if snapshot.cumulative_quantity > admission.final_quantity:
                raise InvalidTransition("reconciliation overfills the admitted quantity")
            if snapshot.cumulative_notional > admission.notional:
                raise InvalidTransition("reconciliation exceeds the admitted notional")
            if snapshot.cumulative_quantity > 0 and snapshot.cumulative_notional <= 0:
                raise InvalidTransition("filled quantity requires positive notional")
            state = str(row["state"])
            legal = {
                "ACKNOWLEDGED": {"ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED", "CANCELLED", "REJECTED", "UNKNOWN"},
                "PARTIALLY_FILLED": {"PARTIALLY_FILLED", "FILLED", "CANCELLED", "UNKNOWN"},
                "UNKNOWN": {"ACKNOWLEDGED", "PARTIALLY_FILLED", "FILLED", "CANCELLED", "REJECTED", "UNKNOWN"},
            }
            target = snapshot.status.value
            if state not in legal or target not in legal[state]:
                raise InvalidTransition(f"cannot reconcile {state} to {target}")
            connection.execute(
                """
                INSERT INTO reconciliation_evidence
                    (proposal_id, broker_order_id, source, cumulative_quantity,
                     cumulative_notional, status, evidence_fingerprint, observed_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot.proposal_id,
                    snapshot.broker_order_id,
                    snapshot.source,
                    _decimal_str(snapshot.cumulative_quantity),
                    _decimal_str(snapshot.cumulative_notional),
                    target,
                    snapshot.evidence_fingerprint,
                    snapshot.observed_at.isoformat(),
                    now,
                ),
            )
            delta_notional = snapshot.cumulative_notional - prior_notional
            self._apply_reservation_reconciliation_locked(
                connection,
                reservation,
                admission,
                snapshot,
                delta_notional,
                now,
            )
            delta_quantity = snapshot.cumulative_quantity - prior_quantity
            if target in {"FILLED", "PARTIALLY_FILLED"} and delta_notional > 0:
                self._apply_position_fill_locked(
                    connection, admission, delta_quantity, delta_notional, now
                )
            if target == "ACKNOWLEDGED" and not row["acknowledgment_json"]:
                generated_ack = OrderAck(
                    proposal_id=snapshot.proposal_id,
                    broker="paper",
                    broker_order_id=snapshot.broker_order_id,
                    status="ACKNOWLEDGED",
                )
                safe_ack = canonical_json(_safe_acknowledgment(generated_ack))
                connection.execute(
                    "UPDATE order_projection SET state = ?, acknowledgment_json = ?, updated_at = ? WHERE proposal_id = ?",
                    (target, safe_ack, now, snapshot.proposal_id),
                )
            else:
                connection.execute(
                    "UPDATE order_projection SET state = ?, updated_at = ? WHERE proposal_id = ?",
                    (target, now, snapshot.proposal_id),
                )
            connection.execute(
                "UPDATE dispatch_attempts SET state = ?, completed_at = COALESCE(completed_at, ?) WHERE proposal_id = ?",
                (target, now, snapshot.proposal_id),
            )
            self._append_event(
                connection,
                snapshot.proposal_id,
                "RECONCILIATION_APPLIED",
                state,
                target,
                {
                    "broker_order_id": snapshot.broker_order_id,
                    "source": snapshot.source,
                    "cumulative_quantity": _decimal_str(snapshot.cumulative_quantity),
                    "cumulative_notional": _decimal_str(snapshot.cumulative_notional),
                    "evidence_fingerprint": snapshot.evidence_fingerprint,
                },
                now,
            )
            return self._get_order_locked(connection, snapshot.proposal_id)

    def _apply_reservation_reconciliation_locked(
        self,
        connection: sqlite3.Connection,
        reservation: PaperReservation,
        admission: ExecutionAdmission,
        snapshot: ReconciliationSnapshot,
        delta_notional: Decimal,
        now: str,
    ) -> None:
        consumed = reservation.consumed + delta_notional
        released = reservation.released
        terminal = snapshot.status in {
            ReconciliationStatus.FILLED,
            ReconciliationStatus.CANCELLED,
            ReconciliationStatus.REJECTED,
        }
        if terminal:
            released = reservation.reserved - consumed
        state = "ACTIVE"
        if snapshot.status is ReconciliationStatus.UNKNOWN:
            # UNKNOWN retains the outstanding reservation and remains retry-blocked.
            state = "ACTIVE"
        elif terminal:
            state = "SETTLED"
        if consumed < 0 or released < 0 or consumed + released > reservation.reserved:
            raise InvalidTransition("reservation accounting is invalid")
        connection.execute(
            """
            UPDATE buying_power_reservations
            SET consumed = ?, released = ?, state = ?, updated_at = ?
            WHERE proposal_id = ?
            """,
            (
                _decimal_str(consumed),
                _decimal_str(released),
                state,
                now,
                snapshot.proposal_id,
            ),
        )
        budget = connection.execute(
            "SELECT * FROM paper_budgets WHERE workspace = ? AND account = ? AND currency = ?",
            (reservation.workspace, reservation.account, reservation.currency),
        ).fetchone()
        if budget is not None and (delta_notional > 0 or terminal):
            budget_consumed = _decimal(budget["consumed"]) + delta_notional
            budget_released = _decimal(budget["released"])
            budget_reserved = _decimal(budget["reserved"]) - delta_notional
            if terminal:
                budget_released += released - reservation.released
                budget_reserved -= released - reservation.released
            connection.execute(
                "UPDATE paper_budgets SET reserved = ?, consumed = ?, released = ?, updated_at = ? WHERE workspace = ? AND account = ? AND currency = ?",
                (
                    _decimal_str(max(Decimal("0"), budget_reserved)),
                    _decimal_str(budget_consumed),
                    _decimal_str(budget_released),
                    now,
                    reservation.workspace,
                    reservation.account,
                    reservation.currency,
                ),
            )

    @staticmethod
    def _apply_position_fill_locked(
        connection: sqlite3.Connection,
        admission: ExecutionAdmission,
        delta_quantity: Decimal,
        delta_notional: Decimal,
        now: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO paper_positions
                (workspace, account, currency, ticker, quantity, notional, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(workspace, account, currency, ticker) DO UPDATE SET
                quantity = paper_positions.quantity + excluded.quantity,
                notional = paper_positions.notional + excluded.notional,
                updated_at = excluded.updated_at
            """,
            (
                admission.workspace,
                admission.account,
                admission.currency,
                admission.ticker,
                _decimal_str(delta_quantity),
                _decimal_str(delta_notional),
                now,
            ),
        )

    def get_paper_position(
        self, account: str, currency: str, ticker: str
    ) -> Optional[Mapping[str, str]]:
        with self._mutex:
            row = self._require_connection().execute(
                "SELECT quantity, notional FROM paper_positions WHERE workspace = ? AND account = ? AND currency = ? AND ticker = ?",
                (self.workspace, account, currency, ticker),
            ).fetchone()
        if row is None:
            return None
        return {"quantity": str(row["quantity"]), "notional": str(row["notional"])}

    def reject(self, proposal_id: str, notes: Optional[str] = None) -> LedgerOrder:
        now = _now()
        with self._transaction() as connection:
            row = self._select_order(connection, proposal_id)
            if row is None:
                raise OrderNotFound(f"order {proposal_id!r} was not found")
            state = str(row["state"])
            if state == "REJECTED":
                return self._order_from_row(row)
            if state != "PENDING":
                raise InvalidTransition(f"cannot reject an order in {state}")
            connection.execute(
                """
                UPDATE order_projection
                SET state = 'REJECTED', rejection_notes = ?, updated_at = ?
                WHERE proposal_id = ? AND state = 'PENDING'
                """,
                (notes, now, proposal_id),
            )
            reservation = connection.execute(
                "SELECT * FROM buying_power_reservations WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            if reservation is not None:
                self._release_full_reservation_locked(connection, reservation, now)
            self._append_event(
                connection,
                proposal_id,
                "ORDER_REJECTED",
                "PENDING",
                "REJECTED",
                {},
                now,
            )
            return self._get_order_locked(connection, proposal_id)

    def mark_failed(self, proposal_id: str, reason_code: str = "DISPATCH_FAILED") -> LedgerOrder:
        return self._finish_submission(proposal_id, "FAILED", reason_code)

    def mark_unknown(
        self, proposal_id: str, reason_code: str = "OUTCOME_UNKNOWN"
    ) -> LedgerOrder:
        return self._finish_submission(proposal_id, "UNKNOWN", reason_code)

    def _finish_submission(
        self, proposal_id: str, target_state: str, reason_code: str
    ) -> LedgerOrder:
        if not _REASON_CODE_PATTERN.fullmatch(reason_code):
            raise ValueError("reason_code must be a short, non-sensitive code")
        now = _now()
        with self._transaction() as connection:
            row = self._select_order(connection, proposal_id)
            if row is None:
                raise OrderNotFound(f"order {proposal_id!r} was not found")
            state = str(row["state"])
            if state == target_state:
                return self._order_from_row(row)
            if state != "SUBMITTING":
                raise InvalidTransition(
                    f"cannot mark an order {target_state} from {state}"
                )
            connection.execute(
                """
                UPDATE dispatch_attempts
                SET state = ?, completed_at = ?
                WHERE proposal_id = ? AND state = 'SUBMITTING'
                """,
                (target_state, now, proposal_id),
            )
            if target_state == "FAILED":
                reservation = connection.execute(
                    "SELECT * FROM buying_power_reservations WHERE proposal_id = ?",
                    (proposal_id,),
                ).fetchone()
                if reservation is not None:
                    self._release_full_reservation_locked(connection, reservation, now)
            connection.execute(
                """
                UPDATE order_projection
                SET state = ?, updated_at = ?
                WHERE proposal_id = ? AND state = 'SUBMITTING'
                """,
                (target_state, now, proposal_id),
            )
            self._append_event(
                connection,
                proposal_id,
                f"DISPATCH_{target_state}",
                "SUBMITTING",
                target_state,
                {"reason_code": reason_code},
                now,
            )
            return self._get_order_locked(connection, proposal_id)

    @staticmethod
    def _release_full_reservation_locked(
        connection: sqlite3.Connection, reservation: sqlite3.Row, now: str
    ) -> None:
        outstanding = _decimal(reservation["reserved"]) - _decimal(reservation["consumed"]) - _decimal(reservation["released"])
        if outstanding <= 0:
            return
        released = _decimal(reservation["released"]) + outstanding
        connection.execute(
            "UPDATE buying_power_reservations SET released = ?, state = 'SETTLED', updated_at = ? WHERE proposal_id = ?",
            (_decimal_str(released), now, str(reservation["proposal_id"])),
        )
        budget = connection.execute(
            "SELECT * FROM paper_budgets WHERE workspace = ? AND account = ? AND currency = ?",
            (str(reservation["workspace"]), str(reservation["account"]), str(reservation["currency"])),
        ).fetchone()
        if budget is not None:
            budget_reserved = max(Decimal("0"), _decimal(budget["reserved"]) - outstanding)
            budget_released = _decimal(budget["released"]) + outstanding
            connection.execute(
                "UPDATE paper_budgets SET reserved = ?, released = ?, updated_at = ? WHERE workspace = ? AND account = ? AND currency = ?",
                (
                    _decimal_str(budget_reserved),
                    _decimal_str(budget_released),
                    now,
                    str(reservation["workspace"]),
                    str(reservation["account"]),
                    str(reservation["currency"]),
                ),
            )

    def recover_abandoned_submissions(self) -> int:
        """Fail closed after restart: ambiguous submissions become UNKNOWN."""

        now = _now()
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT proposal_id FROM order_projection WHERE state = 'SUBMITTING'"
            ).fetchall()
            for row in rows:
                proposal_id = str(row["proposal_id"])
                connection.execute(
                    """
                    UPDATE dispatch_attempts
                    SET state = 'UNKNOWN', completed_at = ?
                    WHERE proposal_id = ? AND state = 'SUBMITTING'
                    """,
                    (now, proposal_id),
                )
                connection.execute(
                    """
                    UPDATE order_projection
                    SET state = 'UNKNOWN', updated_at = ?
                    WHERE proposal_id = ? AND state = 'SUBMITTING'
                    """,
                    (now, proposal_id),
                )
                self._append_event(
                    connection,
                    proposal_id,
                    "STARTUP_RECOVERY",
                    "SUBMITTING",
                    "UNKNOWN",
                    {"reason_code": "ABANDONED_SUBMISSION"},
                    now,
                )
            return len(rows)

    def get_order(self, proposal_id: str) -> Optional[LedgerOrder]:
        with self._mutex:
            row = self._select_order(self._require_connection(), proposal_id)
            return self._order_from_row(row) if row is not None else None

    def list_attempts(self, proposal_id: Optional[str] = None) -> list[DispatchAttempt]:
        sql = "SELECT * FROM dispatch_attempts"
        parameters: tuple[object, ...] = ()
        if proposal_id is not None:
            sql += " WHERE proposal_id = ?"
            parameters = (proposal_id,)
        sql += " ORDER BY attempt_id"
        with self._mutex:
            rows = self._require_connection().execute(sql, parameters).fetchall()
        return [
            DispatchAttempt(
                attempt_id=int(row["attempt_id"]),
                proposal_id=str(row["proposal_id"]),
                state=str(row["state"]),
                claimed_at=str(row["claimed_at"]),
                completed_at=row["completed_at"],
                acknowledgment=(
                    _ack_from_json(str(row["acknowledgment_json"]))
                    if row["acknowledgment_json"]
                    else None
                ),
            )
            for row in rows
        ]

    def list_events(self, proposal_id: Optional[str] = None) -> list[ExecutionEvent]:
        sql = "SELECT * FROM execution_events"
        parameters: tuple[object, ...] = ()
        if proposal_id is not None:
            sql += " WHERE proposal_id = ?"
            parameters = (proposal_id,)
        sql += " ORDER BY event_id"
        with self._mutex:
            rows = self._require_connection().execute(sql, parameters).fetchall()
        return [
            ExecutionEvent(
                event_id=int(row["event_id"]),
                proposal_id=str(row["proposal_id"]),
                event_type=str(row["event_type"]),
                from_state=row["from_state"],
                to_state=str(row["to_state"]),
                payload=json.loads(str(row["payload_json"])),
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    def approval_evidence_count(self, proposal_id: Optional[str] = None) -> int:
        sql = "SELECT COUNT(*) FROM execution_approvals"
        parameters: tuple[object, ...] = ()
        if proposal_id is not None:
            sql += " WHERE proposal_id = ?"
            parameters = (proposal_id,)
        with self._mutex:
            return int(self._require_connection().execute(sql, parameters).fetchone()[0])

    def pragmas(self) -> Mapping[str, Any]:
        with self._mutex:
            connection = self._require_connection()
            return {
                "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
                "synchronous": connection.execute("PRAGMA synchronous").fetchone()[0],
                "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
                "busy_timeout": connection.execute("PRAGMA busy_timeout").fetchone()[0],
                "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
            }

    def _workspace_engaged_locked(self, connection: sqlite3.Connection) -> bool:
        return self._workspace_control_locked(connection).engaged

    def _workspace_control_locked(self, connection: sqlite3.Connection) -> WorkspaceControl:
        row = connection.execute(
            "SELECT * FROM workspace_controls WHERE workspace = ?", (self.workspace,)
        ).fetchone()
        if row is None:
            now = _now()
            connection.execute(
                "INSERT INTO workspace_controls(workspace, engaged, version, reason_code, updated_at) VALUES (?, 0, 0, '', ?)",
                (self.workspace, now),
            )
            return WorkspaceControl(
                workspace=self.workspace,
                engaged=False,
                version=0,
                updated_at=datetime.fromisoformat(now),
            )
        return WorkspaceControl(
            workspace=str(row["workspace"]),
            engaged=bool(row["engaged"]),
            version=int(row["version"]),
            reason_code=str(row["reason_code"]),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    @staticmethod
    def _admission_from_row(row: sqlite3.Row) -> ExecutionAdmission:
        return ExecutionAdmission(
            proposal_id=str(row["proposal_id"]),
            intent_hash=str(row["intent_hash"]),
            workspace=str(row["workspace"]),
            account=str(row["account"]),
            currency=str(row["currency"]),
            ticker=str(row["ticker"]),
            side=str(row["side"]),
            original_quantity=_decimal(row["original_quantity"]),
            final_quantity=_decimal(row["final_quantity"]),
            price=_decimal(row["price"]),
            notional=_decimal(row["notional"]),
            simulator_fill_price=_decimal(row["simulator_fill_price"]),
            simulator_drawdown_pct=_decimal(row["simulator_drawdown_pct"]),
            risk_quantity=_decimal(row["risk_quantity"]),
            current_spread_pct=_decimal(row["current_spread_pct"]),
            evidence_at=datetime.fromisoformat(str(row["evidence_at"])),
            evidence_hash=str(row["evidence_hash"]),
            decision=str(row["decision"]),
            reason_code=str(row["reason_code"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    @staticmethod
    def _budget_from_row(row: sqlite3.Row) -> PaperBudget:
        return PaperBudget(
            workspace=str(row["workspace"]),
            account=str(row["account"]),
            currency=str(row["currency"]),
            amount=_decimal(row["amount"]),
            reserved=_decimal(row["reserved"]),
            consumed=_decimal(row["consumed"]),
            released=_decimal(row["released"]),
        )

    @staticmethod
    def _reservation_from_row(row: sqlite3.Row) -> PaperReservation:
        return PaperReservation(
            proposal_id=str(row["proposal_id"]),
            workspace=str(row["workspace"]),
            account=str(row["account"]),
            currency=str(row["currency"]),
            intent_hash=str(row["intent_hash"]),
            reserved=_decimal(row["reserved"]),
            consumed=_decimal(row["consumed"]),
            released=_decimal(row["released"]),
            state=str(row["state"]),
        )

    def _find_identity(
        self, connection: sqlite3.Connection, proposal_id: str, client_order_id: str
    ) -> Optional[sqlite3.Row]:
        return connection.execute(
            """
            SELECT i.*, p.state, p.acknowledgment_json, p.updated_at
            FROM order_intents AS i
            JOIN order_projection AS p USING (proposal_id)
            WHERE i.proposal_id = ? OR i.client_order_id = ?
            """,
            (proposal_id, client_order_id),
        ).fetchone()

    def _select_order(
        self, connection: sqlite3.Connection, proposal_id: str
    ) -> Optional[sqlite3.Row]:
        return connection.execute(
            """
            SELECT i.*, p.state, p.acknowledgment_json, p.updated_at
            FROM order_intents AS i
            JOIN order_projection AS p USING (proposal_id)
            WHERE i.proposal_id = ?
            """,
            (proposal_id,),
        ).fetchone()

    def _get_order_locked(
        self, connection: sqlite3.Connection, proposal_id: str
    ) -> LedgerOrder:
        row = self._select_order(connection, proposal_id)
        if row is None:
            raise OrderNotFound(f"order {proposal_id!r} was not found")
        return self._order_from_row(row)

    @staticmethod
    def _assert_same_intent(
        row: sqlite3.Row,
        proposal_id: str,
        client_order_id: str,
        digest: str,
    ) -> None:
        if (
            row["proposal_id"] != proposal_id
            or row["client_order_id"] != client_order_id
            or row["intent_hash"] != digest
        ):
            raise IntentConflict(
                "proposal/client-order identity was reused with a changed intent"
            )

    @staticmethod
    def _order_from_row(row: sqlite3.Row) -> LedgerOrder:
        ack_json = row["acknowledgment_json"]
        return LedgerOrder(
            proposal_id=str(row["proposal_id"]),
            client_order_id=str(row["client_order_id"]),
            intent_hash=str(row["intent_hash"]),
            intent=json.loads(str(row["canonical_json"])),
            state=str(row["state"]),
            acknowledgment=_ack_from_json(str(ack_json)) if ack_json else None,
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    @staticmethod
    def _approval_key_from_row(row: sqlite3.Row) -> LedgerApprovalKey:
        return LedgerApprovalKey(
            workspace=str(row["workspace"]),
            key_id=str(row["key_id"]),
            public_key_x963=bytes(row["public_key_x963"]),
            created_at=str(row["created_at"]),
        )

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        proposal_id: str,
        event_type: str,
        from_state: Optional[str],
        to_state: str,
        payload: Mapping[str, Any],
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO execution_events
                (proposal_id, event_type, from_state, to_state, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                proposal_id,
                event_type,
                from_state,
                to_state,
                canonical_json(payload),
                created_at,
            ),
        )


def _intent_identity(intent: OrderIntent) -> tuple[str, str, str, str]:
    snapshot = canonical_json(intent)
    data = json.loads(snapshot)
    proposal_id = str(data.get("proposal_id", ""))
    client_order_id = str(data.get("client_order_id", f"growin-{proposal_id}"))
    if not proposal_id or not client_order_id:
        raise ValueError("intent requires proposal_id and client_order_id")
    digest = hashlib.sha256(snapshot.encode("utf-8")).hexdigest()
    return snapshot, digest, proposal_id, client_order_id


def _safe_acknowledgment(acknowledgment: OrderAck) -> Mapping[str, Any]:
    """Allowlist persisted acknowledgement fields; never retain broker raw data."""

    data = acknowledgment.model_dump(mode="json")
    safe = {
        key: data[key]
        for key in (
            "proposal_id",
            "broker",
            "broker_order_id",
            "status",
            "idempotent_replay",
        )
        if key in data
    }
    safe["raw"] = {}
    safe["idempotent_replay"] = False
    return safe


def _ack_from_json(value: str, *, replay: bool = False) -> OrderAck:
    ack = OrderAck.model_validate_json(value)
    if replay:
        return ack.as_replay()
    return ack


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _decimal(value: object) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise LedgerError("ledger contains an invalid Decimal amount") from exc
    if not parsed.is_finite() or parsed < 0:
        raise LedgerError("ledger contains a non-finite or negative Decimal amount")
    return parsed


def _positive_decimal(value: object, label: str) -> Decimal:
    parsed = _decimal(value)
    if parsed <= 0:
        raise ValueError(f"{label} must be positive")
    return parsed


def _decimal_str(value: Decimal) -> str:
    parsed = _decimal(value)
    return format(parsed, "f")


__all__ = [
    "ApprovalConflict",
    "ApprovalKeyConflict",
    "ClaimResult",
    "ClaimStatus",
    "DispatchAttempt",
    "ExecutionEvent",
    "ExecutionLedger",
    "IntentConflict",
    "InvalidTransition",
    "LedgerError",
    "LedgerApprovalChallenge",
    "LedgerApprovalKey",
    "LedgerOrder",
    "LedgerWriterUnavailable",
    "OrderNotFound",
    "PaperBudget",
    "PaperReservation",
    "ReconciliationSnapshot",
    "WorkspaceControl",
    "canonical_json",
    "default_ledger_path",
    "intent_hash",
]
