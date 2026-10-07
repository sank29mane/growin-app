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
    LedgerReader,
    LedgerUnpinned,
    LedgerVenueMismatch,
    VenueBinding,
    Workspace,
    WorkspaceMismatch,
    coerce_workspace,
    default_ledger_path,
    practice_ledger_path,
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


# ---------------------------------------------------------------------------
# Task 2: the pin is enforced by SQLite itself
# ---------------------------------------------------------------------------

GUARD_MESSAGE = "workspace does not match ledger identity"

# One minimal valid row per workspace-bearing table. :ws is the workspace value.
ROW_SQL = {
    "order_intents": (
        "INSERT INTO order_intents (proposal_id, client_order_id, intent_hash, canonical_json, created_at) "
        "VALUES ('raw-1', 'growin-raw-1', 'h', json_object('workspace', :ws, 'proposal_id', 'raw-1'), 't')"
    ),
    "approval_keys": (
        "INSERT INTO approval_keys (workspace, key_id, public_key_x963, created_at) "
        "VALUES (:ws, 'k1', zeroblob(65), 't')"
    ),
    "approval_challenges": (
        "INSERT INTO approval_challenges (challenge_id, proposal_id, workspace, key_id, intent_hash, "
        "signed_payload, issued_at_epoch, expires_at_epoch, created_at) "
        "VALUES ('c1', 'raw-1', :ws, 'k1', 'h', x'00', 1, 2, 't')"
    ),
    "execution_approvals": (
        "INSERT INTO execution_approvals (approval_id, challenge_id, proposal_id, workspace, key_id, "
        "intent_hash, signed_payload_hash, signature_der, approved_at) "
        "VALUES ('a1', 'c1', 'raw-1', :ws, 'k1', 'h', 'ph', x'00', 't')"
    ),
    "execution_admissions": (
        "INSERT INTO execution_admissions (proposal_id, intent_hash, workspace, account, currency, ticker, "
        "side, original_quantity, final_quantity, price, notional, simulator_fill_price, "
        "simulator_drawdown_pct, risk_quantity, current_spread_pct, evidence_at, evidence_hash, decision, "
        "reason_code, created_at) "
        "VALUES ('raw-1', 'h', :ws, 'paper', 'GBP', 'X', 'BUY', '1', '1', '1', '1', '1', '0', '1', '0', "
        "'t', 'eh', 'ADMITTED', 'OK', 't')"
    ),
    "paper_budgets": (
        "INSERT INTO paper_budgets (workspace, account, currency, amount, created_at, updated_at) "
        "VALUES (:ws, 'paper', 'GBP', '100', 't', 't')"
    ),
    "buying_power_reservations": (
        "INSERT INTO buying_power_reservations (proposal_id, workspace, account, currency, intent_hash, "
        "reserved, state, created_at, updated_at) "
        "VALUES ('raw-1', :ws, 'paper', 'GBP', 'h', '1', 'ACTIVE', 't', 't')"
    ),
    "workspace_controls": (
        "INSERT INTO workspace_controls (workspace, updated_at) VALUES (:ws, 't')"
    ),
    "workspace_control_events": (
        "INSERT INTO workspace_control_events (workspace, version, engaged, purpose, reason_code, "
        "evidence_id, created_at) VALUES (:ws, 1, 1, 'ENGAGE', 'R', 'e', 't')"
    ),
    "paper_positions": (
        "INSERT INTO paper_positions (workspace, account, currency, ticker, updated_at) "
        "VALUES (:ws, 'paper', 'GBP', 'X', 't')"
    ),
}
UPDATE_GUARDED = (
    "paper_budgets",
    "buying_power_reservations",
    "workspace_controls",
    "workspace_control_events",
    "paper_positions",
    "execution_admissions",
)


@pytest.fixture
def uk_file(tmp_path):
    path = tmp_path / "uk" / "execution.sqlite3"
    with ExecutionLedger(path, workspace="uk") as ledger:
        ledger.register_intent(make_intent("uk-1", workspace="uk"))
    return path


