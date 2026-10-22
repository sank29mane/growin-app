"""INSERT OR REPLACE cannot rewrite the append-only ledger tables.

SQLite skips the DELETE trigger for a row that a REPLACE conflict removes unless the
``recursive_triggers`` pragma is on, and that pragma belongs to each connection. So every table here
carries a ``<table>_no_replace`` BEFORE INSERT trigger as well. These tests use a real ledger file
and a plain ``sqlite3`` connection with default pragmas, the way a tamper tool would.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from regime_testkit import bound_admit, gated

from execution import (
    ApprovalService,
    ExecutionLedger,
    ExecutionService,
    PaperDispatcher,
    VenueBinding,
)
from execution import ledger as ledger_module
from execution.models import OrderIntent

KEY_TABLES = [
    "order_intents",
    "execution_events",
    "requote_intents",
    "requote_events",
    "approval_keys",
    "approval_challenges",
    "execution_approvals",
    "ledger_identity",
]
VENUE_TABLES = ["ledger_venue_binding", "ledger_venue_limits", "ledger_quantity_reservations"]
ALL_TABLES = KEY_TABLES + VENUE_TABLES


def _signed_intent() -> OrderIntent:
    return OrderIntent(
        proposal_id="nr-1",
        workspace="uk",
        account="invest",
        broker="paper",
        mode="PAPER",
        ticker="VUSA",
        side="BUY",
        quantity=Decimal("2"),
    )


def _populate_main_ledger(path: Path) -> None:
    """One order through enrolment, challenge, signed approval, acknowledgement and a requote."""

    key = ec.generate_private_key(ec.SECP256R1())
    public = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    with ExecutionLedger(path, require_approval=True, workspace="uk") as ledger:
        approval = ApprovalService(ledger)
        token = b"one-time-secret"
        approval.enrollment_token_path.write_bytes(token)
        os.chmod(approval.enrollment_token_path, 0o600)
        approval.enroll_key(public, token, workspace="uk")
        service = ExecutionService(
            PaperDispatcher(), ledger, require_approval=True, approval_service=approval, **gated()
        )
        intent = _signed_intent()
        service.admit(
            intent,
            currency="GBP",
            price="100",
            simulator_evidence={"simulated_fill_price": "100"},
            risk_evidence={"scaled_size": "2"},
            **bound_admit(),
        )
        ledger.configure_paper_budget("invest", "GBP", "10000", workspace="uk")
        service.reserve("nr-1")
        challenge = service.create_approval_challenge("nr-1", workspace="uk", ttl_seconds=60)
        signature = key.sign(challenge.signed_payload, ec.ECDSA(hashes.SHA256()))
        asyncio.run(service.approve_signed("nr-1", challenge.challenge_id, signature, workspace="uk"))
        order = ledger.get_order("nr-1")
        assert order is not None and order.acknowledgment is not None
        ledger.record_requote_intent(
            requote_id="rq-1",
            proposal_id="nr-1",
            parent_intent_hash=order.intent_hash,
            parent_reconciliation_fingerprint=f"ack:{order.acknowledgment.broker_order_id}",
            idempotency_key="nr-1:snapshot-1",
            snapshot_hash="snapshot-1",
            candidate={
                "side": "BUY",
                "limit_price": "100.05",
                "lower_bound": "99.90",
                "upper_bound": "100.10",
                "policy_version": "local-paper-v1",
            },
        )


def _populate_venue_ledger(path: Path) -> None:
    binding = VenueBinding(venue="t212_practice", account_id="acct-synthetic-0001", currency="GBP")
    with ExecutionLedger(path, workspace="uk", venue=binding) as ledger:
        ledger.configure_venue_limits("1000", "500", workspace="uk")
        intent = ledger.register_intent(
            OrderIntent(
                proposal_id="res-1",
                workspace="uk",
                account="acct-synthetic-0001",
                broker="t212_practice",
                mode="PRACTICE",
                ticker="VODl_EQ",
                side="SELL",
                quantity="1",
                client_order_id="growin-res-1",
            )
        )
    # The ledger only writes a reservation after an admission and a reconciled position, which is
    # a whole practice stack. The row is what matters here, so it goes in raw, once, at set-up.
    plain = sqlite3.connect(path, isolation_level=None)
    try:
        plain.execute(
            "INSERT INTO ledger_quantity_reservations (proposal_id, workspace, account, currency, "
            "ticker, intent_hash, reserved, state, created_at, updated_at) "
            "VALUES ('res-1', 'uk', 'acct-synthetic-0001', 'GBP', 'VODl_EQ', ?, '1', 'ACTIVE', "
            "'2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00')",
            (intent.intent_hash,),
        )
    finally:
        plain.close()


@pytest.fixture
def main_path(tmp_path) -> Path:
    path = tmp_path / "main.sqlite3"
    _populate_main_ledger(path)
    return path


@pytest.fixture
def venue_path(tmp_path) -> Path:
    path = tmp_path / "venue.sqlite3"
    _populate_venue_ledger(path)
    return path


def _plain(path: Path) -> sqlite3.Connection:
    # Default pragmas: recursive_triggers off, foreign keys off, no ledger code in the way.
    connection = sqlite3.connect(path, isolation_level=None)
    assert connection.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
    return connection


def _rows(connection: sqlite3.Connection, table: str) -> list[tuple]:
    return connection.execute(f"SELECT rowid, * FROM {table} ORDER BY rowid").fetchall()


def _columns(connection: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]


def _replacement_statements(connection: sqlite3.Connection, table: str) -> list[str]:
    """Every way to put a row over an existing one, built from the table's own first row.

    One statement per uniqueness constraint (primary key, each UNIQUE, the rowid): the colliding
    column keeps its value and every other key column is moved, so the conflict is on that
    constraint alone.
    """

    columns = _columns(connection, table)
    names = ", ".join(columns)
    keys = [list(key) for key in ledger_module._NO_REPLACE_KEYS[table]]
    statements = [
        f"INSERT OR REPLACE INTO {table} ({names}) SELECT {names} FROM {table}",
        f"REPLACE INTO {table} ({names}) SELECT {names} FROM {table}",
        f"INSERT INTO {table} ({names}) SELECT {names} FROM {table} WHERE true "
        f"ON CONFLICT DO UPDATE SET {columns[1]} = excluded.{columns[1]}",
    ]
    # ``workspace`` is never moved: that trips the separate workspace insert guard, not this one.
    key_columns = {column for key in keys for column in key if column != "workspace"}
    integer_key = {
        column
        for column in key_columns
        if connection.execute(
            f"SELECT typeof({column}) FROM {table} LIMIT 1"
        ).fetchone()[0]
        == "integer"
    }
    for kept in keys:
        moved = ", ".join(
            column
            if column not in key_columns or column in kept
            else (f"{column} + 1000" if column in integer_key else f"'other-' || {column}")
            for column in columns
        )
        statements.append(
            f"INSERT OR REPLACE INTO {table} ({names}) SELECT {moved} FROM {table}"
        )
    # The rowid alone: a new key on an existing rowid still deletes the old row under REPLACE.
    other = ", ".join(
        column
        if column not in key_columns
        else (f"{column} + 1000" if column in integer_key else f"'other-' || {column}")
        for column in columns
    )
    # (An INTEGER PRIMARY KEY is the rowid, so those tables were covered by the loop above.)
    if not integer_key:
        statements.append(
            f"INSERT OR REPLACE INTO {table} (rowid, {names}) SELECT rowid, {other} FROM {table}"
        )
    return statements


def _rewrite_statement(connection: sqlite3.Connection, table: str) -> str:
    """The attack itself: same key, one non-key column overwritten with a marker."""

    columns = _columns(connection, table)
    target = "pinned_at" if table == "ledger_identity" else columns[-1]
    names = ", ".join(columns)
    values = ", ".join("'tampered'" if column == target else column for column in columns)
    return f"INSERT OR REPLACE INTO {table} ({names}) SELECT {values} FROM {table}"


def _path_for(table: str, main_path: Path, venue_path: Path) -> Path:
    return venue_path if table in VENUE_TABLES else main_path


@pytest.mark.parametrize("table", ALL_TABLES)
def test_a_replacing_insert_is_refused_and_the_row_is_unchanged(table, main_path, venue_path):
    path = _path_for(table, main_path, venue_path)
    plain = _plain(path)
    try:
        before = _rows(plain, table)
        assert before, f"{table} must hold a row for this test to mean anything"
        dump = list(plain.iterdump())
        statements = [*_replacement_statements(plain, table), _rewrite_statement(plain, table)]
        assert len(statements) >= 5
        for statement in statements:
            with pytest.raises(sqlite3.IntegrityError, match=f"{table} rows are append-only"):
                plain.execute(statement)
            assert _rows(plain, table) == before
        assert list(plain.iterdump()) == dump
    finally:
        plain.close()


@pytest.mark.parametrize("table", ALL_TABLES)
def test_the_replacement_statements_really_rewrite_the_row_without_the_guard(
    table, main_path, venue_path
):
    # The statements above are not vacuous: with the table's trigger dropped, a rewrite
    # succeeds (the recursive_triggers-off bypass) and the row it replaced stays gone.
    path = _path_for(table, main_path, venue_path)
    plain = _plain(path)
    try:
        plain.execute(f"DROP TRIGGER {table}_no_replace")
        before = _rows(plain, table)
        plain.execute(_rewrite_statement(plain, table))
        assert _rows(plain, table) != before
        assert any("tampered" in map(str, row) for row in _rows(plain, table))
    finally:
        plain.close()


def test_every_guarded_table_has_its_trigger_on_a_new_ledger(main_path, venue_path):
    for path, tables in ((main_path, KEY_TABLES), (venue_path, VENUE_TABLES)):
        plain = _plain(path)
        try:
            names = {row[0] for row in plain.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
        finally:
            plain.close()
        assert {f"{table}_no_replace" for table in tables} <= names
        updatable = [table for table in ledger_module._KEY_UPDATE_GUARDED if table in tables]
        assert updatable
        assert {f"{table}_no_key_update" for table in updatable} <= names


def test_a_fresh_ledger_is_guarded_by_its_own_schema_not_only_by_the_open_step(
    tmp_path, monkeypatch
):
    # Every open arms missing guards, which would hide a gap in the fresh-install SQL. With that
    # step off, a brand-new main ledger and a brand-new venue ledger must still carry every trigger.
    monkeypatch.setattr(ExecutionLedger, "_ensure_no_replace_guards", lambda self: None)
    _populate_main_ledger(tmp_path / "main.sqlite3")
    _populate_venue_ledger(tmp_path / "venue.sqlite3")
    for path, tables in ((tmp_path / "main.sqlite3", KEY_TABLES), (tmp_path / "venue.sqlite3", VENUE_TABLES)):
        names = _trigger_names(path)
        assert {f"{table}_no_replace" for table in tables} <= names
        updatable = [table for table in ledger_module._KEY_UPDATE_GUARDED if table in tables]
        assert updatable
        assert {f"{table}_no_key_update" for table in updatable} <= names


def test_a_duplicate_key_is_still_an_integrity_error_not_a_silent_no_op(main_path):
    plain = _plain(main_path)
    try:
        columns = ", ".join(_columns(plain, "order_intents"))
        before = _rows(plain, "order_intents")
        with pytest.raises(sqlite3.IntegrityError):
            plain.execute(f"INSERT INTO order_intents ({columns}) SELECT {columns} FROM order_intents")
        assert _rows(plain, "order_intents") == before
    finally:
        plain.close()


def test_insert_or_ignore_of_an_existing_key_is_refused_not_swallowed(main_path):
    # No ledger code path uses OR IGNORE or ON CONFLICT on these tables (every writer looks the key
    # up first and does a plain INSERT), so there is no legitimate no-op to preserve. A raw OR IGNORE
    # is refused like the other overwrite forms, and it leaves the row as it was.
    plain = _plain(main_path)
    try:
        columns = ", ".join(_columns(plain, "execution_approvals"))
        before = _rows(plain, "execution_approvals")
        with pytest.raises(sqlite3.IntegrityError, match="execution_approvals rows are append-only"):
            plain.execute(
                f"INSERT OR IGNORE INTO execution_approvals ({columns}) "
                f"SELECT {columns} FROM execution_approvals"
            )
        assert _rows(plain, "execution_approvals") == before
    finally:
        plain.close()


def test_the_ledger_own_writers_and_their_replays_are_unaffected(tmp_path):
    path = tmp_path / "main.sqlite3"
    _populate_main_ledger(path)
    counts = {}
    plain = _plain(path)
    try:
        for table in KEY_TABLES:
            counts[table] = len(_rows(plain, table))
    finally:
        plain.close()
    with ExecutionLedger(path, require_approval=True, workspace="uk") as ledger:
        order = ledger.get_order("nr-1")
        assert order is not None and order.acknowledgment is not None
        # Re-registering the same intent, re-recording the same requote and re-enrolling the same
        # key all go through their lookup and return the earlier row.
        assert ledger.register_intent(_signed_intent()).intent_hash == order.intent_hash
        again = ledger.record_requote_intent(
            requote_id="rq-1",
            proposal_id="nr-1",
            parent_intent_hash=order.intent_hash,
            parent_reconciliation_fingerprint=f"ack:{order.acknowledgment.broker_order_id}",
            idempotency_key="nr-1:snapshot-1",
            snapshot_hash="snapshot-1",
            candidate={
                "side": "BUY",
                "limit_price": "100.05",
                "lower_bound": "99.90",
                "upper_bound": "100.10",
                "policy_version": "local-paper-v1",
            },
        )
        assert again.requote_id == "rq-1"
        assert ledger.approval_evidence_count("nr-1") == 1
    plain = _plain(path)
    try:
        after = {table: len(_rows(plain, table)) for table in KEY_TABLES}
        # Startup recovery appends one requote event on reopen; no replay adds any other row.
        assert after.pop("requote_events") == counts.pop("requote_events") + 1
        assert after == counts
    finally:
        plain.close()


def _strip_guards(path: Path, tables: list[str]) -> None:
    """Turn a ledger into one created before this change: no ``_no_replace`` trigger."""

    plain = _plain(path)
    try:
        for table in tables:
            plain.execute(f"DROP TRIGGER {table}_no_replace")
            if table in ledger_module._KEY_UPDATE_GUARDED:
                plain.execute(f"DROP TRIGGER {table}_no_key_update")
    finally:
        plain.close()


def _table_contents(path: Path, tables: list[str]) -> dict[str, list[tuple]]:
    plain = _plain(path)
    try:
        return {table: _rows(plain, table) for table in tables}
    finally:
        plain.close()


def _trigger_names(path: Path) -> set[str]:
    plain = _plain(path)
    try:
        return {row[0] for row in plain.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}
    finally:
        plain.close()


def test_an_existing_ledger_without_the_guards_gets_them_on_reopen_with_no_data_rewrite(main_path):
    _strip_guards(main_path, KEY_TABLES)
    assert not any(name.endswith("_no_replace") for name in _trigger_names(main_path))
    # Unguarded for real: a replacement goes through on the raw connection.
    plain = _plain(main_path)
    try:
        columns = ", ".join(_columns(plain, "execution_events"))
        plain.execute(f"INSERT OR REPLACE INTO execution_events ({columns}) SELECT {columns} FROM execution_events WHERE event_id = 1")
    finally:
        plain.close()
    before = _table_contents(main_path, KEY_TABLES)

    with ExecutionLedger(main_path, require_approval=True, workspace="uk"):
        pass

    assert {f"{table}_no_replace" for table in KEY_TABLES} <= _trigger_names(main_path)
    assert "requote_intents_no_key_update" in _trigger_names(main_path)
    after = _table_contents(main_path, KEY_TABLES)
    # Opening runs the ledger's own startup recovery, which may move a requote forward and append
    # its event. Nothing else changes, and no row that was there is rewritten or removed.
    assert {t: rows for t, rows in after.items() if not t.startswith("requote_")} == {
        t: rows for t, rows in before.items() if not t.startswith("requote_")
    }
    assert after["requote_events"][: len(before["requote_events"])] == before["requote_events"]
    assert [row[:8] for row in after["requote_intents"]] == [row[:8] for row in before["requote_intents"]]
    plain = _plain(main_path)
    try:
        for table in KEY_TABLES:
            for statement in _replacement_statements(plain, table):
                with pytest.raises(sqlite3.IntegrityError, match=f"{table} rows are append-only"):
                    plain.execute(statement)
        assert {table: _rows(plain, table) for table in KEY_TABLES} == after
    finally:
        plain.close()


def test_an_existing_venue_ledger_without_the_guards_gets_them_on_reopen(venue_path):
    _strip_guards(venue_path, VENUE_TABLES + ["ledger_identity"])
    before = _table_contents(venue_path, VENUE_TABLES + ["ledger_identity"])
    binding = VenueBinding(venue="t212_practice", account_id="acct-synthetic-0001", currency="GBP")
    with ExecutionLedger(venue_path, workspace="uk", venue=binding):
        pass
    assert {f"{table}_no_replace" for table in VENUE_TABLES + ["ledger_identity"]} <= _trigger_names(
        venue_path
    )
    assert "ledger_quantity_reservations_no_key_update" in _trigger_names(venue_path)
    assert _table_contents(venue_path, VENUE_TABLES + ["ledger_identity"]) == before


def test_a_ledger_that_already_has_every_guard_is_not_written_on_reopen(main_path):
    def schema_cookie() -> int:
        plain = _plain(main_path)
        try:
            return plain.execute("PRAGMA schema_version").fetchone()[0]
        finally:
            plain.close()

    before = schema_cookie()
    with ExecutionLedger(main_path, require_approval=True, workspace="uk"):
        pass
    assert schema_cookie() == before


def test_only_the_missing_guard_is_added_on_reopen(main_path):
    _strip_guards(main_path, ["approval_challenges"])
    names = _trigger_names(main_path)
    assert "approval_challenges_no_replace" not in names
    assert "order_intents_no_replace" in names
    with ExecutionLedger(main_path, require_approval=True, workspace="uk"):
        pass
    assert "approval_challenges_no_replace" in _trigger_names(main_path)


def test_the_trigger_set_matches_the_declared_key_map():
    # The ensure-on-open step and the fresh schema are both driven by this map; a table added to
    # one and not the other would leave new ledgers or old ledgers unguarded.
    assert set(ledger_module._NO_REPLACE_KEYS) == set(ALL_TABLES)
    fresh_statements = " ".join(ledger_module._BASE_SCHEMA_STATEMENTS)
    for table in ledger_module._BASE_NO_REPLACE_KEYS:
        assert f"{table}_no_replace" in fresh_statements
    assert sorted(ledger_module._NO_REPLACE_KEYS["execution_approvals"]) == [
        ("approval_id",),
        ("challenge_id",),
        ("proposal_id",),
    ]


# --- UPDATE OR REPLACE -------------------------------------------------------------------------
#
# A table that permits UPDATE can still lose a row: moving one row's key onto another's makes the
# REPLACE conflict-delete the other with no DELETE trigger. ``<table>_no_key_update`` refuses any
# UPDATE that changes a key column or the rowid.


def _seed_second_row(connection: sqlite3.Connection, table: str) -> None:
    """Copy the table's row with every key column moved, so there are two rows to collide."""

    columns = _columns(connection, table)
    key_columns = {column for key in ledger_module._NO_REPLACE_KEYS[table] for column in key}
    moved = ", ".join(f"'other-' || {c}" if c in key_columns else c for c in columns)
    connection.execute(
        f"INSERT INTO {table} ({', '.join(columns)}) SELECT {moved} FROM {table}"
    )
    assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 2


