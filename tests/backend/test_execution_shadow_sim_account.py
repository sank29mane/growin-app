"""Phase 64-07 Task 2: the SHADOW simulation account (SL-1 to SL-9), on real ledger files.

Every ledger here is a fresh file under ``tmp_path``. Nothing contacts a broker, the relay or the
VM: the mint and audit ports are in-memory fakes with the same append and answer shapes the 64-03
store and the real relay will have. Helpers come from ``test_shadow_india_admission``.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable

import pytest

import execution.ledger as ledger_module
from execution import (
    ApprovalConflict,
    ExecutionConflictError,
    ExecutionDisabledError,
    ExecutionLedger,
    ExecutionService,
    LedgerError,
    VenueBinding,
)
from execution.ledger import (
    IndiaCapExceeded,
    IntentConflict,
    InvalidTransition,
    SimulatedCashInsufficient,
    SimulatedCashNegative,
    SimulationInit,
    simulated_booking_id,
)
from execution.models import AdmissionDecision, Workspace
from india_limits_support import NOW, SYMBOL, TICKER
from shadow_india.admission import (
    AuditEntry,
    AuditPage,
    MINT_AMBIGUOUS,
    MintUnresolved,
    PREP_INTERRUPTED_BEFORE_MINT,
    ShadowAbort,
    ShadowPreparation,
    VmDecision,
)
from test_shadow_india_admission import (
    RUN_ID,
    SEED,
    Rig,
    buy,
    frozen_service_clock,  # noqa: F401 - a fixture, used through pytestmark
    make_rig,
    prepare_buy,
    reopen,
    run,
    samples,
    sha,
)

pytestmark = pytest.mark.usefixtures("frozen_service_clock")

SCHEDULE = sha("schedule")


def binding(venue: str = "breeze_relay", account: str = "sim-run1", currency: str = "INR") -> VenueBinding:
    return VenueBinding(venue=venue, account_id=account, currency=currency)


def book(
    rig: Rig,
    proposal_id: str,
    cumulative: Any,
    price: Any,
    charges: Any = "0",
    *,
    attempt: str = "attempt-1",
    scenario: str | None = None,
):
    scenario_sha = scenario or sha(f"scenario-{proposal_id}-{attempt}")
    return rig.ledger.book_simulated_fill(
        simulated_booking_id(attempt, scenario_sha),
        proposal_id,
        cumulative,
        price,
        charges,
        SCHEDULE,
        scenario_sha,
        attempt,
    )


def sell(rig: Rig, proposal_id: str, *, quantity: int, limit: str = "100.00"):
    intent = rig.adapter.build_intent(
        proposal_id, symbol=SYMBOL, side="SELL", quantity=quantity, limit_price=limit
    )
    return run(rig.adapter.prepare(intent, samples(NOW)))


def file_sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def raw(path: Path, *statements: str, drop: Iterable[str] = ()) -> None:
    """Edit a closed ledger file through plain SQLite, dropping named triggers first."""

    connection = sqlite3.connect(path)
    try:
        for trigger in drop:
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        for statement in statements:
            connection.execute(statement)
        connection.commit()
    finally:
        connection.close()


# --- initialization and reopen (r4 F1) ----------------------------------------------------


def test_a_fresh_simulation_ledger_reopens_as_a_simulation_ledger(tmp_path, monkeypatch):
    rig = make_rig(tmp_path)
    assert rig.ledger.is_simulation_ledger
    cash = rig.ledger.simulated_cash_view()
    assert (cash.seed, cash.reserved, cash.net_cash_out) == (SEED, 0, 0)
    assert rig.ledger.get_paper_budget("sim-run1", "INR", workspace="india") is None
    order: list[str] = []
    validate = ExecutionLedger._validate_simulation_ledger_locked
    recover = ExecutionLedger.recover_abandoned_submissions
    configure = ExecutionLedger._configure
    monkeypatch.setattr(
        ExecutionLedger, "_configure",
        lambda self, timeout: (order.append("configure"), configure(self, timeout))[1],
    )
    monkeypatch.setattr(
        ExecutionLedger, "_validate_simulation_ledger_locked",
        lambda self, connection: (order.append("validate"), validate(self, connection))[1],
    )
    monkeypatch.setattr(
        ExecutionLedger, "recover_abandoned_submissions",
        lambda self: (order.append("recover"), recover(self))[1],
    )
    again = reopen(rig)
    try:
        assert again.ledger.is_simulation_ledger
        assert again.ledger.simulated_cash_view().seed == SEED
        # Read-only validation, then the writable configuration, then the writer-side check.
        assert order == ["validate", "configure", "validate", "recover"]
    finally:
        again.close()


def _closed_simulation_file(tmp_path: Path) -> Path:
    rig = make_rig(tmp_path)
    path = rig.ledger.path
    rig.close()
    return path


SIM_TABLES = ("simulation_account", "simulated_fills", "simulated_cash", "simulated_cash_events")
SIM_TRIGGER_NAMES = (
    "simulation_account_no_update", "simulation_account_no_delete",
    "simulation_account_workspace_insert_guard", "simulated_fills_no_update",
    "simulated_fills_no_delete", "simulated_fills_workspace_insert_guard",
    "simulated_cash_no_delete", "simulated_cash_workspace_insert_guard",
    "simulated_cash_identity_immutable", "simulated_cash_events_no_update",
    "simulated_cash_events_no_delete", "simulated_cash_events_workspace_insert_guard",
    "simulation_account_no_replace", "simulated_fills_no_replace", "simulated_cash_no_replace",
    "simulated_cash_events_no_replace",
)


def _tamper_missing_table(path: Path) -> None:
    raw(path, "DROP TABLE simulated_fills", drop=["simulated_fills_no_update", "simulated_fills_no_delete"])


def _tamper_missing_trigger(path: Path) -> None:
    raw(path, drop=["simulated_fills_no_delete"])


def _tamper_missing_no_replace_trigger(path: Path) -> None:
    raw(path, drop=["simulated_cash_events_no_replace"])


def _tamper_missing_seed(path: Path) -> None:
    raw(path, "DELETE FROM simulated_cash", drop=["simulated_cash_no_delete"])


def _tamper_marker_workspace(path: Path) -> None:
    raw(path, "UPDATE simulation_account SET workspace = 'uk'", drop=["simulation_account_no_update"])


def _tamper_marker_binding(path: Path) -> None:
    raw(path, "UPDATE simulation_account SET venue = 't212_practice'", drop=["simulation_account_no_update"])


def _tamper_marker_account(path: Path) -> None:
    raw(path, "UPDATE simulation_account SET account_id = 'sim-other'", drop=["simulation_account_no_update"])


def _tamper_seed_changed(path: Path) -> None:
    raw(path, "UPDATE simulated_cash SET seed = '99999'", drop=["simulated_cash_identity_immutable"])


def _tamper_no_marker_with_tables(path: Path) -> None:
    raw(path, "DROP TABLE simulation_account", drop=["simulation_account_no_update", "simulation_account_no_delete"])


def _tamper_all_simulation_objects_removed(path: Path) -> None:
    # An ordinary bound ledger whose account is still ``sim-run1``: the identity rule catches it.
    raw(
        path,
        *(f"DROP TABLE {table}" for table in ("simulated_cash_events", "simulated_fills", "simulated_cash", "simulation_account")),
        drop=SIM_TRIGGER_NAMES,
    )


TAMPERS = {
    "missing_table": _tamper_missing_table,
    "missing_trigger": _tamper_missing_trigger,
    "missing_no_replace_trigger": _tamper_missing_no_replace_trigger,
    "missing_seed_row": _tamper_missing_seed,
    "marker_workspace": _tamper_marker_workspace,
    "marker_binding": _tamper_marker_binding,
    "marker_account": _tamper_marker_account,
    "seed_differs_from_caller": _tamper_seed_changed,
    "tables_without_marker": _tamper_no_marker_with_tables,
    "sim_account_without_any_marker": _tamper_all_simulation_objects_removed,
}


@pytest.mark.parametrize("name", sorted(TAMPERS))
def test_every_partial_or_mismatched_simulation_file_aborts_before_recovery(tmp_path, monkeypatch, name):
    path = _closed_simulation_file(tmp_path)
    TAMPERS[name](path)
    before = file_sha(path)
    calls: list[str] = []
    monkeypatch.setattr(
        ExecutionLedger, "recover_abandoned_submissions", lambda self: calls.append("recover") or 0
    )
    init = SimulationInit(RUN_ID, Workspace.INDIA, binding("breeze_relay", "sim-run1", "INR"), SEED)
    with pytest.raises(Exception) as caught:
        ExecutionLedger(path, workspace="india", require_approval=False, venue=init.binding, simulation=init)
    assert isinstance(caught.value, (LedgerError, ledger_module.LedgerVenueMismatch, ApprovalConflict)), caught.value
    assert calls == []
    assert file_sha(path) == before


def test_a_marker_file_opened_without_a_simulation_init_aborts(tmp_path, monkeypatch):
    path = _closed_simulation_file(tmp_path)
    before = file_sha(path)
    calls: list[str] = []
    monkeypatch.setattr(
        ExecutionLedger, "recover_abandoned_submissions", lambda self: calls.append("recover") or 0
    )
    with pytest.raises(LedgerError):
        ExecutionLedger(
            path, workspace="india", require_approval=False,
            venue=binding("breeze_relay", "sim-run1", "INR"),
        )
    assert calls == [] and file_sha(path) == before


def test_a_simulation_init_on_an_existing_file_without_a_marker_aborts(tmp_path):
    root = tmp_path / "india-shadow" / RUN_ID
    root.mkdir(parents=True)
    ordinary = ExecutionLedger(
        root / "ledger.sqlite3", workspace="india", require_approval=False,
        venue=binding("breeze_relay", "1234-account", "INR"),
    )
    ordinary.close()
    before = file_sha(root / "ledger.sqlite3")
    init = SimulationInit(RUN_ID, Workspace.INDIA, binding("breeze_relay", "sim-run1", "INR"), SEED)
    with pytest.raises((LedgerError, ledger_module.LedgerVenueMismatch)):
        ExecutionLedger(
            root / "ledger.sqlite3", workspace="india", require_approval=False,
            venue=init.binding, simulation=init,
        )
    assert file_sha(root / "ledger.sqlite3") == before


def _journal_mode(path: Path) -> str:
    probe = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return str(probe.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        probe.close()


@pytest.mark.parametrize(
    "dropped", ["simulated_fills_no_delete", "simulated_fills_no_replace", "simulation_account_no_replace"]
)
def test_a_delete_journal_file_missing_a_trigger_is_refused_byte_for_byte(tmp_path, dropped):
    path = _closed_simulation_file(tmp_path)
    raw(path, "PRAGMA journal_mode = DELETE", drop=[dropped])
    assert _journal_mode(path) == "delete"
    before = file_sha(path)
    init = SimulationInit(RUN_ID, Workspace.INDIA, binding("breeze_relay", "sim-run1", "INR"), SEED)
    with pytest.raises(LedgerError, match="missing a trigger"):
        ExecutionLedger(path, workspace="india", require_approval=False, venue=init.binding, simulation=init)
    # The refusal came from the read-only pass: no WAL switch, no header rewrite, no side files.
    assert file_sha(path) == before
    assert _journal_mode(path) == "delete"
    assert sorted(p.name for p in path.parent.glob("ledger.sqlite3-*")) == []


def test_an_intact_delete_journal_file_still_opens_and_then_runs_in_wal(tmp_path):
    path = _closed_simulation_file(tmp_path)
    raw(path, "PRAGMA journal_mode = DELETE")
    init = SimulationInit(RUN_ID, Workspace.INDIA, binding("breeze_relay", "sim-run1", "INR"), SEED)
    opened = ExecutionLedger(path, workspace="india", require_approval=False, venue=init.binding, simulation=init)
    try:
        assert opened.is_simulation_ledger
        assert _journal_mode(path) == "wal"
    finally:
        opened.close()


def _replacement_statements(path: Path, table: str, key: str) -> list[str]:
    """Four ways to put a row over an existing one, built from the table's own first row."""

    connection = sqlite3.connect(path)
    try:
        columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
    finally:
        connection.close()
    names = ", ".join(columns)
    statements = [
        f"INSERT OR REPLACE INTO {table} ({names}) SELECT {names} FROM {table}",
        f"REPLACE INTO {table} ({names}) SELECT {names} FROM {table}",
        f"INSERT INTO {table} ({names}) SELECT {names} FROM {table} WHERE true "
        f"ON CONFLICT DO UPDATE SET {columns[1]} = excluded.{columns[1]}",
    ]
    if key != "singleton":
        other = ", ".join("'other-key'" if column == key else column for column in columns)
        # A new key on an existing rowid still deletes the old row under REPLACE.
        statements.append(
            f"INSERT OR REPLACE INTO {table} (rowid, {names}) SELECT rowid, {other} FROM {table}"
        )
    return statements


@pytest.mark.parametrize(
    "table,key",
    [
        ("simulation_account", "singleton"),
        ("simulated_fills", "booking_id"),
        ("simulated_cash", "singleton"),
        ("simulated_cash_events", "event_id"),
    ],
)
def test_a_replacing_insert_is_refused_on_every_append_only_simulation_table(tmp_path, table, key):
    rig = make_rig(tmp_path)
    try:
        buy_and_fill(rig, "r1", 4)
    finally:
        rig.close()
    path = rig.ledger.path
    # A plain connection with default pragmas: recursive_triggers is off, as it is for any tamper tool.
    plain = sqlite3.connect(path, isolation_level=None)
    try:
        assert plain.execute("PRAGMA recursive_triggers").fetchone()[0] == 0
        before = plain.execute(f"SELECT rowid, * FROM {table} ORDER BY rowid").fetchall()
        assert before
        for statement in _replacement_statements(path, table, key):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                plain.execute(statement)
            assert plain.execute(f"SELECT rowid, * FROM {table} ORDER BY rowid").fetchall() == before
    finally:
        plain.close()