def raw_connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("PRAGMA foreign_keys = OFF")
    return connection


def test_ten_insert_guards_exist(uk_file):
    connection = ro_connect(uk_file)
    try:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'trigger' "
                "AND name LIKE '%_workspace_insert_guard'"
            )
        }
    finally:
        connection.close()
    assert names == {f"{table}_workspace_insert_guard" for table in ROW_SQL}
    assert len(names) == 10


@pytest.mark.parametrize("table", sorted(ROW_SQL))
def test_insert_guard_refuses_a_foreign_workspace_row(uk_file, table):
    connection = raw_connect(uk_file)
    try:
        # Positive control: the same statement with the pinned workspace is valid.
        connection.execute("BEGIN")
        connection.execute(ROW_SQL[table], {"ws": "uk"})
        connection.execute("ROLLBACK")

        with pytest.raises(sqlite3.IntegrityError, match=GUARD_MESSAGE):
            connection.execute(ROW_SQL[table], {"ws": "india"})

        # Without its trigger the same insert goes through, so the trigger is
        # the thing that refused it.
        connection.execute(f"DROP TRIGGER {table}_workspace_insert_guard")
        connection.execute("BEGIN")
        connection.execute(ROW_SQL[table], {"ws": "india"})
        connection.execute("ROLLBACK")
    finally:
        connection.close()


@pytest.mark.parametrize("table", UPDATE_GUARDED)
def test_update_guard_refuses_changing_a_row_workspace(uk_file, table):
    connection = raw_connect(uk_file)
    try:
        connection.execute("BEGIN")
        connection.execute(ROW_SQL[table], {"ws": "uk"})
        with pytest.raises(sqlite3.IntegrityError, match=GUARD_MESSAGE):
            connection.execute(f"UPDATE {table} SET workspace = 'india'")
        # Rewriting the same workspace is still allowed.
        connection.execute(f"UPDATE {table} SET workspace = 'uk'")
        connection.execute(f"DROP TRIGGER {table}_workspace_update_guard")
        connection.execute(f"UPDATE {table} SET workspace = 'india'")
        connection.execute("ROLLBACK")
    finally:
        connection.close()


@pytest.mark.parametrize(
    "statement",
    ["UPDATE ledger_identity SET workspace = 'india'", "DELETE FROM ledger_identity"],
)
def test_ledger_identity_is_immutable(uk_file, statement):
    connection = raw_connect(uk_file)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="ledger identity is immutable"):
            connection.execute(statement)
        assert connection.execute("SELECT workspace FROM ledger_identity").fetchall() == [("uk",)]
    finally:
        connection.close()


def test_engage_and_clear_still_work_under_the_guards(tmp_path):
    with ExecutionLedger(tmp_path / "execution.sqlite3", workspace="uk") as ledger:
        control = ledger.engage_workspace_control("MANUAL_KILL", workspace="uk")
        assert control.engaged is True
        cleared = ledger.clear_workspace_control(
            workspace="uk", version=control.version, evidence_id="e-1"
        )
        assert cleared.engaged is False


def smuggle_foreign_order(path: Path) -> None:
    connection = raw_connect(path)
    try:
        connection.execute("DROP TRIGGER order_intents_workspace_insert_guard")
        connection.execute(
            "INSERT INTO order_intents VALUES ('smuggled', 'growin-smuggled', 'h', "
            "json_object('workspace', 'india', 'proposal_id', 'smuggled'), 't')"
        )
        connection.execute(
            "INSERT INTO order_projection VALUES ('smuggled', 'PENDING', NULL, NULL, 't', 't')"
        )
    finally:
        connection.close()


def test_stored_foreign_row_is_rejected_on_load_even_if_the_trigger_is_gone(uk_file):
    smuggle_foreign_order(uk_file)
    with ExecutionLedger(uk_file, workspace="uk") as ledger:
        with pytest.raises(WorkspaceMismatch):
            ledger.get_order("smuggled")
        assert ledger.get_order("uk-1") is not None


