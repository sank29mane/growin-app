"""The Mac's durable India latch file and its reset CLI (Phase 63-04, P-18, D-05, T-63-21, T-63-24).

Every value is synthetic; the files live in tmp dirs; nothing touches the real ledger, the
private/ directory or a broker.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

import india_limits_support as ils
from private_config import load_workspace_config
from risk_india import __main__ as cli
from risk_india.drawdown import MarkMissing, ResetRefused
from risk_india.exits import Position
from risk_india.state import (
    STATE_FILE_NAME,
    LatchStore,
    StateUnreadable,
    StateUnwritable,
    decode_state,
    encode_state,
    limits_from_config,
    read_state,
    serialize_state,
    state_path_for,
)

ROOT = Path(__file__).resolve().parents[2]
TCS = "NSE:CASH:TCS"
D1, D2, D3 = date(2026, 10, 8), date(2026, 10, 9), date(2026, 10, 12)


@pytest.fixture
def private(tmp_path):
    return ils.india_private_dir(tmp_path)


@pytest.fixture
def limits(private):
    return limits_from_config(load_workspace_config(private, "india", require_india_execution=True))


def make_store(tmp_path, limits, *, fills=False) -> LatchStore:
    return LatchStore(state_path_for(tmp_path / "india.sqlite3"), limits, has_fills=lambda: fills)


def pos(quantity=1, cost="100.00", key=TCS):
    return Position(key, key.rsplit(":", 1)[-1], quantity, Decimal(cost))


def halt_it(store, *, session=D1):
    """-10% of a 1000 peak: past the -8% halt, short of the -15% end."""
    return store.evaluate_session({}, session, cash=Decimal("900"), positions=[])


# ------------------------------------------------------------------ start and shape


def test_the_file_sits_beside_the_ledger_and_is_created_at_the_pilot_start(tmp_path, limits):
    store = make_store(tmp_path, limits)
    assert store.path == tmp_path / STATE_FILE_NAME
    assert not store.path.exists()
    state = store.load()
    assert state.peak == limits.capital_cap == Decimal("1000.00")
    assert state.last_session is None and not state.halt and not state.ended
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    document = json.loads(store.path.read_text())
    assert document["workspace"] == "india" and document["schema_version"] == 1
    assert len(document["sha256"]) == 64
    assert not list(tmp_path.glob("*.tmp"))


def test_the_state_round_trips_with_every_field_set(tmp_path, limits):
    store = make_store(tmp_path, limits)
    # A stop, a halt, an open exit and a reset record, so every field is exercised.
    store.evaluate_session({TCS: Decimal("80")}, D1, cash=Decimal("500"), positions=[pos()])
    state = store.load()
    assert state.stops and state.open_exits
    reloaded = decode_state(json.loads(json.dumps(encode_state(state))))
    assert reloaded == state
    assert read_state(store.path) == state


# --------------------------------------------------------------- failing closed


def test_absent_with_fills_is_unreadable_and_nothing_is_created(tmp_path, limits):
    store = make_store(tmp_path, limits, fills=True)
    with pytest.raises(StateUnreadable) as info:
        store.load()
    assert info.value.code == "state_unreadable" and info.value.reason == "absent_with_fills"
    assert not store.path.exists()


def test_an_unreadable_ledger_cannot_vouch_for_a_pilot_start(tmp_path, limits):
    def broken():
        raise RuntimeError("ledger closed")

    store = LatchStore(state_path_for(tmp_path / "x.sqlite3"), limits, has_fills=broken)
    with pytest.raises(StateUnreadable):
        store.load()
    assert not store.path.exists()


def _rewrite(path: Path, mutate) -> None:
    document = json.loads(path.read_text())
    mutate(document)
    path.write_text(json.dumps(document))
    os.chmod(path, 0o600)


@pytest.mark.parametrize(
    "name,mutate",
    [
        ("hash", lambda d: d["state"].update(peak="999999")),
        ("workspace", lambda d: d.update(workspace="uk")),
        ("version", lambda d: d.update(schema_version=2)),
        ("unknown_key", lambda d: d.update(extra=1)),
        ("missing_key", lambda d: d.pop("sha256")),
        ("state_unknown_key", lambda d: d["state"].update(extra=1)),
    ],
)
def test_a_tampered_or_foreign_file_is_unreadable(tmp_path, limits, name, mutate):
    store = make_store(tmp_path, limits)
    store.load()
    _rewrite(store.path, mutate)
    with pytest.raises(StateUnreadable):
        store.load()


def test_a_wrong_workspace_with_a_valid_hash_is_still_refused(tmp_path, limits):
    store = make_store(tmp_path, limits)
    store.load()
    document = json.loads(store.path.read_text())
    body = {"schema_version": 1, "workspace": "uk", "state": document["state"]}
    import hashlib

    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    ).hexdigest()
    store.path.write_text(json.dumps({**body, "sha256": digest}))
    os.chmod(store.path, 0o600)
    with pytest.raises(StateUnreadable) as info:
        store.load()
    assert info.value.reason == "workspace_mismatch"


@pytest.mark.parametrize(
    "content",
    [b"", b"{not json", b"[]", b'{"a": 1, "a": 2}', b"\xff\xfe", b'{"peak": 1.5}', b"x" * 2_000_000],
)
def test_garbage_is_unreadable(tmp_path, limits, content):
    store = make_store(tmp_path, limits)
    store.load()
    store.path.write_bytes(content)
    os.chmod(store.path, 0o600)
    with pytest.raises(StateUnreadable):
        store.load()


def test_a_float_in_the_state_is_refused(tmp_path, limits):
    store = make_store(tmp_path, limits)
    store.load()
    text = store.path.read_text().replace('"halt":false', '"halt":false,"x":1.5')
    store.path.write_text(text)
    os.chmod(store.path, 0o600)
    with pytest.raises(StateUnreadable):
        store.load()


def test_open_permissions_a_symlink_and_a_directory_are_refused(tmp_path, limits):
    store = make_store(tmp_path, limits)
    store.load()
    os.chmod(store.path, 0o644)
    with pytest.raises(StateUnreadable) as info:
        store.load()
    assert info.value.reason == "permissions_too_open"
    os.chmod(store.path, 0o600)
    real = tmp_path / "real.json"
    store.path.rename(real)
    store.path.symlink_to(real)
    with pytest.raises(StateUnreadable) as info:
        store.load()
    assert info.value.reason == "symlink"
    store.path.unlink()
    store.path.mkdir()
    with pytest.raises(StateUnreadable):
        store.load()


# -------------------------------------------------------------------- durability


def test_a_failed_rename_leaves_the_old_state_and_no_temp_file(tmp_path, limits, monkeypatch):
    store = make_store(tmp_path, limits)
    before = store.load()
    original = store.path.read_bytes()

    def fail(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(StateUnwritable):
        halt_it(store)
    monkeypatch.undo()
    assert store.path.read_bytes() == original
    assert store.load() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_the_file_and_its_directory_are_fsynced_before_the_write_returns(tmp_path, limits, monkeypatch):
    store = make_store(tmp_path, limits)
    store.load()
    synced = []
    real = os.fsync
    monkeypatch.setattr(os, "fsync", lambda fd: (synced.append(fd), real(fd))[1])
    halt_it(store)
    assert len(synced) >= 2  # the data file, then the directory entry of the rename


def test_a_state_survives_a_new_store_instance_and_a_restart(tmp_path, limits):
    halt_it(make_store(tmp_path, limits))
    again = make_store(tmp_path, limits).load()
    assert again.halt and again.drawdown == Decimal("-0.100000")


# ------------------------------------------------------------ evaluate_session


def test_evaluate_session_persists_halt_and_ended_and_builds_the_batches(tmp_path, limits):
    store = make_store(tmp_path, limits)
    halted = store.evaluate_session(
        {TCS: Decimal("92")}, D1, cash=Decimal("0"), positions=[pos(quantity=10, cost="100.00")]
    )  # equity 920: -8% exactly, inclusive
    assert halted.state.halt and not halted.state.ended
    assert [b.reason for b in halted.batches] == ["halve"]
    assert [(i.isin, i.quantity) for i in halted.batches[0].intents] == [(TCS, 5)]
    ended = store.evaluate_session(
        {TCS: Decimal("85")}, D2, cash=Decimal("0"), positions=[pos(quantity=10, cost="100.00")]
    )  # equity 850: -15% exactly
    assert ended.state.ended and [b.reason for b in ended.batches] == ["flatten"]
    assert make_store(tmp_path, limits).load().ended


def test_a_session_at_or_before_the_last_one_changes_nothing(tmp_path, limits):
    store = make_store(tmp_path, limits)
    halt_it(store, session=D2)
    stamp = store.path.read_bytes()
    result = store.evaluate_session({}, D1, cash=Decimal("1000"), positions=[])
    assert result.batches == () and store.path.read_bytes() == stamp


def test_a_missing_mark_raises_and_writes_nothing(tmp_path, limits):
    store = make_store(tmp_path, limits)
    store.load()
    stamp = store.path.read_bytes()
    with pytest.raises(MarkMissing):
        store.evaluate_session({}, D1, cash=Decimal("900"), positions=[pos()])
    assert store.path.read_bytes() == stamp


def test_latches_never_clear_on_recovery(tmp_path, limits):
    store = make_store(tmp_path, limits)
    halt_it(store, session=D1)
    recovered = store.evaluate_session({}, D2, cash=Decimal("1200"), positions=[])
    assert recovered.state.halt  # only an explicit reset clears it


def test_evaluate_session_on_a_deleted_file_with_fills_refuses(tmp_path, limits):
    with pytest.raises(StateUnreadable):
        make_store(tmp_path, limits, fills=True).evaluate_session({}, D1, cash=Decimal("1000"), positions=[])


# ------------------------------------------------------------- exit fills and reset


def test_a_stop_clears_only_when_the_exit_fill_leaves_nothing_held(tmp_path, limits):
    store = make_store(tmp_path, limits)
    store.evaluate_session({TCS: Decimal("80")}, D1, cash=Decimal("840"), positions=[pos(quantity=2, cost="200.00")])
    assert store.load().stops
    store.apply_exit_fill(TCS, sold_quantity=1, remaining_quantity=1)
    assert store.load().stops  # a partial fill leaves the latch
    store.apply_exit_fill(TCS, sold_quantity=1, remaining_quantity=0)
    cleared = store.load()
    assert not cleared.stops and not cleared.open_exits


def test_reset_halt_follows_the_drawdown_rule_and_never_touches_ended(tmp_path, limits):
    store = make_store(tmp_path, limits)
    halt_it(store, session=D1)
    with pytest.raises(ResetRefused):  # -10% is still at or below -8%: it would only latch again
        store.reset("halt", "operator")
    stamp = store.path.read_bytes()
    store.evaluate_session({}, D2, cash=Decimal("960"), positions=[])  # -4%, halt stays latched
    assert store.load().halt
    cleared = store.reset("halt", "operator")
    assert not cleared.halt and cleared.resets[-1].actor == "operator"
    assert stamp != store.path.read_bytes()
    with pytest.raises(ResetRefused):
        store.reset("ended", "operator")
    with pytest.raises(ResetRefused):
        store.reset("halt", "")  # a reset names its actor


def test_reset_ended_is_refused_and_the_file_is_untouched(tmp_path, limits):
    store = make_store(tmp_path, limits)
    store.evaluate_session({}, D1, cash=Decimal("800"), positions=[])
    stamp = store.path.read_bytes()
    with pytest.raises(ResetRefused):
        store.reset("ended", "operator")
    with pytest.raises(ResetRefused):
        store.reset("halt", "operator", rebase_halt_anchor=True)  # not while the pilot is ended
    assert store.path.read_bytes() == stamp


def test_a_reset_that_cannot_be_audited_does_not_happen(tmp_path, limits):
    store = make_store(tmp_path, limits)
    halt_it(store, session=D1)
    store.evaluate_session({}, D2, cash=Decimal("960"), positions=[])
    stamp = store.path.read_bytes()

    def broken_audit(_before, _after):
        raise OSError("audit log unwritable")

    with pytest.raises(OSError):
        store.reset("halt", "operator", audit=broken_audit)
    assert store.path.read_bytes() == stamp and store.load().halt


# ----------------------------------------------------------------------- the CLI


def prepared_store(tmp_path, private):
    """A halted state (and a later recovered session) at the CLI's default file position."""
    limits = limits_from_config(load_workspace_config(private, "india", require_india_execution=True))
    store = LatchStore(state_path_for(tmp_path / "india.sqlite3"), limits, has_fills=lambda: False)
    halt_it(store, session=D1)
    store.evaluate_session({}, D2, cash=Decimal("960"), positions=[])
    return store