def test_a_replaced_fill_row_cannot_hide_open_exposure_from_the_capital_cap(tmp_path):
    rig = make_rig(tmp_path, capital_cap="1200.00", per_position_cap="1200.00")
    try:
        prepare_buy(rig, "p", quantity=10)
        book(rig, "p", 4, "100.00", "0")
    finally:
        rig.close()
    path = rig.ledger.path
    # Rewrite the booked fill as if all 10 had filled: the 6 still open would leave the exposure.
    plain = sqlite3.connect(path, isolation_level=None)
    try:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            plain.execute(
                "INSERT OR REPLACE INTO simulated_fills SELECT booking_id, proposal_id, workspace, side, "
                "ticker, '10', '10', price, gross, charges, net_cash_out, realised_pnl, schedule_sha256, "
                "scenario_set_sha256, attempt_id, payload_sha256, created_at FROM simulated_fills"
            )
    finally:
        plain.close()
    again = reopen(rig)
    try:
        # 400 held + 600 open + 800 new is 1800 against 1200: denied, as it was before the attempt.
        denied = prepare_buy(again, "q", quantity=8)
        assert denied.decision is AdmissionDecision.DENIED and denied.reason_code == "capital_cap"
        assert again.ledger.get_reservation("q") is None
    finally:
        again.close()


def _paper_ledger(tmp_path: Path) -> Path:
    paper = ExecutionLedger(tmp_path / "paper.sqlite3", workspace="india", require_approval=True)
    paper.configure_paper_budget("paper", "INR", "1000", workspace="india")
    paper.close()
    return tmp_path / "paper.sqlite3"


def _practice_ledger(tmp_path: Path) -> tuple[Path, VenueBinding]:
    practice_binding = binding("t212_practice", "demo-1", "GBP")
    practice = ExecutionLedger(
        tmp_path / "practice.sqlite3", workspace="uk", require_approval=True, venue=practice_binding
    )
    practice.close()
    return tmp_path / "practice.sqlite3", practice_binding


def test_paper_and_practice_ledgers_reopen_unchanged_and_never_gain_the_tables(tmp_path):
    paper_path = _paper_ledger(tmp_path)
    practice_path, practice_binding = _practice_ledger(tmp_path)
    for path, kwargs in (
        (paper_path, {"workspace": "india"}),
        (practice_path, {"workspace": "uk", "venue": practice_binding}),
    ):
        before = file_sha(path)
        reopened = ExecutionLedger(path, require_approval=True, **kwargs)
        try:
            assert reopened.is_simulation_ledger is False
        finally:
            reopened.close()
        assert file_sha(path) == before
        connection = sqlite3.connect(path)
        try:
            names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master")}
        finally:
            connection.close()
        assert not names & set(SIM_TABLES)


def _plant_marker_only(path: Path) -> None:
    raw(
        path,
        "CREATE TABLE simulation_account (singleton INTEGER PRIMARY KEY, run_id TEXT, workspace TEXT, "
        "venue TEXT, account_id TEXT, currency TEXT, created_at TEXT)",
        "INSERT INTO simulation_account VALUES (1, 'run1', 'india', 'breeze_relay', 'sim-run1', 'INR', 'x')",
    )


def _plant_full_set(path: Path) -> None:
    """Copy every simulation table and trigger from a real simulation file into ``path`` by hand."""

    donor_dir = path.parent / "donor"
    donor_dir.mkdir()
    donor = make_rig(donor_dir)
    donor_path = donor.ledger.path
    donor.close()
    source = sqlite3.connect(donor_path)
    try:
        ddl = [
            row[0]
            for row in source.execute(
                "SELECT sql FROM sqlite_master WHERE name LIKE 'simulat%' AND sql IS NOT NULL "
                "ORDER BY CASE type WHEN 'table' THEN 0 ELSE 1 END"
            )
        ]
    finally:
        source.close()
    target = sqlite3.connect(path)
    try:
        for statement in ddl:
            target.execute(statement)
        # An attacker plants rows by hand, so the insert guards are no obstacle to them.
        for guard in ("simulation_account", "simulated_cash"):
            target.execute(f"DROP TRIGGER {guard}_workspace_insert_guard")
        target.execute(
            "INSERT INTO simulation_account VALUES (1, 'run1', 'india', 'breeze_relay', 'sim-run1', 'INR', 'x')"
        )
        target.execute(
            "INSERT INTO simulated_cash VALUES (1, 'india', 'sim-run1', 'INR', '50000', '0', '0', 'x')"
        )
        target.commit()
    finally:
        target.close()


@pytest.mark.parametrize("plant", [_plant_marker_only, _plant_full_set])
@pytest.mark.parametrize("kind", ["paper", "practice"])
def test_a_planted_marker_in_a_paper_or_practice_ledger_never_reaches_the_simulation_branch(
    tmp_path, monkeypatch, plant, kind
):
    if kind == "paper":
        path, kwargs = _paper_ledger(tmp_path), {"workspace": "india"}
    else:
        path, practice_binding = _practice_ledger(tmp_path)
        kwargs = {"workspace": "uk", "venue": practice_binding}
    plant(path)
    before = file_sha(path)
    reached: list[str] = []
    monkeypatch.setattr(
        ExecutionLedger, "reserve_simulated_buy", lambda *a, **k: reached.append("buy")
    )
    monkeypatch.setattr(
        ExecutionLedger, "recover_abandoned_submissions", lambda self: reached.append("recover") or 0
    )
    with pytest.raises(Exception) as caught:
        ExecutionLedger(path, require_approval=True, **kwargs)
    assert isinstance(caught.value, (LedgerError, ledger_module.LedgerVenueMismatch, ApprovalConflict)), caught.value
    # And with a SimulationInit: the file's own identity (paper or practice) can never match it.
    init = SimulationInit(RUN_ID, Workspace.INDIA, binding("breeze_relay", "sim-run1", "INR"), SEED)
    sim_dir = tmp_path / "india-shadow" / RUN_ID
    sim_dir.mkdir(parents=True)
    target = sim_dir / "ledger.sqlite3"
    target.write_bytes(path.read_bytes())
    with pytest.raises(Exception) as caught:
        ExecutionLedger(target, workspace="india", require_approval=False, venue=init.binding, simulation=init)
    assert isinstance(caught.value, (LedgerError, ledger_module.LedgerVenueMismatch, ApprovalConflict)), caught.value
    assert reached == []
    assert file_sha(path) == before


def test_a_fault_during_simulation_initialization_leaves_no_half_pinned_file(tmp_path, monkeypatch):
    # The marker, tables and seed share the transaction that pins the file, so there is no gap.
    root = tmp_path / "sim"
    from shadow_india.admission import open_simulation_ledger

    def boom(self, connection, now):
        raise sqlite3.OperationalError("planted-fault")

    monkeypatch.setattr(ExecutionLedger, "_initialize_simulation_locked", boom)
    with pytest.raises(Exception):
        open_simulation_ledger(root, RUN_ID, SEED)
    monkeypatch.undo()
    # The same call now succeeds on that very file: nothing was pinned without its tables.
    ledger = open_simulation_ledger(root, RUN_ID, SEED)
    try:
        assert ledger.is_simulation_ledger and ledger.simulated_cash_view().seed == SEED
    finally:
        ledger.close()


# --- SL-1 isolation ------------------------------------------------------------------------


def test_a_simulation_ledger_lives_only_under_its_run_directory(tmp_path):
    init = SimulationInit(RUN_ID, Workspace.INDIA, binding("breeze_relay", "sim-run1", "INR"), SEED)
    for wrong in (tmp_path / "ledger.sqlite3", tmp_path / "india-shadow" / "other" / "ledger.sqlite3",
                  tmp_path / "elsewhere" / RUN_ID / "ledger.sqlite3"):
        wrong.parent.mkdir(parents=True, exist_ok=True)
        with pytest.raises(ledger_module.LedgerVenueMismatch):
            ExecutionLedger(wrong, workspace="india", require_approval=False, venue=init.binding, simulation=init)
        assert not wrong.exists()


def test_a_simulation_ledger_must_not_use_the_live_or_paper_path(tmp_path, monkeypatch):
    target = tmp_path / "india-shadow" / RUN_ID / "ledger.sqlite3"
    target.parent.mkdir(parents=True)
    monkeypatch.setattr(ledger_module, "default_ledger_path", lambda workspace: target)
    init = SimulationInit(RUN_ID, Workspace.INDIA, binding("breeze_relay", "sim-run1", "INR"), SEED)
    with pytest.raises(ledger_module.LedgerVenueMismatch):
        ExecutionLedger(target, workspace="india", require_approval=False, venue=init.binding, simulation=init)


@pytest.mark.parametrize(
    "kwargs, message",
    [
        ({"run_id": "bad/run"}, "run id"),
        ({"run_id": ""}, "run id"),
        ({"run_id": "x" * 57}, "run id"),
        ({"seed": Decimal("0")}, "seed"),
        ({"seed": Decimal("-1")}, "seed"),
        ({"seed": Decimal("NaN")}, "seed"),
        ({"seed": Decimal("Infinity")}, "seed"),
        ({"seed": 50000}, "seed"),
        ({"workspace": Workspace.UK}, "India"),
        ({"venue": "t212_practice"}, "breeze_relay"),
        ({"account": "1234"}, "sim-"),
    ],
)
def test_a_malformed_simulation_init_is_refused(kwargs, message):
    run_id = kwargs.get("run_id", RUN_ID)
    other_venue = kwargs.get("venue")
    venue_binding = binding(
        other_venue or "breeze_relay",
        kwargs.get("account", f"sim-{run_id}" if re.fullmatch(r"[A-Za-z0-9_-]{1,56}", run_id) else "sim-ok"),
        "GBP" if other_venue else "INR",
    )
    with pytest.raises(ValueError, match=message):
        SimulationInit(run_id, kwargs.get("workspace", Workspace.INDIA), venue_binding, kwargs.get("seed", SEED))


def test_a_non_sim_account_that_skips_the_init_validation_is_refused_by_the_ledger(tmp_path):
    # SimulationInit.__post_init__ refuses a non-sim- account; the ledger's own check must hold when
    # that validation is bypassed.
    init = SimulationInit(RUN_ID, Workspace.INDIA, binding("breeze_relay", "sim-run1", "INR"), SEED)
    object.__setattr__(init, "binding", binding("breeze_relay", "1234", "INR"))
    assert not init.binding.account_id.startswith("sim-")
    target = tmp_path / "india-shadow" / RUN_ID / "ledger.sqlite3"
    target.parent.mkdir(parents=True)
    with pytest.raises(LedgerError, match="marker does not match"):
        ExecutionLedger(target, workspace="india", require_approval=False, venue=init.binding, simulation=init)


def test_the_simulation_ledger_refuses_broker_paths(tmp_path):
    rig = make_rig(tmp_path)
    try:
        with pytest.raises(ApprovalConflict):
            rig.ledger.configure_paper_budget("sim-run1", "INR", "1000", workspace="india")
        with pytest.raises(ApprovalConflict):
            rig.ledger.configure_venue_limits("1000", "500", workspace="india")
        prepare_buy(rig, "iso-1")
        with pytest.raises(ApprovalConflict):
            rig.ledger.reserve_buying_power("iso-1")
        from execution.models import ReconciliationSnapshot, ReconciliationStatus

        with pytest.raises(ApprovalConflict):
            rig.ledger.reconcile(
                ReconciliationSnapshot(
                    proposal_id="iso-1", broker_order_id="b-1", source="test",
                    status=ReconciliationStatus.FILLED, cumulative_quantity=Decimal(2),
                    cumulative_notional=Decimal(200), evidence_fingerprint="f-1",
                    observed_at=datetime.now(timezone.utc),
                )
            )
        assert rig.ledger.get_paper_budget("sim-run1", "INR", workspace="india") is None
    finally:
        rig.close()


# --- shared helpers for the lifecycle rows --------------------------------------------------

ALL_TABLES = (
    "order_intents", "order_projection", "execution_admissions", "execution_events",
    "buying_power_reservations", "ledger_quantity_reservations", "paper_positions",
    "simulated_fills", "simulated_cash", "simulated_cash_events", "reconciliation_evidence",
)


def snapshot(ledger: ExecutionLedger, tables: Iterable[str] = ALL_TABLES) -> dict[str, list[tuple]]:
    connection = ledger._require_connection()  # noqa: SLF001 - test read of raw rows
    return {
        table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for table in tables
    }


def view_figures(ledger: ExecutionLedger):
    view = ledger.india_account_view()
    cash = ledger.simulated_cash_view()
    return (
        view.positions, tuple(view.open_buys), dict(view.open_sells),
        (cash.seed, cash.reserved, cash.net_cash_out, cash.available),
    )


def buy_and_fill(rig: Rig, proposal_id: str, quantity: int, price: str = "100.00", charges: str = "0"):
    admission = prepare_buy(rig, proposal_id, quantity=quantity)
    assert admission.decision is AdmissionDecision.ADMITTED, admission.reason_code
    return book(rig, proposal_id, quantity, price, charges)


def reservation_row(ledger: ExecutionLedger, proposal_id: str, table: str = "buying_power_reservations"):
    connection = ledger._require_connection()  # noqa: SLF001
    return connection.execute(f"SELECT * FROM {table} WHERE proposal_id = ?", (proposal_id,)).fetchone()


# --- SL-2 buy reservation -------------------------------------------------------------------


def test_three_buys_over_the_capital_cap_deny_the_third(tmp_path):
    rig = make_rig(tmp_path, capital_cap="700.00", per_position_cap="700.00", simulator=None)
    try:
        first = prepare_buy(rig, "m1", quantity=3)
        second = prepare_buy(rig, "m2", quantity=3)
        third = prepare_buy(rig, "m3", quantity=3)
        assert first.decision is second.decision is AdmissionDecision.ADMITTED
        assert third.decision is AdmissionDecision.DENIED and third.reason_code == "capital_cap"
        assert rig.ledger.get_reservation("m3") is None
        assert rig.ledger.simulated_cash_view().reserved == Decimal("600.000")
    finally:
        rig.close()


