"""Operator-confirmed ledger migration (v1 to v5 -> v6) through the real CLI.

Every test runs scripts/ledger_tool.py as a subprocess against real SQLite
files under tmp_path, so the backup, the writer lock and the transaction are
the production ones. Backup directories live outside the repository.
"""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from execution import ExecutionLedger, LedgerUnpinned, WorkspaceMismatch
from ledger_fixture_support import replay_v5_fixture

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOL = REPO_ROOT / "scripts" / "ledger_tool.py"
FIXTURE_ORDERS = ("fixture-approved", "fixture-pending", "fixture-requote-parent")


def run_tool(*args: str) -> tuple[int, dict]:
    completed = subprocess.run(
        [sys.executable, str(TOOL), *map(str, args)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip(), f"no output; stderr={completed.stderr}"
    return completed.returncode, json.loads(completed.stdout)


def ro_connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)


def dump(path: Path) -> list[str]:
    connection = ro_connect(path)
    try:
        return list(connection.iterdump())
    finally:
        connection.close()


def user_version(path: Path) -> int:
    connection = ro_connect(path)
    try:
        return connection.execute("PRAGMA user_version").fetchone()[0]
    finally:
        connection.close()


@pytest.fixture
def ledger_path(tmp_path):
    directory = tmp_path / "ledgers" / "uk"
    directory.mkdir(parents=True)
    return replay_v5_fixture(directory / "execution.sqlite3")


@pytest.fixture
def backup_dir(tmp_path):
    return tmp_path / "backups"  # absent: the tool creates it with mode 0700


def inspect(ledger: Path, backups: Path) -> dict:
    code, manifest = run_tool("inspect", "--ledger", ledger, "--backup-dir", backups)
    assert code == 0, manifest
    return manifest


def apply(ledger: Path, manifest: dict, workspace: str | None = "uk", prefix: str | None = "") -> tuple[int, dict]:
    args = ["apply", "--ledger", ledger, "--manifest", manifest["manifest_path"]]
    if workspace is not None:
        args += ["--confirm-workspace", workspace]
    if prefix is not None:
        args += ["--confirm-backup-sha256", prefix or manifest["backup_sha256"][:12]]
    return run_tool(*args)


# ---------------------------------------------------------------------------
# Task 1: inspect, apply, verify
# ---------------------------------------------------------------------------


def test_inspect_backs_up_the_wal_and_never_writes_the_source(ledger_path, backup_dir, tmp_path):
    holder = sqlite3.connect(ledger_path, isolation_level=None)
    try:
        holder.execute("PRAGMA wal_autocheckpoint = 0")
        holder.execute(
            "INSERT INTO execution_events (proposal_id, event_type, from_state, to_state, "
            "payload_json, created_at) VALUES ('fixture-pending', 'WAL_ONLY', NULL, 'PENDING', '{}', 't')"
        )
        before = dump(ledger_path)

        manifest = inspect(ledger_path, backup_dir)

        assert dump(ledger_path) == before
        assert user_version(ledger_path) == 5

        # The backup has the WAL-only row...
        backup = Path(manifest["backup_path"])
        connection = ro_connect(backup)
        try:
            assert connection.execute(
                "SELECT COUNT(*) FROM execution_events WHERE event_type = 'WAL_ONLY'"
            ).fetchone()[0] == 1
        finally:
            connection.close()
        # ...and a plain copy of only the main file does not.
        plain = tmp_path / "plain-copy.sqlite3"
        shutil.copyfile(ledger_path, plain)
        connection = ro_connect(plain)
        try:
            assert connection.execute(
                "SELECT COUNT(*) FROM execution_events WHERE event_type = 'WAL_ONLY'"
            ).fetchone()[0] == 0
        finally:
            connection.close()
    finally:
        holder.close()

    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.parent.stat().st_mode) == 0o700
    manifest_file = Path(manifest["manifest_path"])
    assert stat.S_IMODE(manifest_file.stat().st_mode) == 0o600
    assert manifest["integrity_check"] == "ok"
    assert manifest["foreign_key_check"] == []
    assert manifest["distinct_workspaces"] == ["uk"]
    assert manifest["decision_required"] == "confirm-one"
    assert manifest["source_user_version"] == 5
    assert manifest["backup_sha256"] == hashlib.sha256(backup.read_bytes()).hexdigest()
    assert manifest["row_counts"]["order_intents"] == 3
    assert manifest["row_counts"]["execution_events"] == 19
    assert manifest["approval_key_ids"] and manifest["approval_key_ids"][0]["workspace"] == "uk"
    assert "approval_challenges.signed_payload" in manifest["workspace_tags"]
    assert "order_intents.canonical_json" in manifest["workspace_tags"]