def args(tmp_path, private, *extra):
    return ["--private-dir", str(private), "--ledger", str(tmp_path / "india.sqlite3"), *extra]


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, action, actor, details, *, workspace):
        self.calls.append((action, actor, details, workspace))


def test_reset_halt_with_confirm_clears_it_and_audits_in_workspace_india(tmp_path, private):
    store = prepared_store(tmp_path, private)
    audit = Recorder()
    code = cli.main(args(tmp_path, private, "reset", "--latch", "halt", "--confirm", "--actor", "sanket"), audit=audit)
    assert code == 0
    assert not store.load().halt
    assert [(a, actor, ws) for a, actor, _d, ws in audit.calls] == [("RISK_LATCH_RESET", "sanket", "india")]
    details = audit.calls[0][2]
    assert details["latch"] == "halt" and details["latches_before"] == ["halt"] and details["latches_after"] == []


def test_reset_without_confirm_changes_nothing(tmp_path, private):
    store = prepared_store(tmp_path, private)
    stamp = store.path.read_bytes()
    audit = Recorder()
    assert cli.main(args(tmp_path, private, "reset", "--latch", "halt"), audit=audit) == 2
    assert store.path.read_bytes() == stamp and audit.calls == []


def test_reset_ended_is_refused_with_an_audited_refusal(tmp_path, private):
    limits = limits_from_config(load_workspace_config(private, "india", require_india_execution=True))
    store = LatchStore(state_path_for(tmp_path / "india.sqlite3"), limits, has_fills=lambda: False)
    store.evaluate_session({}, D1, cash=Decimal("800"), positions=[])
    stamp = store.path.read_bytes()
    audit = Recorder()
    code = cli.main(args(tmp_path, private, "reset", "--latch", "ended", "--confirm", "--actor", "sanket"), audit=audit)
    assert code == 1
    assert store.path.read_bytes() == stamp
    assert [call[0] for call in audit.calls] == ["RISK_LATCH_RESET_REFUSED"]