def _admit_only(rig: Rig, proposal_id: str, quantity: int, limit: str = "100.00"):
    from test_shadow_india_admission import _direct_kwargs

    intent = buy(rig, proposal_id, quantity=quantity, limit=limit)
    return rig.adapter.service.admit(intent, **_direct_kwargs(rig, intent))


def test_two_admissions_raced_before_either_reserves_lose_cleanly(tmp_path):
    from test_shadow_india_admission import FixedFill

    rig = make_rig(tmp_path, capital_cap="700.00", per_position_cap="700.00", simulator=FixedFill("100.00"))
    try:
        assert _admit_only(rig, "race-a", 4).decision is AdmissionDecision.ADMITTED
        assert _admit_only(rig, "race-b", 4).decision is AdmissionDecision.ADMITTED
        service = rig.adapter.service
        service.reserve("race-a")
        with pytest.raises(IndiaCapExceeded):
            service.reserve("race-b")
        assert rig.ledger.get_order("race-b").state == "EXPIRED"  # never left PENDING
        assert rig.ledger.get_reservation("race-b") is None
        assert rig.ledger.simulated_cash_view().reserved == Decimal("400.000")
    finally:
        rig.close()


def test_a_buy_larger_than_the_simulated_cash_is_refused_without_a_trace(tmp_path):
    rig = make_rig(tmp_path, seed=Decimal("150"))
    try:
        from test_shadow_india_admission import FixedFill  # noqa: F401

        _admit_only(rig, "poor", 2)
        before = snapshot(rig.ledger)
        with pytest.raises(SimulatedCashInsufficient) as caught:
            rig.adapter.service.reserve("poor")
        assert caught.value.code == "SIM_CASH_INSUFFICIENT"
        assert snapshot(rig.ledger) == before
    finally:
        rig.close()


# --- SL-3 sell reservation ------------------------------------------------------------------


def test_a_sell_reserves_simulated_quantity_minus_other_open_sells(tmp_path):
    rig = make_rig(tmp_path)
    try:
        buy_and_fill(rig, "own", 10)
        first = sell(rig, "s1", quantity=6)
        assert first.decision is AdmissionDecision.ADMITTED
        assert reservation_row(rig.ledger, "s1", "ledger_quantity_reservations")["reserved"] == "6.0"
        second = sell(rig, "s2", quantity=5)  # 10 held - 6 already open = 4 left
        assert second.decision is AdmissionDecision.DENIED and second.reason_code == "sell_exceeds_holding"
        assert rig.ledger.get_simulated_reservation("s2") is None
    finally:
        rig.close()


def test_every_simulated_method_is_refused_on_a_paper_ledger(tmp_path):
    paper = ExecutionLedger(tmp_path / "paper.sqlite3", workspace="india", require_approval=True)
    try:
        calls = (
            lambda: paper.reserve_simulated_sell("x"),
            lambda: paper.reserve_simulated_buy("x", india_caps=(Decimal(1), Decimal(1))),
            lambda: paper.expire_simulated_order("x", "e", "reason"),
            lambda: paper.simulated_cash_view(),
            lambda: paper.simulated_bookings(),
            lambda: paper.book_simulated_fill(
                sha("id"), "x", 1, "1", "0", SCHEDULE, sha("s"), "a"
            ),
        )
        for call in calls:
            with pytest.raises(ApprovalConflict):
                call()
    finally:
        paper.close()


# --- SL-4 booking ----------------------------------------------------------------------------


def test_a_full_buy_booking_updates_every_store_in_one_step(tmp_path):
    rig = make_rig(tmp_path)
    try:
        booking = buy_and_fill(rig, "b10", 10, price="99.00", charges="5")
        assert (booking.delta_quantity, booking.cumulative_quantity) == (Decimal(10), Decimal(10))
        assert booking.gross == Decimal("990.00") and booking.net_cash_out == Decimal("995.00")
        assert booking.realised_pnl == 0
        assert rig.ledger.get_order("b10").state == "FILLED"
        reservation = rig.ledger.get_reservation("b10")
        assert reservation.state == "SETTLED" and reservation.consumed == Decimal("1000.000")
        assert reservation.released == 0
        position = rig.ledger.get_paper_position("sim-run1", "INR", TICKER, workspace="india")
        assert (Decimal(position["quantity"]), Decimal(position["notional"])) == (Decimal(10), Decimal("995.00"))
        cash = rig.ledger.simulated_cash_view()
        assert (cash.reserved, cash.net_cash_out) == (0, Decimal("995.00"))
        # A booking is never a market fill: no broker evidence, no acknowledgement.
        assert rig.ledger.get_latest_reconciliation("b10") is None
        assert rig.ledger.known_broker_order_ids() == set()
        assert snapshot(rig.ledger, ["reconciliation_evidence"])["reconciliation_evidence"] == []
        assert rig.ledger.get_order("b10").acknowledgment is None
    finally:
        rig.close()


def test_a_full_sell_booking_books_proceeds_and_signed_realised_pnl(tmp_path):
    rig = make_rig(tmp_path)
    try:
        buy_and_fill(rig, "r-buy", 10, price="100.00", charges="5")
        assert sell(rig, "r-sell", quantity=10).decision is AdmissionDecision.ADMITTED
        booking = book(rig, "r-sell", 10, "120.00", "8")
        assert booking.gross == Decimal("1200.00")
        assert booking.net_cash_out == Decimal("-1192.00")
        assert booking.realised_pnl == Decimal("187.00")  # 1192 net proceeds - 1005 cost removed
        assert rig.ledger.get_order("r-sell").state == "FILLED"
        assert rig.ledger.get_simulated_reservation("r-sell")[0].state == "SETTLED"
        position = rig.ledger.get_paper_position("sim-run1", "INR", TICKER, workspace="india")
        assert Decimal(position["quantity"]) == 0 and Decimal(position["notional"]) == 0
        assert rig.ledger.simulated_cash_view().net_cash_out == Decimal("-187.00")
    finally:
        rig.close()


def test_a_booking_is_idempotent_and_a_changed_payload_is_a_conflict(tmp_path):
    rig = make_rig(tmp_path)
    try:
        prepare_buy(rig, "idem", quantity=10)
        scenario = sha("one-scenario")
        first = book(rig, "idem", 4, "100.00", "1", scenario=scenario)
        before = snapshot(rig.ledger)
        again = book(rig, "idem", 4, "100.00", "1", scenario=scenario)
        assert again == first
        assert snapshot(rig.ledger) == before
        assert len(rig.ledger.simulated_bookings("idem")) == 1
        with pytest.raises(IntentConflict):
            book(rig, "idem", 4, "99.00", "1", scenario=scenario)
        assert snapshot(rig.ledger) == before
    finally:
        rig.close()


def test_every_booking_refusal_changes_nothing(tmp_path):
    rig = make_rig(tmp_path, seed=Decimal("1000"))
    try:
        prepare_buy(rig, "ref", quantity=10)
        book(rig, "ref", 4, "100.00", "0", attempt="a-1")
        before = snapshot(rig.ledger)
        figures = view_figures(rig.ledger)
        cases = [
            (InvalidTransition, lambda: book(rig, "ref", 11, "100.00", attempt="a-2")),  # overfill, see below
            (InvalidTransition, lambda: book(rig, "ref", 4, "100.00", attempt="a-3")),  # no advance
            (InvalidTransition, lambda: book(rig, "ref", 5, "100.01", attempt="a-4")),  # worse than limit
            (SimulatedCashNegative, lambda: book(rig, "ref", 10, "100.00", "7", attempt="a-5")),
            (ValueError, lambda: book(rig, "ref", 5, "0", attempt="a-6")),
            (LedgerError, lambda: book(rig, "ref", "abc", "100.00", attempt="a-7")),
            (InvalidTransition, lambda: rig.ledger.book_simulated_fill(
                sha("not-the-derived-id"), "ref", 5, "100.00", "0", SCHEDULE, sha("s"), "a-8")),
        ]
        for error, call in cases:
            with pytest.raises(error):
                call()
            assert snapshot(rig.ledger) == before
            assert view_figures(rig.ledger) == figures
        with pytest.raises(OrderNotFoundType):
            book(rig, "missing", 1, "100.00", attempt="a-9")
    finally:
        rig.close()


OrderNotFoundType = ledger_module.OrderNotFound


def test_a_negative_cash_booking_carries_its_code(tmp_path):
    rig = make_rig(tmp_path, seed=Decimal("1000"))
    try:
        prepare_buy(rig, "neg", quantity=10)
        with pytest.raises(SimulatedCashNegative) as caught:
            book(rig, "neg", 10, "100.00", "7")
        assert caught.value.code == "SIM_CASH_NEGATIVE"
    finally:
        rig.close()


def test_a_terminal_order_accepts_no_booking(tmp_path):
    rig = make_rig(tmp_path)
    try:
        buy_and_fill(rig, "done", 2)
        prepare_buy(rig, "gone", quantity=2)
        rig.ledger.expire_simulated_order("gone", "exp-1", "day_close")
        before = snapshot(rig.ledger)
        for proposal_id in ("done", "gone"):
            with pytest.raises(InvalidTransition):
                book(rig, proposal_id, 2, "100.00", attempt="late")
        assert snapshot(rig.ledger) == before
    finally:
        rig.close()


def test_an_oversell_at_booking_is_refused(tmp_path):
    rig = make_rig(tmp_path)
    try:
        buy_and_fill(rig, "held", 5)
        assert sell(rig, "over", quantity=5).decision is AdmissionDecision.ADMITTED
        with rig.ledger._transaction() as connection:  # noqa: SLF001 - shrink the holding behind the reservation
            connection.execute("UPDATE paper_positions SET quantity = '2'")
        before = snapshot(rig.ledger)
        with pytest.raises(InvalidTransition):
            book(rig, "over", 5, "100.00", attempt="over-1")
        assert snapshot(rig.ledger) == before
    finally:
        rig.close()


# --- SL-5, SL-6, SL-7 and the account view ---------------------------------------------------


def test_a_partial_buy_keeps_the_remainder_reserved_and_open(tmp_path):
    rig = make_rig(tmp_path)
    try:
        prepare_buy(rig, "part", quantity=10)
        book(rig, "part", 4, "100.00", "2")
        assert rig.ledger.get_order("part").state == "PARTIALLY_FILLED"
        reservation = rig.ledger.get_reservation("part")
        assert reservation.state == "ACTIVE" and reservation.consumed == Decimal("400.000")
        positions, open_buys, open_sells, cash = view_figures(rig.ledger)
        assert positions == ((TICKER, Decimal(4), Decimal("402.00")),)
        assert open_buys == ((TICKER, Decimal(6), Decimal("100.00")),)
        assert open_sells == {}
        assert cash == (SEED, Decimal("600.000"), Decimal("402.00"), SEED - Decimal("402.00") - Decimal("600.000"))
    finally:
        rig.close()


def test_open_buy_remainder_counts_toward_the_capital_cap(tmp_path):
    rig = make_rig(tmp_path, capital_cap="1000.00", per_position_cap="1000.00")
    try:
        prepare_buy(rig, "p", quantity=10)
        book(rig, "p", 4, "100.00", "0")
        # 400 held + 600 still open + 100 for the new buy is over 1000, so the new buy is denied.
        denied = prepare_buy(rig, "q", quantity=1)
        assert denied.decision is AdmissionDecision.DENIED and denied.reason_code == "capital_cap"
    finally:
        rig.close()


def test_the_view_and_cash_after_each_step_survive_a_restart(tmp_path):
    rig = make_rig(tmp_path)
    steps: list[tuple[Any, Any]] = []
    try:
        prepare_buy(rig, "v-buy", quantity=10)
        steps.append(("reserved", view_figures(rig.ledger)))
        book(rig, "v-buy", 4, "100.00", "1", attempt="v1")
        steps.append(("partial", view_figures(rig.ledger)))
        book(rig, "v-buy", 10, "100.00", "2", attempt="v2")
        steps.append(("full", view_figures(rig.ledger)))
        positions, open_buys, _sells, _cash = steps[-1][1]
        assert positions == ((TICKER, Decimal(10), Decimal("1003.00")),) and open_buys == ()
        assert rig.ledger.get_reservation("v-buy").state == "SETTLED"
        sell(rig, "v-sell", quantity=10)
        steps.append(("sell reserved", view_figures(rig.ledger)))
        assert steps[-1][1][2] == {TICKER: Decimal(10)}
        book(rig, "v-sell", 4, "100.00", "1", attempt="v3")
        steps.append(("sell partial", view_figures(rig.ledger)))
        assert steps[-1][1][0][0][1] == Decimal(6) and steps[-1][1][2] == {TICKER: Decimal(6)}
        book(rig, "v-sell", 10, "100.00", "1", attempt="v4")
        steps.append(("sell full", view_figures(rig.ledger)))
        assert steps[-1][1][0] == () and steps[-1][1][2] == {}
        assert rig.ledger.get_order("v-sell").state == "FILLED"
        last = steps[-1][1]
        again = reopen(rig)
        try:
            assert view_figures(again.ledger) == last
        finally:
            again.close()
    finally:
        rig.close()


def test_partial_then_expiry_releases_the_remainder_and_only_the_remainder(tmp_path):
    rig = make_rig(tmp_path)
    try:
        prepare_buy(rig, "pe", quantity=10)
        book(rig, "pe", 4, "100.00", "0")
        order = rig.ledger.expire_simulated_order("pe", "close-1", "day_close")
        assert order.state == "EXPIRED"
        reservation = rig.ledger.get_reservation("pe")
        assert reservation.state == "SETTLED" and reservation.released == Decimal("600.000")
        positions, open_buys, _s, cash = view_figures(rig.ledger)
        assert positions == ((TICKER, Decimal(4), Decimal("400.00")),) and open_buys == ()
        assert cash[1] == 0 and cash[2] == Decimal("400.00")
    finally:
        rig.close()


