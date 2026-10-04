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


# ---------------------------------------------------------------------------
# Task 2: refusals, restore round trip, v1 path, show
# ---------------------------------------------------------------------------

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


def build_v1_ledger(path: Path, workspace: str) -> str:
    """A rollback-journal v1 file with one legacy order; returns its intent hash."""

    from execution.ledger import canonical_json
    from execution.models import OrderIntent

    path.parent.mkdir(parents=True, exist_ok=True)
    intent = OrderIntent(
        proposal_id="legacy",
        workspace=workspace,
        account="paper",
        broker="paper",
        mode="PAPER",
        ticker="TQQQ",
        side="BUY",
        quantity="1",
    )
    snapshot = canonical_json(intent)
    digest = hashlib.sha256(snapshot.encode()).hexdigest()
    connection = sqlite3.connect(path)
    connection.executescript(V1_DDL)
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
    return digest


def assert_refused(outcome: tuple[int, dict], code: str, ledger: Path, before: list[str], version: int = 5) -> None:
    exit_code, result = outcome
    assert exit_code == 2, result
    assert result["status"] == "refused"
    assert result["code"] == code, result
    assert dump(ledger) == before
    assert user_version(ledger) == version


def test_apply_refuses_missing_short_or_wrong_confirmation(ledger_path, backup_dir):
    manifest = inspect(ledger_path, backup_dir)
    before = dump(ledger_path)

    assert_refused(apply(ledger_path, manifest, workspace=None), "CONFIRMATION_REQUIRED", ledger_path, before)
    assert_refused(apply(ledger_path, manifest, "uk", prefix=None), "CONFIRMATION_REQUIRED", ledger_path, before)
    assert_refused(apply(ledger_path, manifest, "uk", prefix="abc123"), "CONFIRMATION_REQUIRED", ledger_path, before)
    wrong = "0" * 12 if not manifest["backup_sha256"].startswith("0" * 12) else "f" * 12
    assert_refused(apply(ledger_path, manifest, "uk", prefix=wrong), "CONFIRMATION_MISMATCH", ledger_path, before)


def test_apply_refuses_when_the_backup_file_was_altered(ledger_path, backup_dir):
    manifest = inspect(ledger_path, backup_dir)
    before = dump(ledger_path)
    backup = Path(manifest["backup_path"])
    with open(backup, "ab") as handle:
        handle.write(b"x")
    assert_refused(apply(ledger_path, manifest, "uk"), "BACKUP_HASH_MISMATCH", ledger_path, before)


def test_conflicting_tags_are_blocked_whatever_the_confirmation(ledger_path, backup_dir):
    connection = sqlite3.connect(ledger_path, isolation_level=None)
    connection.execute(
        "INSERT INTO order_intents VALUES ('stray', 'growin-stray', 'h', "
        "'{\"proposal_id\":\"stray\",\"workspace\":\"india\"}', 't')"
    )
    connection.close()
    manifest = inspect(ledger_path, backup_dir)
    assert manifest["distinct_workspaces"] == ["india", "uk"]
    assert manifest["decision_required"] == "blocked-conflict"
    before = dump(ledger_path)
    for workspace in ("uk", "india"):
        assert_refused(apply(ledger_path, manifest, workspace), "CONFLICTING_TAGS", ledger_path, before)


def test_unknown_tag_is_blocked(ledger_path, backup_dir):
    connection = sqlite3.connect(ledger_path, isolation_level=None)
    connection.execute(
        "INSERT INTO execution_events (proposal_id, event_type, from_state, to_state, payload_json, created_at) "
        "VALUES ('fixture-pending', 'X', NULL, 'PENDING', '{\"nested\": [{\"workspace\": \"mars\"}]}', 't')"
    )
    connection.close()
    manifest = inspect(ledger_path, backup_dir)
    assert manifest["decision_required"] == "blocked-unknown"
    before = dump(ledger_path)
    assert_refused(apply(ledger_path, manifest, "uk"), "UNKNOWN_TAG", ledger_path, before)