def test_reset_on_a_still_halting_drawdown_needs_the_explicit_rebase_flag(tmp_path, private):
    limits = limits_from_config(load_workspace_config(private, "india", require_india_execution=True))
    store = LatchStore(state_path_for(tmp_path / "india.sqlite3"), limits, has_fills=lambda: False)
    halt_it(store, session=D1)  # -10%, no recovery
    audit = Recorder()
    base = ["reset", "--latch", "halt", "--confirm", "--actor", "sanket"]
    assert cli.main(args(tmp_path, private, *base), audit=audit) == 1
    assert store.load().halt
    assert cli.main(args(tmp_path, private, *base, "--rebase-halt-anchor"), audit=audit) == 0
    cleared = store.load()
    assert not cleared.halt and cleared.halt_anchor == Decimal("900")
    assert [call[0] for call in audit.calls] == ["RISK_LATCH_RESET_REFUSED", "RISK_LATCH_RESET"]
    assert audit.calls[1][2]["rebase_halt_anchor"] is True


def test_reset_stop_clears_one_key(tmp_path, private):
    limits = limits_from_config(load_workspace_config(private, "india", require_india_execution=True))
    store = LatchStore(state_path_for(tmp_path / "india.sqlite3"), limits, has_fills=lambda: False)
    store.evaluate_session({TCS: Decimal("80")}, D1, cash=Decimal("900"), positions=[pos()])
    audit = Recorder()
    assert cli.main(args(tmp_path, private, "reset", "--latch", "stop", "--isin", TCS, "--confirm", "--actor", "s"), audit=audit) == 0
    assert not store.load().stops
    assert audit.calls[0][2]["isin"] == TCS


