"""The captured v5 ledger fixture replays and is internally consistent.

Uses only sqlite3, json and hashlib. It never imports ExecutionLedger, so it
stays valid after the ledger moves to schema v6.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3

import pytest

from ledger_fixture_support import FIXTURE_SQL_PATH, replay_v5_fixture

LEDGER_TABLES = (
    "order_intents",
    "order_projection",
    "dispatch_attempts",
    "approval_keys",
    "approval_challenges",
    "execution_approvals",
    "execution_events",
    "execution_admissions",
    "paper_budgets",
    "buying_power_reservations",
    "workspace_controls",
    "workspace_control_events",
    "reconciliation_evidence",
    "paper_positions",
    "requote_intents",
    "requote_events",
)


@pytest.fixture
def replayed(tmp_path):
    path = replay_v5_fixture(tmp_path / "execution.sqlite3")
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    yield path, connection
    connection.close()


def test_fixture_text_has_provenance_and_user_version_pragma():
    lines = FIXTURE_SQL_PATH.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("-- ")
    commit_line = next(line for line in lines[:8] if line.startswith("-- base commit: "))
    commit = commit_line.removeprefix("-- base commit: ")
    assert len(commit) == 40 and all(c in "0123456789abcdef" for c in commit)
    assert any("synthetic data; throwaway key" in line for line in lines[:8])
    assert "PRAGMA user_version = 5;" in lines[-3:]


def test_replay_gives_wal_file_with_user_version_5(replayed):
    path, connection = replayed
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
    assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert path.exists()


def test_replay_refuses_to_overwrite_an_existing_file(tmp_path):
    target = tmp_path / "execution.sqlite3"
    target.write_bytes(b"")
    with pytest.raises(FileExistsError):
        replay_v5_fixture(target)


def test_all_ledger_tables_triggers_and_no_identity_table(replayed):
    _, connection = replayed
    tables = {
        row["name"]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    assert tables == set(LEDGER_TABLES) | {"sqlite_sequence"}
    assert "ledger_identity" not in tables
    triggers = {
        row["name"]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")
    }
    for expected in (
        "order_intents_no_update",
        "order_intents_no_delete",
        "execution_events_no_update",
        "execution_events_no_delete",
        "requote_intents_no_delete",
    ):
        assert expected in triggers


@pytest.mark.parametrize("table", LEDGER_TABLES)
def test_every_ledger_table_has_rows(replayed, table):
    _, connection = replayed
    count = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    assert count > 0, f"{table} is empty in the v5 fixture"


def test_intent_hash_is_sha256_of_canonical_json_and_workspace_is_uk(replayed):
    _, connection = replayed
    rows = connection.execute(
        "SELECT proposal_id, intent_hash, canonical_json FROM order_intents"
    ).fetchall()
    assert len(rows) >= 3
    for row in rows:
        assert hashlib.sha256(row["canonical_json"].encode("utf-8")).hexdigest() == row["intent_hash"]
        assert json.loads(row["canonical_json"])["workspace"] == "uk"


def test_fixture_covers_the_scenarios_it_was_captured_for(replayed):
    _, connection = replayed
    states = {
        row["proposal_id"]: row["state"]
        for row in connection.execute("SELECT proposal_id, state FROM buying_power_reservations")
    }
    assert states["fixture-pending"] == "ACTIVE"
    assert states["fixture-requote-parent"] == "ACTIVE"
    accounts = {row[0] for row in connection.execute("SELECT account FROM paper_budgets")}
    assert accounts == {"invest", "requote-fixture"}
    assert connection.execute("SELECT engaged FROM workspace_controls").fetchone()[0] == 0
    assert connection.execute("SELECT COUNT(*) FROM workspace_control_events").fetchone()[0] == 2