def test_only_a_recorded_expiry_releases_a_day_remainder(tmp_path):
    rig = make_rig(tmp_path)
    try:
        prepare_buy(rig, "keep", quantity=10)
        book(rig, "keep", 4, "100.00", "0")
        before = snapshot(rig.ledger)
        again = reopen(rig)  # a restart, however long it took, releases nothing
        try:
            assert snapshot(again.ledger) == before
            assert again.ledger.get_order("keep").state == "PARTIALLY_FILLED"
            assert again.ledger.get_reservation("keep").state == "ACTIVE"
            first = again.ledger.expire_simulated_order("keep", "x-1", "day_close")
            events = len(again.ledger.list_events("keep"))
            assert again.ledger.expire_simulated_order("keep", "x-1", "day_close") == first
            assert len(again.ledger.list_events("keep")) == events
            with pytest.raises(IntentConflict):
                again.ledger.expire_simulated_order("keep", "x-2", "day_close")
        finally:
            again.close()
    finally:
        rig.close()


def test_an_expired_exit_releases_the_reservation_and_keeps_the_shares(tmp_path):
    rig = make_rig(tmp_path)
    try:
        buy_and_fill(rig, "exit-buy", 10)
        sell(rig, "exit", quantity=10)
        rig.ledger.expire_simulated_order("exit", "exp-exit", "inconclusive_no_fill_assumed")
        assert rig.ledger.get_order("exit").state == "EXPIRED"
        assert rig.ledger.get_simulated_reservation("exit")[0].state == "SETTLED"
        positions, _b, open_sells, _c = view_figures(rig.ledger)
        assert positions[0][1] == Decimal(10) and open_sells == {}
        # The next decide re-proposes from the held position and reserves afresh.
        again = sell(rig, "exit-2", quantity=10)
        assert again.decision is AdmissionDecision.ADMITTED
        assert rig.ledger.get_simulated_reservation("exit-2")[0].state == "ACTIVE"
    finally:
        rig.close()


def test_a_vm_refusal_is_evidence_and_never_releases_anything(tmp_path):
    rig = make_rig(tmp_path)
    try:
        prepare_buy(rig, "vm", quantity=2)
        before = snapshot(rig.ledger, ["buying_power_reservations", "simulated_cash", "order_projection"])
        rig.ledger.record_shadow_vm_decision("vm", 7, {"codes": ["kill_switch"]})
        assert snapshot(rig.ledger, ["buying_power_reservations", "simulated_cash", "order_projection"]) == before
        events = [e for e in rig.ledger.list_events("vm") if e.event_type == ledger_module.SHADOW_VM_DECISION_EVENT]
        assert len(events) == 1 and events[0].payload["audit_seq"] == 7
        rig.ledger.record_shadow_vm_decision("vm", 7, {"codes": ["kill_switch"]})  # same seq, same body
        assert len([e for e in rig.ledger.list_events("vm") if e.event_type == ledger_module.SHADOW_VM_DECISION_EVENT]) == 1
        with pytest.raises(IntentConflict):
            rig.ledger.record_shadow_vm_decision("vm", 7, {"codes": ["other"]})
        with pytest.raises(ValueError):
            rig.ledger.record_shadow_vm_decision("vm", -1, {"codes": []})
    finally:
        rig.close()


# --- simulation cash ---------------------------------------------------------------------------


def test_a_profitable_round_trip_survives_reopen_and_funds_the_next_reservation(tmp_path):
    paper = ExecutionLedger(tmp_path / "paper.sqlite3", workspace="india", require_approval=True)
    paper.configure_paper_budget("paper", "INR", "1000", workspace="india")
    paper_before = paper.get_paper_budget("paper", "INR", workspace="india")
    rig = make_rig(tmp_path, seed=Decimal("1100"))
    try:
        buy_and_fill(rig, "c-buy", 10, price="100.00", charges="5")
        sell(rig, "c-sell", quantity=10, limit="100.00")
        book(rig, "c-sell", 10, "120.00", "8")
        cash = rig.ledger.simulated_cash_view()
        assert cash.net_cash_out == Decimal("-187.00") and cash.net_cash_out < 0
        assert cash.available == Decimal("1287.00")
        fills = rig.ledger.simulated_bookings()
        assert sum((f.net_cash_out for f in fills), Decimal(0)) == cash.net_cash_out
        rig = reopen(rig, seed=Decimal("1100"))
        assert rig.ledger.simulated_cash_view() == cash
        # The 1100 seed alone could not fund 12 x 100; the proceeds do.
        assert prepare_buy(rig, "c-next", quantity=12).decision is AdmissionDecision.ADMITTED
        assert rig.ledger.simulated_cash_view().reserved == Decimal("1200.000")
    finally:
        rig.close()
        assert paper.get_paper_budget("paper", "INR", workspace="india") == paper_before
        paper.close()


def test_the_position_cost_basis_is_added_in_decimal_not_through_sqlite(tmp_path):
    rig = make_rig(tmp_path)
    try:
        prepare_buy(rig, "d1", quantity=3)
        book(rig, "d1", 3, "100.00", "700", attempt="d-1")  # cost 300 + 700 = 1000
        assert sell(rig, "d2", quantity=1).decision is AdmissionDecision.ADMITTED
        book(rig, "d2", 1, "100.00", "0", attempt="d-2")  # removes 1000/3
        prepare_buy(rig, "d3", quantity=1)
        book(rig, "d3", 1, "100.00", "400", attempt="d-3")  # cost 100 + 400 = 500
        expected = {"quantity": "3", "notional": "1166.666666666666666666666667"}
        assert rig.ledger.get_paper_position("sim-run1", "INR", TICKER, workspace="india") == expected
        rig = reopen(rig)
        assert rig.ledger.get_paper_position("sim-run1", "INR", TICKER, workspace="india") == expected
    finally:
        rig.close()


# --- signed and unsigned Decimal parsing --------------------------------------------------------


@pytest.mark.parametrize("bad", ["abc", "", None, "NaN", "sNaN", "Infinity", "-Infinity", "-NaN", 1.5])
def test_the_signed_parser_refuses_malformed_and_non_finite_values(bad):
    with pytest.raises(LedgerError):
        ledger_module._signed_decimal(bad)  # noqa: SLF001


@pytest.mark.parametrize("good, expected", [("-187.00", Decimal("-187.00")), ("0", Decimal(0)), ("12", Decimal(12))])
def test_the_signed_parser_accepts_finite_values_of_either_sign(good, expected):
    assert ledger_module._signed_decimal(good) == expected  # noqa: SLF001


def _drop_triggers(connection: sqlite3.Connection, table: str) -> None:
    for (name,) in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = ?", (table,)
    ).fetchall():
        connection.execute(f"DROP TRIGGER {name}")


@pytest.mark.parametrize(
    "table, column",
    [
        ("simulated_cash", "seed"),
        ("simulated_cash", "reserved"),
        ("simulated_fills", "delta_quantity"),
        ("simulated_fills", "cumulative_quantity"),
        ("simulated_fills", "price"),
        ("simulated_fills", "gross"),
        ("simulated_fills", "charges"),
    ],
)
def test_a_negative_in_an_unsigned_field_is_a_ledger_fault(tmp_path, table, column):
    rig = make_rig(tmp_path)
    try:
        buy_and_fill(rig, "neg", 2)
        with rig.ledger._transaction() as connection:  # noqa: SLF001
            _drop_triggers(connection, table)
            connection.execute(f"UPDATE {table} SET {column} = '-1'")
        with pytest.raises(LedgerError):
            rig.ledger.simulated_cash_view()
            rig.ledger.simulated_bookings()
    finally:
        rig.close()


def test_a_negative_in_a_signed_field_is_read_back_as_negative(tmp_path):
    rig = make_rig(tmp_path)
    try:
        buy_and_fill(rig, "pos", 2)
        sell(rig, "pos-sell", quantity=2)
        book(rig, "pos-sell", 2, "150.00", "0")
        fill = rig.ledger.simulated_bookings("pos-sell")[0]
        assert fill.net_cash_out < 0 and fill.realised_pnl > 0
        assert rig.ledger.simulated_cash_view().net_cash_out < 0
    finally:
        rig.close()