def test_unparseable_payload_is_blocked(ledger_path, backup_dir):
    connection = sqlite3.connect(ledger_path, isolation_level=None)
    connection.execute(
        "INSERT INTO execution_events (proposal_id, event_type, from_state, to_state, payload_json, created_at) "
        "VALUES ('fixture-pending', 'X', NULL, 'PENDING', 'not json', 't')"
    )
    connection.close()
    manifest = inspect(ledger_path, backup_dir)
    assert manifest["decision_required"] == "blocked-unparseable"
    assert manifest["unparseable"]
    before = dump(ledger_path)
    assert_refused(apply(ledger_path, manifest, "uk"), "UNPARSEABLE_PAYLOAD", ledger_path, before)


def test_india_only_ledger_refuses_uk_and_accepts_india(tmp_path, backup_dir):
    path = tmp_path / "ledgers" / "india" / "execution.sqlite3"
    build_v1_ledger(path, "india")
    manifest = inspect(path, backup_dir)
    assert manifest["distinct_workspaces"] == ["india"]
    before = dump(path)
    assert_refused(apply(path, manifest, "uk"), "CONFIRMATION_MISMATCH", path, before, version=1)

    code, report = apply(path, manifest, "india")
    assert code == 0, report
    assert report["workspace"] == "india"


def test_ledger_changed_after_inspect_is_stale_and_rolls_back(ledger_path, backup_dir):
    manifest = inspect(ledger_path, backup_dir)
    connection = sqlite3.connect(ledger_path, isolation_level=None)
    connection.execute(
        "INSERT INTO execution_events (proposal_id, event_type, from_state, to_state, payload_json, created_at) "
        "VALUES ('fixture-pending', 'LATE', NULL, 'PENDING', '{}', 't')"
    )
    connection.close()
    before = dump(ledger_path)
    assert_refused(apply(ledger_path, manifest, "uk"), "STALE_INSPECTION", ledger_path, before)


def _insert_india_intent(ledger: Path) -> None:
    connection = sqlite3.connect(ledger, isolation_level=None)
    connection.execute(
        "INSERT INTO order_intents VALUES ('stray', 'growin-stray', 'h', "
        "'{\"proposal_id\":\"stray\",\"workspace\":\"india\"}', 't')"
    )
    connection.close()


def test_rewritten_manifest_digest_cannot_bypass_staleness(ledger_path, backup_dir):
    # Review repro: the manifest digest is not covered by the confirmed backup
    # hash, so rewriting it to the live digest must still be refused.
    manifest = inspect(ledger_path, backup_dir)
    _insert_india_intent(ledger_path)
    connection = ro_connect(ledger_path)
    try:
        live_digest = hashlib.sha256("\n".join(connection.iterdump()).encode("utf-8")).hexdigest()
    finally:
        connection.close()
    manifest_path = Path(manifest["manifest_path"])
    on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
    on_disk["content_digest"] = live_digest
    manifest_path.write_text(json.dumps(on_disk), encoding="utf-8")
    before = dump(ledger_path)
    assert_refused(apply(ledger_path, manifest, "uk"), "STALE_INSPECTION", ledger_path, before)
    with pytest.raises(LedgerUnpinned):
        ExecutionLedger(ledger_path, workspace="uk")


def test_pin_is_checked_against_live_rows_before_commit(ledger_path, backup_dir, monkeypatch):
    from execution import ledger_migration

    manifest = inspect(ledger_path, backup_dir)
    _insert_india_intent(ledger_path)
    before = dump(ledger_path)
    # Defeat the staleness digest so only the in-transaction pin check stands.
    monkeypatch.setattr(ledger_migration, "_content_digest", lambda _connection: "same")
    manifest_path = Path(manifest["manifest_path"])
    on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
    on_disk["content_digest"] = "same"
    manifest_path.write_text(json.dumps(on_disk), encoding="utf-8")
    with pytest.raises(ledger_migration.MigrationRefused) as info:
        ledger_migration.apply_migration(
            ledger_path,
            manifest_path,
            confirm_workspace="uk",
            confirm_backup_sha256_prefix=manifest["backup_sha256"][:12],
        )
    assert info.value.code == "CONFLICTING_TAGS"
    assert dump(ledger_path) == before
    assert user_version(ledger_path) == 5