def test_reader_reads_while_a_writer_holds_the_file(uk_file):
    with ExecutionLedger(uk_file, workspace="uk") as writer:
        with LedgerReader(uk_file, workspace="uk") as reader:
            assert reader.get_order("uk-1").proposal_id == "uk-1"
            assert [event.event_type for event in reader.list_events("uk-1")] == ["INTENT_CREATED"]
            assert reader.get_order("nope") is None
            assert reader.list_attempts() == []
            assert reader.list_requotes() == []
            assert reader.get_admission("uk-1") is None
            assert reader.get_reservation("uk-1") is None
            assert reader.get_paper_budget("paper", "GBP", workspace="uk") is None
            assert reader.get_paper_position("paper", "GBP", "X", workspace="uk") is None
            assert reader.get_approval_key(workspace="uk") is None
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                reader._connection.execute(
                    "INSERT INTO workspace_controls (workspace, updated_at) VALUES ('uk', 't')"
                )
        writer.register_intent(make_intent("uk-2"))


def test_reader_control_read_never_inserts(uk_file):
    before = snapshot(uk_file)
    with LedgerReader(uk_file, workspace="uk") as reader:
        control = reader.get_workspace_control(workspace="uk")
        assert control.engaged is False
        assert control.version == 0
        with pytest.raises(WorkspaceMismatch):
            reader.get_workspace_control(workspace="india")
    assert snapshot(uk_file)["dump"] == before["dump"]


def test_reader_refuses_wrong_workspace_unpinned_missing_and_symlink(india_file, tmp_path):
    with pytest.raises(WorkspaceMismatch):
        LedgerReader(india_file, workspace="uk")

    v5 = replay_v5_fixture(tmp_path / "v5.sqlite3")
    with pytest.raises(LedgerUnpinned):
        LedgerReader(v5, workspace="uk")

    with pytest.raises(LedgerError, match="does not exist"):
        LedgerReader(tmp_path / "missing.sqlite3", workspace="uk")

    link = tmp_path / "link.sqlite3"
    link.symlink_to(india_file)
    with pytest.raises(LedgerError, match="symbolic link"):
        LedgerReader(link, workspace="india")


def test_reader_scoped_reads_check_the_workspace(uk_file):
    with LedgerReader(uk_file, workspace="uk") as reader:
        for call in (
            lambda: reader.get_paper_budget("paper", "GBP", workspace="india"),
            lambda: reader.get_paper_position("paper", "GBP", "X", workspace="india"),
            lambda: reader.get_approval_key(workspace=None),
        ):
            with pytest.raises(WorkspaceMismatch):
                call()


def test_reader_rejects_a_stored_foreign_order(uk_file):
    smuggle_foreign_order(uk_file)
    with LedgerReader(uk_file, workspace="uk") as reader:
        with pytest.raises(WorkspaceMismatch):
            reader.get_order("smuggled")


def test_reader_works_in_a_directory_with_a_space(tmp_path):
    path = tmp_path / "dir with space" / "execution.sqlite3"
    with ExecutionLedger(path, workspace="india") as ledger:
        ledger.register_intent(make_intent("sp-1", workspace="india"))
    with LedgerReader(path, workspace="india") as reader:
        assert reader.get_order("sp-1") is not None


def test_old_sqlite_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 30, 0))
    with pytest.raises(LedgerError, match="3.31"):
        ExecutionLedger(tmp_path / "execution.sqlite3", workspace="uk")


def test_package_exports():
    import execution

    for name in ("Workspace", "WorkspaceMismatch", "LedgerUnpinned", "LedgerReader", "coerce_workspace"):
        assert name in execution.__all__
        assert hasattr(execution, name)


# --- Phase 66: practice ledger binding (D-01, D-21) ---------------------------

V6_PRE66_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "ledger_v6_pre66.sql"
SYNTH_ACCOUNT = "acct-synthetic-0001"


def practice_binding(account_id: str = SYNTH_ACCOUNT) -> VenueBinding:
    return VenueBinding(venue="t212_practice", account_id=account_id, currency="GBP")