def _key_update_statements(connection: sqlite3.Connection, table: str) -> list[str]:
    """Move the second row onto the first, once per key column and once on the rowid."""

    key_columns = sorted({c for key in ledger_module._NO_REPLACE_KEYS[table] for c in key})
    first = "(SELECT MIN(rowid) FROM {t})".format(t=table)
    second = "(SELECT MAX(rowid) FROM {t})".format(t=table)
    statements = []
    for conflict in ("OR REPLACE", "OR IGNORE", "OR ROLLBACK", ""):
        for column in key_columns:
            statements.append(
                f"UPDATE {conflict} {table} SET {column} = "
                f"(SELECT {column} FROM {table} WHERE rowid = {first}) WHERE rowid = {second}"
            )
        statements.append(f"UPDATE {conflict} {table} SET rowid = {first} WHERE rowid = {second}")
    return statements


KEY_UPDATE_CASES = [("requote_intents", "main"), ("ledger_quantity_reservations", "venue")]


@pytest.mark.parametrize(("table", "which"), KEY_UPDATE_CASES)
def test_updating_a_key_onto_another_row_is_refused_and_nothing_is_deleted(
    table, which, main_path, venue_path
):
    path = main_path if which == "main" else venue_path
    plain = _plain(path)
    try:
        _seed_second_row(plain, table)
        before = _rows(plain, table)
        dump = list(plain.iterdump())
        statements = _key_update_statements(plain, table)
        assert len(statements) >= 8
        for statement in statements:
            with pytest.raises(sqlite3.IntegrityError, match=f"{table} keys are immutable"):
                plain.execute(statement)
            assert _rows(plain, table) == before
        assert list(plain.iterdump()) == dump
    finally:
        plain.close()


