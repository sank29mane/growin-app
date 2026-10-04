"""Ledger workspace identity: pin on first open, refuse before any write.

Every test uses real SQLite files under tmp_path. The refusal tests compare the
main-file sha256, the full iterdump and the workspace_controls rows before and
after, so "refused" means "the file was not touched".
"""

from __future__ import annotations

import hashlib
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from execution import (
    ExecutionLedger,
    LedgerError,
    LedgerUnpinned,
    Workspace,
    WorkspaceMismatch,
    coerce_workspace,
    default_ledger_path,
)
from execution.models import OrderIntent
from ledger_fixture_support import replay_v5_fixture


def make_intent(proposal_id: str = "proposal-1", workspace: str = "uk", **overrides) -> OrderIntent:
    values = {
        "proposal_id": proposal_id,
        "workspace": workspace,
        "account": "paper",
        "broker": "paper",
        "mode": "PAPER",
        "ticker": "TQQQ",
        "side": "BUY",
        "quantity": Decimal("1"),
        **overrides,
    }
    return OrderIntent(**values)


def ro_connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def snapshot(path: Path) -> dict:
    connection = ro_connect(path)
    try:
        dump = list(connection.iterdump())
        controls = connection.execute("SELECT * FROM workspace_controls").fetchall()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()
    return {
        "dump": dump,
        "controls": controls,
        "user_version": version,
        "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
    }


V1_DDL = """
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


@pytest.fixture
def india_file(tmp_path):
    path = tmp_path / "india" / "execution.sqlite3"
    with ExecutionLedger(path, workspace="india") as ledger:
        ledger.register_intent(make_intent("india-1", workspace="india"))
    return path


def test_fresh_file_is_pinned_on_first_open(tmp_path):
    path = tmp_path / "execution.sqlite3"
    with ExecutionLedger(path, workspace="uk") as ledger:
        assert ledger.workspace == Workspace.UK
        assert ledger.pragmas()["user_version"] == 6

    connection = ro_connect(path)
    try:
        rows = connection.execute(
            "SELECT singleton, workspace, pinned_by FROM ledger_identity"
        ).fetchall()
        assert rows == [(1, "uk", "first-open")]
        hidden = {row[1]: row[6] for row in connection.execute("PRAGMA table_xinfo(order_intents)")}
        assert hidden["workspace"] == 2  # virtual generated column
    finally:
        connection.close()


def test_reopen_with_same_workspace_runs_no_ddl(tmp_path):
    path = tmp_path / "execution.sqlite3"
    with ExecutionLedger(path, workspace="uk"):
        pass
    before = snapshot(path)
    with ExecutionLedger(path, workspace="uk"):
        pass
    after = snapshot(path)
    assert after["dump"] == before["dump"]
    assert after["user_version"] == 6


def test_wrong_workspace_is_refused_without_touching_the_file(india_file):
    before = snapshot(india_file)
    assert before["user_version"] == 6

    with pytest.raises(WorkspaceMismatch) as excinfo:
        ExecutionLedger(india_file, workspace="uk")
    assert "india" in str(excinfo.value) and "uk" in str(excinfo.value)

    after = snapshot(india_file)
    assert after["dump"] == before["dump"]
    assert after["sha256"] == before["sha256"]
    assert after["controls"] == before["controls"]
    assert all(row[0] != "uk" for row in after["controls"])


def test_replayed_v5_fixture_is_refused_as_unpinned(tmp_path):
    path = replay_v5_fixture(tmp_path / "execution.sqlite3")
    before = snapshot(path)
    assert before["user_version"] == 5

    with pytest.raises(LedgerUnpinned, match="ledger_tool.py"):
        ExecutionLedger(path, workspace="uk")

    after = snapshot(path)
    assert after["dump"] == before["dump"]
    assert after["user_version"] == 5


def test_non_wal_v1_file_is_refused_without_any_byte_change(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript(V1_DDL)
    connection.commit()
    connection.close()

    probe = ro_connect(path)
    try:
        assert probe.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    finally:
        probe.close()
    digest_before = hashlib.sha256(path.read_bytes()).hexdigest()

    with pytest.raises(LedgerUnpinned):
        ExecutionLedger(path, workspace="uk")

    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest_before
    probe = ro_connect(path)
    try:
        assert probe.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
        assert probe.execute("PRAGMA user_version").fetchone()[0] == 1
    finally:
        probe.close()
    assert not path.with_name(path.name + "-wal").exists()


def test_tables_without_a_pin_are_unpinned_whatever_the_version(tmp_path):
    path = tmp_path / "odd.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE order_intents (proposal_id TEXT)")
    connection.commit()
    connection.close()
    with pytest.raises(LedgerUnpinned):
        ExecutionLedger(path, workspace="uk")


def test_newer_schema_is_refused(tmp_path):
    path = tmp_path / "newer.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 7")
    connection.execute("CREATE TABLE t (x)")
    connection.commit()
    connection.close()
    with pytest.raises(LedgerError, match="newer than supported"):
        ExecutionLedger(path, workspace="uk")


def test_zero_byte_file_counts_as_fresh(tmp_path):
    path = tmp_path / "execution.sqlite3"
    path.write_bytes(b"")
    with ExecutionLedger(path, workspace="india") as ledger:
        assert ledger.workspace == Workspace.INDIA
        assert ledger.pragmas()["user_version"] == 6


def test_workspace_argument_is_required_and_validated(tmp_path):
    with pytest.raises(TypeError):
        ExecutionLedger(tmp_path / "a.sqlite3")  # type: ignore[call-arg]
    with pytest.raises(ValueError):
        ExecutionLedger(tmp_path / "b.sqlite3", workspace="us")
    with pytest.raises(ValueError):
        ExecutionLedger(tmp_path / "c.sqlite3", workspace=None)  # type: ignore[arg-type]
    assert not (tmp_path / "a.sqlite3").exists()
    with pytest.raises(TypeError):
        default_ledger_path()  # type: ignore[call-arg]
    assert default_ledger_path("india").as_posix().endswith("workspaces/india/execution.sqlite3")
    assert default_ledger_path(Workspace.UK).as_posix().endswith("workspaces/uk/execution.sqlite3")


def test_coerce_workspace_accepts_only_known_strings():
    assert coerce_workspace("uk") is Workspace.UK
    assert coerce_workspace(Workspace.INDIA) is Workspace.INDIA
    for bad in (None, "US", " uk", "", 1, b"uk"):
        with pytest.raises(ValueError):
            coerce_workspace(bad)


def test_str_of_workspace_is_the_plain_value():
    assert str(Workspace.INDIA) == "india"
    assert f"{Workspace.UK}" == "uk"


def test_require_workspace_checks_the_pin(tmp_path):
    with ExecutionLedger(tmp_path / "execution.sqlite3", workspace="uk") as ledger:
        assert ledger.require_workspace(Workspace.UK) is Workspace.UK
        assert ledger.require_workspace("uk") is Workspace.UK
        with pytest.raises(WorkspaceMismatch):
            ledger.require_workspace("india")
        with pytest.raises(WorkspaceMismatch):
            ledger.require_workspace(None)
        with pytest.raises(WorkspaceMismatch):
            ledger.require_workspace("mars")


def test_ledger_pinned_to_other_import_root_enum_still_matches(tmp_path):
    from backend.execution.models import Workspace as BackendWorkspace

    with ExecutionLedger(tmp_path / "execution.sqlite3", workspace=BackendWorkspace.UK) as ledger:
        assert ledger.workspace == Workspace.UK
        assert ledger.require_workspace(BackendWorkspace.UK) == Workspace.UK