def test_the_cli_writes_a_real_audit_entry_through_log_audit(tmp_path, private, monkeypatch):
    from utils import audit_log

    logger = audit_log.AuditLogger(str(tmp_path / "audit.log"))
    monkeypatch.setattr(audit_log, "_audit_logger", logger)
    prepared_store(tmp_path, private)
    assert cli.main(args(tmp_path, private, "reset", "--latch", "halt", "--confirm", "--actor", "sanket")) == 0
    lines = [json.loads(line) for line in (tmp_path / "audit.log").read_text().splitlines() if line.strip()]
    assert len(lines) == 1
    assert (lines[0]["action"], lines[0]["actor"], lines[0]["workspace"]) == ("RISK_LATCH_RESET", "sanket", "india")
    assert logger.verify_integrity()["status"] == "success"


def test_a_failing_audit_write_stops_the_reset(tmp_path, private):
    store = prepared_store(tmp_path, private)

    def dead_audit(*_a, **_k):
        raise OSError("audit unwritable")

    with pytest.raises(OSError):
        cli.main(args(tmp_path, private, "reset", "--latch", "halt", "--confirm", "--actor", "s"), audit=dead_audit)
    assert store.load().halt


def test_the_cli_needs_valid_india_config(tmp_path, private):
    (private / "india" / "execution.json").unlink()
    assert cli.main(args(tmp_path, private, "status"), audit=Recorder()) == 2