@pytest.mark.parametrize(("table", "which"), KEY_UPDATE_CASES)
def test_the_key_update_statements_really_delete_a_row_without_the_guard(
    table, which, main_path, venue_path
):
    path = main_path if which == "main" else venue_path
    plain = _plain(path)
    try:
        _seed_second_row(plain, table)
        plain.execute(f"DROP TRIGGER {table}_no_key_update")
        first_key = sorted({c for key in ledger_module._NO_REPLACE_KEYS[table] for c in key})[0]
        plain.execute(
            f"UPDATE OR REPLACE {table} SET {first_key} = "
            f"(SELECT {first_key} FROM {table} WHERE rowid = (SELECT MIN(rowid) FROM {table})) "
            f"WHERE rowid = (SELECT MAX(rowid) FROM {table})"
        )
        assert plain.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
    finally:
        plain.close()


def test_a_requote_state_update_still_works_on_a_plain_connection(main_path):
    plain = _plain(main_path)
    try:
        plain.execute(
            "UPDATE requote_intents SET state = 'REJECTED', reason_code = 'TEST', "
            "replacement_proposal_id = 'rp-1', updated_at = 'now' WHERE requote_id = 'rq-1'"
        )
        row = plain.execute(
            "SELECT state, reason_code, replacement_proposal_id FROM requote_intents"
        ).fetchone()
        assert row == ("REJECTED", "TEST", "rp-1")
    finally:
        plain.close()


