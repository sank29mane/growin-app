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
from types import MappingProxyType
from typing import Any, Iterable, Iterator, Mapping, Optional

from .models import (
    AdmissionDecision,
    ExecutionAdmission,
    OrderAck,
    OrderIntent,
    OrderSide,
    PaperBudget,
    PaperReservation,
    ReconciliationSnapshot,
    ReconciliationStatus,
    Workspace,
    WorkspaceControl,
)
from .venue import (
    VENUE_T212_PRACTICE,
    VenueBinding,
    allowed_mode,
    allowed_modes,
    intent_refusal,
    is_shadow_binding,
    refusal_text,
    registered_kinds,
    spec_for,
)


SCHEMA_VERSION = 6
LEGACY_SCHEMA_VERSIONS = range(1, 6)
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


class IndiaCapExceeded(ApprovalConflict):
    """The reservation would break an India cap. ``code`` is the O6 reason (63-04, C1)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class RequoteConflict(LedgerError):
    """Raised when immutable local re-quote evidence is inconsistent."""


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


@dataclass(frozen=True)
class LedgerRequote:
    """Durable, non-executable local re-quote candidate evidence."""

    requote_id: str
    proposal_id: str
    parent_intent_hash: str
    parent_reconciliation_fingerprint: str
    idempotency_key: str
    snapshot_hash: str
    candidate: Mapping[str, Any]
    state: str
    reason_code: str
    replacement_proposal_id: str
    created_at: str
    updated_at: str


class WorkspaceMismatch(LedgerError):
    """Raised when a request names a workspace that is not the ledger's pin."""


class LedgerUnpinned(LedgerError):
    """Raised when a ledger file has no workspace pin (legacy v1 to v5 or unmarked)."""


class LedgerVenueMismatch(LedgerError):
    """Raised when a ledger's venue binding is not the one the opener asked for.

    The text names no account id and no currency.
    """


def coerce_workspace(value: object) -> Workspace:
    """Return the ``Workspace`` for ``value`` or raise ``ValueError``.

    Accepts a plain string or any str-based enum member (so a ``Workspace``
    imported through either ``execution`` or ``backend.execution`` works).
    Nothing else is accepted: no None, no case folding, no whitespace trimming.
    """

    if not isinstance(value, str):
        raise ValueError("workspace must be 'uk' or 'india'")
    raw = value.value if isinstance(value, Enum) else str(value)
    try:
        return Workspace(raw)
    except ValueError:
        raise ValueError("workspace must be 'uk' or 'india'") from None


def default_ledger_path(workspace: Workspace | str) -> Path:
    """Return the local macOS ledger path without creating it."""

    pinned = coerce_workspace(workspace)
    return (
        Path.home()
        / "Library"
        / "Application Support"
        / "Growin"
        / "workspaces"
        / pinned.value
        / "execution.sqlite3"
    )


def practice_ledger_path() -> Path:
    """Return the Trading 212 practice-ledger path (uk only) without creating it.

    It is the ``t212_practice`` spec's default ledger path, and it is never
    ``default_ledger_path``: practice code must not open, read or migrate the
    real ledger of the same workspace (Phase 66 D-01).
    """

    spec = spec_for(VENUE_T212_PRACTICE)
    if spec is None:
        raise LedgerVenueMismatch("the practice venue is not registered")
    return spec.ledger_path()


def _same_file_path(candidate: Path, canonical: Path) -> bool:
    """True when two paths can name one file: same real path, case-folded, or same inode."""

    left = os.path.realpath(candidate)
    right = os.path.realpath(canonical)
    if left == right or left.casefold() == right.casefold():
        return True
    try:
        return os.path.samefile(left, right)
    except OSError:
        return False


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


# SQLite runs no DELETE trigger for the row an INSERT OR REPLACE (or REPLACE INTO, or an upsert)
# conflict-deletes unless recursive_triggers is on, and that is a per-connection pragma. So the
# UPDATE/DELETE triggers alone do not make a table append-only. A BEFORE INSERT trigger fires
# whatever the pragma is, so a replacement is refused on any connection. Each tuple below is one
# uniqueness constraint (primary key or UNIQUE), because a conflict on any of them removes a row;
# the rowid is always checked as well.
#
# No code path inserts into these tables with OR IGNORE or ON CONFLICT: every writer does a plain
# INSERT after its own lookup. RAISE(ABORT) is therefore the whole policy. It raises
# ``sqlite3.IntegrityError`` exactly as the UNIQUE constraint it pre-empts did, so the one caller
# that catches a duplicate (the approval claim) still maps it to ``ApprovalConflict``.
_BASE_NO_REPLACE_KEYS: Mapping[str, tuple[tuple[str, ...], ...]] = {
    "order_intents": (("proposal_id",), ("client_order_id",)),
    "execution_events": (("event_id",),),
    "requote_intents": (("requote_id",), ("idempotency_key",)),
    "requote_events": (("event_id",),),
    "approval_keys": (("workspace", "key_id"),),
    "approval_challenges": (("challenge_id",),),
    "execution_approvals": (("approval_id",), ("challenge_id",), ("proposal_id",)),
}


def _no_replace_trigger_sql(table: str, keys: tuple[tuple[str, ...], ...]) -> str:
    """The ``<table>_no_replace`` guard: refuse any insert that collides with an existing row."""

    clauses = [
        "(" + " AND ".join(f"{column} = NEW.{column}" for column in key) + ")" for key in keys
    ]
    clauses.append("rowid = NEW.rowid")
    return f"""
    CREATE TRIGGER IF NOT EXISTS {table}_no_replace
    BEFORE INSERT ON {table}
    WHEN EXISTS (SELECT 1 FROM {table} WHERE {" OR ".join(clauses)})
    BEGIN
        SELECT RAISE(ABORT, '{table} rows are append-only');
    END
    """


_BASE_SCHEMA_STATEMENTS = (
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
    CREATE TABLE IF NOT EXISTS requote_intents (
        requote_id TEXT PRIMARY KEY,
        proposal_id TEXT NOT NULL REFERENCES order_intents(proposal_id),
        parent_intent_hash TEXT NOT NULL,
        parent_reconciliation_fingerprint TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        snapshot_hash TEXT NOT NULL,
        candidate_json TEXT NOT NULL,
        state TEXT NOT NULL,
        reason_code TEXT NOT NULL DEFAULT '',
        replacement_proposal_id TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS requote_intents_parent_state
    ON requote_intents(proposal_id, state, created_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS requote_events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        requote_id TEXT NOT NULL REFERENCES requote_intents(requote_id),
        event_type TEXT NOT NULL,
        from_state TEXT,
        to_state TEXT NOT NULL,
        payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL
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
    CREATE TRIGGER IF NOT EXISTS requote_intents_no_delete
    BEFORE DELETE ON requote_intents
    BEGIN
        SELECT RAISE(ABORT, 'requote intents are immutable');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS requote_events_no_update
    BEFORE UPDATE ON requote_events
    BEGIN
        SELECT RAISE(ABORT, 'requote events are append-only');
    END
    """,
    """
    CREATE TRIGGER IF NOT EXISTS requote_events_no_delete
    BEFORE DELETE ON requote_events
    BEGIN
        SELECT RAISE(ABORT, 'requote events are append-only');
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
) + tuple(_no_replace_trigger_sql(table, keys) for table, keys in _BASE_NO_REPLACE_KEYS.items())


@dataclass(frozen=True)
class IndiaAccountView:
    """What the Mac's India limits need to know about the ledger, read in one pass (63-04).

    ``positions`` are ``(ticker, quantity, cost basis)`` for every held ticker. ``open_buys``
    are ``(ticker, unfilled quantity, limit price)`` for every admitted BUY whose buying-power
    reservation is still ACTIVE. ``open_sells`` maps a ticker to the admitted SELL quantity
    that has not reached a terminal state (waiting for approval, or claimed).
    """

    positions: tuple[tuple[str, Decimal, Decimal], ...]
    open_buys: tuple[tuple[str, Decimal, Decimal], ...]
    open_sells: Mapping[str, Decimal]


@dataclass(frozen=True)
class LedgerIdentity:
    """What a ledger file says about its owner, read without any write."""

    kind: str  # "fresh", "pinned", "unpinned" or "newer"
    workspace: Optional[Workspace]
    user_version: int
    # Present only in a practice ledger created by Phase 66 code. A file with
    # no binding table is a paper ledger, exactly as every v6 file was before.
    binding: Optional[VenueBinding] = None


def _require_sqlite_capabilities(connection: sqlite3.Connection) -> None:
    """Fail closed on a SQLite build without generated columns or JSON1."""

    if sqlite3.sqlite_version_info < (3, 31, 0):
        raise LedgerError(
            "SQLite 3.31.0 or newer is required for ledger workspace identity "
            f"(found {sqlite3.sqlite_version})"
        )
    try:
        value = connection.execute("SELECT json_extract('{\"a\": 1}', '$.a')").fetchone()[0]
    except sqlite3.Error:
        raise LedgerError(
            "SQLite 3.31.0 or newer with JSON1 is required for ledger workspace identity"
        ) from None
    if value != 1:
        raise LedgerError("SQLite json_extract returned an unexpected result")


def _read_identity(connection: sqlite3.Connection) -> LedgerIdentity:
    """Classify a ledger file. Read-only: it only runs SELECTs and a PRAGMA read."""

    user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    tables = {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }
    if user_version > SCHEMA_VERSION:
        return LedgerIdentity("newer", None, user_version)
    if user_version == 0 and not tables:
        return LedgerIdentity("fresh", None, user_version)
    if user_version == SCHEMA_VERSION and "ledger_identity" in tables:
        row = connection.execute(
            "SELECT workspace FROM ledger_identity WHERE singleton = 1"
        ).fetchone()
        if row is not None:
            try:
                workspace = Workspace(str(row[0]))
            except ValueError:
                workspace = None
            if workspace is not None:
                binding = _read_binding(connection, tables)
                return LedgerIdentity("pinned", workspace, user_version, binding)
    return LedgerIdentity("unpinned", None, user_version)


_VENUE_BINDING_TABLE = "ledger_venue_binding"


def _read_binding(
    connection: sqlite3.Connection, tables: set[str]
) -> Optional[VenueBinding]:
    """Read the practice binding, if the file has one. Fails closed on a bad row."""

    if _VENUE_BINDING_TABLE not in tables:
        return None
    row = connection.execute(
        f"SELECT venue, account_id, currency FROM {_VENUE_BINDING_TABLE} WHERE singleton = 1"
    ).fetchone()
    if row is None:
        raise LedgerVenueMismatch("ledger venue binding is missing")
    try:
        return VenueBinding(venue=str(row[0]), account_id=str(row[1]), currency=str(row[2]))
    except ValueError:
        raise LedgerVenueMismatch("ledger venue binding is invalid") from None


def _venue_binding_check() -> str:
    """The binding CHECK, enumerated from the registered specs and nothing wider.

    One (venue, currency) pair per registered kind. Kind and currency are
    validated by ``VenueSpec`` to ``[a-z0-9_]`` and ``[A-Z]{3}``, so they are
    safe to inline here.
    """

    pairs = []
    for kind in registered_kinds():
        spec = spec_for(kind)
        if spec is not None:
            pairs.append(f"(venue = '{spec.kind}' AND currency = '{spec.currency}')")
    return " OR ".join(pairs) if pairs else "0"


_VENUE_LIMITS_TABLE = "ledger_venue_limits"

# Every write-once table that is not in the base set, plus the base set. A ledger opened for write
# gets any of these guards it lacks (``_ensure_no_replace_guards``), whatever version created it.
_NO_REPLACE_KEYS: Mapping[str, tuple[tuple[str, ...], ...]] = {
    **_BASE_NO_REPLACE_KEYS,
    "ledger_identity": (("singleton",),),
    _VENUE_BINDING_TABLE: (("singleton",),),
    _VENUE_LIMITS_TABLE: (("singleton",),),
}


def _install_venue_limits_table(connection: sqlite3.Connection) -> None:
    """Create the empty, write-once caps table. Idempotent; caller owns the transaction.

    It exists only in a bound-venue ledger. One row, never updated or deleted.
    """

    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_VENUE_LIMITS_TABLE} (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            capital_cap TEXT NOT NULL,
            per_position_cap TEXT NOT NULL,
            configured_at TEXT NOT NULL
        )
        """
    )
    for event in ("UPDATE", "DELETE"):
        connection.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {_VENUE_LIMITS_TABLE}_no_{event.lower()}
            BEFORE {event} ON {_VENUE_LIMITS_TABLE}
            BEGIN
                SELECT RAISE(ABORT, 'ledger venue limits are immutable');
            END
            """
        )
    connection.execute(
        _no_replace_trigger_sql(_VENUE_LIMITS_TABLE, _NO_REPLACE_KEYS[_VENUE_LIMITS_TABLE])
    )


_QUANTITY_TABLE = "ledger_quantity_reservations"

# Event types a bound-venue caller may add through ``record_audit_event``.
_AUDIT_EVENT_TYPES = frozenset(
    {
        "CANCEL_REQUESTED",
        "CANCEL_RESPONSE",
        "RECONCILIATION_ESCALATED",
        "RECONCILIATION_ANOMALY",
        # 63-05: the Mac's side of the VM's order audit. Each carries the intent_sha256 and the
        # decision, so the operator can match it to a ``GET /v1/orders/audit`` entry.
        "RELAY_CHALLENGED",
        "RELAY_AUTHORIZE_RESULT",
    }
)
# The purpose of a VM-minted order challenge (growin-orders/1, O4). The Mac verifies and claims
# these bytes exactly as the VM minted them; the Mac never builds them.
RELAY_ORDER_PURPOSE = "growin.relay.order"
# The Mac's own O3 intent for a relay challenge, written beside the stored challenge (fix r1).
RELAY_INTENT_EXPECTED_EVENT = "RELAY_INTENT_EXPECTED"
_RELAY_PAYLOAD_KEYS = frozenset(
    {
        "version",
        "purpose",
        "challenge_id",
        "nonce",
        "issued_at",
        "expires_at",
        "key_id",
        "limits_sha256",
        "intent",
    }
)
# States in which an order may already be at the broker (Phase 66, D-16).
_IN_FLIGHT_STATES = ("SUBMITTING", "UNKNOWN", "ACKNOWLEDGED", "PARTIALLY_FILLED")
# Phase 63-04: an India paper SELL holds no reservation. It is "claimed" once approval has
# consumed its challenge, and from then until a terminal state its quantity is open.
_SELL_CLAIMED_STATES = ("SUBMITTING", "APPROVED", "ACKNOWLEDGED", "PARTIALLY_FILLED", "UNKNOWN")
EXIT_BATCH_EVENT = "EXIT_BATCH_REGISTERED"
EXIT_BATCH_REASONS = frozenset({"halve", "flatten", "stop"})
_BATCH_ID_PATTERN = re.compile(r"^[a-z0-9-]{8,64}$")
SELL_EXCEEDS_HOLDING = "sell_exceeds_holding"


def _canonical_ascii(value: Any) -> bytes:
    """The VM's canonical form (O3): sorted keys, compact, ASCII. ``true`` and ``1`` differ."""

    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("ascii")


def _relay_intent_is_expected(
    connection: sqlite3.Connection, proposal_id: str, challenge_id: str, embedded: Any
) -> bool:
    """True when the embedded intent is, byte for byte, the intent the Mac stored for this challenge.

    The record is the ``RELAY_INTENT_EXPECTED`` event written with the challenge. No record means
    the challenge did not come through the Mac's minter, so it cannot be claimed.
    """

    rows = connection.execute(
        "SELECT payload_json FROM execution_events WHERE proposal_id = ? AND event_type = ?",
        (proposal_id, RELAY_INTENT_EXPECTED_EVENT),
    ).fetchall()
    expected = []
    for row in rows:
        try:
            recorded = json.loads(str(row["payload_json"]))
        except ValueError:
            return False
        if isinstance(recorded, Mapping) and recorded.get("challenge_id") == challenge_id:
            expected.append(recorded.get("intent"))
    if len(expected) != 1 or not isinstance(expected[0], Mapping):
        return False
    try:
        return _canonical_ascii(embedded) == _canonical_ascii(expected[0])
    except (TypeError, ValueError):
        return False


def _relay_intent_matches(
    embedded: Any, intent: Mapping[str, Any], proposal_id: str, admission: Any
) -> bool:
    """True when the VM-minted intent is the order the Mac admitted, field by field.

    The Mac already compared the whole embedded intent when it stored the challenge
    (``check_minted_bytes``). This is the claim-time check against the durable order and the
    admission, so a challenge row cannot be claimed for any other order.
    """

    if not isinstance(embedded, Mapping):
        return False
    try:
        limit = Decimal(str(embedded.get("limit_price")))
        stored_limit = Decimal(str(intent.get("limit_price")))
        quantity = embedded.get("quantity")
        symbol = str(intent.get("ticker", "")).rsplit(":", 1)[-1]
        return (
            embedded.get("proposal_id") == proposal_id
            and embedded.get("workspace") == "india"
            and embedded.get("broker") == "icici-breeze"
            and embedded.get("mode") == "SHADOW"
            and embedded.get("exchange") == "NSE"
            and embedded.get("product") == "cash"
            and embedded.get("order_type") == "limit"
            and embedded.get("validity") == "day"
            and str(embedded.get("side", "")).upper() == str(intent.get("side")).upper()
            and embedded.get("stock_code") == symbol
            and isinstance(quantity, int)
            and not isinstance(quantity, bool)
            and Decimal(quantity) == Decimal(str(admission["final_quantity"]))
            and Decimal(quantity) == Decimal(str(intent.get("quantity")))
            and limit == stored_limit
        )
    except (InvalidOperation, ValueError, TypeError):
        return False