def test_inspect_is_refused_while_a_writer_holds_the_lock(tmp_path, backup_dir):
    path = tmp_path / "live" / "execution.sqlite3"
    with ExecutionLedger(path, workspace="uk"):
        before = dump(path)
        code, result = run_tool("inspect", "--ledger", path, "--backup-dir", backup_dir)
        assert dump(path) == before
    assert code == 2
    assert result["code"] == "WRITER_ACTIVE"
    assert not backup_dir.exists() or not any(backup_dir.iterdir())


def test_apply_pins_a_uk_only_ledger_and_keeps_every_byte(ledger_path, backup_dir):
    manifest = inspect(ledger_path, backup_dir)
    code, report = apply(ledger_path, manifest, "uk")
    assert code == 0, report
    assert report["status"] == "applied"
    assert report["workspace"] == "uk"

    connection = ro_connect(ledger_path)
    backup = ro_connect(manifest["backup_path"])
    try:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 6
        assert connection.execute(
            "SELECT workspace, pinned_by FROM ledger_identity"
        ).fetchall() == [("uk", "operator-confirmed-migration")]
        assert {row[0] for row in connection.execute("SELECT DISTINCT workspace FROM order_intents")} == {"uk"}

        compared = {
            "order_intents": ("proposal_id", ("canonical_json", "intent_hash")),
            "approval_challenges": ("challenge_id", ("signed_payload", "intent_hash")),
            "execution_approvals": ("approval_id", ("signature_der", "signed_payload_hash")),
            "approval_keys": ("key_id", ("public_key_x963",)),
            "execution_events": ("event_id", ("payload_json",)),
        }
        for table, (key, columns) in compared.items():
            sql = f"SELECT {key}, {', '.join(columns)} FROM {table} ORDER BY {key}"
            before = backup.execute(sql).fetchall()
            assert before, table
            assert connection.execute(sql).fetchall() == before, table
    finally:
        connection.close()
        backup.close()

    code, verified = run_tool(
        "verify", "--ledger", ledger_path, "--manifest", manifest["manifest_path"]
    )
    assert code == 0, verified
    assert verified["status"] == "verified"
    assert "byte-identity" in verified["checks"]

    with ExecutionLedger(ledger_path, workspace="uk") as ledger:
        for proposal_id in FIXTURE_ORDERS:
            assert ledger.get_order(proposal_id) is not None
    with pytest.raises(WorkspaceMismatch):
        ExecutionLedger(ledger_path, workspace="india")


def test_apply_adds_only_structure_no_existing_row_changes(ledger_path, backup_dir):
    manifest = inspect(ledger_path, backup_dir)
    before_counts = manifest["row_counts"]
    code, _ = apply(ledger_path, manifest, "uk")
    assert code == 0
    connection = ro_connect(ledger_path)
    try:
        for table, count in before_counts.items():
            assert connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == count
    finally:
        connection.close()


def test_ledger_tool_never_compares_against_a_plain_file_copy():
    source = (REPO_ROOT / "backend" / "execution" / "ledger_migration.py").read_text()
    assert ".backup(" in source
    assert "shutil.copy" not in source and "copyfile" not in source