def test_a_quantity_reservation_accounting_update_still_works_on_a_plain_connection(venue_path):
    plain = _plain(venue_path)
    try:
        plain.execute(
            "UPDATE ledger_quantity_reservations SET consumed = '1', released = '0', "
            "state = 'SETTLED', updated_at = 'now' WHERE proposal_id = 'res-1'"
        )
        row = plain.execute(
            "SELECT consumed, state FROM ledger_quantity_reservations"
        ).fetchone()
        assert row == ("1", "SETTLED")
    finally:
        plain.close()


def test_the_ledger_own_requote_state_update_is_unaffected(main_path):
    with ExecutionLedger(main_path, require_approval=True, workspace="uk") as ledger:
        order = ledger.get_order("nr-1")
        assert order is not None and order.acknowledgment is not None
        ledger.record_requote_intent(
            requote_id="rq-2",
            proposal_id="nr-1",
            parent_intent_hash=order.intent_hash,
            parent_reconciliation_fingerprint=f"ack:{order.acknowledgment.broker_order_id}",
            idempotency_key="nr-1:snapshot-2",
            snapshot_hash="snapshot-2",
            candidate={
                "side": "BUY",
                "limit_price": "100.05",
                "lower_bound": "99.90",
                "upper_bound": "100.10",
                "policy_version": "local-paper-v1",
            },
        )
        blocked = ledger.block_requote("rq-2", "TEST_BLOCK")
        assert blocked.state == "BLOCKED_TEST_BLOCK"
        assert ledger.get_requote("rq-1") is not None