def structure(path: Path) -> dict:
    """Table, trigger and index names plus per-table row counts."""

    connection = ro_connect(path)
    try:
        objects = sorted(
            (row[0], row[1])
            for row in connection.execute(
                "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        )
        counts = {
            name: connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            for kind, name in objects
            if kind == "table"
        }
    finally:
        connection.close()
    return {"objects": objects, "counts": counts}


def replay_v6_pre66_fixture(path: Path) -> Path:
    """A v6 paper ledger as the pre-66 ledger code wrote it (captured SQL)."""

    connection = sqlite3.connect(path, isolation_level=None)
    try:
        connection.executescript(V6_PRE66_FIXTURE.read_text(encoding="utf-8"))
        connection.execute("PRAGMA journal_mode=WAL").fetchone()
    finally:
        connection.close()
    return path


def test_practice_ledger_records_venue_account_and_currency_at_creation(tmp_path):
    path = tmp_path / "practice" / "execution.sqlite3"
    with ExecutionLedger(path, workspace="uk", venue=practice_binding()) as ledger:
        assert ledger.workspace == Workspace.UK
        assert ledger.venue_binding == practice_binding()
        assert ledger.pragmas()["user_version"] == 6  # no SCHEMA_VERSION bump

    connection = ro_connect(path)
    try:
        rows = connection.execute(
            "SELECT singleton, venue, account_id, currency FROM ledger_venue_binding"
        ).fetchall()
        identity = connection.execute("SELECT workspace FROM ledger_identity").fetchall()
    finally:
        connection.close()
    assert rows == [(1, "t212_practice", SYNTH_ACCOUNT, "GBP")]
    assert identity == [("uk",)]


def test_practice_ledger_reopens_only_with_its_own_binding(tmp_path):
    path = tmp_path / "execution.sqlite3"
    with ExecutionLedger(path, workspace="uk", venue=practice_binding()):
        pass
    before = snapshot(path)

    with ExecutionLedger(path, workspace="uk", venue=practice_binding()) as ledger:
        assert ledger.venue_binding == practice_binding()
    with pytest.raises(LedgerVenueMismatch) as other_account:
        ExecutionLedger(path, workspace="uk", venue=practice_binding("acct-other-0002"))
    assert SYNTH_ACCOUNT not in str(other_account.value)
    assert "acct-other-0002" not in str(other_account.value)

    after = snapshot(path)
    assert after["dump"] == before["dump"]
    assert after["sha256"] == before["sha256"]


def test_binding_refuses_another_currency_or_venue_before_it_can_be_written(tmp_path):
    with pytest.raises(ValueError):
        VenueBinding(venue="t212_practice", account_id=SYNTH_ACCOUNT, currency="INR")
    with pytest.raises(ValueError):
        VenueBinding(venue="paper", account_id=SYNTH_ACCOUNT, currency="GBP")
    with pytest.raises(ValueError):
        VenueBinding(venue="t212_practice", account_id="", currency="GBP")
    assert not (tmp_path / "execution.sqlite3").exists()


def test_practice_binding_is_a_uk_binding(tmp_path):
    with pytest.raises(LedgerVenueMismatch):
        ExecutionLedger(tmp_path / "india.sqlite3", workspace="india", venue=practice_binding())
    assert not (tmp_path / "india.sqlite3").exists()


def test_real_uk_ledger_is_never_opened_as_practice(tmp_path):
    path = tmp_path / "execution.sqlite3"
    with ExecutionLedger(path, workspace="uk") as ledger:
        ledger.register_intent(make_intent("real-1"))
    before = snapshot(path)

    with pytest.raises(LedgerVenueMismatch):
        ExecutionLedger(path, workspace="uk", venue=practice_binding())

    after = snapshot(path)
    assert after["dump"] == before["dump"]
    assert after["sha256"] == before["sha256"]


def test_practice_ledger_is_never_opened_as_paper(tmp_path):
    path = tmp_path / "execution.sqlite3"
    with ExecutionLedger(path, workspace="uk", venue=practice_binding()):
        pass
    before = snapshot(path)

    with pytest.raises(LedgerVenueMismatch):
        ExecutionLedger(path, workspace="uk")

    assert snapshot(path)["sha256"] == before["sha256"]


def test_a_ledger_bound_to_a_kind_that_is_no_longer_registered_refuses_to_open(tmp_path):
    from venue_registry import VenueSpec, override_venue_specs

    kind = "test_only_venue"
    spec = VenueSpec(
        kind=kind,
        workspace="india",
        currency="INR",
        modes=frozenset({"PRACTICE"}),
        ledger_path=lambda: tmp_path / "unused.sqlite3",
        dispatcher_key=kind,
    )
    path = tmp_path / "execution.sqlite3"
    with override_venue_specs({kind: spec}):
        binding = VenueBinding(venue=kind, account_id="acct-test-only-01", currency="INR")
        with ExecutionLedger(path, workspace="india", venue=binding):
            pass
        # Still registered: it reopens.
        with ExecutionLedger(path, workspace="india", venue=binding):
            pass
    before = snapshot(path)

    # Out of the override the kind is gone. Neither an unbound open (as paper)
    # nor a bound open of the stored kind may succeed; the file is untouched.
    with pytest.raises(LedgerVenueMismatch):
        ExecutionLedger(path, workspace="india")
    with pytest.raises(LedgerVenueMismatch):
        ExecutionLedger(path, workspace="india", venue=binding)
    assert snapshot(path)["sha256"] == before["sha256"]


def test_venue_binding_is_database_immutable(tmp_path):
    path = tmp_path / "execution.sqlite3"
    with ExecutionLedger(path, workspace="uk", venue=practice_binding()):
        pass
    connection = sqlite3.connect(path)
    try:
        for statement in (
            "UPDATE ledger_venue_binding SET account_id = 'acct-other-0002'",
            "DELETE FROM ledger_venue_binding",
        ):
            with pytest.raises(sqlite3.DatabaseError, match="immutable"):
                connection.execute(statement)
    finally:
        connection.close()


def test_a_binding_table_with_no_valid_row_fails_closed(tmp_path):
    path = tmp_path / "execution.sqlite3"
    with ExecutionLedger(path, workspace="uk", venue=practice_binding()):
        pass
    connection = sqlite3.connect(path)
    try:
        connection.execute("DROP TRIGGER ledger_venue_binding_no_delete")
        connection.execute("DELETE FROM ledger_venue_binding")
        connection.commit()
    finally:
        connection.close()
    for venue in (None, practice_binding()):
        with pytest.raises(LedgerVenueMismatch):
            ExecutionLedger(path, workspace="uk", venue=venue)


def test_practice_ledger_path_is_not_the_real_uk_ledger_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    practice = practice_ledger_path()
    real = default_ledger_path("uk")
    assert practice != real
    assert practice.parent != real.parent
    assert tmp_path in practice.parents


def test_a_v6_ledger_written_before_66_opens_with_no_structural_change(tmp_path):
    path = replay_v6_pre66_fixture(tmp_path / "execution.sqlite3")
    before = structure(path)
    assert before["counts"]["order_intents"] == 1
    assert "ledger_venue_binding" not in {name for _, name in before["objects"]}

    with ExecutionLedger(path, workspace="uk") as ledger:
        assert ledger.venue_binding is None
        assert ledger.allowed_mode.value == "PAPER"
        assert ledger.pragmas()["user_version"] == 6
        assert ledger.get_order("fixture-1") is not None

    after = structure(path)
    assert after["objects"] == before["objects"]
    assert after["counts"] == before["counts"]


def test_a_v6_ledger_written_before_66_is_refused_as_practice_untouched(tmp_path):
    path = replay_v6_pre66_fixture(tmp_path / "execution.sqlite3")
    before = snapshot(path)

    with pytest.raises(LedgerVenueMismatch):
        ExecutionLedger(path, workspace="uk", venue=practice_binding())

    after = snapshot(path)
    assert after["dump"] == before["dump"]
    assert after["sha256"] == before["sha256"]