def _install_quantity_reservations_table(connection: sqlite3.Connection) -> None:
    """Create the empty SELL quantity-reservation table. Idempotent; caller owns the transaction.

    It exists only in a bound-venue ledger (Phase 66, D-19, D-21). A paper ledger
    never gets it, so a SELL there stays denied exactly as before. Rows are never
    deleted; the workspace insert guard matches every other ledger table.
    """

    connection.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {_QUANTITY_TABLE} (
            proposal_id TEXT PRIMARY KEY REFERENCES order_intents(proposal_id),
            workspace TEXT NOT NULL,
            account TEXT NOT NULL,
            currency TEXT NOT NULL,
            ticker TEXT NOT NULL,
            intent_hash TEXT NOT NULL,
            reserved TEXT NOT NULL,
            consumed TEXT NOT NULL DEFAULT '0',
            released TEXT NOT NULL DEFAULT '0',
            state TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS {_QUANTITY_TABLE}_no_delete
        BEFORE DELETE ON {_QUANTITY_TABLE}
        BEGIN
            SELECT RAISE(ABORT, 'quantity reservations are never deleted');
        END
        """
    )
    connection.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS {_QUANTITY_TABLE}_workspace_insert_guard
        BEFORE INSERT ON {_QUANTITY_TABLE}
        WHEN NEW.workspace IS NOT {_PIN_SUBQUERY}
        BEGIN
            SELECT RAISE(ABORT, '{_WORKSPACE_GUARD_MESSAGE}');
        END
        """
    )


def _install_venue_binding(
    connection: sqlite3.Connection, binding: VenueBinding, bound_at: str
) -> None:
    """Write the immutable practice binding. Caller owns the transaction.

    Only a fresh practice ledger gets this table; no existing ledger is altered.
    """

    connection.execute(
        f"""
        CREATE TABLE {_VENUE_BINDING_TABLE} (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            venue TEXT NOT NULL,
            account_id TEXT NOT NULL CHECK (length(account_id) BETWEEN 1 AND 64),
            currency TEXT NOT NULL,
            bound_at TEXT NOT NULL,
            CHECK ({_venue_binding_check()})
        )
        """
    )
    for event in ("UPDATE", "DELETE"):
        connection.execute(
            f"""
            CREATE TRIGGER {_VENUE_BINDING_TABLE}_no_{event.lower()}
            BEFORE {event} ON {_VENUE_BINDING_TABLE}
            BEGIN
                SELECT RAISE(ABORT, 'ledger venue binding is immutable');
            END
            """
        )
    connection.execute(
        _no_replace_trigger_sql(_VENUE_BINDING_TABLE, _NO_REPLACE_KEYS[_VENUE_BINDING_TABLE])
    )
    connection.execute(
        f"INSERT INTO {_VENUE_BINDING_TABLE} (singleton, venue, account_id, currency, bound_at) "
        "VALUES (1, ?, ?, ?, ?)",
        (binding.venue, binding.account_id, binding.currency, bound_at),
    )
    _install_venue_limits_table(connection)
    _install_quantity_reservations_table(connection)


# --- the SHADOW simulation account (Phase 64-07) ----------------------------------------
#
# A simulation ledger is an ordinary bound ``breeze_relay`` ledger plus a marker table and four
# more tables that only the 64-07 adapter's ``SimulationInit`` can create, in a fresh file. No
# existing ledger is migrated and ``SCHEMA_VERSION`` does not move. Everything here is inert in a
# paper or practice ledger: without the marker the new methods refuse.

SIM_ACCOUNT_PREFIX = "sim-"
SIMULATION_RUN_DIRECTORY = "india-shadow"
SIM_BOOKING_KIND = "k3"
SIM_CASH_NEGATIVE = "SIM_CASH_NEGATIVE"
SIM_CASH_INSUFFICIENT = "SIM_CASH_INSUFFICIENT"
SIMULATED_BOOKING_EVENT = "SIMULATED_FILL_BOOKED"
SIMULATED_EXPIRY_EVENT = "SIMULATED_ORDER_EXPIRED"
SHADOW_VM_DECISION_EVENT = "SHADOW_VM_DECISION"
_SIM_MARKER_TABLE = "simulation_account"
_SIM_FILLS_TABLE = "simulated_fills"
_SIM_CASH_TABLE = "simulated_cash"
_SIM_EVENTS_TABLE = "simulated_cash_events"
_SIMULATION_TABLES = (_SIM_MARKER_TABLE, _SIM_FILLS_TABLE, _SIM_CASH_TABLE, _SIM_EVENTS_TABLE)
_SIMULATION_TRIGGERS = frozenset(
    {
        f"{_SIM_MARKER_TABLE}_no_update",
        f"{_SIM_MARKER_TABLE}_no_delete",
        f"{_SIM_MARKER_TABLE}_workspace_insert_guard",
        f"{_SIM_FILLS_TABLE}_no_update",
        f"{_SIM_FILLS_TABLE}_no_delete",
        f"{_SIM_FILLS_TABLE}_workspace_insert_guard",
        f"{_SIM_CASH_TABLE}_no_delete",
        f"{_SIM_CASH_TABLE}_workspace_insert_guard",
        f"{_SIM_CASH_TABLE}_identity_immutable",
        f"{_SIM_EVENTS_TABLE}_no_update",
        f"{_SIM_EVENTS_TABLE}_no_delete",
        f"{_SIM_EVENTS_TABLE}_workspace_insert_guard",
        f"{_SIM_MARKER_TABLE}_no_replace",
        f"{_SIM_FILLS_TABLE}_no_replace",
        f"{_SIM_CASH_TABLE}_no_replace",
        f"{_SIM_EVENTS_TABLE}_no_replace",
    }
)
# The key column of each simulation table. A second row with the same key, or the same rowid, is
# a replacement, never an insert.
_SIM_KEYS = (
    (_SIM_MARKER_TABLE, "singleton"),
    (_SIM_FILLS_TABLE, "booking_id"),
    (_SIM_CASH_TABLE, "singleton"),
    (_SIM_EVENTS_TABLE, "event_id"),
)
_RUN_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,56}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_EXPIRY_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SIM_CASH_KINDS = ("RESERVE", "BOOK_BUY", "BOOK_SELL", "EXPIRE")


class SimulatedCashInsufficient(ApprovalConflict):
    """A simulated buy reservation does not fit the simulated cash. Nothing was reserved."""

    code = SIM_CASH_INSUFFICIENT


class SimulatedCashNegative(InvalidTransition):
    """A booking would take simulated cash below zero. Nothing was booked."""

    code = SIM_CASH_NEGATIVE


@dataclass(frozen=True)
class SimulationInit:
    """What a fresh simulation ledger is created with, and what every reopen must repeat.

    ``account`` is ``sim-<run_id>`` and is also the ledger's bound account id, so a simulated
    order can never carry a broker account id. ``seed`` is the simulated cash, written once.
    """

    run_id: str
    workspace: Workspace
    binding: VenueBinding
    seed: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or _RUN_ID_PATTERN.fullmatch(self.run_id) is None:
            raise ValueError("a simulation run id is 1 to 56 letters, digits, underscores or hyphens")
        if self.workspace is not Workspace.INDIA:
            raise ValueError("a simulation account exists only in the India workspace")
        if not isinstance(self.binding, VenueBinding) or not is_shadow_binding(self.binding):
            raise ValueError("a simulation account is bound to the breeze_relay SHADOW venue")
        if self.binding.account_id != self.account:
            raise ValueError("a simulation account id is sim-<run_id>")
        if not isinstance(self.seed, Decimal) or not self.seed.is_finite() or self.seed <= 0:
            raise ValueError("the simulation seed must be a positive finite Decimal")

    @property
    def account(self) -> str:
        return f"{SIM_ACCOUNT_PREFIX}{self.run_id}"


@dataclass(frozen=True)
class SimulatedCash:
    """The simulation account's cash. ``net_cash_out`` is signed: a profitable round trip is below zero."""

    seed: Decimal
    reserved: Decimal
    net_cash_out: Decimal

    @property
    def available(self) -> Decimal:
        return self.seed - self.net_cash_out - self.reserved


@dataclass(frozen=True)
class SimulatedBooking:
    """One booked simulated fill, as stored. ``net_cash_out`` and ``realised_pnl`` are signed."""

    booking_id: str
    proposal_id: str
    side: str
    ticker: str
    delta_quantity: Decimal
    cumulative_quantity: Decimal
    price: Decimal
    gross: Decimal
    charges: Decimal
    net_cash_out: Decimal
    realised_pnl: Decimal
    schedule_sha256: str
    scenario_set_sha256: str
    attempt_id: str
    payload_sha256: str
    created_at: str