# --- the key map against the real schema --------------------------------------------------------


def _real_keys(connection: sqlite3.Connection, table: str) -> set[frozenset[str]]:
    """Every uniqueness constraint SQLite itself knows: UNIQUE and PRIMARY KEY indexes (including
    the automatic ones) and an INTEGER PRIMARY KEY, which is the rowid and has no index."""

    keys: set[frozenset[str]] = set()
    for row in connection.execute(f"PRAGMA index_list({table})").fetchall():
        name, unique = row[1], row[2]
        if unique:
            keys.add(
                frozenset(info[2] for info in connection.execute(f"PRAGMA index_info({name})"))
            )
    info = connection.execute(f"PRAGMA table_info({table})").fetchall()
    primary = [column for column in info if column[5]]
    if len(primary) == 1 and primary[0][2].upper() == "INTEGER":
        keys.add(frozenset({primary[0][1]}))
    return keys


def test_the_key_map_equals_the_unique_constraints_of_the_real_tables(main_path, venue_path):
    for path, tables in ((main_path, KEY_TABLES), (venue_path, VENUE_TABLES)):
        plain = _plain(path)
        try:
            for table in tables:
                declared = {frozenset(key) for key in ledger_module._NO_REPLACE_KEYS[table]}
                assert declared == _real_keys(plain, table), table
        finally:
            plain.close()