def test_verify_failure_after_commit_restores_the_backup(ledger_path, backup_dir, monkeypatch):
    from execution import ledger_migration

    manifest = inspect(ledger_path, backup_dir)
    before = dump(ledger_path)

    def failing_verify(_ledger, _manifest):
        raise ledger_migration.MigrationRefused("VERIFY_FAILED", "forced")

    monkeypatch.setattr(ledger_migration, "_verify", failing_verify)
    with pytest.raises(ledger_migration.MigrationRefused) as info:
        ledger_migration.apply_migration(
            ledger_path,
            manifest["manifest_path"],
            confirm_workspace="uk",
            confirm_backup_sha256_prefix=manifest["backup_sha256"][:12],
        )
    assert info.value.code == "VERIFY_FAILED"
    assert "restored from the backup" in info.value.message
    assert dump(ledger_path) == before
    assert user_version(ledger_path) == 5
    assert list(ledger_path.parent.glob("*.v6-aside-*"))
    on_disk = json.loads(Path(manifest["manifest_path"]).read_text(encoding="utf-8"))
    assert "confirmed_workspace" not in on_disk
    with pytest.raises(LedgerUnpinned):
        ExecutionLedger(ledger_path, workspace="uk")


def test_environment_never_supplies_ownership(ledger_path, backup_dir, monkeypatch):
    monkeypatch.setenv("GROWIN_WORKSPACE", "india")
    monkeypatch.setenv("GROWIN_EXECUTION_DB_PATH", str(ledger_path))
    manifest = inspect(ledger_path, backup_dir)
    assert manifest["decision_required"] == "confirm-one"
    before = dump(ledger_path)
    assert_refused(apply(ledger_path, manifest, "india"), "CONFIRMATION_MISMATCH", ledger_path, before)
    assert_refused(apply(ledger_path, manifest, workspace=None), "CONFIRMATION_REQUIRED", ledger_path, before)
    code, report = apply(ledger_path, manifest, "uk")
    assert code == 0, report
    assert report["workspace"] == "uk"


def test_backup_dir_inside_the_repo_is_refused_and_not_created(ledger_path):
    inside = REPO_ROOT / "tmp-ledger-backups-must-not-exist"
    before = dump(ledger_path)
    outcome = run_tool("inspect", "--ledger", ledger_path, "--backup-dir", inside)
    assert_refused(outcome, "BACKUP_INSIDE_REPO", ledger_path, before)
    assert not inside.exists()


def test_inspect_of_a_pinned_ledger_is_refused(ledger_path, backup_dir, tmp_path):
    manifest = inspect(ledger_path, backup_dir)
    assert apply(ledger_path, manifest, "uk")[0] == 0
    before = dump(ledger_path)
    outcome = run_tool("inspect", "--ledger", ledger_path, "--backup-dir", tmp_path / "second")
    assert_refused(outcome, "ALREADY_PINNED", ledger_path, before, version=6)


def test_missing_ledger_and_symlink_are_refused(tmp_path, backup_dir, ledger_path):
    code, result = run_tool("inspect", "--ledger", tmp_path / "nope.sqlite3", "--backup-dir", backup_dir)
    assert (code, result["code"]) == (2, "LEDGER_MISSING")
    link = tmp_path / "link.sqlite3"
    link.symlink_to(ledger_path)
    code, result = run_tool("inspect", "--ledger", link, "--backup-dir", backup_dir)
    assert (code, result["code"]) == (2, "SYMLINK_REFUSED")


def test_restore_round_trip_moves_aside_and_never_deletes(ledger_path, backup_dir):
    manifest = inspect(ledger_path, backup_dir)
    original_dump = dump(ledger_path)
    assert apply(ledger_path, manifest, "uk")[0] == 0
    assert user_version(ledger_path) == 6
    v6_dump = dump(ledger_path)

    # Without the flag nothing moves.
    code, result = run_tool("restore", "--ledger", ledger_path, "--manifest", manifest["manifest_path"])
    assert (code, result["code"]) == (2, "RESTORE_CONFIRMATION_REQUIRED")
    assert dump(ledger_path) == v6_dump

    present = [
        p for p in (ledger_path, Path(f"{ledger_path}-wal"), Path(f"{ledger_path}-shm")) if p.exists()
    ]
    assert ledger_path in present
    code, result = run_tool(
        "restore", "--ledger", ledger_path, "--manifest", manifest["manifest_path"], "--confirm-restore"
    )
    assert code == 0, result
    assert result["status"] == "restored"

    assert len(result["aside"]) == len(present)
    for moved in result["aside"]:
        assert Path(moved).exists()
        assert ".v6-aside-" in moved
    aside_main = next(Path(m) for m in result["aside"] if m.split(".v6-aside-")[0] == str(ledger_path))
    assert user_version(aside_main) == 6

    assert user_version(ledger_path) == 5
    assert dump(ledger_path) == original_dump
    digest = hashlib.sha256("\n".join(dump(ledger_path)).encode("utf-8")).hexdigest()
    assert digest == manifest["content_digest"]
    assert stat.S_IMODE(ledger_path.stat().st_mode) == 0o600
    with pytest.raises(LedgerUnpinned):
        ExecutionLedger(ledger_path, workspace="uk")

    # The same backup and manifest can pin the restored file again.
    code, report = apply(ledger_path, manifest, "uk")
    assert code == 0, report