def test_the_cli_refuses_to_reset_a_deleted_file_with_fills(tmp_path, private):
    from execution import ExecutionLedger

    with ExecutionLedger(tmp_path / "india.sqlite3", workspace="india") as ledger:
        ils.seed_position(ledger, TCS, 1, "100.00")
    audit = Recorder()
    code = cli.main(args(tmp_path, private, "reset", "--latch", "halt", "--confirm", "--actor", "s"), audit=audit)
    assert code == 1
    assert not (tmp_path / STATE_FILE_NAME).exists()  # it did not recreate a clean state
    assert [call[0] for call in audit.calls] == ["RISK_LATCH_RESET_REFUSED"]


def test_python_dash_m_risk_india_status_runs(tmp_path, private):
    prepared_store(tmp_path, private)
    result = subprocess.run(
        [sys.executable, "-m", "risk_india", *args(tmp_path, private, "status")],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(ROOT / "backend")},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "latches: halt" in result.stdout


def test_the_serialised_state_has_no_float_and_a_stable_byte_form(tmp_path, limits):
    store = make_store(tmp_path, limits)
    state = store.load()
    assert serialize_state(state) == serialize_state(decode_state(encode_state(state)))
    # Decimals are strings: the only JSON numbers in the file are whole-share integers.
    assert b'"peak":"1000.00"' in serialize_state(state)