def test_a_dropped_key_is_caught_by_the_schema_check(main_path, monkeypatch):
    # Break-proof of the check itself: the order_intents map without client_order_id (the gap
    # that review round 1 found) no longer equals the table, and the guard built from it lets a
    # REPLACE that collides only on client_order_id delete the old intent.
    weakened = dict(ledger_module._NO_REPLACE_KEYS)
    weakened["order_intents"] = (("proposal_id",),)
    plain = _plain(main_path)
    try:
        declared = {frozenset(key) for key in weakened["order_intents"]}
        assert declared != _real_keys(plain, "order_intents")
        plain.execute("DROP TRIGGER order_intents_no_replace")
        plain.execute(ledger_module._no_replace_trigger_sql("order_intents", weakened["order_intents"]))
        columns = _columns(plain, "order_intents")
        names = ", ".join(columns)
        moved = ", ".join(
            "'other-' || proposal_id" if column == "proposal_id" else column for column in columns
        )
        plain.execute(f"INSERT OR REPLACE INTO order_intents ({names}) SELECT {moved} FROM order_intents")
        assert plain.execute("SELECT COUNT(*) FROM order_intents").fetchone()[0] == 1
    finally:
        plain.close()


def test_every_guarded_table_that_permits_update_has_a_key_update_guard(main_path, venue_path):
    # A table with no unconditional BEFORE UPDATE trigger accepts UPDATE OR REPLACE, so it must be
    # listed for ``_no_key_update``. A table that refuses every UPDATE must not need it.
    for path in (main_path, venue_path):
        plain = _plain(path)
        try:
            triggers = plain.execute(
                "SELECT tbl_name, name, sql FROM sqlite_master WHERE type = 'trigger'"
            ).fetchall()
        finally:
            plain.close()
        refusing = {
            table
            for table, name, sql in triggers
            if name == f"{table}_no_update" and " WHEN " not in sql
        }
        present = {table for table, _name, _sql in triggers}
        for table in ledger_module._NO_REPLACE_KEYS:
            if table not in present:
                continue
            assert (table in refusing) != (table in ledger_module._KEY_UPDATE_GUARDED), table