def test_signed_parsing_is_confined_to_cash_and_pnl_columns():
    source = Path(ledger_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    callers: dict[str, set[str]] = {"_signed_decimal": set(), "_signed_decimal_str": set()}
    for function in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
        for node in ast.walk(function):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in callers:
                callers[node.func.id].add(function.name)
    assert callers["_signed_decimal"] == {"_signed_decimal_str", "_simulated_cash_locked", "_booking_from_row"}
    assert callers["_signed_decimal_str"] == {"_apply_simulated_cash_locked", "book_simulated_fill"}


_SIM_FUNCTIONS = (
    "_simulated_cash_locked", "_apply_simulated_cash_locked", "_simulated_filled_quantity_locked",
    "_simulated_open_sells_locked", "_validate_simulation_ledger_locked", "reserve_simulated_buy",
    "reserve_simulated_sell", "book_simulated_fill", "expire_simulated_order", "simulated_cash_view",
    "simulated_bookings", "simulated_booking", "get_simulated_reservation", "record_shadow_vm_decision",
)


def _sql_strings(function: ast.AST) -> list[str]:
    found: list[str] = []
    for node in ast.walk(function):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.append(node.value)
    return found


def test_no_simulation_function_does_money_arithmetic_in_sql():
    tree = ast.parse(Path(ledger_module.__file__).read_text(encoding="utf-8"))
    functions = {
        n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    scanned = 0
    for name in _SIM_FUNCTIONS:
        function = functions.get(name)
        if function is None:
            continue
        scanned += 1
        for text in _sql_strings(function):
            if not re.search(r"\b(SELECT|UPDATE|INSERT)\b", text):
                continue
            assert not re.search(r"\b(SUM|TOTAL|AVG|CAST)\s*\(", text, re.IGNORECASE), (name, text)
            assert not re.search(r"=\s*\w+\s*[-+*/]\s*[\w?]", text), (name, text)
    assert scanned >= 10


# --- service branches ---------------------------------------------------------------------------


def _spies(monkeypatch) -> list[tuple[str, tuple, dict]]:
    calls: list[tuple[str, tuple, dict]] = []
    for name in ("reserve_sell_quantity", "reserve_buying_power", "reconcile", "reserve_simulated_buy"):
        original = getattr(ExecutionLedger, name)

        def wrapper(self, *args, __name=name, __original=original, **kwargs):
            calls.append((__name, args, kwargs))
            return __original(self, *args, **kwargs)

        monkeypatch.setattr(ExecutionLedger, name, wrapper)
    return calls


def test_a_simulated_sell_with_a_broker_quantity_is_a_conflict_and_touches_nothing(tmp_path, monkeypatch):
    rig = make_rig(tmp_path)
    try:
        buy_and_fill(rig, "own", 5)
        calls = _spies(monkeypatch)
        intent = rig.adapter.build_intent("bq", symbol=SYMBOL, side="SELL", quantity=2, limit_price="100.00")
        from test_shadow_india_admission import _direct_kwargs

        assert rig.adapter.service.admit(intent, **_direct_kwargs(rig, intent)).decision is AdmissionDecision.ADMITTED
        before = snapshot(rig.ledger)
        with pytest.raises(ExecutionConflictError):
            rig.adapter.service.reserve("bq", broker_available_quantity="99")
        assert snapshot(rig.ledger) == before and calls == []
    finally:
        rig.close()


def test_a_simulated_buy_with_no_guard_is_disabled_and_reserves_nothing(tmp_path):
    rig = make_rig(tmp_path)
    try:
        from test_shadow_india_admission import _direct_kwargs

        intent = buy(rig, "ng", quantity=2)
        assert rig.adapter.service.admit(intent, **_direct_kwargs(rig, intent)).decision is AdmissionDecision.ADMITTED
        before = snapshot(rig.ledger)
        rig.adapter.service._india_guard = None  # noqa: SLF001 - the unguarded wiring
        with pytest.raises(ExecutionDisabledError):
            rig.adapter.service.reserve("ng")
        assert snapshot(rig.ledger) == before
        assert rig.ledger.get_reservation("ng") is None
    finally:
        rig.close()


def test_simulated_reserve_never_reaches_a_broker_figure(tmp_path, monkeypatch):
    rig = make_rig(tmp_path)
    try:
        calls = _spies(monkeypatch)
        prepare_buy(rig, "clean", quantity=2)
        names = [name for name, *_ in calls]
        assert names == ["reserve_simulated_buy"]
        assert calls[0][2] == {"india_caps": (rig.guard.limits.capital_cap, rig.guard.limits.per_position_cap)}
    finally:
        rig.close()


def _paper_india(tmp_path, seed: bool = True):
    from india_limits_support import make_guard, make_service, india_private_dir, open_ledger, seed_position

    private = india_private_dir(tmp_path)
    ledger = open_ledger(tmp_path, name="svc.sqlite3")
    guard = make_guard(ledger, private)
    if seed:
        seed_position(ledger, TICKER, 5, "500.00", guard=guard)
    return ledger, guard, make_service(ledger, guard)


def test_unbound_india_paper_reserve_branches_are_unchanged(tmp_path, monkeypatch):
    from india_limits_support import admit, make_intent

    ledger, guard, service = _paper_india(tmp_path, seed=False)
    try:
        calls = _spies(monkeypatch)
        assert admit(service, make_intent("pb", quantity=2)).decision is AdmissionDecision.ADMITTED
        service.reserve("pb")
        from india_limits_support import seed_position

        seed_position(ledger, TICKER, 5, "300.00", guard=guard)
        assert [name for name, *_ in calls] == ["reserve_buying_power"]
        assert calls[0][2] == {
            "india_caps": (guard.limits.capital_cap, guard.limits.per_position_cap)
        }
        calls.clear()
        assert admit(service, make_intent("ps", side="SELL", quantity=2)).decision is AdmissionDecision.ADMITTED
        assert service.reserve("ps") is None
        assert calls == []
        assert ledger.get_reservation("ps") is None
        assert ledger.is_simulation_ledger is False
    finally:
        ledger.close()


def test_bound_non_simulation_sell_needs_the_broker_quantity_and_buy_uses_no_caps(tmp_path, monkeypatch):
    path, practice = _practice_ledger(tmp_path)
    ledger = ExecutionLedger(path, workspace="uk", require_approval=True, venue=practice)
    try:
        service = ExecutionService(None, ledger, require_approval=False)
        from types import SimpleNamespace
        from execution.models import OrderSide

        calls = _spies(monkeypatch)
        monkeypatch.setattr(
            ledger, "get_admission", lambda proposal_id: SimpleNamespace(side=OrderSide.SELL)
        )
        with pytest.raises(ExecutionConflictError):
            service.reserve("x")
        assert calls == []
        monkeypatch.setattr(ledger, "reserve_sell_quantity", lambda *a, **k: calls.append(("sell", a, k)) or "R")
        assert service.reserve("x", broker_available_quantity="7") == "R"
        assert calls == [("sell", ("x",), {"broker_available_quantity": "7"})]
        calls.clear()
        monkeypatch.setattr(ledger, "get_admission", lambda proposal_id: SimpleNamespace(side=OrderSide.BUY))
        monkeypatch.setattr(ledger, "reserve_buying_power", lambda *a, **k: calls.append(("buy", a, k)) or "B")
        assert service.reserve("y") == "B"
        assert calls == [("buy", ("y",), {"india_caps": None})]
    finally:
        ledger.close()


# --- rollback: every write set, every table -----------------------------------------------------


def write_set(ledger: ExecutionLedger, operation) -> list[tuple[str, str]]:
    statements: list[str] = []
    connection = ledger._require_connection()  # noqa: SLF001
    connection.set_trace_callback(statements.append)
    try:
        operation()
    finally:
        connection.set_trace_callback(None)
    found: list[tuple[str, str]] = []
    for statement in statements:
        match = re.match(r"\s*(INSERT(?: OR \w+)? INTO|UPDATE|DELETE FROM)\s+(\w+)", statement)
        if match:
            verb = "INSERT" if match.group(1).startswith("INSERT") else match.group(1).split()[0]
            pair = (verb, match.group(2))
            if pair not in found:
                found.append(pair)
    return found


def _fresh(tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
    return make_rig(tmp_path)


def _scenario_book_buy(tmp_path):
    rig = _fresh(tmp_path)
    prepare_buy(rig, "op", quantity=10)
    return rig, lambda: book(rig, "op", 10, "100.00", "1", attempt="r1"), [
        ("UPDATE", "buying_power_reservations"), ("UPDATE", "paper_positions"), ("INSERT", "paper_positions"),
        ("UPDATE", "simulated_cash"), ("INSERT", "simulated_cash_events"), ("INSERT", "simulated_fills"),
        ("UPDATE", "order_projection"), ("INSERT", "execution_events"),
    ]


def _scenario_book_sell(tmp_path):
    rig = _fresh(tmp_path)
    buy_and_fill(rig, "held", 10)
    sell(rig, "op", quantity=10)
    return rig, lambda: book(rig, "op", 10, "110.00", "1", attempt="r2"), [
        ("UPDATE", "ledger_quantity_reservations"), ("UPDATE", "paper_positions"),
        ("UPDATE", "simulated_cash"), ("INSERT", "simulated_cash_events"), ("INSERT", "simulated_fills"),
        ("UPDATE", "order_projection"), ("INSERT", "execution_events"),
    ]


def _scenario_expire_buy(tmp_path):
    rig = _fresh(tmp_path)
    prepare_buy(rig, "op", quantity=3)
    return rig, lambda: rig.ledger.expire_simulated_order("op", "ex-1", "day_close"), [
        ("UPDATE", "buying_power_reservations"), ("UPDATE", "simulated_cash"),
        ("INSERT", "simulated_cash_events"), ("UPDATE", "order_projection"), ("INSERT", "execution_events"),
    ]


def _scenario_expire_sell(tmp_path):
    rig = _fresh(tmp_path)
    buy_and_fill(rig, "held", 3)
    sell(rig, "op", quantity=3)
    return rig, lambda: rig.ledger.expire_simulated_order("op", "ex-2", "day_close"), [
        ("UPDATE", "ledger_quantity_reservations"), ("UPDATE", "order_projection"),
        ("INSERT", "execution_events"),
    ]


_SCENARIOS = {
    "book_buy": _scenario_book_buy,
    "book_sell": _scenario_book_sell,
    "expire_buy": _scenario_expire_buy,
    "expire_sell": _scenario_expire_sell,
}


def _done(rig: Rig, kind: str) -> int:
    if kind.startswith("book"):
        return len(rig.ledger.simulated_bookings("op"))
    return len([e for e in rig.ledger.list_events("op") if e.event_type == ledger_module.SIMULATED_EXPIRY_EVENT])


@pytest.mark.parametrize("kind", sorted(_SCENARIOS))
def test_every_write_of_a_simulation_operation_rolls_back_whole(tmp_path, kind):
    probe_rig, probe_op, listed = _SCENARIOS[kind](tmp_path / "probe")
    try:
        derived = write_set(probe_rig.ledger, probe_op)
    finally:
        probe_rig.close()
    # The derived set is the explicit list; a new write the list does not name fails here.
    assert {pair for pair in derived} <= set(listed)
    assert {table for _verb, table in derived} == {table for _verb, table in listed}
    planted = 0
    for verb, table in derived:
        case = tmp_path / f"{verb}-{table}"
        rig, operation, _ = _SCENARIOS[kind](case)
        try:
            before = snapshot(rig.ledger)
            figures = view_figures(rig.ledger)
            before_done = _done(rig, kind)
            trigger = f"plant_{verb.lower()}_{table}"
            with rig.ledger._transaction() as connection:  # noqa: SLF001
                connection.execute(
                    f"CREATE TRIGGER {trigger} BEFORE {verb} ON {table} BEGIN SELECT RAISE(ABORT, 'planted'); END"
                )
            planted += 1
            with pytest.raises((sqlite3.Error, LedgerError)):
                operation()
            assert snapshot(rig.ledger) == before, (verb, table)
            assert view_figures(rig.ledger) == figures, (verb, table)
            assert _done(rig, kind) == before_done
            with rig.ledger._transaction() as connection:  # noqa: SLF001
                connection.execute(f"DROP TRIGGER {trigger}")
            operation()
            operation()  # a replay of the same operation is a no-op
            assert _done(rig, kind) == before_done + 1, (verb, table)
        finally:
            rig.close()
    assert planted == len(derived) >= 3


# --- the account view refuses broker evidence --------------------------------------------------


def test_planted_reconciliation_evidence_makes_the_next_view_raise(tmp_path):
    rig = make_rig(tmp_path)
    try:
        buy_and_fill(rig, "ev", 2)
        columns = [
            row["name"]
            for row in rig.ledger._require_connection().execute("PRAGMA table_info(reconciliation_evidence)")  # noqa: SLF001
        ]
        assert "proposal_id" in columns
        with rig.ledger._transaction() as connection:  # noqa: SLF001
            _drop_triggers(connection, "reconciliation_evidence")
            connection.execute(
                "INSERT INTO reconciliation_evidence (proposal_id, broker_order_id, source, "
                "cumulative_quantity, cumulative_notional, status, evidence_fingerprint, observed_at, "
                "created_at) VALUES ('ev', 'b-1', 'planted', '1', '1', 'FILLED', 'fp', 't', 't')"
            )
        with pytest.raises(LedgerError):
            rig.ledger.india_account_view()
    finally:
        rig.close()


# --- ShadowPreparation: durable preparation, recovery that never mints, booking across stores ----

class Crash(Exception):
    """A planted process death at one evidence append."""


class MemEvidence:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.crash_before: str | None = None
        self.crash_after: str | None = None

    def append(self, kind: str, payload: Any) -> dict[str, Any]:
        if self.crash_before == kind:
            self.crash_before = None
            raise Crash(kind)
        record = {"kind": kind, **payload}
        self.rows.append(record)
        if self.crash_after == kind:
            self.crash_after = None
            raise Crash(kind)
        return record

    def records(self) -> list[dict[str, Any]]:
        return list(self.rows)

    def kinds(self, intent_id: str | None = None) -> list[str]:
        return [r["kind"] for r in self.rows if intent_id is None or r.get("intent_id") == intent_id]


class ScriptedMint:
    def __init__(self) -> None:
        self.calls = 0
        self.mode = "decide"
        self.seq = 5
        self.entered = asyncio.Event() if False else None
        self.gate: asyncio.Event | None = None
        self.codes = ("kill_switch",)

    async def mint(self, request):
        self.calls += 1
        if self.gate is not None:
            self.entered.set()
            await self.gate.wait()
        if self.mode == "unresolved":
            raise MintUnresolved("no typed answer")
        if self.mode == "timeout":
            raise asyncio.TimeoutError()
        return VmDecision(request.intent_id, request.intent_sha256, tuple(self.codes), self.seq, sha("e-hash"))


class ListAudit:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []
        self.verified = True
        self.pages: list[int] = []

    def page(self, from_seq: int) -> AuditPage:
        self.pages.append(from_seq)
        return AuditPage(tuple(self.entries), self.verified)


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def make_prep(tmp_path: Path, **rig_kwargs: Any) -> SimpleNamespace:
    tmp_path.mkdir(parents=True, exist_ok=True)
    rig = make_rig(tmp_path, **rig_kwargs)
    evidence, mint, audit, clock = MemEvidence(), ScriptedMint(), ListAudit(), Clock()
    prep = ShadowPreparation(
        run_id=RUN_ID, adapter=rig.adapter, evidence=evidence, mint=mint, audit=audit, clock=clock
    )
    return SimpleNamespace(rig=rig, prep=prep, ev=evidence, mint=mint, audit=audit, clock=clock)


def go(
    h: SimpleNamespace, *, signal: str = "sig-1", session: str = "s-1", quantity: int = 2, leg: str = "entry",
    count: int = 3,
):
    return run(
        h.prep.prepare(
            session_id=session, signal_id=signal, leg=leg, symbol=SYMBOL, side="BUY",
            quantity=quantity, limit_price="100.00", samples=samples(NOW, count),
        )
    )


def audit_entry(h: SimpleNamespace, pid: str, seq: int = 5, codes=("kill_switch",), digest: str | None = None):
    intent = next(r for r in h.ev.rows if r["kind"] == "intent" and r["intent_id"] == pid)
    return AuditEntry(seq, pid, digest or intent["intent_sha256"], tuple(codes), sha(f"hash-{seq}"))


def pid_of(h: SimpleNamespace, signal: str = "sig-1", session: str = "s-1", leg: str = "entry") -> str:
    return h.prep.proposal_id(session, signal, leg)


def test_the_live_path_orders_its_evidence_around_the_ledger_and_mints_once(tmp_path):
    h = make_prep(tmp_path)
    try:
        result = go(h)
        pid = pid_of(h)
        assert (result.status, result.proposal_id) == ("vm_decided", pid)
        assert h.ev.kinds(pid) == ["intent", "reservation", "mint_started", "vm_decision"]
        assert h.mint.calls == 1
        # A VM refusal is evidence. The reservation and the order are untouched.
        assert h.rig.ledger.get_order(pid).state == "PENDING"
        assert h.rig.ledger.get_reservation(pid).state == "ACTIVE"
        stamped = [e for e in h.rig.ledger.list_events(pid) if e.event_type == ledger_module.SHADOW_VM_DECISION_EVENT]
        assert len(stamped) == 1
        reservation = next(r for r in h.ev.rows if r["kind"] == "reservation")
        assert reservation["reservation_created_at"] == h.rig.ledger.get_simulated_reservation(pid)[1]
    finally:
        h.rig.close()


def test_a_denied_intent_never_reaches_the_mint(tmp_path):
    h = make_prep(tmp_path)
    try:
        result = go(h, count=2)  # too few quote readings for the regime window
        assert result.status == "denied"
        assert h.ev.kinds(result.proposal_id) == ["intent", "denied"]
        assert h.mint.calls == 0
        assert h.rig.ledger.get_reservation(result.proposal_id) is None
    finally:
        h.rig.close()


def test_a_cash_refusal_closes_the_order_and_never_reaches_the_mint(tmp_path):
    h = make_prep(tmp_path, seed=Decimal("150"))
    try:
        result = go(h, quantity=2)
        assert result.status == "reservation_refused"
        assert h.ev.kinds(result.proposal_id) == ["intent", "reservation_refused"]
        assert h.rig.ledger.get_order(result.proposal_id).state == "EXPIRED"
        assert h.mint.calls == 0
        assert h.prep.recover() == {result.proposal_id: "closed"}
    finally:
        h.rig.close()


def test_an_unexpected_vm_answer_aborts_before_anything_is_recorded(tmp_path):
    h = make_prep(tmp_path)
    try:
        h.mint.codes = ("kill_switch", "other")
        with pytest.raises(ShadowAbort) as caught:
            go(h)
        assert caught.value.code == "vm_codes_unexpected"
        assert "vm_decision" not in h.ev.kinds()
        assert not [e for e in h.rig.ledger.list_events(pid_of(h)) if e.event_type == ledger_module.SHADOW_VM_DECISION_EVENT]
    finally:
        h.rig.close()


def test_recovery_during_a_held_mint_defers_and_never_posts_again(tmp_path):
    h = make_prep(tmp_path)
    try:
        h.mint.gate = asyncio.Event()

        async def scenario():
            h.mint.entered = asyncio.Event()
            task = asyncio.ensure_future(
                h.prep.prepare(
                    session_id="s-1", signal_id="sig-1", leg="entry", symbol=SYMBOL, side="BUY",
                    quantity=2, limit_price="100.00", samples=samples(NOW),
                )
            )
            await h.mint.entered.wait()
            pid = pid_of(h)
            during = h.prep.recover()
            h.mint.gate.set()
            result = await task
            return pid, during, result

        pid, during, result = asyncio.run(asyncio.wait_for(scenario(), 20))
        assert during == {pid: "deferred"} and result.status == "vm_decided"
        assert h.mint.calls == 1 and h.audit.pages == []
        assert h.prep.recover() == {pid: "vm_decided"}
        assert h.mint.calls == 1
    finally:
        h.rig.close()


def test_a_crash_before_the_reservation_record_resumes_as_interrupted_before_mint(tmp_path):
    h = make_prep(tmp_path)
    try:
        h.ev.crash_before = "reservation"
        with pytest.raises(Crash):
            go(h)
        pid = pid_of(h)
        assert h.prep.recover() == {pid: PREP_INTERRUPTED_BEFORE_MINT}
        assert h.ev.kinds(pid) == ["intent", "reservation", "attempt_inconclusive"]
        assert h.mint.calls == 0
        assert [r for r in h.ev.rows if r["kind"] == "session_excluded"][0]["session_id"] == "s-1"
        # The reservation is only released by the close step, never by recovery.
        assert h.rig.ledger.get_reservation(pid).state == "ACTIVE"
        assert h.prep.release_inconclusive() == [pid]
        assert h.rig.ledger.get_order(pid).state == "EXPIRED"
        assert h.prep.release_inconclusive() == []
    finally:
        h.rig.close()


def test_a_crash_after_mint_started_with_no_audit_entry_is_ambiguous_after_the_timeout(tmp_path):
    h = make_prep(tmp_path)
    try:
        h.ev.crash_after = "mint_started"
        with pytest.raises(Crash):
            go(h)
        pid = pid_of(h)
        assert h.mint.calls == 0
        assert h.prep.recover() == {pid: "deferred"}  # inside the mint timeout
        h.clock.advance(30)
        assert h.prep.recover() == {pid: MINT_AMBIGUOUS}
        assert h.prep.recover() == {pid: MINT_AMBIGUOUS}
        assert h.mint.calls == 0
        kinds = h.ev.kinds(pid)
        assert kinds.count("vm_ambiguous") == 1 and kinds.count("attempt_inconclusive") == 1
        assert [r["kind"] for r in h.ev.rows].count("session_excluded") == 1
        assert h.prep.release_inconclusive() == [pid]
    finally:
        h.rig.close()


def test_a_mint_answer_lost_before_its_record_is_recovered_from_the_audit(tmp_path):
    h = make_prep(tmp_path)
    try:
        h.ev.crash_before = "vm_decision"
        with pytest.raises(Crash):
            go(h)
        pid = pid_of(h)
        h.audit.entries.append(audit_entry(h, pid))
        h.clock.advance(30)
        assert h.prep.recover() == {pid: "vm_decided"}
        assert h.mint.calls == 1  # the live call only; recovery posted nothing
        decision = [r for r in h.ev.rows if r["kind"] == "vm_decision"][0]
        assert decision["recovered_from_audit"] is True and decision["audit_seq"] == 5
        assert len([e for e in h.rig.ledger.list_events(pid) if e.event_type == ledger_module.SHADOW_VM_DECISION_EVENT]) == 1
        assert h.prep.recover() == {pid: "vm_decided"}
        assert h.mint.calls == 1
    finally:
        h.rig.close()


def test_a_crash_between_the_decision_record_and_the_ledger_writes_the_ledger_side(tmp_path):
    h = make_prep(tmp_path)
    try:
        h.ev.crash_after = "vm_decision"
        with pytest.raises(Crash):
            go(h)
        pid = pid_of(h)
        assert not [e for e in h.rig.ledger.list_events(pid) if e.event_type == ledger_module.SHADOW_VM_DECISION_EVENT]
        assert h.prep.recover() == {pid: "vm_decided"}
        assert len([e for e in h.rig.ledger.list_events(pid) if e.event_type == ledger_module.SHADOW_VM_DECISION_EVENT]) == 1
        assert h.prep.recover() == {pid: "vm_decided"}
        assert len([e for e in h.rig.ledger.list_events(pid) if e.event_type == ledger_module.SHADOW_VM_DECISION_EVENT]) == 1
        assert h.mint.calls == 1
    finally:
        h.rig.close()


@pytest.mark.parametrize("mode", ["unresolved", "timeout"])
def test_an_unresolved_mint_is_never_retried_and_a_late_entry_is_recorded_once(tmp_path, mode):
    h = make_prep(tmp_path)
    try:
        h.mint.mode = mode
        result = go(h)
        pid = result.proposal_id
        assert result.status == "mint_unresolved" and h.mint.calls == 1
        assert h.prep.recover() == {pid: "deferred"}
        h.clock.advance(60)
        assert h.prep.recover() == {pid: MINT_AMBIGUOUS}
        assert h.mint.calls == 1
        # The VM entry shows up later: recorded as a late entry, once, whatever the repeats.
        h.audit.entries.append(audit_entry(h, pid))
        assert h.prep.scan_audit() == {pid: "vm_late_entry"}
        assert h.prep.scan_audit() == {}
        assert len([r for r in h.ev.rows if r["kind"] == "vm_late_entry"]) == 1
        assert h.prep.recover() == {pid: MINT_AMBIGUOUS}
        assert h.mint.calls == 1
    finally:
        h.rig.close()


def test_two_ambiguous_intents_in_one_session_exclude_it_once(tmp_path):
    h = make_prep(tmp_path)
    try:
        h.mint.mode = "unresolved"
        go(h, signal="a")
        go(h, signal="b")
        h.clock.advance(60)
        outcomes = h.prep.recover()
        assert set(outcomes.values()) == {MINT_AMBIGUOUS} and len(outcomes) == 2
        assert [r["kind"] for r in h.ev.rows].count("session_excluded") == 1
    finally:
        h.rig.close()


def _hold_past_timeout_recover_then_release(h, *, codes=None):
    """Hold the mint past its timeout, recover (zero audit entries), then let the handler answer."""

    h.mint.gate = asyncio.Event()

    async def scenario():
        h.mint.entered = asyncio.Event()
        task = asyncio.ensure_future(
            h.prep.prepare(
                session_id="s-1", signal_id="sig-1", leg="entry", symbol=SYMBOL, side="BUY",
                quantity=2, limit_price="100.00", samples=samples(NOW),
            )
        )
        await h.mint.entered.wait()
        h.clock.advance(60)  # held past the mint timeout
        during = h.prep.recover()
        assert h.audit.pages == [0]  # recovery read the audit and found no entry
        if codes is not None:
            h.mint.codes = codes
        h.mint.gate.set()  # the held handler now completes
        return during, await task

    return asyncio.run(asyncio.wait_for(scenario(), 20))


def test_a_held_mint_that_answers_after_recovery_called_it_ambiguous_stays_inconclusive(tmp_path):
    h = make_prep(tmp_path)
    try:
        during, result = _hold_past_timeout_recover_then_release(h)
        pid = pid_of(h)
        assert during == {pid: MINT_AMBIGUOUS}
        assert result.status == "mint_unresolved" and result.vm_decision is None
        for _ in range(2):
            assert h.prep.recover() == {pid: MINT_AMBIGUOUS}
        assert h.mint.calls == 1
        kinds = h.ev.kinds(pid)
        assert "vm_decision" not in kinds and kinds.count("vm_late_entry") == 1
        assert kinds.count("attempt_inconclusive") == 1
        assert [r["kind"] for r in h.ev.rows].count("session_excluded") == 1
        late = next(r for r in h.ev.rows if r["kind"] == "vm_late_entry")
        assert (late["audit_seq"], late["entry_hash"]) == (5, sha("e-hash"))
        assert not [
            e for e in h.rig.ledger.list_events(pid) if e.event_type == ledger_module.SHADOW_VM_DECISION_EVENT
        ]
        # The VM entry the handler wrote is the one the late-entry row names: the scan agrees, once.
        intent = next(r for r in h.ev.rows if r["kind"] == "intent")
        h.audit.entries.append(AuditEntry(5, pid, intent["intent_sha256"], ("kill_switch",), sha("e-hash")))
        assert h.prep.scan_audit() == {}
        assert len([r for r in h.ev.rows if r["kind"] == "vm_late_entry"]) == 1
        assert h.prep.release_inconclusive() == [pid]
    finally:
        h.rig.close()


def test_a_held_mint_answering_with_other_codes_after_ambiguity_aborts(tmp_path):
    h = make_prep(tmp_path)
    try:
        with pytest.raises(ShadowAbort) as caught:
            _hold_past_timeout_recover_then_release(h, codes=("kill_switch", "other"))
        assert caught.value.code == "vm_codes_unexpected"
        assert "vm_late_entry" not in h.ev.kinds() and "vm_decision" not in h.ev.kinds()
    finally:
        h.rig.close()


def test_recovery_gives_an_ambiguous_mint_precedence_over_a_later_decision_row(tmp_path):
    # Second layer. Even if a decision row reaches the evidence after the ambiguity record (an older
    # writer, a replay), the attempt is still INCONCLUSIVE, never vm_decided.
    h = make_prep(tmp_path)
    try:
        h.mint.mode = "unresolved"
        pid = go(h).proposal_id
        h.clock.advance(60)
        assert h.prep.recover() == {pid: MINT_AMBIGUOUS}
        digest = next(r for r in h.ev.rows if r["kind"] == "intent")["intent_sha256"]
        h.prep._record_decision(  # noqa: SLF001
            "s-1", VmDecision(pid, digest, ("kill_switch",), 5, sha("e-hash")), recovered=False
        )
        assert "vm_decision" in h.ev.kinds(pid)
        for _ in range(2):
            assert h.prep.recover() == {pid: MINT_AMBIGUOUS}
        assert h.prep.release_inconclusive() == [pid]
    finally:
        h.rig.close()


def test_a_decided_intent_is_not_released_by_the_close_step(tmp_path):
    h = make_prep(tmp_path)
    try:
        result = go(h)
        assert h.prep.release_inconclusive() == []
        assert h.rig.ledger.get_reservation(result.proposal_id).state == "ACTIVE"
    finally:
        h.rig.close()


def _two_entries(h, pid):
    h.audit.entries[:] = [audit_entry(h, pid, 5), audit_entry(h, pid, 6)]


def _wrong_codes(h, pid):
    h.audit.entries[:] = [audit_entry(h, pid, 5, codes=("kill_switch", "x"))]


def _wrong_hash(h, pid):
    h.audit.entries[:] = [audit_entry(h, pid, 5, digest=sha("other"))]


@pytest.mark.parametrize(
    "plant, code",
    [
        (_two_entries, "audit_duplicate_entries"),
        (_wrong_codes, "audit_entry_mismatch"),
        (_wrong_hash, "audit_entry_mismatch"),
    ],
)
def test_recovery_aborts_on_an_audit_it_cannot_reconcile(tmp_path, plant, code):
    h = make_prep(tmp_path)
    try:
        h.ev.crash_after = "mint_started"
        with pytest.raises(Crash):
            go(h)
        pid = pid_of(h)
        plant(h, pid)
        h.clock.advance(60)
        before = (len(h.ev.rows), snapshot(h.rig.ledger))
        with pytest.raises(ShadowAbort) as caught:
            h.prep.recover()
        assert caught.value.code == code
        assert (len(h.ev.rows), snapshot(h.rig.ledger)) == before
        assert h.mint.calls == 0
    finally:
        h.rig.close()


def test_an_unverified_audit_chain_aborts_every_audit_read(tmp_path):
    h = make_prep(tmp_path)
    try:
        h.ev.crash_after = "mint_started"
        with pytest.raises(Crash):
            go(h)
        h.audit.verified = False
        h.clock.advance(60)
        with pytest.raises(ShadowAbort) as first:
            h.prep.recover()
        with pytest.raises(ShadowAbort) as second:
            h.prep.scan_audit()
        assert first.value.code == second.value.code == "audit_chain_unverified"
    finally:
        h.rig.close()


def test_scan_audit_aborts_on_an_entry_with_no_intent_or_no_mint_start(tmp_path):
    h = make_prep(tmp_path)
    try:
        h.audit.entries[:] = [AuditEntry(1, "ghost", sha("x"), ("kill_switch",), sha("h"))]
        with pytest.raises(ShadowAbort) as caught:
            h.prep.scan_audit()
        assert caught.value.code == "audit_entry_without_intent_record"
        h.audit.entries.clear()
        h.ev.crash_before = "reservation"
        with pytest.raises(Crash):
            go(h)
        h.audit.entries[:] = [audit_entry(h, pid_of(h))]
        with pytest.raises(ShadowAbort) as caught:
            h.prep.scan_audit()
        assert caught.value.code == "audit_entry_without_mint_started"
    finally:
        h.rig.close()


def test_recovery_aborts_on_a_ledger_order_with_no_intent_record_and_on_a_lone_decision(tmp_path):
    h = make_prep(tmp_path)
    try:
        prepare_buy(h.rig, "stray", quantity=1)  # an order the evidence never saw
        with pytest.raises(ShadowAbort) as caught:
            h.prep.recover()
        assert caught.value.code == "ledger_order_without_intent_record"
    finally:
        h.rig.close()
    h = make_prep(tmp_path / "two")
    try:
        h.ev.crash_after = "mint_started"
        with pytest.raises(Crash):
            go(h)
        pid = pid_of(h)
        h.rig.ledger.record_shadow_vm_decision(pid, 9, {"codes": ["kill_switch"]})
        with pytest.raises(ShadowAbort) as caught:
            h.prep.recover()
        assert caught.value.code == "ledger_decision_without_evidence"
    finally:
        h.rig.close()


def test_an_intent_the_ledger_never_saw_is_abandoned_cleanly(tmp_path):
    h = make_prep(tmp_path)
    try:
        h.ev.append("intent", {
            "intent_id": "lost", "session_id": "s-1", "signal_id": "z", "leg": "entry",
            "intent_sha256": sha("lost"), "symbol": SYMBOL, "side": "BUY", "quantity": "1", "limit_price": "100",
        })
        assert h.prep.recover() == {"lost": "prep_abandoned"}
        assert h.prep.recover() == {"lost": "closed"}
        assert h.mint.calls == 0
    finally:
        h.rig.close()


# SL-8: booking across the evidence store and the ledger.


def _booking_args(pid: str, **overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = dict(
        proposal_id=pid, cumulative_quantity=Decimal(2), price=Decimal("100.00"), charges=Decimal("1"),
        schedule_sha256=SCHEDULE, scenario_set_sha256=sha("scenario-set"), attempt_id="att-1",
    )
    values.update(overrides)
    return values


def test_a_booking_is_evidenced_before_and_after_the_ledger_commit(tmp_path):
    h = make_prep(tmp_path)
    try:
        pid = go(h).proposal_id
        booking = h.prep.book_fill(**_booking_args(pid))
        assert [k for k in h.ev.kinds(pid) if k.startswith("booking")] == ["booking_intent", "booking_done"]
        assert h.rig.ledger.simulated_booking(booking.booking_id) == booking
        done = [r for r in h.ev.rows if r["kind"] == "booking_done"][0]
        assert done["ledger_event_id"] == next(
            e.event_id for e in h.rig.ledger.list_events(pid) if e.event_type == ledger_module.SIMULATED_BOOKING_EVENT
        )
        assert h.prep.recover_bookings() == {booking.booking_id: "done"}
    finally:
        h.rig.close()


def test_a_crash_after_the_booking_intent_rebooks_exactly_once(tmp_path):
    h = make_prep(tmp_path)
    try:
        pid = go(h).proposal_id
        h.ev.crash_after = "booking_intent"
        with pytest.raises(Crash):
            h.prep.book_fill(**_booking_args(pid))
        assert h.rig.ledger.simulated_bookings() == []
        outcomes = h.prep.recover_bookings()
        assert list(outcomes.values()) == ["rebooked"]
        assert len(h.rig.ledger.simulated_bookings()) == 1
        assert h.prep.recover_bookings() == {next(iter(outcomes)): "done"}
        assert len(h.rig.ledger.simulated_bookings()) == 1
        assert h.rig.ledger.simulated_cash_view().net_cash_out == Decimal("201.00")
    finally:
        h.rig.close()


def test_a_crash_after_the_ledger_commit_adopts_the_booking_without_rebooking(tmp_path):
    h = make_prep(tmp_path)
    try:
        pid = go(h).proposal_id
        h.ev.crash_before = "booking_done"
        with pytest.raises(Crash):
            h.prep.book_fill(**_booking_args(pid))
        cash = h.rig.ledger.simulated_cash_view()
        outcomes = h.prep.recover_bookings()
        assert list(outcomes.values()) == ["adopted"]
        assert h.rig.ledger.simulated_cash_view() == cash
        assert len(h.rig.ledger.simulated_bookings()) == 1
        assert h.prep.recover_bookings() == {next(iter(outcomes)): "done"}
    finally:
        h.rig.close()


def test_booking_recovery_aborts_on_a_payload_mismatch_and_on_an_unknown_ledger_booking(tmp_path):
    h = make_prep(tmp_path)
    try:
        pid = go(h).proposal_id
        h.ev.crash_before = "booking_done"
        with pytest.raises(Crash):
            h.prep.book_fill(**_booking_args(pid))
        for record in h.ev.rows:
            if record["kind"] == "booking_intent":
                record["price"] = "99.00"
        with pytest.raises(ShadowAbort) as caught:
            h.prep.recover_bookings()
        assert caught.value.code == "booking_payload_mismatch"
    finally:
        h.rig.close()
    h = make_prep(tmp_path / "two")
    try:
        pid = go(h).proposal_id
        scenario = sha("loose")
        h.rig.ledger.book_simulated_fill(
            simulated_booking_id("att-x", scenario), pid, 1, "100.00", "0", SCHEDULE, scenario, "att-x"
        )
        with pytest.raises(ShadowAbort) as caught:
            h.prep.recover_bookings()
        assert caught.value.code == "ledger_booking_without_intent"
    finally:
        h.rig.close()


def _refuse_one(h, pid: str, *, attempt: str = "att-bad") -> str:
    """Book a BUY fill one tick above its limit: the ledger refuses it and changes nothing."""

    with pytest.raises(InvalidTransition):
        h.prep.book_fill(**_booking_args(pid, price=Decimal("100.01"), attempt_id=attempt))
    return simulated_booking_id(attempt, sha("scenario-set"))


def test_a_refused_booking_is_closed_and_recovery_never_retries_it(tmp_path):
    h = make_prep(tmp_path)
    try:
        pid = go(h).proposal_id
        cash = h.rig.ledger.simulated_cash_view()
        bad = _refuse_one(h, pid)
        refusal = [r for r in h.ev.rows if r["kind"] == "booking_refused"]
        assert [k for k in h.ev.kinds(pid) if k.startswith("booking")] == ["booking_intent", "booking_refused"]
        assert refusal[0]["booking_id"] == bad and refusal[0]["code"] == "InvalidTransition"
        assert set(refusal[0]) == {"kind", "intent_id", "booking_id", "code"}  # the code only
        for _ in range(3):
            assert h.prep.recover_bookings() == {bad: "refused"}
        assert h.rig.ledger.simulated_bookings() == [] and h.rig.ledger.simulated_cash_view() == cash
        assert len([r for r in h.ev.rows if r["kind"] == "booking_refused"]) == 1
        assert "booking_done" not in h.ev.kinds()
    finally:
        h.rig.close()


def test_a_refusal_that_died_before_its_row_is_closed_by_the_next_recovery(tmp_path):
    h = make_prep(tmp_path)
    try:
        pid = go(h).proposal_id
        h.ev.crash_before = "booking_refused"
        with pytest.raises(Crash):
            h.prep.book_fill(**_booking_args(pid, price=Decimal("100.01"), attempt_id="att-bad"))
        bad = simulated_booking_id("att-bad", sha("scenario-set"))
        assert "booking_refused" not in h.ev.kinds()
        assert h.prep.recover_bookings() == {bad: "refused"}
        assert h.prep.recover_bookings() == {bad: "refused"}
        assert h.ev.kinds().count("booking_refused") == 1
        assert h.rig.ledger.simulated_bookings() == []
    finally:
        h.rig.close()


def test_a_refused_intent_does_not_block_a_committed_booking_that_never_got_its_done_row(tmp_path):
    h = make_prep(tmp_path)
    try:
        pid = go(h).proposal_id
        bad = _refuse_one(h, pid)
        h.ev.crash_before = "booking_done"
        with pytest.raises(Crash):
            h.prep.book_fill(**_booking_args(pid))
        good = simulated_booking_id("att-1", sha("scenario-set"))
        cash = h.rig.ledger.simulated_cash_view()
        assert h.prep.recover_bookings() == {bad: "refused", good: "adopted"}
        assert h.rig.ledger.simulated_cash_view() == cash and len(h.rig.ledger.simulated_bookings()) == 1
        assert h.prep.recover_bookings() == {bad: "refused", good: "done"}
    finally:
        h.rig.close()


def test_a_retry_of_a_refused_booking_id_that_committed_is_adopted_not_treated_as_refused(tmp_path):
    # Same attempt and scenario set, so the same booking id: refused once (row written), then the
    # corrected retry commits and the process dies before its done row.
    h = make_prep(tmp_path)
    try:
        pid = go(h).proposal_id
        bid = _refuse_one(h, pid, attempt="att-1")
        assert "booking_refused" in h.ev.kinds(pid)
        h.ev.crash_before = "booking_done"
        with pytest.raises(Crash):
            h.prep.book_fill(**_booking_args(pid))
        assert bid == simulated_booking_id("att-1", sha("scenario-set"))
        assert h.rig.ledger.simulated_booking(bid) is not None
        assert h.prep.recover_bookings() == {bid: "adopted"}
        done = [r for r in h.ev.rows if r["kind"] == "booking_done"]
        assert [r["booking_id"] for r in done] == [bid]
        assert h.prep.recover_bookings() == {bid: "done"}
        assert len(h.rig.ledger.simulated_bookings()) == 1
    finally:
        h.rig.close()


def test_a_lost_refusal_row_then_a_same_id_commit_is_adopted_against_the_latest_intent(tmp_path):
    # The first attempt's refusal row never landed, so two intent rows share one booking id with
    # different payloads. The ledger holds the second, so adoption must compare against the last.
    h = make_prep(tmp_path)
    try:
        pid = go(h).proposal_id
        h.ev.crash_before = "booking_refused"
        with pytest.raises(Crash):
            h.prep.book_fill(**_booking_args(pid, price=Decimal("100.01")))
        h.ev.crash_before = "booking_done"
        with pytest.raises(Crash):
            h.prep.book_fill(**_booking_args(pid))
        bid = simulated_booking_id("att-1", sha("scenario-set"))
        intents = [r for r in h.ev.rows if r["kind"] == "booking_intent"]
        assert len(intents) == 2 and intents[0]["price"] != intents[1]["price"]
        assert "booking_refused" not in h.ev.kinds() and h.rig.ledger.simulated_booking(bid) is not None
        assert h.prep.recover_bookings() == {bid: "adopted"}
        assert [r["booking_id"] for r in h.ev.rows if r["kind"] == "booking_done"] == [bid]
        assert h.prep.recover_bookings() == {bid: "done"}
    finally:
        h.rig.close()


def test_an_unexplained_intent_ahead_of_a_committed_booking_is_still_adopted_first(tmp_path):
    # The refused attempt's own row never landed (killed first), so it is unexplained evidence. The
    # later committed booking is adopted before anything is retried, and the refusal is recorded.
    h = make_prep(tmp_path)
    try:
        pid = go(h).proposal_id
        h.ev.crash_before = "booking_refused"
        with pytest.raises(Crash):
            h.prep.book_fill(**_booking_args(pid, price=Decimal("100.01"), attempt_id="att-bad"))
        bad = simulated_booking_id("att-bad", sha("scenario-set"))
        h.ev.crash_before = "booking_done"
        with pytest.raises(Crash):
            h.prep.book_fill(**_booking_args(pid))
        good = simulated_booking_id("att-1", sha("scenario-set"))
        order: list[str] = []
        book_fill = h.rig.ledger.book_simulated_fill
        h.rig.ledger.book_simulated_fill = lambda bid, *a, **k: (order.append(bid), book_fill(bid, *a, **k))[1]
        assert h.prep.recover_bookings() == {bad: "refused", good: "adopted"}
        assert order == [bad]  # the committed booking was adopted without a ledger call
        assert [r["kind"] for r in h.ev.rows if r["kind"] in ("booking_done", "booking_refused")] == [
            "booking_done", "booking_refused"
        ]  # adoption ran first
    finally:
        h.rig.close()


def test_an_intent_the_ledger_neither_holds_nor_refuses_still_surfaces(tmp_path):
    h = make_prep(tmp_path)
    try:
        pid = go(h).proposal_id
        h.ev.crash_after = "booking_intent"
        with pytest.raises(Crash):
            h.prep.book_fill(**_booking_args(pid))

        def broken(*args: Any, **kwargs: Any):
            raise LedgerError("disk gone")

        h.rig.ledger.book_simulated_fill = broken
        for _ in range(2):
            with pytest.raises(LedgerError, match="disk gone"):
                h.prep.recover_bookings()
        assert "booking_refused" not in h.ev.kinds() and "booking_done" not in h.ev.kinds()
    finally:
        h.rig.close()


def test_a_refusal_row_with_no_intent_is_abort_class(tmp_path):
    h = make_prep(tmp_path)
    try:
        h.ev.append("booking_refused", {"intent_id": "x", "booking_id": sha("nothing"), "code": "InvalidTransition"})
        with pytest.raises(ShadowAbort) as caught:
            h.prep.recover_bookings()
        assert caught.value.code == "booking_refusal_without_intent"
    finally:
        h.rig.close()


# --- real process death: a child is killed mid-step, a new process reopens and recovers ---------

class FileEvidence:
    """An fsync'd JSON-lines evidence file. ``crash`` = ``(kind, "before" | "after")`` kills the process."""

    def __init__(self, path: Path, crash: tuple[str, str] | None = None) -> None:
        self.path = path
        self.crash = crash

    def append(self, kind: str, payload: Any) -> dict[str, Any]:
        if self.crash == (kind, "before"):
            os._exit(137)
        record = {"kind": kind, **payload}
        with open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        if self.crash == (kind, "after"):
            os._exit(137)
        return record

    def records(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line]


_CHILD = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {backend!r})
    sys.path.insert(0, {tests!r})
    from datetime import datetime, timezone
    from decimal import Decimal
    from pathlib import Path
    from test_execution_shadow_sim_account import (
        FileEvidence, ListAudit, ScriptedMint, ShadowPreparation, SCHEDULE, go_with, sha,
    )
    import execution.service as service_module
    from india_limits_support import NOW
    from test_shadow_india_admission import RUN_ID, SEED, _FrozenDatetime, make_guard
    from shadow_india.admission import ShadowAdmissionAdapter, open_simulation_ledger

    service_module.datetime = _FrozenDatetime
    base = Path({base!r})
    ledger = open_simulation_ledger(base / "sim", RUN_ID, SEED)
    guard = make_guard(ledger, base / "private")
    adapter = ShadowAdmissionAdapter(ledger, guard, clock=lambda: NOW)
    crash = {crash!r}
    prep = ShadowPreparation(
        run_id=RUN_ID, adapter=adapter, evidence=FileEvidence(base / "evidence.jsonl", crash),
        mint=ScriptedMint(), audit=ListAudit(),
    )
    pid = go_with(prep)
    if {book!r}:
        prep.book_fill(
            proposal_id=pid, cumulative_quantity=Decimal(2), price=Decimal("100.00"), charges=Decimal(1),
            schedule_sha256=SCHEDULE, scenario_set_sha256=sha("scenario-set"), attempt_id="att-1",
        )
    sys.exit(0)
    """
)


def go_with(prep: ShadowPreparation) -> str:
    result = run(
        prep.prepare(
            session_id="s-1", signal_id="sig-1", leg="entry", symbol=SYMBOL, side="BUY", quantity=2,
            limit_price="100.00", samples=samples(NOW),
        )
    )
    return result.proposal_id


def _kill(base: Path, crash: tuple[str, str], *, book: bool = False) -> None:
    script = _CHILD.format(
        backend=str(Path(ledger_module.__file__).resolve().parents[1]),
        tests=str(Path(__file__).resolve().parent),
        base=str(base),
        crash=crash,
        book=book,
    )
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=120,
        env={**os.environ, "GROWIN_ANALYTICS_ENABLED": "false", "CI": "true"},
    )
    assert done.returncode == 137, (done.returncode, done.stderr[-2000:])


def _reopen_prep(base: Path):
    from test_shadow_india_admission import make_guard
    from shadow_india.admission import ShadowAdmissionAdapter, open_simulation_ledger

    ledger = open_simulation_ledger(base / "sim", RUN_ID, SEED)
    guard = make_guard(ledger, base / "private")
    evidence, mint, audit, clock = FileEvidence(base / "evidence.jsonl"), ScriptedMint(), ListAudit(), Clock()
    prep = ShadowPreparation(
        run_id=RUN_ID, adapter=ShadowAdmissionAdapter(ledger, guard, clock=lambda: NOW), evidence=evidence, mint=mint,
        audit=audit, clock=clock,
    )
    return SimpleNamespace(ledger=ledger, prep=prep, ev=evidence, mint=mint, audit=audit, clock=clock)


def _closed_rig(tmp_path: Path) -> None:
    make_rig(tmp_path).close()


RESERVED_AFTER_BUY_ADMISSION = Decimal("200.00")  # 2 shares at the 100.00 limit


def _assert_one_reservation(h, *, reserved: Decimal) -> None:
    """The killed child left exactly one buy reservation and this much simulated cash reserved."""

    assert _reservation_count(h.ledger) == (1, 0)
    assert h.ledger.simulated_cash_view().reserved == reserved


def _kinds(h) -> list[str]:
    return [r["kind"] for r in h.ev.records()]


def test_killed_before_the_reservation_record_a_new_process_marks_the_attempt_inconclusive(tmp_path):
    _closed_rig(tmp_path)
    _kill(tmp_path, ("reservation", "before"))
    h = _reopen_prep(tmp_path)
    try:
        pid = h.prep.proposal_id("s-1", "sig-1", "entry")
        _assert_one_reservation(h, reserved=RESERVED_AFTER_BUY_ADMISSION)
        assert h.prep.recover() == {pid: PREP_INTERRUPTED_BEFORE_MINT}
        assert h.mint.calls == 0 and h.ledger.get_reservation(pid).state == "ACTIVE"
        _assert_one_reservation(h, reserved=RESERVED_AFTER_BUY_ADMISSION)
        assert h.prep.release_inconclusive() == [pid]
    finally:
        h.ledger.close()


def test_killed_right_after_mint_started_a_new_process_waits_then_calls_it_ambiguous(tmp_path):
    _closed_rig(tmp_path)
    _kill(tmp_path, ("mint_started", "after"))
    h = _reopen_prep(tmp_path)
    try:
        pid = h.prep.proposal_id("s-1", "sig-1", "entry")
        _assert_one_reservation(h, reserved=RESERVED_AFTER_BUY_ADMISSION)
        assert h.prep.recover() == {pid: "deferred"}
        h.clock.advance(60)
        assert h.prep.recover() == {pid: MINT_AMBIGUOUS}
        assert h.mint.calls == 0
        _assert_one_reservation(h, reserved=RESERVED_AFTER_BUY_ADMISSION)
    finally:
        h.ledger.close()


def test_killed_with_the_vm_answer_unrecorded_a_new_process_reads_it_from_the_audit(tmp_path):
    _closed_rig(tmp_path)
    _kill(tmp_path, ("vm_decision", "before"))
    h = _reopen_prep(tmp_path)
    try:
        pid = h.prep.proposal_id("s-1", "sig-1", "entry")
        intent = next(r for r in h.ev.records() if r["kind"] == "intent")
        h.audit.entries.append(AuditEntry(5, pid, intent["intent_sha256"], ("kill_switch",), sha("h")))
        h.clock.advance(60)
        _assert_one_reservation(h, reserved=RESERVED_AFTER_BUY_ADMISSION)
        assert h.prep.recover() == {pid: "vm_decided"}
        assert h.mint.calls == 0  # the child made the one POST; the new process makes none
        _assert_one_reservation(h, reserved=RESERVED_AFTER_BUY_ADMISSION)
    finally:
        h.ledger.close()


def test_killed_between_the_decision_record_and_the_ledger_a_new_process_finishes_the_ledger(tmp_path):
    _closed_rig(tmp_path)
    _kill(tmp_path, ("vm_decision", "after"))
    h = _reopen_prep(tmp_path)
    try:
        pid = h.prep.proposal_id("s-1", "sig-1", "entry")
        _assert_one_reservation(h, reserved=RESERVED_AFTER_BUY_ADMISSION)
        assert not [e for e in h.ledger.list_events(pid) if e.event_type == ledger_module.SHADOW_VM_DECISION_EVENT]
        assert h.prep.recover() == {pid: "vm_decided"}
        assert len([e for e in h.ledger.list_events(pid) if e.event_type == ledger_module.SHADOW_VM_DECISION_EVENT]) == 1
        assert h.mint.calls == 0
        _assert_one_reservation(h, reserved=RESERVED_AFTER_BUY_ADMISSION)
    finally:
        h.ledger.close()


def test_killed_after_the_booking_intent_a_new_process_rebooks_once(tmp_path):
    _closed_rig(tmp_path)
    _kill(tmp_path, ("booking_intent", "after"), book=True)
    h = _reopen_prep(tmp_path)
    try:
        assert h.ledger.simulated_bookings() == []
        _assert_one_reservation(h, reserved=RESERVED_AFTER_BUY_ADMISSION)  # nothing booked yet
        assert list(h.prep.recover_bookings().values()) == ["rebooked"]
        assert len(h.ledger.simulated_bookings()) == 1 and h.ledger.simulated_cash_view().net_cash_out == Decimal("201")
        assert list(h.prep.recover_bookings().values()) == ["done"]
        # A full fill settles the one reservation; recovery created no second one.
        _assert_one_reservation(h, reserved=Decimal(0))
    finally:
        h.ledger.close()


def test_killed_after_the_ledger_commit_a_new_process_adopts_the_booking(tmp_path):
    _closed_rig(tmp_path)
    _kill(tmp_path, ("booking_done", "before"), book=True)
    h = _reopen_prep(tmp_path)
    try:
        cash = h.ledger.simulated_cash_view()
        assert len(h.ledger.simulated_bookings()) == 1
        _assert_one_reservation(h, reserved=Decimal(0))  # the ledger commit landed before the kill
        assert list(h.prep.recover_bookings().values()) == ["adopted"]
        assert h.ledger.simulated_cash_view() == cash and len(h.ledger.simulated_bookings()) == 1
        _assert_one_reservation(h, reserved=Decimal(0))
    finally:
        h.ledger.close()


# --- rows the plan names that the groups above do not already cover ------------------------------


def _reservation_count(ledger: ExecutionLedger) -> tuple[int, int]:
    connection = ledger._require_connection()  # noqa: SLF001
    return (
        connection.execute("SELECT COUNT(*) FROM buying_power_reservations").fetchone()[0],
        connection.execute("SELECT COUNT(*) FROM ledger_quantity_reservations").fetchone()[0],
    )


def test_recovery_never_creates_a_reservation(tmp_path):
    h = make_prep(tmp_path)
    try:
        h.ev.crash_before = "reservation"
        with pytest.raises(Crash):
            go(h)
        before = _reservation_count(h.rig.ledger)
        cash = h.rig.ledger.simulated_cash_view()
        assert before == (1, 0)
        for _ in range(3):
            h.prep.recover()
        assert _reservation_count(h.rig.ledger) == before
        assert h.rig.ledger.simulated_cash_view() == cash
    finally:
        h.rig.close()


def test_an_admitted_order_with_no_reservation_is_closed_not_left_pending(tmp_path):
    h = make_prep(tmp_path)
    try:
        pid = pid_of(h)
        intent = h.rig.adapter.build_intent(pid, symbol=SYMBOL, side="BUY", quantity=2, limit_price="100.00")
        from execution.ledger import intent_hash
        from test_shadow_india_admission import _direct_kwargs

        h.ev.append("intent", {
            "intent_id": pid, "session_id": "s-1", "signal_id": "sig-1", "leg": "entry",
            "intent_sha256": intent_hash(intent), "symbol": SYMBOL, "side": "BUY",
            "quantity": "2", "limit_price": "100.00",
        })
        # The process died after the admission and before the reservation.
        admission = h.rig.adapter.service.admit(intent, **_direct_kwargs(h.rig, intent))
        assert admission.decision is AdmissionDecision.ADMITTED
        assert h.rig.ledger.get_order(pid).state == "PENDING" and h.rig.ledger.get_reservation(pid) is None
        assert h.prep.recover() == {pid: "prep_abandoned"}
        assert h.rig.ledger.get_order(pid).state == "EXPIRED"
        assert _reservation_count(h.rig.ledger) == (0, 0)
        assert h.prep.recover() == {pid: "closed"}
        assert h.mint.calls == 0
    finally:
        h.rig.close()


def test_scan_audit_aborts_on_a_second_or_malformed_late_entry(tmp_path):
    def ambiguous() -> SimpleNamespace:
        h = make_prep(tmp_path / f"h{len(list(tmp_path.iterdir()))}")
        h.mint.mode = "unresolved"
        pid = go(h).proposal_id
        h.clock.advance(60)
        assert h.prep.recover() == {pid: MINT_AMBIGUOUS}
        h.pid = pid
        return h

    h = ambiguous()
    try:
        h.audit.entries[:] = [audit_entry(h, h.pid, 5), audit_entry(h, h.pid, 6)]
        with pytest.raises(ShadowAbort) as caught:
            h.prep.scan_audit()
        assert caught.value.code == "audit_duplicate_entries"
    finally:
        h.rig.close()
    h = ambiguous()
    try:
        h.audit.entries[:] = [audit_entry(h, h.pid, 5, codes=("kill_switch", "x"))]
        with pytest.raises(ShadowAbort) as caught:
            h.prep.scan_audit()
        assert caught.value.code == "audit_entry_codes"
        h.audit.entries[:] = [audit_entry(h, h.pid, 5, codes=("other",))]
        with pytest.raises(ShadowAbort):
            h.prep.scan_audit()
        assert "vm_late_entry" not in h.ev.kinds()
    finally:
        h.rig.close()
    h = ambiguous()
    try:
        h.audit.entries[:] = [audit_entry(h, h.pid, 5)]
        assert h.prep.scan_audit() == {h.pid: "vm_late_entry"}
        h.audit.entries[:] = [audit_entry(h, h.pid, 7)]
        with pytest.raises(ShadowAbort) as caught:
            h.prep.scan_audit()
        assert caught.value.code == "audit_duplicate_entries"
    finally:
        h.rig.close()


def test_simulated_cash_that_drifts_from_its_events_is_a_ledger_fault(tmp_path):
    rig = make_rig(tmp_path)
    try:
        prepare_buy(rig, "drift", quantity=2)
        with rig.ledger._transaction() as connection:  # noqa: SLF001
            _drop_triggers(connection, "simulated_cash")
            connection.execute("UPDATE simulated_cash SET reserved = '1'")
        with pytest.raises(LedgerError):
            rig.ledger.simulated_cash_view()
        with rig.ledger._transaction() as connection:  # noqa: SLF001
            connection.execute("UPDATE simulated_cash SET reserved = '200', net_cash_out = '5'")
        with pytest.raises(LedgerError):
            rig.ledger.simulated_cash_view()
    finally:
        rig.close()


# The reservation arithmetic also refuses these three, with the same exception type. These rows pin
# the refusal to the guard that is meant to make it, so removing that guard cannot hide behind the
# second line of defence.


def test_overfill_is_refused_by_the_quantity_guard_itself(tmp_path):
    rig = make_rig(tmp_path)
    try:
        prepare_buy(rig, "of", quantity=10)
        with pytest.raises(InvalidTransition, match="overfills"):
            book(rig, "of", 11, "100.00")
    finally:
        rig.close()


def test_oversell_is_refused_by_the_holding_guard_itself(tmp_path):
    rig = make_rig(tmp_path)
    try:
        buy_and_fill(rig, "held2", 5)
        assert sell(rig, "os", quantity=5).decision is AdmissionDecision.ADMITTED
        with rig.ledger._transaction() as connection:  # noqa: SLF001
            connection.execute("UPDATE paper_positions SET quantity = '2'")
        with pytest.raises(InvalidTransition, match="exceeds the simulated holding"):
            book(rig, "os", 5, "100.00", attempt="os-1")
    finally:
        rig.close()


def test_a_terminal_order_is_refused_by_the_state_guard_itself(tmp_path):
    rig = make_rig(tmp_path)
    try:
        prepare_buy(rig, "t1", quantity=2)
        rig.ledger.expire_simulated_order("t1", "x-t1", "day_close")
        with pytest.raises(InvalidTransition, match="cannot book a fill"):
            book(rig, "t1", 2, "100.00", attempt="t-1")
        buy_and_fill(rig, "t2", 2)
        with pytest.raises(InvalidTransition, match="cannot book a fill"):
            book(rig, "t2", 2, "100.00", attempt="t-2")
    finally:
        rig.close()