def test_restore_is_refused_while_a_writer_holds_the_lock(ledger_path, backup_dir):
    manifest = inspect(ledger_path, backup_dir)
    assert apply(ledger_path, manifest, "uk")[0] == 0
    with ExecutionLedger(ledger_path, workspace="uk"):
        code, result = run_tool(
            "restore", "--ledger", ledger_path, "--manifest", manifest["manifest_path"], "--confirm-restore"
        )
    assert (code, result["code"]) == (2, "WRITER_ACTIVE")
    assert user_version(ledger_path) == 6


def test_v1_legacy_ledger_migrates_through_the_tool(tmp_path, backup_dir):
    path = tmp_path / "ledgers" / "uk" / "execution.sqlite3"
    digest = build_v1_ledger(path, "uk")
    manifest = inspect(path, backup_dir)
    assert manifest["source_user_version"] == 1
    assert manifest["distinct_workspaces"] == ["uk"]
    assert "execution_approvals" in manifest["absent_tables"]

    code, report = apply(path, manifest, "uk")
    assert code == 0, report
    assert user_version(path) == 6

    with ExecutionLedger(path, workspace="uk") as ledger:
        assert ledger.get_order("legacy").intent_hash == digest
        assert ledger.pragmas()["user_version"] == 6
    columns = {row[1] for row in sqlite3.connect(path).execute("PRAGMA table_info(dispatch_attempts)")}
    assert "approval_id" in columns


def test_show_reads_a_pinned_ledger_while_a_writer_holds_it(ledger_path, backup_dir):
    manifest = inspect(ledger_path, backup_dir)
    assert apply(ledger_path, manifest, "uk")[0] == 0
    with ExecutionLedger(ledger_path, workspace="uk"):
        code, result = run_tool("show", "--ledger", ledger_path, "--workspace", "uk")
        assert code == 0, result
        assert result["pin"]["workspace"] == "uk"
        assert result["pin"]["pinned_by"] == "operator-confirmed-migration"
        assert {order["proposal_id"] for order in result["orders"]} == set(FIXTURE_ORDERS)

        code, result = run_tool(
            "show", "--ledger", ledger_path, "--workspace", "uk", "--proposal-id", "fixture-pending"
        )
        assert code == 0 and result["order_count"] == 1

        code, result = run_tool("show", "--ledger", ledger_path, "--workspace", "india")
        assert code in (1, 2)
        assert result["error"] == "WorkspaceMismatch"


def test_show_on_an_unpinned_ledger_names_ledger_unpinned(ledger_path):
    before = dump(ledger_path)
    code, result = run_tool("show", "--ledger", ledger_path, "--workspace", "uk")
    assert code in (1, 2)
    assert result["error"] == "LedgerUnpinned"
    assert dump(ledger_path) == before


@pytest.mark.parametrize("relative", ["backend/execution/ledger_migration.py", "scripts/ledger_tool.py"])
def test_tool_sources_never_read_the_environment(relative):
    tree = ast.parse((REPO_ROOT / relative).read_text(encoding="utf-8"))
    banned = {"getenv", "environ", "environb", "getenvb"}
    offenders = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in banned:
            offenders.append(node.attr)
        elif isinstance(node, ast.Name) and node.id in banned:
            offenders.append(node.id)
        elif isinstance(node, ast.ImportFrom) and any(alias.name in banned for alias in node.names):
            offenders.append(node.module or "")
    assert offenders == []