def simulated_booking_id(attempt_id: str, scenario_set_sha256: str) -> str:
    """``sha256(attempt_id, scenario_set_sha256, "k3")``: one booking per attempt and scenario set."""

    text = json.dumps(
        [attempt_id, scenario_set_sha256, SIM_BOOKING_KIND],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _install_simulation_tables(
    connection: sqlite3.Connection, init: SimulationInit, created_at: str
) -> None:
    """Create the marker and the simulation tables with their triggers. Caller owns the transaction.

    Called only from ``_initialize_simulation_locked``, never from ``_pin_fresh_file`` or an
    ordinary open. The marker is immutable.
    """

    connection.execute(
        f"""
        CREATE TABLE {_SIM_MARKER_TABLE} (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            run_id TEXT NOT NULL,
            workspace TEXT NOT NULL,
            venue TEXT NOT NULL,
            account_id TEXT NOT NULL,
            currency TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        f"""
        CREATE TABLE {_SIM_FILLS_TABLE} (
            booking_id TEXT PRIMARY KEY,
            proposal_id TEXT NOT NULL REFERENCES order_intents(proposal_id),
            workspace TEXT NOT NULL,
            side TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
            ticker TEXT NOT NULL,
            delta_quantity TEXT NOT NULL,
            cumulative_quantity TEXT NOT NULL,
            price TEXT NOT NULL,
            gross TEXT NOT NULL,
            charges TEXT NOT NULL,
            net_cash_out TEXT NOT NULL,
            realised_pnl TEXT NOT NULL,
            schedule_sha256 TEXT NOT NULL,
            scenario_set_sha256 TEXT NOT NULL,
            attempt_id TEXT NOT NULL,
            payload_sha256 TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        f"""
        CREATE TABLE {_SIM_CASH_TABLE} (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            workspace TEXT NOT NULL,
            account TEXT NOT NULL,
            currency TEXT NOT NULL,
            seed TEXT NOT NULL,
            reserved TEXT NOT NULL,
            net_cash_out TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    kinds = ", ".join(f"'{kind}'" for kind in _SIM_CASH_KINDS)
    connection.execute(
        f"""
        CREATE TABLE {_SIM_EVENTS_TABLE} (
            event_id TEXT PRIMARY KEY,
            workspace TEXT NOT NULL,
            proposal_id TEXT NOT NULL REFERENCES order_intents(proposal_id),
            kind TEXT NOT NULL CHECK (kind IN ({kinds})),
            reserved_delta TEXT NOT NULL,
            cash_out_delta TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    for table, events in (
        (_SIM_MARKER_TABLE, ("UPDATE", "DELETE")),
        (_SIM_FILLS_TABLE, ("UPDATE", "DELETE")),
        (_SIM_CASH_TABLE, ("DELETE",)),
        (_SIM_EVENTS_TABLE, ("UPDATE", "DELETE")),
    ):
        for event in events:
            connection.execute(
                f"""
                CREATE TRIGGER {table}_no_{event.lower()}
                BEFORE {event} ON {table}
                BEGIN
                    SELECT RAISE(ABORT, '{table} rows are append-only');
                END
                """
            )
    for table, key in _SIM_KEYS:
        # SQLite runs no DELETE trigger for the row an INSERT OR REPLACE (or REPLACE, or an upsert)
        # removes unless recursive_triggers is on, and that is a per-connection pragma. A BEFORE
        # INSERT trigger fires whatever the pragma is, so a replacement is refused on any connection.
        connection.execute(
            f"""
            CREATE TRIGGER {table}_no_replace
            BEFORE INSERT ON {table}
            WHEN EXISTS (SELECT 1 FROM {table} WHERE {key} = NEW.{key} OR rowid = NEW.rowid)
            BEGIN
                SELECT RAISE(ABORT, '{table} rows are append-only');
            END
            """
        )
    for table in _SIMULATION_TABLES:
        connection.execute(
            f"""
            CREATE TRIGGER {table}_workspace_insert_guard
            BEFORE INSERT ON {table}
            WHEN NEW.workspace IS NOT {_PIN_SUBQUERY}
            BEGIN
                SELECT RAISE(ABORT, '{_WORKSPACE_GUARD_MESSAGE}');
            END
            """
        )
    connection.execute(
        f"""
        CREATE TRIGGER {_SIM_CASH_TABLE}_identity_immutable
        BEFORE UPDATE ON {_SIM_CASH_TABLE}
        WHEN NEW.workspace IS NOT OLD.workspace OR NEW.account IS NOT OLD.account
          OR NEW.currency IS NOT OLD.currency OR NEW.seed IS NOT OLD.seed
        BEGIN
            SELECT RAISE(ABORT, 'simulated cash identity and seed are immutable');
        END
        """
    )
    connection.execute(
        f"INSERT INTO {_SIM_MARKER_TABLE} "
        "(singleton, run_id, workspace, venue, account_id, currency, created_at) "
        "VALUES (1, ?, ?, ?, ?, ?, ?)",
        (
            init.run_id,
            init.workspace.value,
            init.binding.venue,
            init.binding.account_id,
            init.binding.currency,
            created_at,
        ),
    )


def _signed_decimal(value: object) -> Decimal:
    """A finite Decimal of either sign, for simulation cash and realised P&L only.

    ``_decimal`` refuses negatives and stays that way for every other field. A malformed or
    non-finite value (``abc``, empty, ``None``, ``NaN``, ``sNaN``, ``Infinity``) is a ledger fault.
    """

    if isinstance(value, float):
        raise LedgerError("ledger contains a float where a Decimal is required")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise LedgerError("ledger contains an invalid Decimal amount") from exc
    if not parsed.is_finite():
        raise LedgerError("ledger contains a non-finite Decimal amount")
    return parsed


def _signed_decimal_str(value: Decimal) -> str:
    return format(_signed_decimal(value), "f")


def _payload_decimal_text(value: Decimal) -> str:
    """One text per numeric value (``10`` and ``10.0`` are the same), for payload hashes."""

    return format(value.normalize() if value != 0 else Decimal(0), "f")


def _apply_base_schema(connection: sqlite3.Connection, from_version: int) -> None:
    """Create or upgrade the v1 to v5 table set. The caller owns the transaction."""

    if from_version == 1:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(dispatch_attempts)").fetchall()
        }
        if "approval_id" not in columns:
            connection.execute(
                "ALTER TABLE dispatch_attempts ADD COLUMN approval_id TEXT "
                "REFERENCES execution_approvals(approval_id)"
            )
    for statement in _BASE_SCHEMA_STATEMENTS:
        connection.execute(statement)
    if from_version < 3:
        budget_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(paper_budgets)").fetchall()
        }
        if budget_columns and "reserved" not in budget_columns:
            connection.execute(
                "ALTER TABLE paper_budgets ADD COLUMN reserved TEXT NOT NULL DEFAULT '0'"
            )
    if from_version < 5:
        requote_columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(requote_intents)").fetchall()
        }
        if requote_columns and "replacement_proposal_id" not in requote_columns:
            connection.execute(
                "ALTER TABLE requote_intents ADD COLUMN replacement_proposal_id TEXT NOT NULL DEFAULT ''"
            )


_WORKSPACE_GUARD_MESSAGE = "workspace does not match ledger identity"
_PIN_SUBQUERY = "(SELECT workspace FROM ledger_identity WHERE singleton = 1)"
_INSERT_GUARDED_TABLES = (
    "approval_keys",
    "approval_challenges",
    "execution_approvals",
    "execution_admissions",
    "paper_budgets",
    "buying_power_reservations",
    "workspace_controls",
    "workspace_control_events",
    "paper_positions",
)
_UPDATE_GUARDED_TABLES = (
    "paper_budgets",
    "buying_power_reservations",
    "workspace_controls",
    "workspace_control_events",
    "paper_positions",
    "execution_admissions",
)


def _install_identity(
    connection: sqlite3.Connection,
    workspace: Workspace,
    pinned_by: str,
    pinned_at: str,
) -> None:
    """Pin the file to ``workspace`` and arm the SQLite guards. Caller owns the transaction."""

    pinned = coerce_workspace(workspace)
    connection.execute(
        """
        CREATE TABLE ledger_identity (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            workspace TEXT NOT NULL CHECK (workspace IN ('uk', 'india')),
            pinned_at TEXT NOT NULL,
            pinned_by TEXT NOT NULL
                CHECK (pinned_by IN ('first-open', 'operator-confirmed-migration'))
        )
        """
    )
    for event in ("UPDATE", "DELETE"):
        connection.execute(
            f"""
            CREATE TRIGGER ledger_identity_no_{event.lower()}
            BEFORE {event} ON ledger_identity
            BEGIN
                SELECT RAISE(ABORT, 'ledger identity is immutable');
            END
            """
        )
    connection.execute(
        _no_replace_trigger_sql("ledger_identity", _NO_REPLACE_KEYS["ledger_identity"])
    )
    connection.execute(
        "INSERT INTO ledger_identity (singleton, workspace, pinned_at, pinned_by) "
        "VALUES (1, ?, ?, ?)",
        (pinned.value, pinned_at, pinned_by),
    )
    # A generated column needs no UPDATE, so the order_intents immutability
    # triggers stay in place. It is hidden from table_info; look in table_xinfo.
    order_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_xinfo(order_intents)").fetchall()
    }
    if "workspace" not in order_columns:
        connection.execute(
            "ALTER TABLE order_intents ADD COLUMN workspace TEXT "
            "GENERATED ALWAYS AS (json_extract(canonical_json, '$.workspace')) VIRTUAL"
        )
    connection.execute(
        f"""
        CREATE TRIGGER IF NOT EXISTS order_intents_workspace_insert_guard
        BEFORE INSERT ON order_intents
        WHEN json_extract(NEW.canonical_json, '$.workspace') IS NOT {_PIN_SUBQUERY}
        BEGIN
            SELECT RAISE(ABORT, '{_WORKSPACE_GUARD_MESSAGE}');
        END
        """
    )
    for table in _INSERT_GUARDED_TABLES:
        connection.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_workspace_insert_guard
            BEFORE INSERT ON {table}
            WHEN NEW.workspace IS NOT {_PIN_SUBQUERY}
            BEGIN
                SELECT RAISE(ABORT, '{_WORKSPACE_GUARD_MESSAGE}');
            END
            """
        )
    for table in _UPDATE_GUARDED_TABLES:
        connection.execute(
            f"""
            CREATE TRIGGER IF NOT EXISTS {table}_workspace_update_guard
            BEFORE UPDATE OF workspace ON {table}
            WHEN NEW.workspace IS NOT OLD.workspace
            BEGIN
                SELECT RAISE(ABORT, '{_WORKSPACE_GUARD_MESSAGE}');
            END
            """
        )
    connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION:d}")


def _ro_uri(path: Path) -> str:
    """Read-only SQLite URI. as_uri percent-encodes spaces in the real ledger path."""

    return path.resolve().as_uri() + "?mode=ro"


class ExecutionLedger:
    """SQLite-backed execution authority for one local workspace."""

    def __init__(
        self,
        path: os.PathLike[str] | str | None = None,
        *,
        workspace: Workspace | str,
        busy_timeout_ms: int = 5_000,
        require_approval: bool = False,
        venue: Optional[VenueBinding] = None,
        simulation: Optional[SimulationInit] = None,
    ) -> None:
        if not 1 <= busy_timeout_ms <= 60_000:
            raise ValueError("busy_timeout_ms must be between 1 and 60000")

        self.workspace: Workspace = coerce_workspace(workspace)
        # Fixed by ``_validate_simulation_ledger_locked`` on every open; false until then.
        self._simulation_init = simulation
        self._simulation_active = False
        if simulation is not None and (simulation.binding != venue or simulation.workspace != self.workspace):
            raise ValueError("the simulation identity must match the requested workspace and venue")
        if venue is not None:
            if not isinstance(venue, VenueBinding):
                raise ValueError("venue must be a VenueBinding")
            spec = spec_for(venue.venue)
            if spec is None:
                raise LedgerVenueMismatch("the ledger venue is not registered")
            if self.workspace.value != spec.workspace:
                raise LedgerVenueMismatch(
                    f"this venue's ledger exists only for the {spec.workspace} workspace"
                )
        # None means a paper ledger. A bound ledger is only ever created or
        # reopened by passing the binding it was created with.
        self.venue_binding: Optional[VenueBinding] = venue
        self.require_approval = require_approval
        if path is not None:
            self.path = Path(path)
        elif venue is not None:
            self.path = spec.ledger_path()
        else:
            self.path = default_ledger_path(self.workspace)
        if venue is not None and _same_file_path(self.path, default_ledger_path(self.workspace)):
            # D-01: a bound ledger is never the workspace's real ledger, even
            # when that file is absent or empty. Refused before any file, lock
            # or directory is created or opened.
            raise LedgerVenueMismatch(
                "a bound venue ledger must not use the workspace's real ledger path"
            )
        if simulation is not None:
            self._require_simulation_path(simulation)
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
            # Decide ownership on a read-only connection before anything can
            # write: _configure switches the journal mode, which rewrites the
            # header of a rollback-journal file, so a refused open must never
            # reach it.
            identity = self._probe_identity()
            if identity is not None:
                self._enforce_identity(identity)
                self._enforce_venue(identity)
                if identity.kind != "fresh":
                    # An existing file is validated on a read-only connection, so a file that is
                    # not exactly what the caller asked for is refused with its bytes untouched.
                    self._probe_simulation_ledger()
            self._connection = sqlite3.connect(
                self.path,
                timeout=busy_timeout_ms / 1_000,
                isolation_level=None,
                check_same_thread=False,
            )
            self._connection.row_factory = sqlite3.Row
            if identity is None:
                _require_sqlite_capabilities(self._connection)
            self._configure(busy_timeout_ms)
            if identity is None or identity.kind == "fresh":
                self._pin_fresh_file()
            # Every open, fresh or reopen, validates the simulation marker before any startup
            # recovery can touch the file. An existing file was already validated read-only above;
            # this repeats the check on the writer connection and is the only one a fresh file has.
            self._validate_simulation_ledger_locked(self._require_connection())
            self._ensure_no_replace_guards()
            os.chmod(self.path, 0o600)
            self.recover_abandoned_submissions()
            self.recover_pending_requotes()
        except Exception:
            self.close()
            raise

    def __enter__(self) -> "ExecutionLedger":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def require_workspace(self, workspace: object) -> Workspace:
        """Return the coerced workspace when it equals the pin, else raise."""

        try:
            requested = coerce_workspace(workspace)
        except ValueError:
            raise WorkspaceMismatch(
                f"request names no valid workspace; ledger is pinned to {self.workspace.value}"
            ) from None
        if requested != self.workspace:
            raise WorkspaceMismatch(
                f"request names workspace {requested.value}; "
                f"ledger is pinned to {self.workspace.value}"
            )
        return requested

    def _ensure_no_replace_guards(self) -> None:
        """Arm the ``_no_replace`` triggers on a ledger that was created without them.

        Idempotent and schema-only: it adds missing triggers and rewrites no row. A ledger that
        already has every guard is not written at all.
        """

        connection = self._require_connection()
        existing = {
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT type, name FROM sqlite_master WHERE type IN ('table', 'trigger')"
            ).fetchall()
        }
        missing = [
            table
            for table in _NO_REPLACE_KEYS
            if ("table", table) in existing and ("trigger", f"{table}_no_replace") not in existing
        ]
        if not missing:
            return
        with self._transaction() as writer:
            for table in missing:
                writer.execute(_no_replace_trigger_sql(table, _NO_REPLACE_KEYS[table]))

    def _probe_identity(self) -> Optional[LedgerIdentity]:
        """Read the identity on a mode=ro connection. None means the file does not exist."""

        if not self.path.exists():
            return None
        probe = sqlite3.connect(_ro_uri(self.path), uri=True)
        try:
            _require_sqlite_capabilities(probe)
            return _read_identity(probe)
        finally:
            probe.close()

    def _enforce_identity(self, identity: LedgerIdentity) -> None:
        if identity.kind == "newer":
            raise LedgerError(
                f"ledger schema {identity.user_version} is newer than supported version "
                f"{SCHEMA_VERSION}"
            )
        if identity.kind == "unpinned":
            raise LedgerUnpinned(
                f"ledger {self.path} (schema {identity.user_version}) has no workspace pin. "
                "Ownership is never inferred. The operator must run "
                "scripts/ledger_tool.py inspect and then apply with an explicit "
                "confirmation."
            )
        if identity.kind == "pinned" and identity.workspace != self.workspace:
            pinned = identity.workspace.value if identity.workspace else "unknown"
            raise WorkspaceMismatch(
                f"ledger {self.path} is pinned to workspace {pinned}; "
                f"requested {self.workspace.value}"
            )

    def _enforce_venue(self, identity: LedgerIdentity) -> None:
        """Refuse a venue binding that is not exactly the one requested.

        Paper (no binding requested) and practice are different ledgers: a
        practice file never opens as paper, and a file with no binding (the
        real ledger of the workspace) never opens as practice.
        """

        if identity.kind != "pinned":
            return
        if identity.binding != self.venue_binding:
            raise LedgerVenueMismatch(
                f"ledger {self.path} venue binding does not match the requested venue"
            )

    def _pin_fresh_file(self) -> None:
        with self._transaction() as connection:
            # The writer lock is held, so this only guards a file created
            # between the probe and this transaction.
            if _read_identity(connection).kind != "fresh":
                raise LedgerError("ledger file changed while it was being opened")
            _apply_base_schema(connection, 0)
            now = _now()
            _install_identity(connection, self.workspace, "first-open", now)
            if self.venue_binding is not None:
                _install_venue_binding(connection, self.venue_binding, now)
            if self._simulation_init is not None:
                # Same transaction as the pin: a crash can never leave a pinned file with no marker.
                self._initialize_simulation_locked(connection, now)

    def _require_simulation_path(self, simulation: SimulationInit) -> None:
        """A simulation ledger lives only under ``india-shadow/<run_id>/``, never a live or paper path."""

        parent = self.path.parent
        if parent.name != simulation.run_id or parent.parent.name != SIMULATION_RUN_DIRECTORY:
            raise LedgerVenueMismatch(
                f"a simulation ledger lives only under {SIMULATION_RUN_DIRECTORY}/<run_id>/"
            )
        spec = spec_for(simulation.binding.venue)
        for forbidden in (default_ledger_path(self.workspace), spec.ledger_path() if spec else None):
            if forbidden is not None and _same_file_path(self.path, forbidden):
                raise LedgerVenueMismatch("a simulation ledger must not use a live or paper ledger path")

    def _initialize_simulation_locked(self, connection: sqlite3.Connection, now: str) -> None:
        """Marker, tables, triggers and the one seed row, in the caller's transaction (fresh file only)."""

        init = self._simulation_init
        if init is None:
            raise LedgerError("simulation initialization needs a SimulationInit")
        _install_simulation_tables(connection, init, now)
        connection.execute(
            f"INSERT INTO {_SIM_CASH_TABLE} "
            "(singleton, workspace, account, currency, seed, reserved, net_cash_out, updated_at) "
            "VALUES (1, ?, ?, ?, ?, '0', '0', ?)",
            (
                init.workspace.value,
                init.account,
                init.binding.currency,
                _decimal_str(init.seed),
                now,
            ),
        )

    def _probe_simulation_ledger(self) -> None:
        """Validate an existing file on a mode=ro connection, before any pragma or write."""

        probe = sqlite3.connect(_ro_uri(self.path), uri=True)
        try:
            probe.row_factory = sqlite3.Row
            self._validate_simulation_ledger_locked(probe)
        finally:
            probe.close()

    def _validate_simulation_ledger_locked(self, connection: sqlite3.Connection) -> None:
        """Decide, once per open, whether this file is a simulation ledger. Never repairs.

        (a) No marker, no simulation table, a non-``sim-`` account and no ``SimulationInit``:
        an ordinary ledger. (b) The marker, every table, trigger and the seed row present and equal
        to the ledger identity and to the caller's ``SimulationInit``: a simulation ledger.
        (c) Anything else aborts with ``LedgerError`` and writes nothing.
        """

        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        present = tables & set(_SIMULATION_TABLES)
        account = self.venue_binding.account_id if self.venue_binding is not None else ""
        init = self._simulation_init
        if not present:
            if init is not None or account.startswith(SIM_ACCOUNT_PREFIX):
                raise LedgerError("simulation ledger marker is missing")
            self._simulation_active = False
            return
        if _SIM_MARKER_TABLE not in tables:
            raise LedgerError("simulation tables exist without the simulation marker")
        if init is None:
            raise LedgerError("a simulation ledger must be opened with its SimulationInit")
        if present != set(_SIMULATION_TABLES):
            raise LedgerError("simulation ledger is missing a table")
        triggers = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        }
        if not _SIMULATION_TRIGGERS <= triggers:
            raise LedgerError("simulation ledger is missing a trigger")
        marker = connection.execute(
            f"SELECT run_id, workspace, venue, account_id, currency FROM {_SIM_MARKER_TABLE} "
            "WHERE singleton = 1"
        ).fetchone()
        if marker is None:
            raise LedgerError("simulation ledger marker row is missing")
        binding = self.venue_binding
        if (
            binding is None
            or str(marker["workspace"]) != self.workspace.value
            or str(marker["venue"]) != binding.venue
            or str(marker["account_id"]) != binding.account_id
            or str(marker["currency"]) != binding.currency
            or not binding.account_id.startswith(SIM_ACCOUNT_PREFIX)
            or str(marker["run_id"]) != init.run_id
            or binding != init.binding
        ):
            raise LedgerError("simulation marker does not match the ledger identity")
        cash = connection.execute(
            f"SELECT workspace, account, currency, seed FROM {_SIM_CASH_TABLE} WHERE singleton = 1"
        ).fetchone()
        if cash is None:
            raise LedgerError("simulation ledger cash row is missing")
        try:
            seed_matches = _decimal(cash["seed"]) == init.seed
        except LedgerError:
            seed_matches = False
        if (
            str(cash["workspace"]) != self.workspace.value
            or str(cash["account"]) != init.account
            or str(cash["currency"]) != binding.currency
            or not seed_matches
        ):
            raise LedgerError("simulation cash row does not match the simulation identity")
        self._simulation_active = True

    @property
    def is_simulation_ledger(self) -> bool:
        """True only for a file whose marker was validated on this open."""

        return self._simulation_active

    def _refuse_on_simulation(self, operation: str) -> None:
        if self._simulation_active:
            raise ApprovalConflict(f"{operation} is not available on a simulation ledger")

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
        if self._simulation_active:
            # A simulation ledger registers SHADOW intents for its own sim-<run_id> account only.
            self._require_intent_allowed(intent.model_dump(mode="json"))
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
            previous_state = str(order["state"])
            next_state = previous_state
            if admission.decision is AdmissionDecision.DENIED and previous_state == "PENDING":
                next_state = "REJECTED"
                connection.execute(
                    """
                    UPDATE order_projection
                    SET state = 'REJECTED', rejection_notes = ?, updated_at = ?
                    WHERE proposal_id = ? AND state = 'PENDING'
                    """,
                    (admission.reason_code, now, proposal_id),
                )
            self._append_event(
                connection,
                proposal_id,
                "ADMISSION_DECIDED",
                previous_state,
                next_state,
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
        self,
        account: str,
        currency: str,
        amount: Decimal | str | int | float,
        *,
        workspace: Workspace | str,
    ) -> PaperBudget:
        self.require_workspace(workspace)
        self._refuse_on_simulation("configuring a paper budget")
        binding = self.venue_binding
        if binding is not None and (
            account != binding.account_id or currency != binding.currency
        ):
            raise ApprovalConflict(
                "a practice ledger budget must use its bound account and currency"
            )
        amount_decimal = _positive_decimal(amount, "budget amount")
        now = _now()
        with self._transaction() as connection:
            return self._upsert_paper_budget(connection, account, currency, amount_decimal, now)

    def _upsert_paper_budget(
        self,
        connection: sqlite3.Connection,
        account: str,
        currency: str,
        amount_decimal: Decimal,
        now: str,
    ) -> PaperBudget:
        """Insert the budget or confirm it is unchanged. The caller owns the transaction."""

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

    def configure_venue_limits(
        self,
        capital_cap: Decimal | str | int | float,
        per_position_cap: Decimal | str | int | float,
        *,
        workspace: Workspace | str,
    ) -> PaperBudget:
        """Persist both venue caps once and set the bound budget to the capital cap.

        Bound-venue ledgers only (D-03). The first call stores both caps and the
        budget in one transaction. Every later call must pass the same two
        values: a change to either cap, not only the capital cap, raises
        ``ApprovalConflict`` and writes nothing. Changing caps means a new
        ledger. This stores the per-position cap; enforcing it is a later plan.
        """

        self.require_workspace(workspace)
        self._refuse_on_simulation("configuring venue limits")
        binding = self.venue_binding
        if binding is None:
            raise ApprovalConflict("venue limits exist only in a bound-venue ledger")
        capital = _positive_decimal(capital_cap, "capital cap")
        per_position = _positive_decimal(per_position_cap, "per-position cap")
        if per_position > capital:
            raise ApprovalConflict("per-position cap exceeds the capital cap")
        now = _now()
        with self._transaction() as connection:
            _install_venue_limits_table(connection)
            _install_quantity_reservations_table(connection)
            row = connection.execute(
                f"SELECT capital_cap, per_position_cap FROM {_VENUE_LIMITS_TABLE} "
                "WHERE singleton = 1"
            ).fetchone()
            if row is None:
                connection.execute(
                    f"INSERT INTO {_VENUE_LIMITS_TABLE} "
                    "(singleton, capital_cap, per_position_cap, configured_at) "
                    "VALUES (1, ?, ?, ?)",
                    (_decimal_str(capital), _decimal_str(per_position), now),
                )
            elif (
                _decimal(row["capital_cap"]) != capital
                or _decimal(row["per_position_cap"]) != per_position
            ):
                raise ApprovalConflict("venue limits are immutable once configured")
            return self._upsert_paper_budget(
                connection, binding.account_id, binding.currency, capital, now
            )

    def get_paper_budget(
        self, account: str, currency: str, *, workspace: Workspace | str
    ) -> Optional[PaperBudget]:
        self.require_workspace(workspace)
        with self._mutex:
            row = self._require_connection().execute(
                "SELECT * FROM paper_budgets WHERE workspace = ? AND account = ? AND currency = ?",
                (self.workspace, account, currency),
            ).fetchone()
        return self._budget_from_row(row) if row is not None else None

    def reserve_buying_power(
        self, proposal_id: str, *, india_caps: Optional[tuple[Decimal, Decimal]] = None
    ) -> PaperReservation:
        """Atomically check budget and reserve an admitted BUY notional.

        In the India paper ledger the caller must pass ``india_caps`` (capital cap, per-position
        cap): both are enforced inside this transaction at limit-price notional, and a missing
        pair refuses the reservation (63-04, C1).
        """

        self._refuse_on_simulation("reserving buying power")
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
            if self.workspace is Workspace.INDIA and self.venue_binding is None:
                if india_caps is None:
                    raise ApprovalConflict("india limits are required before reservation")
                self._assert_india_caps_locked(connection, admission, intent, india_caps, proposal_id)
            if self.venue_binding is not None:
                # D-18: held notional plus every other open BUY plus this order
                # must stay within the per-position cap, checked in this same
                # transaction so two reservations cannot both pass.
                limits = self._venue_limits_locked(connection)
                if limits is None:
                    raise ApprovalConflict("venue limits are required before reservation")
                held = self._held_position_locked(connection, admission)
                others = self._outstanding_buy_notional_locked(
                    connection, admission.ticker, exclude=proposal_id
                )
                if held[1] + others + admission.notional > limits[1]:
                    raise ApprovalConflict("per-position cap would be exceeded")
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
            connection = self._require_connection()
            row = connection.execute(
                "SELECT * FROM buying_power_reservations WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            if row is None and self.venue_binding is not None:
                # A practice SELL reserves held quantity, not buying power.
                row = connection.execute(
                    f"SELECT * FROM {_QUANTITY_TABLE} WHERE proposal_id = ?",
                    (proposal_id,),
                ).fetchone()
        return self._reservation_from_row(row) if row is not None else None

    def find_active_pending_reservation(
        self, account: str, *, workspace: Workspace | str
    ) -> Optional[str]:
        """Return the newest pending proposal that still owns this account's reservation."""

        self.require_workspace(workspace)
        with self._mutex:
            row = self._require_connection().execute(
                """
                SELECT reservation.proposal_id
                FROM buying_power_reservations AS reservation
                JOIN order_projection AS projection
                  ON projection.proposal_id = reservation.proposal_id
                WHERE reservation.workspace = ?
                  AND reservation.account = ?
                  AND reservation.state = 'ACTIVE'
                  AND projection.state = 'PENDING'
                ORDER BY reservation.created_at DESC
                LIMIT 1
                """,
                (self.workspace, account),
            ).fetchone()
        return str(row["proposal_id"]) if row is not None else None

    # --- bound-venue accounting (Phase 66, practice ledgers only) -------------
    #
    # Everything in this block is inert in a paper ledger: it needs a venue
    # binding, and the quantity-reservation table exists only in bound ledgers.

    def _require_bound(self) -> VenueBinding:
        if self.venue_binding is None:
            raise ApprovalConflict("this operation exists only in a bound-venue ledger")
        return self.venue_binding

    def _venue_limits_locked(
        self, connection: sqlite3.Connection
    ) -> Optional[tuple[Decimal, Decimal]]:
        try:
            row = connection.execute(
                f"SELECT capital_cap, per_position_cap FROM {_VENUE_LIMITS_TABLE} "
                "WHERE singleton = 1"
            ).fetchone()
        except sqlite3.OperationalError:
            return None
        if row is None:
            return None
        return _decimal(row["capital_cap"]), _decimal(row["per_position_cap"])

    def get_venue_limits(self) -> Optional[tuple[Decimal, Decimal]]:
        """``(capital_cap, per_position_cap)`` of a bound ledger, else None."""

        if self.venue_binding is None:
            return None
        with self._mutex:
            return self._venue_limits_locked(self._require_connection())

    @staticmethod
    def _held_position_locked(
        connection: sqlite3.Connection, admission: ExecutionAdmission
    ) -> tuple[Decimal, Decimal]:
        """``(quantity, cost notional)`` held for the admission's ticker."""

        row = connection.execute(
            "SELECT quantity, notional FROM paper_positions "
            "WHERE workspace = ? AND account = ? AND currency = ? AND ticker = ?",
            (admission.workspace, admission.account, admission.currency, admission.ticker),
        ).fetchone()
        if row is None:
            return Decimal("0"), Decimal("0")
        return _decimal(row["quantity"]), _decimal(row["notional"])

    @staticmethod
    def _outstanding_buy_notional_locked(
        connection: sqlite3.Connection, ticker: str, *, exclude: str = ""
    ) -> Decimal:
        total = Decimal("0")
        for row in connection.execute(
            """
            SELECT r.reserved, r.consumed, r.released
            FROM buying_power_reservations AS r
            JOIN execution_admissions AS a ON a.proposal_id = r.proposal_id
            WHERE r.state = 'ACTIVE' AND a.ticker = ? AND r.proposal_id != ?
            """,
            (ticker, exclude),
        ).fetchall():
            total += _decimal(row["reserved"]) - _decimal(row["consumed"]) - _decimal(
                row["released"]
            )
        return total

    @staticmethod
    def _outstanding_sell_quantity_locked(
        connection: sqlite3.Connection, ticker: str, *, exclude: str = ""
    ) -> Decimal:
        total = Decimal("0")
        for row in connection.execute(
            f"SELECT reserved, consumed, released FROM {_QUANTITY_TABLE} "
            "WHERE state = 'ACTIVE' AND ticker = ? AND proposal_id != ?",
            (ticker, exclude),
        ).fetchall():
            total += _decimal(row["reserved"]) - _decimal(row["consumed"]) - _decimal(
                row["released"]
            )
        return total

    def practice_headroom(self, account: str, currency: str, ticker: str) -> Mapping[str, Decimal]:
        """Read-only figures admission uses to deny before it records a decision.

        ``held_quantity`` and ``held_notional`` are the ledger's own reconciled
        position; ``open_buy_notional`` and ``open_sell_quantity`` are active
        reservations; ``budget_available`` is the bound budget's remainder.
        """

        self._require_bound()
        with self._mutex:
            connection = self._require_connection()
            row = connection.execute(
                "SELECT quantity, notional FROM paper_positions "
                "WHERE workspace = ? AND account = ? AND currency = ? AND ticker = ?",
                (self.workspace, account, currency, ticker),
            ).fetchone()
            held_quantity = _decimal(row["quantity"]) if row is not None else Decimal("0")
            held_notional = _decimal(row["notional"]) if row is not None else Decimal("0")
            budget = connection.execute(
                "SELECT * FROM paper_budgets WHERE workspace = ? AND account = ? AND currency = ?",
                (self.workspace, account, currency),
            ).fetchone()
            available = (
                self._budget_from_row(budget).available if budget is not None else Decimal("0")
            )
            return {
                "held_quantity": held_quantity,
                "held_notional": held_notional,
                "open_buy_notional": self._outstanding_buy_notional_locked(connection, ticker),
                "open_sell_quantity": self._outstanding_sell_quantity_locked(connection, ticker),
                "budget_available": available,
            }

    def reserve_sell_quantity(
        self, proposal_id: str, *, broker_available_quantity: Decimal | str | int
    ) -> PaperReservation:
        """Reserve held quantity for an admitted practice SELL (D-19).

        The quantity must not exceed the ledger's reconciled position minus the
        other open SELL reservations, and must not exceed the broker's own
        ``quantityAvailableForTrading``. Both checks and the insert share one
        transaction. A paper ledger has no such table and refuses.
        """

        self._require_bound()
        self._refuse_on_simulation("reserving against a broker quantity")
        broker_available = _decimal(broker_available_quantity)
        now = _now()
        with self._transaction() as connection:
            order = self._select_order(connection, proposal_id)
            if order is None:
                raise OrderNotFound(f"order {proposal_id!r} was not found")
            admission_row = connection.execute(
                "SELECT * FROM execution_admissions WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            if admission_row is None:
                raise ApprovalConflict("execution admission is required before reservation")
            admission = self._admission_from_row(admission_row)
            if admission.decision is not AdmissionDecision.ADMITTED:
                raise ApprovalConflict("execution admission denied the intent")
            if admission.intent_hash != str(order["intent_hash"]):
                raise IntentConflict("admission intent hash does not match stored intent")
            if admission.side is not OrderSide.SELL:
                raise ApprovalConflict("quantity reservation is for SELL orders only")
            if self._workspace_engaged_locked(connection):
                raise ApprovalConflict("workspace execution control is engaged")
            existing = connection.execute(
                f"SELECT * FROM {_QUANTITY_TABLE} WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            if existing is not None:
                return self._reservation_from_row(existing)
            held, _ = self._held_position_locked(connection, admission)
            others = self._outstanding_sell_quantity_locked(
                connection, admission.ticker, exclude=proposal_id
            )
            if admission.final_quantity > held - others:
                raise ApprovalConflict("SELL exceeds the reconciled position")
            if admission.final_quantity > broker_available:
                raise ApprovalConflict("SELL exceeds the broker's available quantity")
            connection.execute(
                f"""
                INSERT INTO {_QUANTITY_TABLE}
                    (proposal_id, workspace, account, currency, ticker, intent_hash,
                     reserved, state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
                """,
                (
                    proposal_id,
                    self.workspace,
                    admission.account,
                    admission.currency,
                    admission.ticker,
                    admission.intent_hash,
                    _decimal_str(admission.final_quantity),
                    now,
                    now,
                ),
            )
            self._append_event(
                connection,
                proposal_id,
                "QUANTITY_RESERVED",
                str(order["state"]),
                str(order["state"]),
                {
                    "ticker": admission.ticker,
                    "reserved": _decimal_str(admission.final_quantity),
                    "intent_hash": admission.intent_hash,
                },
                now,
            )
            row = connection.execute(
                f"SELECT * FROM {_QUANTITY_TABLE} WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise LedgerError("quantity reservation did not persist")
            return self._reservation_from_row(row)

    def _release_quantity_reservation_locked(
        self, connection: sqlite3.Connection, proposal_id: str, now: str
    ) -> None:
        if self.venue_binding is None:
            return
        row = connection.execute(
            f"SELECT * FROM {_QUANTITY_TABLE} WHERE proposal_id = ?", (proposal_id,)
        ).fetchone()
        if row is None:
            return
        outstanding = (
            _decimal(row["reserved"]) - _decimal(row["consumed"]) - _decimal(row["released"])
        )
        if outstanding <= 0:
            return
        connection.execute(
            f"UPDATE {_QUANTITY_TABLE} SET released = ?, state = 'SETTLED', updated_at = ? "
            "WHERE proposal_id = ?",
            (_decimal_str(_decimal(row["released"]) + outstanding), now, proposal_id),
        )

    # --- India paper SELL and the Mac's India limits (Phase 63-04) ------------
    #
    # No schema change. An India paper ledger has no quantity-reservation table (that
    # exists only in bound ledgers), so a SELL's open quantity is derived from the existing
    # admission and order-state rows, inside the same transaction that decides.

    def is_india_paper_sell(self, side: object) -> bool:
        """True for a SELL in the unbound India ledger: admitted and claimed without a reservation."""

        return (
            self.workspace is Workspace.INDIA
            and self.venue_binding is None
            and str(getattr(side, "value", side)) == OrderSide.SELL.value
        )

    @staticmethod
    def _india_open_sell_quantity_locked(
        connection: sqlite3.Connection,
        ticker: str,
        *,
        exclude: str = "",
        include_pending: bool,
    ) -> Decimal:
        """Admitted SELL quantity of ``ticker`` that has not reached a terminal state.

        ``include_pending`` adds SELLs that are admitted but not yet claimed. Admission counts
        them (an operator cannot queue more than is held). The challenge and the claim do
        not, so the first approval to be claimed wins and the later one is refused.
        """

        states = (("PENDING",) if include_pending else ()) + _SELL_CLAIMED_STATES
        placeholders = ", ".join("?" for _ in states)
        total = Decimal("0")
        for row in connection.execute(
            f"""
            SELECT a.final_quantity
            FROM execution_admissions AS a
            JOIN order_projection AS p ON p.proposal_id = a.proposal_id
            WHERE a.ticker = ? AND a.side = 'SELL' AND a.decision = 'ADMITTED'
              AND a.proposal_id != ? AND p.state IN ({placeholders})
            """,
            (ticker, exclude, *states),
        ).fetchall():
            total += _decimal(row["final_quantity"])
        return total

    def _assert_india_sell_headroom_locked(
        self, connection: sqlite3.Connection, admission: sqlite3.Row
    ) -> None:
        """Refuse a SELL that exceeds the held quantity minus every other claimed SELL."""

        held, _ = self._held_position_locked(connection, self._admission_from_row(admission))
        claimed = self._india_open_sell_quantity_locked(
            connection,
            str(admission["ticker"]),
            exclude=str(admission["proposal_id"]),
            include_pending=False,
        )
        if _decimal(admission["final_quantity"]) > held - claimed:
            raise ApprovalConflict(SELL_EXCEEDS_HOLDING)

    @staticmethod
    def _fills_exist(connection: sqlite3.Connection) -> bool:
        if connection.execute("SELECT 1 FROM paper_positions LIMIT 1").fetchone() is not None:
            return True
        return (
            connection.execute(
                "SELECT 1 FROM reconciliation_evidence "
                "WHERE CAST(cumulative_quantity AS REAL) > 0 LIMIT 1"
            ).fetchone()
            is not None
        )

    def has_fills(self) -> bool:
        """True when this ledger has ever recorded a fill (a position row or fill evidence).

        The Mac latch file uses it to tell a pilot start (no file, no fills) from a deleted
        file (no file, fills): the second must fail closed.
        """

        with self._mutex:
            return self._fills_exist(self._require_connection())

    @staticmethod
    def _india_positions_locked(
        connection: sqlite3.Connection, workspace: str
    ) -> tuple[tuple[str, Decimal, Decimal], ...]:
        return tuple(
            (str(row["ticker"]), _decimal(row["quantity"]), _decimal(row["notional"]))
            for row in connection.execute(
                "SELECT ticker, quantity, notional FROM paper_positions "
                "WHERE workspace = ? ORDER BY ticker",
                (workspace,),
            ).fetchall()
            if _decimal(row["quantity"]) > 0
        )

    @staticmethod
    def _india_open_buys_locked(
        connection: sqlite3.Connection, *, exclude: str = "", simulated: bool = False
    ) -> list[tuple[str, Decimal, Decimal]]:
        """``(ticker, unfilled quantity, limit price)`` of every ACTIVE buy reservation.

        The limit price is the intent's own, never the admission mid, so a cap check measures
        what the broker could be asked to pay. ``exclude`` leaves one order out (the order
        being rechecked must not count itself). ``simulated`` reads the filled quantity from the
        simulated fills instead of broker evidence; a simulation ledger holds no evidence rows.
        """

        open_buys: list[tuple[str, Decimal, Decimal]] = []
        for row in connection.execute(
            """
            SELECT a.proposal_id, a.ticker, a.final_quantity, a.price, i.canonical_json
            FROM buying_power_reservations AS r
            JOIN execution_admissions AS a ON a.proposal_id = r.proposal_id
            JOIN order_intents AS i ON i.proposal_id = r.proposal_id
            WHERE r.state = 'ACTIVE' AND a.side = 'BUY' AND a.decision = 'ADMITTED'
              AND a.proposal_id != ?
            ORDER BY a.ticker, a.proposal_id
            """,
            (exclude,),
        ).fetchall():
            if simulated:
                filled = ExecutionLedger._simulated_filled_quantity_locked(
                    connection, str(row["proposal_id"])
                )
            else:
                filled_row = connection.execute(
                    "SELECT cumulative_quantity FROM reconciliation_evidence "
                    "WHERE proposal_id = ? ORDER BY evidence_id DESC LIMIT 1",
                    (str(row["proposal_id"]),),
                ).fetchone()
                filled = _decimal(filled_row["cumulative_quantity"]) if filled_row else Decimal("0")
            unfilled = _decimal(row["final_quantity"]) - filled
            if unfilled <= 0:
                continue
            limit = json.loads(str(row["canonical_json"])).get("limit_price")
            open_buys.append(
                (
                    str(row["ticker"]),
                    unfilled,
                    _decimal(limit) if limit is not None else _decimal(row["price"]),
                )
            )
        return open_buys

    def _assert_india_caps_locked(
        self,
        connection: sqlite3.Connection,
        admission: ExecutionAdmission,
        intent: Mapping[str, Any],
        caps: tuple[Decimal, Decimal],
        proposal_id: str,
    ) -> None:
        """Both India caps, at limit-price notional, in the reservation's own transaction.

        Admission checks the same caps earlier, but two admissions can both pass before either
        reserves. This check runs under the writer transaction that inserts the reservation, so
        the second reservation sees the first and is refused (C1).
        """

        capital_cap, per_position_cap = caps
        limit = intent.get("limit_price")
        price = _decimal(limit) if limit is not None else admission.price
        notional = admission.final_quantity * price
        costs: dict[str, Decimal] = {}
        for ticker, _quantity, cost in self._india_positions_locked(connection, self.workspace.value):
            costs[ticker] = costs.get(ticker, Decimal("0")) + cost
        pending: dict[str, Decimal] = {}
        if self._simulation_active:
            self._assert_no_reconciliation_evidence_locked(connection)
        for ticker, unfilled, limit_price in self._india_open_buys_locked(
            connection, exclude=proposal_id, simulated=self._simulation_active
        ):
            pending[ticker] = pending.get(ticker, Decimal("0")) + unfilled * limit_price
        deployed = sum(costs.values(), Decimal("0")) + sum(pending.values(), Decimal("0")) + notional
        if deployed > capital_cap:
            raise IndiaCapExceeded("capital_cap")
        own = costs.get(admission.ticker, Decimal("0")) + pending.get(admission.ticker, Decimal("0")) + notional
        if own > per_position_cap:
            raise IndiaCapExceeded("per_position_cap")

    def india_account_view(self, *, exclude_proposal_id: str = "") -> IndiaAccountView:
        """Positions, open buys and open sells for the Mac's India rules, read in one pass.

        ``exclude_proposal_id`` leaves that order's own reservation and open sell out, for a
        recheck of an order that is already admitted.
        """

        if self.workspace is not Workspace.INDIA:
            raise WorkspaceMismatch("the India account view exists only in the India ledger")
        with self._mutex:
            connection = self._require_connection()
            positions = self._india_positions_locked(connection, self.workspace.value)
            if self._simulation_active:
                self._assert_no_reconciliation_evidence_locked(connection)
                return IndiaAccountView(
                    positions,
                    tuple(
                        self._india_open_buys_locked(
                            connection, exclude=exclude_proposal_id, simulated=True
                        )
                    ),
                    MappingProxyType(
                        self._simulated_open_sell_quantity_locked(
                            connection, exclude=exclude_proposal_id
                        )
                    ),
                )
            open_buys = self._india_open_buys_locked(connection, exclude=exclude_proposal_id)
            open_sells: dict[str, Decimal] = {}
            states = ("PENDING",) + _SELL_CLAIMED_STATES
            for row in connection.execute(
                f"""
                SELECT a.ticker, a.final_quantity
                FROM execution_admissions AS a
                JOIN order_projection AS p ON p.proposal_id = a.proposal_id
                WHERE a.side = 'SELL' AND a.decision = 'ADMITTED' AND a.proposal_id != ?
                  AND p.state IN ({", ".join("?" for _ in states)})
                """,
                (exclude_proposal_id, *states),
            ).fetchall():
                ticker = str(row["ticker"])
                open_sells[ticker] = open_sells.get(ticker, Decimal("0")) + _decimal(
                    row["final_quantity"]
                )
        return IndiaAccountView(positions, tuple(open_buys), MappingProxyType(open_sells))

    # --- the simulation account (Phase 64-07) ---------------------------------
    #
    # Everything below branches on the marker validated at open. A paper or practice ledger
    # refuses each method, and nothing here touches ``paper_budgets``, ``reconciliation_evidence``
    # or a broker figure. Simulated money moves in Python ``Decimal`` inside the one transaction
    # that books it, never through SQL arithmetic.

    def _require_simulation(self, operation: str) -> None:
        if not self._simulation_active:
            raise ApprovalConflict(f"{operation} exists only on a simulation ledger")

    @staticmethod
    def _simulated_filled_quantity_locked(
        connection: sqlite3.Connection, proposal_id: str
    ) -> Decimal:
        total = Decimal("0")
        for row in connection.execute(
            f"SELECT delta_quantity FROM {_SIM_FILLS_TABLE} WHERE proposal_id = ?",
            (proposal_id,),
        ).fetchall():
            total += _decimal(row["delta_quantity"])
        return total

    @staticmethod
    def _simulated_open_sell_quantity_locked(
        connection: sqlite3.Connection, *, exclude: str = ""
    ) -> dict[str, Decimal]:
        """Ticker to the remaining quantity of every admitted SELL not yet FILLED or EXPIRED.

        Remaining is the admitted quantity minus the simulated fills booked so far, so a partial
        fill shrinks the open sell and a booking that leaves the projection PENDING is visible.
        """

        open_sells: dict[str, Decimal] = {}
        for row in connection.execute(
            """
            SELECT a.proposal_id, a.ticker, a.final_quantity
            FROM execution_admissions AS a
            JOIN order_projection AS p ON p.proposal_id = a.proposal_id
            WHERE a.side = 'SELL' AND a.decision = 'ADMITTED' AND a.proposal_id != ?
              AND p.state IN ('PENDING', 'PARTIALLY_FILLED')
            """,
            (exclude,),
        ).fetchall():
            remaining = _decimal(row["final_quantity"]) - ExecutionLedger._simulated_filled_quantity_locked(
                connection, str(row["proposal_id"])
            )
            if remaining > 0:
                ticker = str(row["ticker"])
                open_sells[ticker] = open_sells.get(ticker, Decimal("0")) + remaining
        return open_sells

    @staticmethod
    def _assert_no_reconciliation_evidence_locked(connection: sqlite3.Connection) -> None:
        """A simulation ledger never holds broker evidence, so the two filled sources cannot mix."""

        if connection.execute("SELECT 1 FROM reconciliation_evidence LIMIT 1").fetchone() is not None:
            raise LedgerError("a simulation ledger holds reconciliation evidence")

    def _simulated_cash_locked(self, connection: sqlite3.Connection) -> SimulatedCash:
        """The running cash row, checked against the Decimal sum of its events. Abort-class on drift."""

        row = connection.execute(
            f"SELECT seed, reserved, net_cash_out FROM {_SIM_CASH_TABLE} WHERE singleton = 1"
        ).fetchone()
        if row is None:
            raise LedgerError("simulation cash row is missing")
        if connection.execute("SELECT 1 FROM paper_budgets LIMIT 1").fetchone() is not None:
            raise LedgerError("a simulation ledger holds a paper budget row")
        cash = SimulatedCash(
            seed=_decimal(row["seed"]),
            reserved=_decimal(row["reserved"]),
            net_cash_out=_signed_decimal(row["net_cash_out"]),
        )
        events_reserved = Decimal("0")
        events_cash_out = Decimal("0")
        for event in connection.execute(
            f"SELECT reserved_delta, cash_out_delta FROM {_SIM_EVENTS_TABLE} ORDER BY rowid"
        ).fetchall():
            events_reserved += _signed_decimal(event["reserved_delta"])
            events_cash_out += _signed_decimal(event["cash_out_delta"])
        if events_reserved != cash.reserved or events_cash_out != cash.net_cash_out:
            raise LedgerError("simulated cash differs from the sum of its events")
        return cash

    def _apply_simulated_cash_locked(
        self,
        connection: sqlite3.Connection,
        *,
        event_id: str,
        proposal_id: str,
        kind: str,
        reserved_delta: Decimal,
        cash_out_delta: Decimal,
        now: str,
    ) -> SimulatedCash:
        """Read the row, add in Decimal, write it back and append the event, in the caller's transaction."""

        cash = self._simulated_cash_locked(connection)
        reserved = cash.reserved + reserved_delta
        net_cash_out = cash.net_cash_out + cash_out_delta
        if reserved < 0:
            raise LedgerError("simulated cash reserved would go below zero")
        updated = SimulatedCash(cash.seed, reserved, net_cash_out)
        if kind == "RESERVE" and updated.available < 0:
            raise SimulatedCashInsufficient("simulated cash is insufficient for this reservation")
        if kind == "BOOK_BUY" and updated.available < 0:
            raise SimulatedCashNegative("this booking would take simulated cash below zero")
        connection.execute(
            f"UPDATE {_SIM_CASH_TABLE} SET reserved = ?, net_cash_out = ?, updated_at = ? "
            "WHERE singleton = 1",
            (_decimal_str(reserved), _signed_decimal_str(net_cash_out), now),
        )
        connection.execute(
            f"""
            INSERT INTO {_SIM_EVENTS_TABLE}
                (event_id, workspace, proposal_id, kind, reserved_delta, cash_out_delta, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                self.workspace,
                proposal_id,
                kind,
                _signed_decimal_str(reserved_delta),
                _signed_decimal_str(cash_out_delta),
                now,
            ),
        )
        return updated

    def simulated_cash_view(self) -> SimulatedCash:
        """The simulation account's cash. Raises ``LedgerError`` on drift from its events."""

        self._require_simulation("the simulated cash view")
        with self._mutex:
            return self._simulated_cash_locked(self._require_connection())

    @staticmethod
    def _apply_simulated_position_fill_locked(
        connection: sqlite3.Connection,
        admission: ExecutionAdmission,
        delta_quantity: Decimal,
        cost: Decimal,
        now: str,
    ) -> None:
        """Add a simulated buy to the position in Python ``Decimal``, inside the booking transaction.

        ``_apply_position_fill_locked`` adds TEXT through SQLite numeric arithmetic, which rounds
        ``1166.666666666666666666666667`` to ``1166.66666666667``. A simulation never uses it.
        """

        row = connection.execute(
            "SELECT quantity, notional FROM paper_positions "
            "WHERE workspace = ? AND account = ? AND currency = ? AND ticker = ?",
            (admission.workspace, admission.account, admission.currency, admission.ticker),
        ).fetchone()
        if row is None:
            connection.execute(
                """
                INSERT INTO paper_positions
                    (workspace, account, currency, ticker, quantity, notional, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    admission.workspace,
                    admission.account,
                    admission.currency,
                    admission.ticker,
                    _decimal_str(delta_quantity),
                    _decimal_str(cost),
                    now,
                ),
            )
            return
        connection.execute(
            "UPDATE paper_positions SET quantity = ?, notional = ?, updated_at = ? "
            "WHERE workspace = ? AND account = ? AND currency = ? AND ticker = ?",
            (
                _decimal_str(_decimal(row["quantity"]) + delta_quantity),
                _decimal_str(_decimal(row["notional"]) + cost),
                now,
                admission.workspace,
                admission.account,
                admission.currency,
                admission.ticker,
            ),
        )

    def _simulated_order_locked(
        self, connection: sqlite3.Connection, proposal_id: str, side: OrderSide
    ) -> tuple[sqlite3.Row, Mapping[str, Any], ExecutionAdmission]:
        """The stored order, its intent and its ADMITTED admission, checked for a simulation write."""

        order = self._select_order(connection, proposal_id)
        if order is None:
            raise OrderNotFound(f"order {proposal_id!r} was not found")
        intent = json.loads(str(order["canonical_json"]))
        self._require_intent_allowed(intent)
        admission_row = connection.execute(
            "SELECT * FROM execution_admissions WHERE proposal_id = ?", (proposal_id,)
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
        if admission.side is not side:
            raise ApprovalConflict(f"this operation is for {side.value} orders only")
        if self._workspace_engaged_locked(connection):
            raise ApprovalConflict("workspace execution control is engaged")
        return order, intent, admission

    def reserve_simulated_buy(
        self, proposal_id: str, *, india_caps: tuple[Decimal, Decimal]
    ) -> PaperReservation:
        """Reserve an admitted simulated BUY at limit notional, with both India caps, in one transaction.

        The reservation row is the usual ``buying_power_reservations`` row and its ``created_at``
        is the pinned submission time (P-05). The simulated cash and both caps are checked in the
        same writer transaction that inserts it, so two admissions racing to reserve cannot both pass.
        """

        self._require_simulation("reserve_simulated_buy")
        now = _now()
        with self._transaction() as connection:
            order, intent, admission = self._simulated_order_locked(
                connection, proposal_id, OrderSide.BUY
            )
            existing_row = connection.execute(
                "SELECT * FROM buying_power_reservations WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            if existing_row is not None:
                existing = self._reservation_from_row(existing_row)
                if existing.intent_hash != admission.intent_hash:
                    raise IntentConflict("reservation intent hash does not match")
                return existing
            limit = intent.get("limit_price")
            if limit is None or admission.notional != admission.final_quantity * _decimal(limit):
                raise IntentConflict("admission notional is not the limit notional")
            cash = self._simulated_cash_locked(connection)
            if cash.available < admission.notional:
                raise SimulatedCashInsufficient("simulated cash is insufficient for this reservation")
            self._assert_india_caps_locked(connection, admission, intent, india_caps, proposal_id)
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
            self._apply_simulated_cash_locked(
                connection,
                event_id=f"reservation:{proposal_id}",
                proposal_id=proposal_id,
                kind="RESERVE",
                reserved_delta=admission.notional,
                cash_out_delta=Decimal("0"),
                now=now,
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
                "SELECT * FROM buying_power_reservations WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise LedgerError("reservation did not persist")
            return self._reservation_from_row(row)

    def reserve_simulated_sell(self, proposal_id: str) -> PaperReservation:
        """Reserve simulated holdings for an admitted SELL. Never reads a broker figure."""

        self._require_simulation("reserve_simulated_sell")
        now = _now()
        with self._transaction() as connection:
            order, _intent, admission = self._simulated_order_locked(
                connection, proposal_id, OrderSide.SELL
            )
            existing = connection.execute(
                f"SELECT * FROM {_QUANTITY_TABLE} WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            if existing is not None:
                return self._reservation_from_row(existing)
            held, _cost = self._held_position_locked(connection, admission)
            others = self._simulated_open_sell_quantity_locked(
                connection, exclude=proposal_id
            ).get(admission.ticker, Decimal("0"))
            if admission.final_quantity > held - others:
                raise ApprovalConflict(SELL_EXCEEDS_HOLDING)
            connection.execute(
                f"""
                INSERT INTO {_QUANTITY_TABLE}
                    (proposal_id, workspace, account, currency, ticker, intent_hash,
                     reserved, state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
                """,
                (
                    proposal_id,
                    self.workspace,
                    admission.account,
                    admission.currency,
                    admission.ticker,
                    admission.intent_hash,
                    _decimal_str(admission.final_quantity),
                    now,
                    now,
                ),
            )
            self._append_event(
                connection,
                proposal_id,
                "QUANTITY_RESERVED",
                str(order["state"]),
                str(order["state"]),
                {
                    "ticker": admission.ticker,
                    "reserved": _decimal_str(admission.final_quantity),
                    "intent_hash": admission.intent_hash,
                },
                now,
            )
            row = connection.execute(
                f"SELECT * FROM {_QUANTITY_TABLE} WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                raise LedgerError("quantity reservation did not persist")
            return self._reservation_from_row(row)

    def get_simulated_reservation(
        self, proposal_id: str
    ) -> Optional[tuple[PaperReservation, str]]:
        """The order's reservation and its ``created_at``, buy (buying power) or sell (quantity)."""

        self._require_simulation("get_simulated_reservation")
        with self._mutex:
            connection = self._require_connection()
            row = connection.execute(
                "SELECT * FROM buying_power_reservations WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            if row is None:
                row = connection.execute(
                    f"SELECT * FROM {_QUANTITY_TABLE} WHERE proposal_id = ?", (proposal_id,)
                ).fetchone()
        if row is None:
            return None
        return self._reservation_from_row(row), str(row["created_at"])

    @staticmethod
    def _booking_from_row(row: sqlite3.Row) -> SimulatedBooking:
        return SimulatedBooking(
            booking_id=str(row["booking_id"]),
            proposal_id=str(row["proposal_id"]),
            side=str(row["side"]),
            ticker=str(row["ticker"]),
            delta_quantity=_decimal(row["delta_quantity"]),
            cumulative_quantity=_decimal(row["cumulative_quantity"]),
            price=_decimal(row["price"]),
            gross=_decimal(row["gross"]),
            charges=_decimal(row["charges"]),
            net_cash_out=_signed_decimal(row["net_cash_out"]),
            realised_pnl=_signed_decimal(row["realised_pnl"]),
            schedule_sha256=str(row["schedule_sha256"]),
            scenario_set_sha256=str(row["scenario_set_sha256"]),
            attempt_id=str(row["attempt_id"]),
            payload_sha256=str(row["payload_sha256"]),
            created_at=str(row["created_at"]),
        )

    def simulated_booking(self, booking_id: str) -> Optional[SimulatedBooking]:
        self._require_simulation("simulated_booking")
        with self._mutex:
            row = self._require_connection().execute(
                f"SELECT * FROM {_SIM_FILLS_TABLE} WHERE booking_id = ?", (booking_id,)
            ).fetchone()
        return self._booking_from_row(row) if row is not None else None

    def simulated_bookings(self, proposal_id: Optional[str] = None) -> list[SimulatedBooking]:
        self._require_simulation("simulated_bookings")
        sql = f"SELECT * FROM {_SIM_FILLS_TABLE}"
        parameters: tuple[object, ...] = ()
        if proposal_id is not None:
            sql += " WHERE proposal_id = ?"
            parameters = (proposal_id,)
        sql += " ORDER BY rowid"
        with self._mutex:
            rows = self._require_connection().execute(sql, parameters).fetchall()
        return [self._booking_from_row(row) for row in rows]

    def book_simulated_fill(
        self,
        booking_id: str,
        proposal_id: str,
        cumulative_quantity: Decimal | str | int,
        price: Decimal | str | int,
        charges: Decimal | str | int,
        schedule_sha256: str,
        scenario_set_sha256: str,
        attempt_id: str,
    ) -> SimulatedBooking:
        """Book one simulated fill: fill row, reservation, position, cash, P&L and projection, in one transaction.

        ``cumulative_quantity`` is the order's cumulative booked quantity after this fill. The same
        booking id with the same payload is a no-op; with another payload it is ``IntentConflict``.
        Every refusal changes nothing.
        """

        self._require_simulation("book_simulated_fill")
        if booking_id != simulated_booking_id(attempt_id, scenario_set_sha256):
            raise InvalidTransition("booking id is not sha256(attempt, scenario set, k3)")
        for label, digest in (("schedule", schedule_sha256), ("scenario set", scenario_set_sha256)):
            if not isinstance(digest, str) or _SHA256_PATTERN.fullmatch(digest) is None:
                raise ValueError(f"{label} sha256 is malformed")
        if not isinstance(attempt_id, str) or not 1 <= len(attempt_id) <= 128:
            raise ValueError("attempt id is malformed")
        cumulative = _positive_decimal(cumulative_quantity, "cumulative quantity")
        fill_price = _positive_decimal(price, "fill price")
        fee = _decimal(charges)
        payload_sha256 = hashlib.sha256(
            canonical_json(
                {
                    "booking_id": booking_id,
                    "proposal_id": proposal_id,
                    "cumulative_quantity": _payload_decimal_text(cumulative),
                    "price": _payload_decimal_text(fill_price),
                    "charges": _payload_decimal_text(fee),
                    "schedule_sha256": schedule_sha256,
                    "scenario_set_sha256": scenario_set_sha256,
                    "attempt_id": attempt_id,
                }
            ).encode("utf-8")
        ).hexdigest()
        now = _now()
        with self._transaction() as connection:
            existing = connection.execute(
                f"SELECT * FROM {_SIM_FILLS_TABLE} WHERE booking_id = ?", (booking_id,)
            ).fetchone()
            if existing is not None:
                if str(existing["payload_sha256"]) != payload_sha256:
                    raise IntentConflict("booking id was reused with a different payload")
                return self._booking_from_row(existing)
            order = self._select_order(connection, proposal_id)
            if order is None:
                raise OrderNotFound(f"order {proposal_id!r} was not found")
            side = OrderSide(str(json.loads(str(order["canonical_json"]))["side"]))
            order, intent, admission = self._simulated_order_locked(connection, proposal_id, side)
            state = str(order["state"])
            if state not in ("PENDING", "PARTIALLY_FILLED"):
                raise InvalidTransition(f"cannot book a fill on an order in {state}")
            limit = _decimal(intent["limit_price"])
            if (side is OrderSide.BUY and fill_price > limit) or (
                side is OrderSide.SELL and fill_price < limit
            ):
                raise InvalidTransition("a simulated fill is never worse than the limit")
            prior = self._simulated_filled_quantity_locked(connection, proposal_id)
            if cumulative > admission.final_quantity:
                raise InvalidTransition("booking overfills the admitted quantity")
            delta = cumulative - prior
            if delta <= 0:
                raise InvalidTransition("booked quantity must advance")
            gross = delta * fill_price
            filled = cumulative == admission.final_quantity
            if side is OrderSide.BUY:
                reservation = connection.execute(
                    "SELECT * FROM buying_power_reservations WHERE proposal_id = ?", (proposal_id,)
                ).fetchone()
                if reservation is None or str(reservation["state"]) != "ACTIVE":
                    raise InvalidTransition("an active reservation is required to book a fill")
                reserved = _decimal(reservation["reserved"])
                consumed = _decimal(reservation["consumed"]) + delta * limit
                released = _decimal(reservation["released"])
                if filled:
                    released = reserved - consumed
                if consumed < 0 or released < 0 or consumed + released > reserved:
                    raise InvalidTransition("reservation accounting is invalid")
                released_now = released - _decimal(reservation["released"])
                connection.execute(
                    "UPDATE buying_power_reservations "
                    "SET consumed = ?, released = ?, state = ?, updated_at = ? WHERE proposal_id = ?",
                    (
                        _decimal_str(consumed),
                        _decimal_str(released),
                        "SETTLED" if filled else "ACTIVE",
                        now,
                        proposal_id,
                    ),
                )
                cost = gross + fee
                self._apply_simulated_position_fill_locked(connection, admission, delta, cost, now)
                cash_out = cost
                realised = Decimal("0")
                self._apply_simulated_cash_locked(
                    connection,
                    event_id=f"booking:{booking_id}",
                    proposal_id=proposal_id,
                    kind="BOOK_BUY",
                    reserved_delta=-(delta * limit + released_now),
                    cash_out_delta=cash_out,
                    now=now,
                )
            else:
                position = connection.execute(
                    "SELECT quantity, notional FROM paper_positions "
                    "WHERE workspace = ? AND account = ? AND currency = ? AND ticker = ?",
                    (admission.workspace, admission.account, admission.currency, admission.ticker),
                ).fetchone()
                held = _decimal(position["quantity"]) if position is not None else Decimal("0")
                held_cost = _decimal(position["notional"]) if position is not None else Decimal("0")
                if delta > held:
                    raise InvalidTransition("a simulated SELL exceeds the simulated holding")
                reservation = connection.execute(
                    f"SELECT * FROM {_QUANTITY_TABLE} WHERE proposal_id = ?", (proposal_id,)
                ).fetchone()
                if reservation is None or str(reservation["state"]) != "ACTIVE":
                    raise InvalidTransition("an active reservation is required to book a fill")
                reserved = _decimal(reservation["reserved"])
                consumed = _decimal(reservation["consumed"]) + delta
                released = _decimal(reservation["released"])
                if filled:
                    released = reserved - consumed
                if consumed < 0 or released < 0 or consumed + released > reserved:
                    raise InvalidTransition("quantity reservation accounting is invalid")
                connection.execute(
                    f"UPDATE {_QUANTITY_TABLE} "
                    "SET consumed = ?, released = ?, state = ?, updated_at = ? WHERE proposal_id = ?",
                    (
                        _decimal_str(consumed),
                        _decimal_str(released),
                        "SETTLED" if filled else "ACTIVE",
                        now,
                        proposal_id,
                    ),
                )
                remaining = held - delta
                remaining_cost = (
                    Decimal("0") if remaining == 0 else max(Decimal("0"), held_cost - held_cost * delta / held)
                )
                cost_removed = held_cost - remaining_cost
                self._apply_position_sell_locked(connection, admission, delta, now)
                net_proceeds = gross - fee
                cash_out = -net_proceeds
                realised = net_proceeds - cost_removed
                self._apply_simulated_cash_locked(
                    connection,
                    event_id=f"booking:{booking_id}",
                    proposal_id=proposal_id,
                    kind="BOOK_SELL",
                    reserved_delta=Decimal("0"),
                    cash_out_delta=cash_out,
                    now=now,
                )
            connection.execute(
                f"""
                INSERT INTO {_SIM_FILLS_TABLE}
                    (booking_id, proposal_id, workspace, side, ticker, delta_quantity,
                     cumulative_quantity, price, gross, charges, net_cash_out, realised_pnl,
                     schedule_sha256, scenario_set_sha256, attempt_id, payload_sha256, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    booking_id,
                    proposal_id,
                    self.workspace,
                    side.value,
                    admission.ticker,
                    _decimal_str(delta),
                    _decimal_str(cumulative),
                    _decimal_str(fill_price),
                    _decimal_str(gross),
                    _decimal_str(fee),
                    _signed_decimal_str(cash_out),
                    _signed_decimal_str(realised),
                    schedule_sha256,
                    scenario_set_sha256,
                    attempt_id,
                    payload_sha256,
                    now,
                ),
            )
            target = "FILLED" if filled else "PARTIALLY_FILLED"
            connection.execute(
                "UPDATE order_projection SET state = ?, updated_at = ? WHERE proposal_id = ?",
                (target, now, proposal_id),
            )
            self._append_event(
                connection,
                proposal_id,
                SIMULATED_BOOKING_EVENT,
                state,
                target,
                {
                    "booking_id": booking_id,
                    "cumulative_quantity": _decimal_str(cumulative),
                    "price": _decimal_str(fill_price),
                    "charges": _decimal_str(fee),
                },
                now,
            )
            row = connection.execute(
                f"SELECT * FROM {_SIM_FILLS_TABLE} WHERE booking_id = ?", (booking_id,)
            ).fetchone()
            return self._booking_from_row(row)

    def expire_simulated_order(self, proposal_id: str, expiry_id: str, reason: str) -> LedgerOrder:
        """Close a simulated order and release what it still holds, in one transaction.

        Only a recorded expiry releases a DAY remainder: no timer, restart or elapsed time does. The
        order goes to EXPIRED; a buy releases its outstanding notional and the same amount of
        simulated cash, a sell releases its quantity. Holdings never change. The same expiry id on
        an EXPIRED order is a no-op; another id is ``IntentConflict``.
        """

        self._require_simulation("expire_simulated_order")
        if not isinstance(reason, str) or _REASON_CODE_PATTERN.fullmatch(reason) is None:
            raise ValueError("expiry reason must be a short, non-sensitive code")
        if not isinstance(expiry_id, str) or _EXPIRY_ID_PATTERN.fullmatch(expiry_id) is None:
            raise ValueError("expiry id is malformed")
        now = _now()
        with self._transaction() as connection:
            row = self._select_order(connection, proposal_id)
            if row is None:
                raise OrderNotFound(f"order {proposal_id!r} was not found")
            state = str(row["state"])
            if state == "EXPIRED":
                recorded = [
                    json.loads(str(event["payload_json"])).get("expiry_id")
                    for event in connection.execute(
                        "SELECT payload_json FROM execution_events "
                        "WHERE proposal_id = ? AND event_type = ?",
                        (proposal_id, SIMULATED_EXPIRY_EVENT),
                    ).fetchall()
                ]
                if expiry_id not in recorded:
                    raise IntentConflict("the order was already expired under another expiry id")
                return self._order_from_row(row)
            if state not in ("PENDING", "PARTIALLY_FILLED"):
                raise InvalidTransition(f"cannot expire an order in {state}")
            released_notional = Decimal("0")
            reservation = connection.execute(
                "SELECT * FROM buying_power_reservations WHERE proposal_id = ?", (proposal_id,)
            ).fetchone()
            if reservation is not None:
                released_notional = (
                    _decimal(reservation["reserved"])
                    - _decimal(reservation["consumed"])
                    - _decimal(reservation["released"])
                )
                self._release_full_reservation_locked(connection, reservation, now)
                if released_notional > 0:
                    self._apply_simulated_cash_locked(
                        connection,
                        event_id=f"expiry:{proposal_id}:{expiry_id}",
                        proposal_id=proposal_id,
                        kind="EXPIRE",
                        reserved_delta=-released_notional,
                        cash_out_delta=Decimal("0"),
                        now=now,
                    )
            self._release_quantity_reservation_locked(connection, proposal_id, now)
            connection.execute(
                "UPDATE order_projection SET state = 'EXPIRED', updated_at = ? "
                "WHERE proposal_id = ? AND state = ?",
                (now, proposal_id, state),
            )
            self._append_event(
                connection,
                proposal_id,
                SIMULATED_EXPIRY_EVENT,
                state,
                "EXPIRED",
                {
                    "expiry_id": expiry_id,
                    "reason": reason,
                    "released_notional": _decimal_str(released_notional),
                },
                now,
            )
            return self._get_order_locked(connection, proposal_id)

    def record_shadow_vm_decision(
        self, proposal_id: str, audit_seq: int, decision: Mapping[str, Any]
    ) -> None:
        """Record the VM's answer for an intent, keyed by its audit seq. Evidence only.

        A refusal never changes the reservation or the projection. The same seq with the same
        decision is a no-op; with another decision it is ``IntentConflict``.
        """

        self._require_simulation("record_shadow_vm_decision")
        if not isinstance(audit_seq, int) or isinstance(audit_seq, bool) or audit_seq < 0:
            raise ValueError("audit seq must be a non-negative integer")
        payload = {"audit_seq": audit_seq, **dict(decision)}
        text = canonical_json(payload)
        now = _now()
        with self._transaction() as connection:
            row = self._select_order(connection, proposal_id)
            if row is None:
                raise OrderNotFound(f"order {proposal_id!r} was not found")
            for event in connection.execute(
                "SELECT payload_json FROM execution_events WHERE proposal_id = ? AND event_type = ?",
                (proposal_id, SHADOW_VM_DECISION_EVENT),
            ).fetchall():
                recorded = json.loads(str(event["payload_json"]))
                if recorded.get("audit_seq") == audit_seq:
                    if canonical_json(recorded) != text:
                        raise IntentConflict("the VM decision for this audit seq differs")
                    return
            state = str(row["state"])
            self._append_event(
                connection, proposal_id, SHADOW_VM_DECISION_EVENT, state, state, payload, now
            )

    def record_exit_batch(self, proposal_id: str, batch_id: str, reason: str) -> None:
        """Store a halve, flatten or stop batch tag with a registered SELL proposal.

        The tag is a ledger event, not an intent field, so the immutable intent and its hash
        are unchanged. Repeating the same tag is a no-op; a different one is refused.
        """

        if self.workspace is not Workspace.INDIA or self.venue_binding is not None:
            raise ApprovalConflict("exit batches exist only in the India paper ledger")
        if _BATCH_ID_PATTERN.fullmatch(batch_id) is None or reason not in EXIT_BATCH_REASONS:
            raise ValueError("exit batch id or reason is invalid")
        payload = {"batch_id": batch_id, "reason": reason}
        now = _now()
        with self._transaction() as connection:
            row = self._select_order(connection, proposal_id)
            if row is None:
                raise OrderNotFound(f"order {proposal_id!r} was not found")
            existing = connection.execute(
                "SELECT payload_json FROM execution_events "
                "WHERE proposal_id = ? AND event_type = ?",
                (proposal_id, EXIT_BATCH_EVENT),
            ).fetchone()
            if existing is not None:
                if json.loads(str(existing["payload_json"])) != payload:
                    raise IntentConflict("the proposal already belongs to another exit batch")
                return
            state = str(row["state"])
            self._append_event(connection, proposal_id, EXIT_BATCH_EVENT, state, state, payload, now)

    def get_exit_batch(self, proposal_id: str) -> Optional[Mapping[str, str]]:
        """``{"batch_id", "reason"}`` for a registered exit proposal, else None."""

        with self._mutex:
            row = self._require_connection().execute(
                "SELECT payload_json FROM execution_events "
                "WHERE proposal_id = ? AND event_type = ? ORDER BY event_id LIMIT 1",
                (proposal_id, EXIT_BATCH_EVENT),
            ).fetchone()
        if row is None:
            return None
        payload = json.loads(str(row["payload_json"]))
        return {"batch_id": str(payload["batch_id"]), "reason": str(payload["reason"])}

    def _assert_no_in_flight_locked(
        self,
        connection: sqlite3.Connection,
        intent: Mapping[str, Any],
        proposal_id: str,
    ) -> None:
        """D-16: refuse a claim while the same ticker has an order that may be at the broker.

        Run inside the claim transaction, so two approvals cannot both pass it.
        Bound-venue ledgers only: paper ledgers keep today's behaviour.
        """

        if self.venue_binding is None:
            return
        placeholders = ", ".join("?" for _ in _IN_FLIGHT_STATES)
        row = connection.execute(
            f"""
            SELECT i.proposal_id
            FROM order_intents AS i
            JOIN order_projection AS p ON p.proposal_id = i.proposal_id
            WHERE json_extract(i.canonical_json, '$.ticker') = ?
              AND json_extract(i.canonical_json, '$.account') = ?
              AND i.proposal_id != ?
              AND p.state IN ({placeholders})
            LIMIT 1
            """,
            (str(intent.get("ticker")), str(intent.get("account")), proposal_id, *_IN_FLIGHT_STATES),
        ).fetchone()
        if row is not None:
            raise ApprovalConflict("ticker already has an order in flight")

    def list_orders(self, states: Optional[Iterable[str]] = None) -> list[LedgerOrder]:
        """Orders, oldest first, optionally limited to the given states."""

        sql = (
            "SELECT i.*, p.state, p.acknowledgment_json, p.updated_at "
            "FROM order_intents AS i JOIN order_projection AS p USING (proposal_id)"
        )
        parameters: tuple[object, ...] = ()
        if states is not None:
            wanted = tuple(states)
            if not wanted:
                return []
            sql += " WHERE p.state IN (%s)" % ", ".join("?" for _ in wanted)
            parameters = wanted
        sql += " ORDER BY i.created_at, i.proposal_id"
        with self._mutex:
            rows = self._require_connection().execute(sql, parameters).fetchall()
        for row in rows:
            self._check_row_workspace(row)
        return [self._order_from_row(row) for row in rows]

    def known_broker_order_ids(self) -> set[str]:
        """Every broker order id this ledger has stored in an acknowledgement."""

        found: set[str] = set()
        for order in self.list_orders():
            if order.acknowledgment is not None:
                found.add(order.acknowledgment.broker_order_id)
        return found

    def record_audit_event(
        self, proposal_id: str, event_type: str, payload: Mapping[str, Any]
    ) -> None:
        """Append one allow-listed audit event; the order's state does not change."""

        self._require_bound()
        if event_type not in _AUDIT_EVENT_TYPES:
            raise ValueError("audit event type is not allowed")
        now = _now()
        with self._transaction() as connection:
            row = self._select_order(connection, proposal_id)
            if row is None:
                raise OrderNotFound(f"order {proposal_id!r} was not found")
            state = str(row["state"])
            self._append_event(connection, proposal_id, event_type, state, state, payload, now)

    def record_cancel_requested(self, proposal_id: str) -> str:
        """Write the cancel event before any DELETE and return the stored broker id (D-22).

        Only an acknowledged or partially filled order with a stored broker id
        can be cancelled, and only once: a second request raises, so a cancel
        is never sent twice.
        """

        self._require_bound()
        now = _now()
        with self._transaction() as connection:
            row = self._select_order(connection, proposal_id)
            if row is None:
                raise OrderNotFound(f"order {proposal_id!r} was not found")
            state = str(row["state"])
            if state not in {"ACKNOWLEDGED", "PARTIALLY_FILLED"} or not row["acknowledgment_json"]:
                raise InvalidTransition(
                    f"cannot cancel an order in {state} without an acknowledgement"
                )
            already = connection.execute(
                "SELECT 1 FROM execution_events "
                "WHERE proposal_id = ? AND event_type = 'CANCEL_REQUESTED'",
                (proposal_id,),
            ).fetchone()
            if already is not None:
                raise InvalidTransition("a cancel was already requested for this order")
            broker_order_id = _ack_from_json(str(row["acknowledgment_json"])).broker_order_id
            self._append_event(
                connection,
                proposal_id,
                "CANCEL_REQUESTED",
                state,
                state,
                {"broker_order_id": broker_order_id},
                now,
            )
            return broker_order_id

    def engage_workspace_control(
        self,
        reason_code: str = "MANUAL_KILL",
        *,
        workspace: Workspace | str,
        actor: str = "local",
        evidence_id: str = "",
    ) -> WorkspaceControl:
        self.require_workspace(workspace)
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

    def get_workspace_control(self, *, workspace: Workspace | str) -> WorkspaceControl:
        self.require_workspace(workspace)
        with self._mutex:
            return self._workspace_control_locked(self._require_connection())

    def clear_workspace_control(
        self,
        *,
        workspace: Workspace | str,
        version: int,
        evidence_id: str,
        purpose: str = "growin.execution.control.clear",
    ) -> WorkspaceControl:
        self.require_workspace(workspace)
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
        self, key_id: str, public_key_x963: bytes, *, workspace: Workspace | str
    ) -> LedgerApprovalKey:
        """Enroll the first workspace key; replacement requires a later phase."""

        self.require_workspace(workspace)
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

    def get_approval_key(self, *, workspace: Workspace | str) -> Optional[LedgerApprovalKey]:
        self.require_workspace(workspace)
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
        expected_intent: Optional[Mapping[str, Any]] = None,
    ) -> LedgerApprovalChallenge:
        """Store one challenge. ``expected_intent`` is the Mac's own O3 intent for a relay order.

        It is written as an immutable ``RELAY_INTENT_EXPECTED`` event in the same transaction, so
        the claim can compare the VM's bytes with what the Mac built, not with a copy derived
        from those bytes.
        """

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
                "SELECT * FROM execution_admissions WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            if admission is None or str(admission["decision"]) != AdmissionDecision.ADMITTED.value:
                raise ApprovalConflict("admitted evidence is required before approval")
            if str(admission["intent_hash"]) != str(order["intent_hash"]):
                raise ApprovalConflict("approval admission does not match the immutable intent")
            if self.is_india_paper_sell(str(intent.get("side"))):
                # An India paper SELL holds no reservation. Its gate is the position: held
                # minus every other claimed SELL, inside this transaction (63-04, D-06).
                self._assert_india_sell_headroom_locked(connection, admission)
            else:
                sell_in_bound = (
                    self.venue_binding is not None
                    and str(intent.get("side")) == OrderSide.SELL.value
                )
                reservation = connection.execute(
                    (
                        f"SELECT state, intent_hash FROM {_QUANTITY_TABLE} WHERE proposal_id = ?"
                        if sell_in_bound
                        else "SELECT state, intent_hash FROM buying_power_reservations WHERE proposal_id = ?"
                    ),
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
            if expected_intent is not None:
                self._append_event(
                    connection,
                    proposal_id,
                    RELAY_INTENT_EXPECTED_EVENT,
                    "PENDING",
                    "PENDING",
                    {"challenge_id": challenge_id, "intent": dict(expected_intent)},
                    now,
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

        self._refuse_on_simulation("claiming an order for dispatch")
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
            relay_order = (
                isinstance(signed_payload, Mapping)
                and signed_payload.get("purpose") == RELAY_ORDER_PURPOSE
            )
            expected_payload = {} if relay_order else {
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
                "order_type": intent.get("order_type"),
                "limit_price": intent.get("limit_price"),
                "replaces_proposal_id": intent.get("replaces_proposal_id", ""),
                "requote_id": intent.get("requote_id", ""),
                "issued_at": int(challenge["issued_at_epoch"]),
                "expires_at": int(challenge["expires_at_epoch"]),
                "key_id": key_id,
            }
            if not isinstance(signed_payload, Mapping) or any(
                signed_payload.get(field) != value
                for field, value in expected_payload.items()
            ):
                raise ApprovalConflict("approval payload does not match stored intent")
            if relay_order and (
                self.venue_binding is None
                or str(intent.get("mode")) != "SHADOW"
                or set(signed_payload) != _RELAY_PAYLOAD_KEYS
                or signed_payload.get("version") != 1
                or signed_payload.get("challenge_id") != challenge_id
                or signed_payload.get("key_id") != key_id
                or signed_payload.get("issued_at") != int(challenge["issued_at_epoch"])
                or signed_payload.get("expires_at") != int(challenge["expires_at_epoch"])
            ):
                raise ApprovalConflict("relay order payload does not match the challenge")
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
            self._require_intent_allowed(intent)
            admission = connection.execute(
                "SELECT * FROM execution_admissions WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            if admission is None or str(admission["decision"]) != AdmissionDecision.ADMITTED.value:
                raise ApprovalConflict("admitted evidence is required before signed claim")
            if str(admission["intent_hash"]) != stored_intent_hash:
                raise ApprovalConflict("signed claim admission does not match the immutable intent")
            if relay_order:
                if not _relay_intent_is_expected(
                    connection, proposal_id, challenge_id, signed_payload.get("intent")
                ):
                    raise ApprovalConflict("relay order intent is not the intent the Mac expected")
                if not _relay_intent_matches(
                    signed_payload.get("intent"), intent, proposal_id, admission
                ):
                    raise ApprovalConflict("relay order intent does not match the admitted order")
            elif (
                str(signed_payload.get("admitted_quantity", "")) != str(admission["final_quantity"])
                or str(signed_payload.get("currency", "")) != str(admission["currency"])
                or str(signed_payload.get("price", "")) != str(admission["price"])
                or str(signed_payload.get("notional", "")) != str(admission["notional"])
                or str(signed_payload.get("evidence_hash", "")) != str(admission["evidence_hash"])
            ):
                raise ApprovalConflict("approval evidence does not match admitted quantity")
            if self.is_india_paper_sell(str(admission["side"])):
                # An India paper SELL holds no reservation. The position is checked here, in
                # the claim transaction, against every other claimed SELL, so two approvals
                # that each passed admission cannot both sell the same shares (63-04, D-06).
                self._assert_india_sell_headroom_locked(connection, admission)
            else:
                if str(admission["side"]) == OrderSide.SELL.value and self.venue_binding is not None:
                    # A practice SELL holds a quantity reservation, not buying power.
                    reservation = connection.execute(
                        f"SELECT * FROM {_QUANTITY_TABLE} WHERE proposal_id = ?",
                        (proposal_id,),
                    ).fetchone()
                else:
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
            self._assert_no_in_flight_locked(connection, intent, proposal_id)

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

        self._refuse_on_simulation("claiming an order for dispatch")
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
            self._require_intent_allowed(json.loads(str(row["canonical_json"])))
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
            self._assert_no_in_flight_locked(
                connection, json.loads(str(row["canonical_json"])), proposal_id
            )

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

    def acknowledge_local_requote_fixture(
        self, proposal_id: str, acknowledgment: OrderAck
    ) -> OrderAck:
        """Acknowledge the fixed Phase 52 local UAT parent without dispatching.

        This is not a general execution shortcut.  It is restricted to the
        single local fixture account used to demonstrate a cancelled parent
        and fresh replacement.  It deliberately creates no dispatch attempt.
        """

        if str(acknowledgment.proposal_id) != proposal_id:
            raise IntentConflict("acknowledgement proposal_id does not match the order")
        safe_ack = _safe_acknowledgment(acknowledgment)
        safe_json = canonical_json(safe_ack)
        now = _now()
        with self._transaction() as connection:
            row = self._select_order(connection, proposal_id)
            if row is None:
                raise OrderNotFound(f"order {proposal_id!r} was not found")
            intent = json.loads(str(row["canonical_json"]))
            if (
                intent.get("account") != "paper-requote-uat-v1"
                or intent.get("broker") != "paper"
                or intent.get("mode") != "PAPER"
                or acknowledgment.broker != "paper"
            ):
                raise ApprovalConflict("local fixture acknowledgement is not authorized")
            state = str(row["state"])
            if state == "ACKNOWLEDGED":
                if row["acknowledgment_json"] != safe_json:
                    raise IntentConflict("order already has a different acknowledgement")
                return _ack_from_json(safe_json, replay=True)
            if state != "PENDING":
                raise InvalidTransition(f"cannot locally acknowledge an order in {state}")
            connection.execute(
                """
                UPDATE order_projection
                SET state = 'ACKNOWLEDGED', acknowledgment_json = ?, updated_at = ?
                WHERE proposal_id = ? AND state = 'PENDING'
                """,
                (safe_json, now, proposal_id),
            )
            self._append_event(
                connection,
                proposal_id,
                "LOCAL_REQUOTE_UAT_FIXTURE_ACKNOWLEDGED",
                "PENDING",
                "ACKNOWLEDGED",
                json.loads(safe_json),
                now,
            )
        return _ack_from_json(safe_json)

    def reconcile(self, snapshot: ReconciliationSnapshot) -> LedgerOrder:
        """Apply one monotonic typed reconciliation snapshot atomically."""

        self._refuse_on_simulation("a broker reconciliation snapshot")
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
            # A practice SELL reconciles against its quantity reservation (D-19).
            is_sell = admission.side is OrderSide.SELL and self.venue_binding is not None
            reservation_row = connection.execute(
                (
                    f"SELECT * FROM {_QUANTITY_TABLE} WHERE proposal_id = ?"
                    if is_sell
                    else "SELECT * FROM buying_power_reservations WHERE proposal_id = ?"
                ),
                (snapshot.proposal_id,),
            ).fetchone()
            if reservation_row is None:
                raise ApprovalConflict("active reservation is required before reconciliation")
            reservation = self._reservation_from_row(reservation_row)
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
            if not is_sell and snapshot.cumulative_notional > admission.notional:
                # A BUY never costs more than the limit price it was reserved at.
                # A SELL's proceeds may exceed the limit notional (price improvement).
                raise InvalidTransition("reconciliation exceeds the admitted notional")
            if snapshot.cumulative_quantity > 0 and snapshot.cumulative_notional <= 0:
                raise InvalidTransition("filled quantity requires positive notional")
            if (snapshot.cumulative_quantity > prior_quantity) != (
                snapshot.cumulative_notional > prior_notional
            ):
                raise InvalidTransition("fill quantity and notional must advance together")
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
            delta_quantity = snapshot.cumulative_quantity - prior_quantity
            if is_sell:
                self._apply_quantity_reconciliation_locked(
                    connection, reservation, snapshot, delta_quantity, now
                )
                # Every validated positive fill delta moves the position, whatever
                # status carries it: a first-seen CANCELLED or UNKNOWN snapshot
                # that already shows a partial fill still consumed that quantity.
                if delta_quantity > 0:
                    self._apply_position_sell_locked(connection, admission, delta_quantity, now)
            else:
                self._apply_reservation_reconciliation_locked(
                    connection,
                    reservation,
                    admission,
                    snapshot,
                    delta_notional,
                    now,
                )
                if delta_quantity > 0 and delta_notional > 0:
                    self._apply_position_fill_locked(
                        connection, admission, delta_quantity, delta_notional, now
                    )
            # An adopted broker id must stick: a bound ledger stores the
            # acknowledgement on the first reconciliation that supplies one,
            # whatever state it adopts. A paper ledger keeps its old behaviour.
            if not row["acknowledgment_json"] and (
                target == "ACKNOWLEDGED" or self.venue_binding is not None
            ):
                generated_ack = OrderAck(
                    proposal_id=snapshot.proposal_id,
                    broker=(
                        self.venue_binding.venue if self.venue_binding is not None else "paper"
                    ),
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

    def get_latest_reconciliation(self, proposal_id: str) -> Optional[ReconciliationSnapshot]:
        """Return the latest durable local reconciliation evidence, if any."""

        with self._mutex:
            row = self._require_connection().execute(
                "SELECT * FROM reconciliation_evidence WHERE proposal_id = ? ORDER BY evidence_id DESC LIMIT 1",
                (proposal_id,),
            ).fetchone()
        if row is None:
            return None
        return ReconciliationSnapshot(
            proposal_id=str(row["proposal_id"]), broker_order_id=str(row["broker_order_id"]),
            source=str(row["source"]), cumulative_quantity=_decimal(row["cumulative_quantity"]),
            cumulative_notional=_decimal(row["cumulative_notional"]), status=str(row["status"]),
            evidence_fingerprint=str(row["evidence_fingerprint"]),
            observed_at=datetime.fromisoformat(str(row["observed_at"])),
        )

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

    def _apply_quantity_reconciliation_locked(
        self,
        connection: sqlite3.Connection,
        reservation: PaperReservation,
        snapshot: ReconciliationSnapshot,
        delta_quantity: Decimal,
        now: str,
    ) -> None:
        """Consume the SELL quantity reservation on a fill; settle it on a terminal state."""

        consumed = reservation.consumed + delta_quantity
        released = reservation.released
        terminal = snapshot.status in {
            ReconciliationStatus.FILLED,
            ReconciliationStatus.CANCELLED,
            ReconciliationStatus.REJECTED,
        }
        if terminal:
            released = reservation.reserved - consumed
        state = "SETTLED" if terminal else "ACTIVE"
        if consumed < 0 or released < 0 or consumed + released > reservation.reserved:
            raise InvalidTransition("quantity reservation accounting is invalid")
        connection.execute(
            f"UPDATE {_QUANTITY_TABLE} SET consumed = ?, released = ?, state = ?, updated_at = ? "
            "WHERE proposal_id = ?",
            (
                _decimal_str(consumed),
                _decimal_str(released),
                state,
                now,
                snapshot.proposal_id,
            ),
        )

    @staticmethod
    def _apply_position_sell_locked(
        connection: sqlite3.Connection,
        admission: ExecutionAdmission,
        delta_quantity: Decimal,
        now: str,
    ) -> None:
        """Reduce the position and return the sold cost basis (D-19).

        The cost basis removed is the position's average cost times the quantity
        sold, so the ticker's per-position headroom grows back by exactly that.
        """

        row = connection.execute(
            "SELECT quantity, notional FROM paper_positions "
            "WHERE workspace = ? AND account = ? AND currency = ? AND ticker = ?",
            (admission.workspace, admission.account, admission.currency, admission.ticker),
        ).fetchone()
        held = _decimal(row["quantity"]) if row is not None else Decimal("0")
        cost = _decimal(row["notional"]) if row is not None else Decimal("0")
        if delta_quantity > held:
            raise InvalidTransition("SELL fill exceeds the reconciled position")
        remaining = held - delta_quantity
        remaining_cost = Decimal("0") if remaining == 0 else cost - cost * delta_quantity / held
        connection.execute(
            "UPDATE paper_positions SET quantity = ?, notional = ?, updated_at = ? "
            "WHERE workspace = ? AND account = ? AND currency = ? AND ticker = ?",
            (
                _decimal_str(remaining),
                _decimal_str(max(Decimal("0"), remaining_cost)),
                now,
                admission.workspace,
                admission.account,
                admission.currency,
                admission.ticker,
            ),
        )

    def get_paper_position(
        self, account: str, currency: str, ticker: str, *, workspace: Workspace | str
    ) -> Optional[Mapping[str, str]]:
        self.require_workspace(workspace)
        with self._mutex:
            row = self._require_connection().execute(
                "SELECT quantity, notional FROM paper_positions WHERE workspace = ? AND account = ? AND currency = ? AND ticker = ?",
                (self.workspace, account, currency, ticker),
            ).fetchone()
        if row is None:
            return None
        return {"quantity": str(row["quantity"]), "notional": str(row["notional"])}

    def reject(self, proposal_id: str, notes: Optional[str] = None) -> LedgerOrder:
        if self._simulation_active and self.get_reservation(proposal_id) is not None:
            # A reserved simulated order is closed by ``expire_simulated_order``, which also
            # returns the simulated cash. A bare reject would leave that cash reserved.
            raise ApprovalConflict("a reserved simulated order is closed by expire_simulated_order")
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
            self._release_quantity_reservation_locked(connection, proposal_id, now)
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
                self._release_quantity_reservation_locked(connection, proposal_id, now)
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

    def record_requote_intent(
        self,
        *,
        requote_id: str,
        proposal_id: str,
        parent_intent_hash: str,
        parent_reconciliation_fingerprint: str,
        idempotency_key: str,
        snapshot_hash: str,
        candidate: Mapping[str, Any],
    ) -> LedgerRequote:
        """Record or replay one immutable, local-only candidate.

        This method never changes the parent order, its reservation, approval,
        dispatch state, or any transport.  The parent must still be a locally
        acknowledged BUY with an active reservation and the exact acknowledgement
        identity supplied as the reconciliation fingerprint.
        """

        for label, value in {
            "requote id": requote_id,
            "proposal id": proposal_id,
            "parent intent hash": parent_intent_hash,
            "parent reconciliation fingerprint": parent_reconciliation_fingerprint,
            "idempotency key": idempotency_key,
            "snapshot hash": snapshot_hash,
        }.items():
            if not isinstance(value, str) or not value:
                raise ValueError(f"{label} is required")
        payload = canonical_json(candidate)
        now = _now()
        with self._transaction() as connection:
            if self._workspace_engaged_locked(connection):
                raise RequoteConflict("workspace execution control is engaged")
            existing = connection.execute(
                "SELECT * FROM requote_intents WHERE requote_id = ? OR idempotency_key = ?",
                (requote_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                existing_requote = self._requote_from_row(existing)
                if (
                    existing_requote.requote_id != requote_id
                    or existing_requote.idempotency_key != idempotency_key
                    or existing_requote.proposal_id != proposal_id
                    or existing_requote.parent_intent_hash != parent_intent_hash
                    or existing_requote.parent_reconciliation_fingerprint
                    != parent_reconciliation_fingerprint
                    or existing_requote.snapshot_hash != snapshot_hash
                    or canonical_json(existing_requote.candidate) != payload
                ):
                    raise RequoteConflict("re-quote idempotency identity was reused")
                return existing_requote

            order = self._select_order(connection, proposal_id)
            if order is None:
                raise OrderNotFound(f"order {proposal_id!r} was not found")
            if str(order["intent_hash"]) != parent_intent_hash:
                raise RequoteConflict("parent immutable intent hash does not match")
            if str(order["state"]) != "ACKNOWLEDGED":
                raise RequoteConflict("parent order is not eligible for local re-quote")
            if not order["acknowledgment_json"]:
                raise RequoteConflict("parent acknowledgement is required")
            acknowledgment = _ack_from_json(str(order["acknowledgment_json"]))
            expected_fingerprint = f"ack:{acknowledgment.broker_order_id}"
            if parent_reconciliation_fingerprint != expected_fingerprint:
                raise RequoteConflict("parent acknowledgement identity does not match")
            reservation_row = connection.execute(
                "SELECT state, intent_hash FROM buying_power_reservations WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
            if (
                reservation_row is None
                or str(reservation_row["state"]) != "ACTIVE"
                or str(reservation_row["intent_hash"]) != parent_intent_hash
            ):
                raise RequoteConflict("parent active reservation is required")
            connection.execute(
                """
                INSERT INTO requote_intents
                    (requote_id, proposal_id, parent_intent_hash,
                     parent_reconciliation_fingerprint, idempotency_key,
                     snapshot_hash, candidate_json, state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'EVALUATED', ?, ?)
                """,
                (
                    requote_id,
                    proposal_id,
                    parent_intent_hash,
                    parent_reconciliation_fingerprint,
                    idempotency_key,
                    snapshot_hash,
                    payload,
                    now,
                    now,
                ),
            )
            self._append_requote_event(
                connection,
                requote_id,
                "REQUOTE_EVALUATED",
                None,
                "EVALUATED",
                {"proposal_id": proposal_id, "snapshot_hash": snapshot_hash},
                now,
            )
            row = connection.execute(
                "SELECT * FROM requote_intents WHERE requote_id = ?", (requote_id,)
            ).fetchone()
            if row is None:
                raise LedgerError("re-quote intent did not persist")
            return self._requote_from_row(row)

    def block_requote(self, requote_id: str, reason_code: str) -> LedgerRequote:
        """Stop a candidate locally without altering the parent order."""

        if not _REASON_CODE_PATTERN.fullmatch(reason_code):
            raise ValueError("reason_code must be a short, non-sensitive code")
        now = _now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM requote_intents WHERE requote_id = ?", (requote_id,)
            ).fetchone()
            if row is None:
                raise OrderNotFound(f"re-quote {requote_id!r} was not found")
            current = self._requote_from_row(row)
            target = f"BLOCKED_{reason_code}"
            if current.state == target:
                return current
            if current.state != "EVALUATED":
                raise InvalidTransition(
                    f"cannot block re-quote {requote_id!r} from {current.state}"
                )
            connection.execute(
                "UPDATE requote_intents SET state = ?, reason_code = ?, updated_at = ? WHERE requote_id = ?",
                (target, reason_code, now, requote_id),
            )
            self._append_requote_event(
                connection,
                requote_id,
                "REQUOTE_BLOCKED",
                current.state,
                target,
                {"reason_code": reason_code},
                now,
            )
            updated = connection.execute(
                "SELECT * FROM requote_intents WHERE requote_id = ?", (requote_id,)
            ).fetchone()
            if updated is None:
                raise LedgerError("re-quote intent disappeared")
            return self._requote_from_row(updated)

    def mark_requote_replacement_prepared(
        self, requote_id: str, replacement_proposal_id: str
    ) -> LedgerRequote:
        """Bind one fresh pending intent to a reconciled local candidate."""

        if not replacement_proposal_id:
            raise ValueError("replacement proposal id is required")
        now = _now()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM requote_intents WHERE requote_id = ?", (requote_id,)
            ).fetchone()
            if row is None:
                raise OrderNotFound(f"re-quote {requote_id!r} was not found")
            current = self._requote_from_row(row)
            if current.state == "REPLACEMENT_PREPARED":
                if current.replacement_proposal_id != replacement_proposal_id:
                    raise RequoteConflict("re-quote already prepared a different replacement")
                return current
            if current.state != "BLOCKED_NO_MUTATION_CAPABILITY":
                raise InvalidTransition("re-quote is not eligible for replacement preparation")
            replacement = self._select_order(connection, replacement_proposal_id)
            if replacement is None or str(replacement["state"]) != "PENDING":
                raise RequoteConflict("replacement must be a fresh pending intent")
            replacement_intent = json.loads(str(replacement["canonical_json"]))
            if (
                replacement_intent.get("replaces_proposal_id") != current.proposal_id
                or replacement_intent.get("requote_id") != requote_id
            ):
                raise RequoteConflict("replacement lineage does not match re-quote")
            connection.execute(
                "UPDATE requote_intents SET state = 'REPLACEMENT_PREPARED', replacement_proposal_id = ?, updated_at = ? WHERE requote_id = ?",
                (replacement_proposal_id, now, requote_id),
            )
            self._append_requote_event(
                connection, requote_id, "REPLACEMENT_PREPARED", current.state,
                "REPLACEMENT_PREPARED", {"replacement_proposal_id": replacement_proposal_id}, now,
            )
            updated = connection.execute(
                "SELECT * FROM requote_intents WHERE requote_id = ?", (requote_id,)
            ).fetchone()
            if updated is None:
                raise LedgerError("re-quote intent disappeared")
            return self._requote_from_row(updated)

    def get_requote(self, requote_id: str) -> Optional[LedgerRequote]:
        with self._mutex:
            row = self._require_connection().execute(
                "SELECT * FROM requote_intents WHERE requote_id = ?", (requote_id,)
            ).fetchone()
        return self._requote_from_row(row) if row is not None else None

    def list_requotes(self, proposal_id: Optional[str] = None) -> list[LedgerRequote]:
        sql = "SELECT * FROM requote_intents"
        parameters: tuple[object, ...] = ()
        if proposal_id is not None:
            sql += " WHERE proposal_id = ?"
            parameters = (proposal_id,)
        sql += " ORDER BY created_at, requote_id"
        with self._mutex:
            rows = self._require_connection().execute(sql, parameters).fetchall()
        return [self._requote_from_row(row) for row in rows]

    def list_requote_events(self, requote_id: str) -> list[Mapping[str, Any]]:
        with self._mutex:
            rows = self._require_connection().execute(
                "SELECT * FROM requote_events WHERE requote_id = ? ORDER BY event_id",
                (requote_id,),
            ).fetchall()
        return [
            {
                "event_id": int(row["event_id"]),
                "event_type": str(row["event_type"]),
                "from_state": row["from_state"],
                "to_state": str(row["to_state"]),
                "payload": json.loads(str(row["payload_json"])),
                "created_at": str(row["created_at"]),
            }
            for row in rows
        ]

    def recover_pending_requotes(self) -> int:
        """Fail closed after restart; fresh evidence is always required."""

        with self._mutex:
            rows = self._require_connection().execute(
                "SELECT requote_id FROM requote_intents WHERE state = 'EVALUATED'"
            ).fetchall()
        for row in rows:
            self.block_requote(str(row["requote_id"]), "RESTART_REQUIRES_FRESH_EVIDENCE")
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
        row = connection.execute(
            """
            SELECT i.*, p.state, p.acknowledgment_json, p.updated_at
            FROM order_intents AS i
            JOIN order_projection AS p USING (proposal_id)
            WHERE i.proposal_id = ? OR i.client_order_id = ?
            """,
            (proposal_id, client_order_id),
        ).fetchone()
        self._check_row_workspace(row)
        return row

    def _select_order(
        self, connection: sqlite3.Connection, proposal_id: str
    ) -> Optional[sqlite3.Row]:
        row = connection.execute(
            """
            SELECT i.*, p.state, p.acknowledgment_json, p.updated_at
            FROM order_intents AS i
            JOIN order_projection AS p USING (proposal_id)
            WHERE i.proposal_id = ?
            """,
            (proposal_id,),
        ).fetchone()
        self._check_row_workspace(row)
        return row

    def _require_intent_allowed(self, intent: Mapping[str, Any]) -> None:
        """Refuse an order this ledger's venue does not accept (D-08).

        LIVE is refused in every ledger. A paper ledger accepts PAPER only; a
        practice ledger accepts PRACTICE only, for its venue and bound account.
        """

        refusal = intent_refusal(
            intent.get("mode", ""),
            intent["broker"],
            intent["account"],
            self.venue_binding,
        )
        if refusal is not None:
            raise ApprovalConflict(refusal_text(refusal))

    @property
    def allowed_mode(self):
        """The one order mode this ledger accepts (single-mode venues and paper)."""

        return allowed_mode(self.venue_binding)

    @property
    def allowed_modes(self) -> frozenset[str]:
        """Every order mode this ledger accepts."""

        return allowed_modes(self.venue_binding)

    def _check_row_workspace(self, row: Optional[sqlite3.Row]) -> None:
        """Defense in depth: a stored intent must carry this ledger's workspace."""

        if row is not None and row["workspace"] != self.workspace.value:
            raise WorkspaceMismatch(
                f"stored order {row['proposal_id']!r} carries workspace "
                f"{row['workspace']!r}; ledger is pinned to {self.workspace.value}"
            )

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
    def _requote_from_row(row: sqlite3.Row) -> LedgerRequote:
        return LedgerRequote(
            requote_id=str(row["requote_id"]),
            proposal_id=str(row["proposal_id"]),
            parent_intent_hash=str(row["parent_intent_hash"]),
            parent_reconciliation_fingerprint=str(
                row["parent_reconciliation_fingerprint"]
            ),
            idempotency_key=str(row["idempotency_key"]),
            snapshot_hash=str(row["snapshot_hash"]),
            candidate=json.loads(str(row["candidate_json"])),
            state=str(row["state"]),
            reason_code=str(row["reason_code"]),
            replacement_proposal_id=str(row["replacement_proposal_id"]),
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

    @staticmethod
    def _append_requote_event(
        connection: sqlite3.Connection,
        requote_id: str,
        event_type: str,
        from_state: Optional[str],
        to_state: str,
        payload: Mapping[str, Any],
        created_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO requote_events
                (requote_id, event_type, from_state, to_state, payload_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                requote_id,
                event_type,
                from_state,
                to_state,
                canonical_json(payload),
                created_at,
            ),
        )


class LedgerReader:
    """Read-only view of a pinned ledger for inspection.

    Opens the file with ``mode=ro``: no writer lock, no recovery, no PRAGMA
    writes and no private config. It still refuses an unpinned file and a
    workspace that differs from the pin, and it never inserts anything (a
    missing workspace control reads as not engaged, version 0).
    """

    def __init__(self, path: os.PathLike[str] | str, *, workspace: Workspace | str) -> None:
        self.workspace: Workspace = coerce_workspace(workspace)
        self.path = Path(path)
        if self.path.is_symlink():
            raise LedgerError("ledger path must not be a symbolic link")
        if not self.path.exists():
            raise LedgerError(f"ledger {self.path} does not exist")
        self._mutex = threading.RLock()
        self._connection: Optional[sqlite3.Connection] = sqlite3.connect(
            _ro_uri(self.path), uri=True, check_same_thread=False
        )
        try:
            self._connection.row_factory = sqlite3.Row
            _require_sqlite_capabilities(self._connection)
            identity = _read_identity(self._connection)
            if identity.kind == "newer":
                raise LedgerError(
                    f"ledger schema {identity.user_version} is newer than supported version "
                    f"{SCHEMA_VERSION}"
                )
            if identity.kind in ("fresh", "unpinned"):
                raise LedgerUnpinned(
                    f"ledger {self.path} (schema {identity.user_version}) has no workspace pin"
                )
            if identity.workspace != self.workspace:
                pinned = identity.workspace.value if identity.workspace else "unknown"
                raise WorkspaceMismatch(
                    f"ledger {self.path} is pinned to workspace {pinned}; "
                    f"requested {self.workspace.value}"
                )
        except Exception:
            self.close()
            raise

    def __enter__(self) -> "LedgerReader":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        with self._mutex:
            if self._connection is not None:
                self._connection.close()
                self._connection = None

    def require_workspace(self, workspace: object) -> Workspace:
        try:
            requested = coerce_workspace(workspace)
        except ValueError:
            raise WorkspaceMismatch(
                f"request names no valid workspace; ledger is pinned to {self.workspace.value}"
            ) from None
        if requested != self.workspace:
            raise WorkspaceMismatch(
                f"request names workspace {requested.value}; "
                f"ledger is pinned to {self.workspace.value}"
            )
        return requested

    def _require_connection(self) -> sqlite3.Connection:
        if self._connection is None:
            raise LedgerError("ledger reader is closed")
        return self._connection

    def get_order(self, proposal_id: str) -> Optional[LedgerOrder]:
        with self._mutex:
            row = self._require_connection().execute(
                """
                SELECT i.*, p.state, p.acknowledgment_json, p.updated_at
                FROM order_intents AS i
                JOIN order_projection AS p USING (proposal_id)
                WHERE i.proposal_id = ?
                """,
                (proposal_id,),
            ).fetchone()
        if row is None:
            return None
        if row["workspace"] != self.workspace.value:
            raise WorkspaceMismatch(
                f"stored order {proposal_id!r} carries workspace {row['workspace']!r}; "
                f"ledger is pinned to {self.workspace.value}"
            )
        return ExecutionLedger._order_from_row(row)

    def get_admission(self, proposal_id: str) -> Optional[ExecutionAdmission]:
        with self._mutex:
            row = self._require_connection().execute(
                "SELECT * FROM execution_admissions WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
        return ExecutionLedger._admission_from_row(row) if row is not None else None

    def get_reservation(self, proposal_id: str) -> Optional[PaperReservation]:
        with self._mutex:
            row = self._require_connection().execute(
                "SELECT * FROM buying_power_reservations WHERE proposal_id = ?",
                (proposal_id,),
            ).fetchone()
        return ExecutionLedger._reservation_from_row(row) if row is not None else None

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

    def list_requotes(self, proposal_id: Optional[str] = None) -> list[LedgerRequote]:
        sql = "SELECT * FROM requote_intents"
        parameters: tuple[object, ...] = ()
        if proposal_id is not None:
            sql += " WHERE proposal_id = ?"
            parameters = (proposal_id,)
        sql += " ORDER BY created_at, requote_id"
        with self._mutex:
            rows = self._require_connection().execute(sql, parameters).fetchall()
        return [ExecutionLedger._requote_from_row(row) for row in rows]

    def get_paper_budget(
        self, account: str, currency: str, *, workspace: Workspace | str
    ) -> Optional[PaperBudget]:
        pinned = self.require_workspace(workspace)
        with self._mutex:
            row = self._require_connection().execute(
                "SELECT * FROM paper_budgets WHERE workspace = ? AND account = ? AND currency = ?",
                (pinned.value, account, currency),
            ).fetchone()
        return ExecutionLedger._budget_from_row(row) if row is not None else None

    def get_paper_position(
        self, account: str, currency: str, ticker: str, *, workspace: Workspace | str
    ) -> Optional[Mapping[str, str]]:
        pinned = self.require_workspace(workspace)
        with self._mutex:
            row = self._require_connection().execute(
                "SELECT quantity, notional FROM paper_positions WHERE workspace = ? AND account = ? AND currency = ? AND ticker = ?",
                (pinned.value, account, currency, ticker),
            ).fetchone()
        if row is None:
            return None
        return {"quantity": str(row["quantity"]), "notional": str(row["notional"])}

    def has_fills(self) -> bool:
        """True when the ledger has ever recorded a fill (see ``ExecutionLedger.has_fills``)."""

        with self._mutex:
            return ExecutionLedger._fills_exist(self._require_connection())

    def get_workspace_control(self, *, workspace: Workspace | str) -> WorkspaceControl:
        pinned = self.require_workspace(workspace)
        with self._mutex:
            row = self._require_connection().execute(
                "SELECT * FROM workspace_controls WHERE workspace = ?", (pinned.value,)
            ).fetchone()
        if row is None:
            return WorkspaceControl(
                workspace=pinned.value,
                engaged=False,
                version=0,
                updated_at=datetime.now(timezone.utc),
            )
        return WorkspaceControl(
            workspace=str(row["workspace"]),
            engaged=bool(row["engaged"]),
            version=int(row["version"]),
            reason_code=str(row["reason_code"]),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )

    def get_approval_key(self, *, workspace: Workspace | str) -> Optional[LedgerApprovalKey]:
        pinned = self.require_workspace(workspace)
        with self._mutex:
            row = self._require_connection().execute(
                "SELECT * FROM approval_keys WHERE workspace = ?", (pinned.value,)
            ).fetchone()
        return ExecutionLedger._approval_key_from_row(row) if row is not None else None


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
    "IndiaAccountView",
    "IndiaCapExceeded",
    "IntentConflict",
    "InvalidTransition",
    "LEGACY_SCHEMA_VERSIONS",
    "LedgerError",
    "LedgerApprovalChallenge",
    "LedgerApprovalKey",
    "LedgerIdentity",
    "LedgerOrder",
    "LedgerReader",
    "LedgerRequote",
    "LedgerUnpinned",
    "LedgerVenueMismatch",
    "LedgerWriterUnavailable",
    "OrderNotFound",
    "PaperBudget",
    "PaperReservation",
    "ReconciliationSnapshot",
    "RequoteConflict",
    "SCHEMA_VERSION",
    "SHADOW_VM_DECISION_EVENT",
    "SIMULATED_BOOKING_EVENT",
    "SIMULATED_EXPIRY_EVENT",
    "SIMULATION_RUN_DIRECTORY",
    "SIM_ACCOUNT_PREFIX",
    "SIM_BOOKING_KIND",
    "SIM_CASH_INSUFFICIENT",
    "SIM_CASH_NEGATIVE",
    "SimulatedBooking",
    "SimulatedCash",
    "SimulatedCashInsufficient",
    "SimulatedCashNegative",
    "SimulationInit",
    "practice_ledger_path",
    "Workspace",
    "WorkspaceControl",
    "WorkspaceMismatch",
    "canonical_json",
    "coerce_workspace",
    "default_ledger_path",
    "intent_hash",
    "simulated_booking_id",
]
