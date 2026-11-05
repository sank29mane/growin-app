"""64-09 Task B: the production shadow runtime, built from real files over an in-process VM.

Real: research store, frozen dataset, seeded session store, private config, pinned calendar, evidence store, simulation
ledger, latch, admission guard, VM order app (kill flag ``blocked``). Scripted: the four relay READ routes and the signal
step. Nothing reaches a broker, ``private/``, the real VM, GCP or NSE; nothing calls authorize or halt.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import shutil
import sqlite3
import sys
from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

import shadow_india.runner as runner_module
import test_pilot_data_crosscheck as cc
import test_shadow_india_release_cli as rc
from brokers.breeze_gateway.config import ConfigError
from brokers.breeze_gateway.errors import (
    OrdersBuildInvalid,
    OrdersBuildRouteUnavailable,
    OrdersBuildUnstamped,
    TunnelError,
    UpstreamError,
)
from brokers.breeze_gateway.orders import OrdersClient, OrdersProtocolError
from execution.ledger import ExecutionLedger, SimulationProbeRefused, probe_simulation_ledger
from execution.models import Workspace
from pilot_data.store import DB_FILE_NAME, PilotDataStore
from shadow_india import __main__ as cli
from shadow_india import policy
from shadow_india import runtime as runtime_module
from shadow_india.admission import simulation_init
from shadow_india.evidence import LOG_NAME, TORN_DIR, EvidenceStore, read_records
from shadow_india.portfolio import Instrument, PortfolioError
from shadow_runtime_support import (
    AS_OF, CANBK_CODE, CANBK_ISIN, CANBK_SYMBOL, FORBIDDEN, KEY_ID, RUN, SESSION, STEADY_CODE, STEADY_ISIN,
    STEADY_SYMBOL, WINDOW_START, build_env, build_seeded, calendar_doc, evaluated, master_zip, sha,
    tree_state, write_json,
)
from test_gateway_orders_key import swap_key
from test_shadow_india_runner import BUILD
from test_gateway_orders_routes import OTHER
from test_shadow_india_admission import frozen_service_clock as admission_clock  # noqa: F401  (fixture)
from test_shadow_india_release_cli import planted, world  # noqa: F401  (fixtures for the re-run rows)
from test_shadow_india_runner import frozen_service_clock, metadata  # noqa: F401  (fixtures)

EXIT_REFUSED = 1
SEED = Decimal("50000")


# --- fixtures ---------------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def seeded(tmp_path_factory):
    return build_seeded(tmp_path_factory.mktemp("seeded"))


@pytest.fixture(autouse=True)
def runner_seams(monkeypatch):
    """The signal step and the 64-08 reads are fixed: the reference and the relay are what this file exercises."""

    monkeypatch.setattr(
        runner_module, "latest_index_list", lambda store, name, *, fetched_by=None: SimpleNamespace(list_name=name)
    )
    monkeypatch.setattr(runner_module, "band_for_upcoming_session", lambda *a, **k: SimpleNamespace(status="fixed"))
    monkeypatch.setattr(runner_module, "previous_session", lambda calendar, session: AS_OF)


@pytest.fixture()
def env(tmp_path, seeded, metadata, monkeypatch):  # noqa: F811
    built = build_env(tmp_path, seeded, metadata)
    built.script.result = evaluated("CANBK", "STEADYCO")
    monkeypatch.setattr(runner_module, "evaluate_session", built.script)
    monkeypatch.setattr(runner_module, "held_names_quarantined", built.script.held_names_quarantined)
    return built


class Spies:
    """Every writable open the runtime makes, every relay call, and the ``forbidden=`` each evidence open was given."""

    def __init__(self) -> None:
        self.evidence: list[tuple[str, tuple]] = []
        self.ledgers: list[tuple] = []
        self.stores: list[dict] = []
        self.runs: list[tuple] = []
        self.link: list[str] = []

    def clear(self) -> None:
        for held in (self.evidence, self.ledgers, self.stores, self.runs, self.link):
            held.clear()

    @property
    def writable_opens(self) -> int:
        writable_stores = [s for s in self.stores if not s.get("read_only")]
        return len(self.evidence) + len(self.ledgers) + len(self.runs) + len(writable_stores)


@pytest.fixture()
def spies(monkeypatch, env):
    seen = Spies()
    for name in ("open", "create"):
        original = getattr(EvidenceStore, name)

        def spy(*args, _name=name, _original=original, **kwargs):
            seen.evidence.append((_name, tuple(kwargs.get("forbidden") or ())))
            return _original(*args, **kwargs)

        monkeypatch.setattr(EvidenceStore, name, staticmethod(spy))
    real_ledger = runtime_module.open_simulation_ledger

    def ledger_spy(*args, **kwargs):
        seen.ledgers.append(args)
        return real_ledger(*args, **kwargs)

    monkeypatch.setattr(runtime_module, "open_simulation_ledger", ledger_spy)
    real_open_run = runtime_module.open_run

    def run_spy(*args, **kwargs):
        seen.runs.append(args)
        return real_open_run(*args, **kwargs)

    monkeypatch.setattr(runtime_module, "open_run", run_spy)
    real_store = runtime_module.PilotDataStore

    def store_spy(*args, **kwargs):
        seen.stores.append(dict(kwargs))
        return real_store(*args, **kwargs)

    monkeypatch.setattr(runtime_module, "PilotDataStore", store_spy)

    class CountingLink:
        def __init__(self, inner):
            self._inner = inner

        def client(self):
            seen.link.append("client")
            return self._inner.client()

        def orders(self):
            seen.link.append("orders")
            return self._inner.orders()

        def close(self):
            seen.link.append("close")

    env.link = CountingLink(env.link)
    return seen


def go(env, capsys, command, *extra, factory=True, **kwargs):
    runtime_factory = env.factory() if factory else None
    code = cli.main(env.argv(command, *extra), runtime_factory=runtime_factory, **kwargs)
    out = capsys.readouterr().out.strip()
    return code, (json.loads(out.splitlines()[-1]) if out else {})


def opened(env, capsys):
    code, out = go(env, capsys, "open-run")
    assert code == 0, out
    return out


def audit(env):
    return [entry.raw for entry in env.vm.orders.audit_all()]


def everything(env):
    return {
        name: tree_state(path)
        for name, path in (("evidence", env.root), ("sim", env.sim), ("store", env.session_store))
    }


def store_sql(env, statement, *params):
    connection = duckdb.connect(str(env.session_store / DB_FILE_NAME))
    try:
        connection.execute(statement, list(params))
    finally:
        connection.close()


def decide_args(env, session=SESSION):
    return SimpleNamespace(
        command="decide", root=env.root, run_id=RUN, session=session, private_dir=env.private, forbidden_source=None
    )


# --- the end to end world -----------------------------------------------------------------------------------------


def test_open_run_decide_close_reach_the_vm_kill_flag_and_nothing_else(env, capsys):
    out = opened(env, capsys)
    assert out["opened"]["run_id"] == RUN and out["opened"]["run_start_session"] == SESSION.isoformat()
    code, decided = go(env, capsys, "decide")
    assert code == 0 and decided["status"] == "done" and [s for _i, s in decided["intents"]] == ["vm_decided"] * 2
    code, closed = go(env, capsys, "close")
    assert code == 0 and closed["outcome"] == "ok"
    entries = audit(env)
    assert [e["codes"] for e in entries] == [["kill_switch"], ["kill_switch"]]
    assert {e["decision"] for e in entries} == {"REFUSED"} and {e["kill"] for e in entries} == {"blocked"}
    assert env.vm.http.order_calls() == [("POST", "/v1/orders/intents")] * 2
    assert [r["code"] for r in read_records(env.root, RUN) if r["kind"] == "vm_decision"] == ["kill_switch"] * 2
    assert env.ledger_path().is_file() and env.latch_path().is_file()


def test_the_runtime_only_reaches_the_read_routes_and_the_mint(env, capsys):
    opened(env, capsys)
    go(env, capsys, "decide")
    go(env, capsys, "close")
    gets = {path for method, path in env.vm.http.calls if method == "GET"}
    assert gets <= {"/v1/orders/audit", "/v1/orders/build", "/v1/orders/key"}  # the VM leg; the four read routes are scripted above it
    assert set(env.reads.paths) <= {"/v1/health", "/v1/quotes", "/v1/security-master", "/v1/bars/daily"}
    assert {(m, p) for m, p in env.vm.http.calls if m != "GET"} == {("POST", "/v1/orders/intents")}
    banned = ("authorize", "halt", "preview", "login", "session", "touch")
    assert not [p for _m, p in env.vm.http.calls if any(word in p for word in banned)]
    assert env.vm.stack.transport.non_get() == [] and set(env.vm.metadata.methods) <= {"GET"}
    created = [str(p.relative_to(env.tmp)) for p in env.tmp.rglob("*") if p.is_file()]
    assert not [c for c in created if "paper" in c or "live" in c]
    assert {p.parent.name for p in env.sim.rglob("ledger.sqlite3")} == {RUN}
    assert {p.relative_to(env.sim).parts[0] for p in env.sim.rglob("*")} == {"india-shadow"}


def test_a_name_whose_symbol_differs_from_its_stock_code_reaches_the_vm_mint_and_gets_the_423(env, capsys):
    """CANBK trades as CANBAN at Breeze. Real ``RelayQuoteSamples`` carry the quote of the Breeze code; the Mac guard
    checks it against the instrument's mapped code, so the order is admitted and the VM mint answers the kill flag."""

    assert CANBK_SYMBOL != CANBK_CODE
    opened(env, capsys)
    code, decided = go(env, capsys, "decide")
    assert code == 0 and [s for _i, s in decided["intents"]] == ["vm_decided"] * 2
    assert not [r for r in read_records(env.root, RUN) if r["kind"] == "denied"]
    assert env.vm.http.order_calls() == [("POST", "/v1/orders/intents")] * 2
    entries = audit(env)
    assert [e["codes"] for e in entries] == [["kill_switch"], ["kill_switch"]]  # the typed 423, not a Mac denial
    assert {e["stock_code"] for e in entries} == {CANBK_CODE, STEADY_CODE}  # the VM intent carries the Breeze codes
    assert set(env.reads.quote_calls) == {CANBK_CODE, STEADY_CODE}
    assert not {CANBK_SYMBOL, STEADY_SYMBOL} & {e["stock_code"] for e in entries}


# --- phase 1: a refusal leaves every byte as it was ----------------------------------------------------------------------


def _drop(name):
    return lambda env: (env.private / "india" / name).unlink()


def _runtime(**changes):
    return lambda env: env.write_runtime(**changes)


def _break_strategy(env):
    body = json.loads((env.private / "india" / "strategy.json").read_text())
    body["params_sha256"] = "0" * 64
    write_json(env.private / "india" / "strategy.json", body)


def _break_dataset(env):
    victim = next(p for p in sorted(env.dataset.rglob("*")) if p.is_file())
    victim.chmod(0o600)
    victim.write_bytes(victim.read_bytes() + b"x")


def _garbage_store(env):
    (env.session_store / DB_FILE_NAME).write_bytes(b"not a store")


def _bad_calendar_sha(env):
    env.write_runtime(calendar_sha256="e" * 64)


def _break_seed_report(env):
    report = env.session_store / "seed_report.json"
    report.chmod(0o600)
    report.write_bytes(b"{")


PHASE1 = [
    ("forbidden_missing", _drop("shadow_forbidden.json"), "forbidden_literals_refused:"),
    ("runtime_missing", _drop("shadow_runtime.json"), "runtime_config_refused:SHADOW_RUNTIME_MISSING"),
    ("strategy", _break_strategy, ""),
    ("execution", _drop("execution.json"), "execution_config_refused:"),
    ("calendar_unpinned", lambda env: env.calendar.unlink(), "calendar_unpinned"),
    ("calendar_pin", _bad_calendar_sha, "calendar_pin_mismatch"),
    ("dataset", _break_dataset, "dataset_refused:"),
    ("store_missing", _runtime(session_store_root="/nonexistent-store-root"), "session_store_missing"),
    ("store_garbage", _garbage_store, "session_store_refused"),
    ("seed_report", _break_seed_report, "seed_refused:"),
    ("window_not_seeded", _runtime(window_start="2031-01-02"), "window_start_not_seeded"),
]


@pytest.mark.parametrize(("name", "mutate", "code"), PHASE1, ids=[row[0] for row in PHASE1])
def test_a_phase_1_refusal_has_a_closed_code_and_leaves_every_byte_and_every_open_untouched(
    env, capsys, spies, name, mutate, code
):
    opened(env, capsys)
    mutate(env)
    spies.clear()
    paths_before, calls_before = len(env.reads.paths), len(env.vm.http.calls)
    before = everything(env)
    exit_code, out = go(env, capsys, "decide")
    assert exit_code == EXIT_REFUSED and out["refused"].startswith(code) and out["refused"], out
    assert everything(env) == before  # no byte moved, no file made, no sidecar
    assert spies.writable_opens == 0  # B3: no writable open before every check passed
    assert spies.link == [] and len(env.reads.paths) == paths_before and len(env.vm.http.calls) == calls_before  # B5
    assert all(s.get("read_only") for s in spies.stores)
    text = json.dumps(out)
    assert str(env.tmp) not in text and KEY_ID not in text


def test_the_seed_must_match_the_dataset_the_report_and_be_a_session_store(env, capsys, spies):
    opened(env, capsys)
    spies.clear()
    store_sql(env, "UPDATE session_store_seeds SET dataset_sha256 = ?", "f" * 64)
    assert go(env, capsys, "decide") == (EXIT_REFUSED, {"refused": "seed_dataset_mismatch"})  # B8
    store_sql(env, "UPDATE session_store_seeds SET dataset_sha256 = ?, seed_report_sha256 = ?", sha("x"), "a" * 64)
    assert go(env, capsys, "decide") == (EXIT_REFUSED, {"refused": "seed_report_unrecorded"})
    store_sql(env, "DROP TABLE session_store_seeds")
    assert go(env, capsys, "decide") == (EXIT_REFUSED, {"refused": "not_a_session_store"})
    assert spies.link == [] and spies.writable_opens == 0


def test_the_window_start_must_equal_the_seeded_one_in_the_report_and_in_the_store_row(env, capsys, spies):
    """The seed was cut at one window start. A config inside the seeded range, or a store row that records another
    start, reads a different store than the plan names: equality, not a range."""

    opened(env, capsys)
    seeded_from = date.fromisoformat(env.runtime_file()["window_start"])
    assert seeded_from == WINDOW_START
    spies.clear()
    for moved in (seeded_from + timedelta(days=1), seeded_from - timedelta(days=1)):  # one day inside, one day before
        env.write_runtime(window_start=moved.isoformat())
        assert go(env, capsys, "decide") == (EXIT_REFUSED, {"refused": "window_start_not_seeded"}), moved
    env.write_runtime(window_start=seeded_from.isoformat())
    store_sql(env, "UPDATE session_store_seeds SET window_start = window_start + INTERVAL 1 DAY")  # the row says another start
    assert go(env, capsys, "decide") == (EXIT_REFUSED, {"refused": "window_start_not_seeded"})
    # the config and the store row agree on a day inside the range, but the seed report names the earlier start
    env.write_runtime(window_start=(seeded_from + timedelta(days=1)).isoformat())
    assert go(env, capsys, "decide") == (EXIT_REFUSED, {"refused": "window_start_not_seeded"})
    assert spies.link == [] and spies.writable_opens == 0


def test_an_interrupted_evidence_tail_survives_a_refusal_that_a_writable_open_would_have_archived(env, capsys, spies):
    """The preflight is read-only: a wrong calendar pin or an unreadable seed report never gives
    ``EvidenceStore.open`` the chance to archive the torn tail."""

    opened(env, capsys)
    report_bytes = (env.session_store / "seed_report.json").read_bytes()
    log = env.evidence_dir() / LOG_NAME
    log.write_bytes(log.read_bytes() + b'{"kind":"pa')
    torn = env.evidence_dir() / TORN_DIR
    listed = sorted(p.name for p in torn.iterdir()) if torn.exists() else []
    before = log.read_bytes()
    for mutate, expected in ((_bad_calendar_sha, "calendar_pin_mismatch"), (_break_seed_report, "seed_refused:")):
        mutate(env)
        spies.clear()
        code, out = go(env, capsys, "decide")
        assert code == EXIT_REFUSED and out["refused"].startswith(expected), out
        assert log.read_bytes() == before and before.endswith(b'{"kind":"pa')
        assert (sorted(p.name for p in torn.iterdir()) if torn.exists() else []) == listed == []
        assert not list(env.evidence_dir().glob("*aborted*")) and spies.evidence == []
        (env.session_store / "seed_report.json").chmod(0o600)
        (env.session_store / "seed_report.json").write_bytes(report_bytes)
        env.write_runtime(calendar_sha256=hashlib.sha256(env.calendar.read_bytes()).hexdigest())


# --- the instruments ----------------------------------------------------------------------------------------------


def _member(isin, symbol, code, series="EQ"):
    return SimpleNamespace(anchor_isin=isin, nse_symbol=symbol, stock_code=code, anchor_series=series)


@pytest.mark.parametrize(
    ("members", "code"),
    [
        ([_member(CANBK_ISIN, "CANBK", "SAME"), _member(STEADY_ISIN, "STEADYCO", "SAME")], "instrument_mapping_conflict"),
        ([_member(CANBK_ISIN, "CANBK", "A"), _member(CANBK_ISIN, "STEADYCO", "B")], "instrument_mapping_conflict"),
        ([_member(CANBK_ISIN, "CANBK", "A"), _member(STEADY_ISIN, "CANBK", "B")], "instrument_mapping_conflict"),
        ([_member("not-an-isin", "CANBK", "A")], "instrument_invalid"),
        ([_member(CANBK_ISIN, "CANBK", "")], "instrument_invalid"),
    ],
    ids=["same_code", "same_isin", "same_symbol", "bad_isin", "empty_code"],
)
def test_an_instrument_mapping_conflict_is_refused_before_any_file_is_made(env, capsys, monkeypatch, members, code):
    """B13: dropping the conflict check lets a conflicting universe through and the run would create a ledger."""

    def fake(store, root, manifest, window_start):
        return SimpleNamespace(targets=SimpleNamespace(members=members))

    monkeypatch.setattr(runtime_module, "_preflight_seed", fake)
    before = everything(env)
    assert go(env, capsys, "open-run") == (EXIT_REFUSED, {"refused": code})
    assert everything(env) == before and not env.ledger_path().exists() and not env.evidence_dir().exists()


def test_the_live_pins_that_cannot_be_read_are_refused(env, capsys, monkeypatch):
    class Broken(runner_module.LiveTablePins):
        def cost_sha256(self):
            raise OSError("table unreadable")

    monkeypatch.setattr(runtime_module, "LiveTablePins", Broken)
    assert go(env, capsys, "open-run") == (EXIT_REFUSED, {"refused": "pins_unavailable"})
    assert not env.evidence_dir().exists() and not env.ledger_path().exists()


# --- the run and the simulation ledger -------------------------------------------------------------------------------------


def test_open_run_refuses_an_existing_run_or_ledger_and_the_others_refuse_a_missing_one(env, capsys):
    assert go(env, capsys, "decide") == (EXIT_REFUSED, {"refused": "run_not_opened"})
    opened(env, capsys)
    before = everything(env)
    assert go(env, capsys, "open-run") == (EXIT_REFUSED, {"refused": "run_exists"})
    assert everything(env) == before
    first = env.root
    env.root = first.parent / "evidence2"  # a ledger with no run: open-run refuses it and never adopts it
    assert go(env, capsys, "open-run") == (EXIT_REFUSED, {"refused": "simulation_ledger_exists"})
    assert not env.root.exists()
    env.root = first
    env.ledger_path().unlink()
    assert go(env, capsys, "decide") == (EXIT_REFUSED, {"refused": "simulation_ledger_missing"})


def test_a_restart_cannot_move_the_run_start_session(env, capsys):
    opened(env, capsys)
    later = SESSION + timedelta(days=1)
    pre = runtime_module.preflight(decide_args(env, later), relay=False, calendar=False)
    assert pre.run_start_session == SESSION and pre.session == later
    env.write_runtime(window_start="2025-03-04")  # a moved window is not the seeded window: refused in the seed check
    assert go(env, capsys, "decide") == (EXIT_REFUSED, {"refused": "window_start_not_seeded"})


def test_a_run_whose_recorded_window_is_not_the_configs_is_run_start_mismatch(env, capsys):
    """The ``run_start`` record binds the window; the seed check refuses a moved config first, so this reads it directly."""

    opened(env, capsys)
    started, _record = runtime_module._run_start(env.root, RUN, WINDOW_START)
    assert started == SESSION
    with pytest.raises(runtime_module.RuntimeRefused) as moved:
        runtime_module._run_start(env.root, RUN, WINDOW_START + timedelta(days=1))
    assert moved.value.code == "run_start_mismatch"


def _sim_ledger(path: Path, *, run=RUN, seed=SEED):
    """A simulation ledger written the way ``open_simulation_ledger`` writes one, at ``path`` (any ``india-shadow/<run>``)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    simulation = simulation_init(run, seed)
    return ExecutionLedger(
        path, workspace=Workspace.INDIA, require_approval=False, venue=simulation.binding, simulation=simulation
    )


def _wrong_ledger(path: Path, case: str) -> None:
    """Plant at ``path`` (``.../india-shadow/<RUN>/ledger.sqlite3``) a ledger the run must not adopt."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if case == "empty_file":
        sqlite3.connect(path).close()
    elif case == "other_seed":
        _sim_ledger(path, seed=Decimal("1234")).close()
    elif case == "other_run":  # a ledger born for another run, moved under this run's directory
        elsewhere = path.parents[2] / "elsewhere" / "india-shadow" / "other-run" / "ledger.sqlite3"
        _sim_ledger(elsewhere, run="other-run").close()
        shutil.copy(elsewhere, path)
    elif case == "india_plain":  # an India ledger that carries no simulation venue binding
        ExecutionLedger(path, workspace=Workspace.INDIA).close()
    elif case == "uk_workspace":
        ExecutionLedger(path.with_name("uk.sqlite3"), workspace=Workspace.UK).close()
        shutil.copy(path.with_name("uk.sqlite3"), path)
        path.with_name("uk.sqlite3").unlink()
    else:
        path.write_text("not a database")


CASES = {  # the file planted -> the reason the probe gives, the same check the writer open fails on
    "other_seed": "simulation",
    "other_run": "venue_mismatch",
    "india_plain": "venue_mismatch",
    "uk_workspace": "workspace_mismatch",
    "plain_file": "unreadable",
}


@pytest.mark.parametrize("case", list(CASES))
def test_the_probe_refuses_what_the_ledger_constructor_refuses_and_writes_nothing(tmp_path, case):
    """B16: one set of module functions backs both, so a ledger the writer open would refuse is refused read-only."""

    path = tmp_path / "india-shadow" / RUN / "ledger.sqlite3"
    _wrong_ledger(path, case)
    before = path.read_bytes()
    with pytest.raises(SimulationProbeRefused) as probed:
        probe_simulation_ledger(path, workspace=Workspace.INDIA, simulation=simulation_init(RUN, SEED))
    assert probed.value.reason == CASES[case] and path.read_bytes() == before
    with pytest.raises(Exception):
        _sim_ledger(path)  # the writer open refuses the very same file


def test_the_probe_refuses_a_fresh_file_the_writer_open_would_have_adopted_and_writes_nothing(tmp_path):
    """The one place the two differ on purpose: phase 1 never adopts a ledger, so a file with no simulation marker is
    ``not_pinned`` here, where the writer open would have created the marker."""

    path = tmp_path / "india-shadow" / RUN / "ledger.sqlite3"
    path.parent.mkdir(parents=True)
    sqlite3.connect(path).close()
    before = tree_state(tmp_path)
    with pytest.raises(SimulationProbeRefused) as probed:
        probe_simulation_ledger(path, workspace=Workspace.INDIA, simulation=simulation_init(RUN, SEED))
    assert probed.value.reason == "not_pinned" and tree_state(tmp_path) == before


def test_the_probe_accepts_the_ledger_the_run_wrote_and_leaves_it_untouched(env, capsys):
    opened(env, capsys)
    before = tree_state(env.ledger_path().parent)
    probe_simulation_ledger(env.ledger_path(), workspace=Workspace.INDIA, simulation=simulation_init(RUN, SEED))
    assert tree_state(env.ledger_path().parent) == before


@pytest.mark.parametrize("case", ["other_seed", "uk_workspace", "other_run"])
def test_a_wrong_ledger_is_refused_in_phase_1_before_the_relay_is_called(env, capsys, spies, case):
    opened(env, capsys)
    path = env.ledger_path()
    for leftover in path.parent.iterdir():
        leftover.unlink()
    _wrong_ledger(path, case)
    spies.clear()
    calls = len(env.vm.http.calls)
    code, out = go(env, capsys, "decide")
    assert code == EXIT_REFUSED and out["refused"].startswith("simulation_ledger_refused:"), out
    assert spies.link == [] and spies.writable_opens == 0 and len(env.vm.http.calls) == calls


# --- the reference prices -------------------------------------------------------------------------------------------------


@pytest.fixture()
def reference(env, seeded):
    members = {m.anchor_isin: m for m in seeded["report"].targets.members}
    store = PilotDataStore(env.session_store, workspace="india")
    try:
        yield runtime_module.SessionStoreReference(store, members, 3)
    finally:
        store.close()


def _bar(isin, series, close, value="100000"):
    return SimpleNamespace(isin=isin, series=series, close=Decimal(close), traded_value=Decimal(value))


def test_the_reference_reads_the_raw_close_from_the_store_and_records_a_missing_day(reference):
    closes = reference.closes(cc.D4, [CANBK_ISIN, STEADY_ISIN])
    assert closes == {CANBK_ISIN: Decimal("120"), STEADY_ISIN: Decimal("100")} and reference.gaps == {}
    assert reference.closes(date(2025, 3, 8), [CANBK_ISIN]) == {} and reference.gaps == {CANBK_ISIN: "close_missing"}
    assert reference.closes(cc.D4, ["INE999Z01010"]) == {} and reference.gaps["INE999Z01010"] == "close_missing"


def test_a_block_buyback_or_t0_bar_of_the_same_isin_is_never_a_close(reference, monkeypatch):
    """B10: without the series filter the extra rows are a conflict or replace the close."""

    bars = [_bar(CANBK_ISIN, "EQ", "120"), _bar(CANBK_ISIN, "BL", "999"), _bar(CANBK_ISIN, "T0", "1"), _bar(CANBK_ISIN, "BO", "5")]
    monkeypatch.setattr(runtime_module, "primary_bars_on", lambda store, day: bars)
    assert reference.closes(AS_OF, [CANBK_ISIN]) == {CANBK_ISIN: Decimal("120")} and reference.gaps == {}
    monkeypatch.setattr(runtime_module, "primary_bars_on", lambda store, day: [_bar(CANBK_ISIN, "BL", "999")])
    assert reference.closes(AS_OF, [CANBK_ISIN]) == {} and reference.gaps == {CANBK_ISIN: "close_missing"}


def test_two_regular_rows_for_one_name_are_a_conflict_and_never_the_first_row(reference, monkeypatch):
    """B11: taking the first row would price the name at 120 and say nothing."""

    two = [_bar(CANBK_ISIN, "EQ", "120"), _bar(CANBK_ISIN, "EQ", "125")]
    monkeypatch.setattr(runtime_module, "primary_bars_on", lambda store, day: two)
    assert reference.closes(AS_OF, [CANBK_ISIN]) == {} and reference.gaps == {CANBK_ISIN: "close_conflict"}


def test_a_row_dated_after_the_session_changes_neither_the_close_nor_the_adv(env, seeded):
    """The look-ahead shock: a poisoned later date leaves every read for the earlier session as it was."""

    members = {m.anchor_isin: m for m in seeded["report"].targets.members}
    with PilotDataStore(env.session_store, workspace="india") as store:
        ref = runtime_module.SessionStoreReference(store, members, 3)
        before = (ref.closes(cc.D4, [CANBK_ISIN]), ref.adv(CANBK_ISIN, cc.D4))
    store_sql(env, "UPDATE bhavcopy_bars SET close = close * 1000, traded_value = traded_value * 1000 WHERE trade_date > ?", cc.D4)
    with PilotDataStore(env.session_store, workspace="india") as store:
        ref = runtime_module.SessionStoreReference(store, members, 3)
        assert (ref.closes(cc.D4, [CANBK_ISIN]), ref.adv(CANBK_ISIN, cc.D4)) == before
        assert ref.closes(cc.D5, [CANBK_ISIN])[CANBK_ISIN] > 100000  # the shock is real: the later day did move


def test_adv_is_the_median_traded_value_over_the_window_and_none_when_short_or_conflicted(reference, monkeypatch):
    values = {date(2025, 3, 3): "300", date(2025, 3, 4): "100", date(2025, 3, 5): "200", date(2025, 3, 6): "900"}
    monkeypatch.setattr(
        runtime_module, "primary_bars_on",
        lambda store, day: [_bar(CANBK_ISIN, "EQ", "1", values[day])] if day in values else [],
    )
    monkeypatch.setattr(reference._store, "query", lambda sql, params=None: [(d,) for d in sorted(values, reverse=True)])
    assert reference.adv(CANBK_ISIN, date(2025, 3, 6)) == Decimal("200")  # the median of 900, 200 and 100
    reference._adv_window = 9
    assert reference.adv(CANBK_ISIN, date(2025, 3, 6)) is None  # too few rows
    reference._adv_window = 3
    both = [_bar(CANBK_ISIN, "EQ", "1"), _bar(CANBK_ISIN, "EQ", "2")]
    monkeypatch.setattr(runtime_module, "primary_bars_on", lambda store, day: both)
    assert reference.adv(CANBK_ISIN, date(2025, 3, 6)) is None  # a conflicted day gives no ADV
    assert reference.adv("INE999Z01010", date(2025, 3, 6)) is None


def test_structural_actions_name_only_a_split_or_bonus_effective_on_the_session(reference):
    assert reference.structural_actions(cc.D3, [CANBK_ISIN, STEADY_ISIN]) == {CANBK_ISIN: "split"}
    assert reference.structural_actions(cc.D4, [CANBK_ISIN, STEADY_ISIN]) == {}


def test_a_gap_pauses_an_unfrozen_held_name_but_a_frozen_one_keeps_its_carried_mark(
    tmp_path, seeded, admission_clock  # noqa: F811
):
    """B14: the reference omits a name it has no close for. The portfolio then pauses ``mark_missing`` for a held name,
    except one frozen by an open ``unresolved_corporate_action`` pause, which keeps its carried mark."""

    w = rc.make_world(tmp_path, holding=True)
    try:
        w.plant_latch()
        members = {m.anchor_isin: m for m in seeded["report"].targets.members}
        store = tmp_path / "store"
        shutil.copytree(seeded["session"], store)
        with PilotDataStore(store, workspace="india") as session_store:
            ref = runtime_module.SessionStoreReference(session_store, members, 3)
            closes = ref.closes(date(2025, 3, 8), [rc.RELIANCE])  # no row on a Saturday: a gap, not a guess
            assert closes == {} and ref.gaps == {rc.RELIANCE: "close_missing"}
        portfolio = w.build_portfolio()
        with pytest.raises(PortfolioError) as caught:
            portfolio.mark_session(rc.D1, closes)
        assert caught.value.code == "mark_missing"
        missing = [r for r in read_records(w.root, w.run_id) if r["kind"] == "pause" and r.get("event") == "missing_bar"]
        assert [r["scope"] for r in missing] == [f"isin:{rc.RELIANCE}"]  # unfrozen: paused, named, never guessed
        w.close()
        frozen = tmp_path / "frozen"
        frozen.mkdir()
        w2 = rc.make_world(frozen, holding=True)
        try:
            w2.plant_latch()
            w2.raise_event(rc.ACTION, "split", isin=rc.RELIANCE)
            w2.build_portfolio().mark_session(rc.D1, {})  # frozen: no close needed, the carried mark stands
            assert not [r for r in read_records(w2.root, w2.run_id) if r.get("event") == "missing_bar"]
        finally:
            w2.close()
    finally:
        w.close()


# --- quote readings -----------------------------------------------------------------------------------------------------


def _samples(env, code=CANBK_CODE, isin=CANBK_ISIN, symbol=CANBK_SYMBOL):
    return runtime_module.RelayQuoteSamples(env.link, clock=env.clock).samples(Instrument(isin, symbol, code))


def test_three_distinct_readings_cost_exactly_three_quote_gets_and_each_is_pinned_to_its_bytes(env):
    out = _samples(env)
    assert env.reads.quote_calls == [CANBK_CODE] * 3 and env.reads.paths.count("/v1/quotes") == 3  # B9
    assert len({s.raw_sha256 for s in out}) == 3 and len({s.ask for s in out}) == 3
    assert [s.fetched_at for s in out] == sorted(s.fetched_at for s in out) and len({s.fetched_at for s in out}) == 3
    for sample in out:
        assert sample.fetched_at == sample.evidence.observed_at
        assert sample.evidence.quote.isin == CANBK_ISIN and sample.evidence.quote.stock_code == CANBK_CODE
        assert sample.evidence.quote.bid == sample.bid and sample.evidence.quote.ask == sample.ask


def test_each_reading_hashes_the_body_it_parsed(env, monkeypatch):
    real = env.proxy.quote

    def tampered(code, **kw):
        return replace(real(code, **kw), sha256=sha("another body"))

    monkeypatch.setattr(env.proxy, "quote", tampered)
    with pytest.raises(UpstreamError) as caught:
        _samples(env)
    assert caught.value.reason == "quote_hash_mismatch" and env.reads.quote_calls == [CANBK_CODE]


def test_a_quote_for_another_isin_or_an_unbound_code_is_refused(env):
    env.reads.master = master_zip({STEADY_ISIN: CANBK_CODE})
    with pytest.raises(UpstreamError) as caught:
        _samples(env)
    assert caught.value.reason == "isin_mismatch"
    env.reads.master = master_zip({})
    with pytest.raises(UpstreamError) as caught:
        _samples(env)
    assert caught.value.reason == "isin_unbound"


# --- the key preflight ---------------------------------------------------------------------------------------------------


def _no_vm_leg(env, since):
    """Nothing reached the VM's order routes after ``since`` (a call count): no mint, no audit read."""

    later = env.vm.http.calls[since:]
    assert [c for c in later if c[0] != "GET"] == [] and [c for c in later if c[1] == "/v1/orders/audit"] == []


def test_a_key_the_vm_does_not_hold_is_refused_before_any_intent_or_evidence(env, capsys, spies):
    opened(env, capsys)
    env.write_runtime(vm_key_id="ab" * 32)
    spies.clear()
    before, since = everything(env), len(env.vm.http.calls)
    code, out = go(env, capsys, "decide")
    assert (code, out) == (EXIT_REFUSED, {"refused": "vm_key_mismatch"})  # B12: without the preflight the mint runs
    _no_vm_leg(env, since)
    assert everything(env) == before and spies.writable_opens == 0
    assert ("GET", "/v1/orders/key") in env.vm.http.calls


def test_a_key_that_differs_by_one_character_or_by_case_is_a_mismatch(env):
    """B18: a prefix or a case-insensitive compare would accept these."""

    last = KEY_ID[:-1] + ("0" if KEY_ID[-1] != "0" else "1")
    for expected in (last, KEY_ID.upper(), KEY_ID[:-2], KEY_ID + "0"):
        with pytest.raises(runtime_module.RuntimeRefused) as caught:
            runtime_module.activate_relay(env.link, expected)
        assert caught.value.code == "vm_key_mismatch"
    assert runtime_module.activate_relay(env.link, KEY_ID) is not None


def test_a_vm_without_the_key_route_is_refused_and_never_falls_back_to_the_audit_read(env, capsys):
    """B17: a fallback to ``audit_all()`` would read the audit route and accept the key it names."""

    opened(env, capsys)
    routes = env.vm.stack.app.router.routes
    routes[:] = [r for r in routes if getattr(r, "path", "") != "/v1/orders/key"]
    since = len(env.vm.http.calls)
    code, out = go(env, capsys, "decide")
    assert (code, out) == (EXIT_REFUSED, {"refused": "vm_key_route_unavailable"})
    _no_vm_leg(env, since)


def test_a_key_reply_that_is_not_a_lowercase_64_hex_is_unreadable(env, capsys, monkeypatch):
    opened(env, capsys)
    from brokers.breeze_gateway.orders import OrdersClient, OrdersProtocolError

    def bad(self):
        raise OrdersProtocolError("bad_field")

    monkeypatch.setattr(OrdersClient, "current_key", bad)
    since = len(env.vm.http.calls)
    code, out = go(env, capsys, "decide")
    assert (code, out) == (EXIT_REFUSED, {"refused": "vm_key_unreadable"})
    _no_vm_leg(env, since)


def test_a_key_rotated_between_the_read_and_the_mint_is_the_vms_to_refuse(env, capsys):
    opened(env, capsys)
    runtime = env.factory()(decide_args(env))
    try:
        swap_key(env.vm.stack, OTHER)  # the VM now holds another key than the one the preflight read
        runtime.runner.decide(SESSION)
    finally:
        runtime.shutdown()
    entries = audit(env)
    assert env.vm.http.order_calls() and entries and {e["decision"] for e in entries} == {"REFUSED"}
    assert "key_mismatch" in {c for e in entries for c in e["codes"]} or "kill_switch" in {c for e in entries for c in e["codes"]}


# --- relay and config ---------------------------------------------------------------------------------------------------


def test_a_relay_config_the_gateway_refuses_is_a_phase_1_refusal(env, capsys, spies):
    def refusing():
        raise ConfigError("CONFIG_INVALID", "relay")

    opened(env, capsys)
    spies.clear()
    before = everything(env)
    code = cli.main(
        env.argv("decide"),
        runtime_factory=lambda a: runtime_module.production_runtime(
            a, link_factory=lambda: env.link, clock=env.clock, gateway_config_loader=refusing
        ),
    )
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert code == EXIT_REFUSED and out["refused"].startswith("relay_config_refused:")
    assert spies.writable_opens == 0 and everything(env) == before


def test_a_link_that_cannot_open_is_a_typed_refusal(env, capsys):
    class Down:
        def client(self):
            raise TunnelError("tunnel_down")

        def close(self):
            pass

    opened(env, capsys)
    env.link = Down()
    assert go(env, capsys, "decide") == (EXIT_REFUSED, {"refused": "relay_unavailable"})


# --- forbidden literals on every open ---------------------------------------------------------------------------------------


def test_every_evidence_open_and_create_carries_the_registered_literals(env, capsys, spies):
    """B1: one open without ``forbidden=`` and a registered literal could be written into a run."""

    opened(env, capsys)
    go(env, capsys, "decide")
    go(env, capsys, "close")
    assert {name for name, _f in spies.evidence} >= {"open", "create"}
    assert all(forbidden == FORBIDDEN for _name, forbidden in spies.evidence)


def test_the_release_runtime_opens_its_evidence_with_the_registered_literals_too(env, capsys, spies):
    """B1, release side: ``release_runtime`` opens the evidence store itself; it must pass ``forbidden=`` as well."""

    opened(env, capsys)
    go(env, capsys, "decide")
    spies.clear()
    args = cli._parser().parse_args(env.argv("resume", "--reason", "reviewed the recovery"))
    release = runtime_module.release_runtime(args, clock=env.clock)
    release.shutdown()
    assert [name for name, _f in spies.evidence] == ["open"]
    assert all(forbidden == FORBIDDEN for _name, forbidden in spies.evidence)


def test_a_registered_literal_is_refused_in_the_evidence_text(env, capsys):
    opened(env, capsys)
    policy.raise_event(
        EvidenceStore.open(env.root, run_id=RUN, session_id=runner_module.session_id_for(SESSION), forbidden=FORBIDDEN),
        "stale_data", "a pause",
    )
    code, out = go(env, capsys, "resume", "--reason", f"a note with {FORBIDDEN[0]} inside")
    assert (code, out) == (EXIT_REFUSED, {"refused": "canary_refused"})
    assert FORBIDDEN[0].encode() not in b"".join(p.read_bytes() for p in env.evidence_dir().rglob("*") if p.is_file())


# --- the live table pins ------------------------------------------------------------------------------------------------------


def test_the_pins_a_decide_signs_are_the_live_tables_read_again_each_time(env, capsys, monkeypatch):
    """B4: a fixed pin would not move when a committed table does."""

    opened(env, capsys)
    live = runner_module.LiveTablePins()
    go(env, capsys, "decide")
    first = env.script.inputs[-1]
    assert (first.tick_sha256, first.cost_sha256, first.scenario_sha256) == (
        live.tick_sha256(), live.cost_sha256(), live.scenario_sha256()
    )
    written = b"".join(p.read_bytes() for p in env.evidence_dir().rglob("*") if p.is_file()).decode("utf-8", "replace")
    assert first.tick_sha256 in written and first.cost_sha256 in written and first.scenario_sha256 in written
    monkeypatch.setattr(runner_module, "load_fill_scenarios", lambda: SimpleNamespace(scenarios_hash=sha("drifted")))
    go(env, capsys, "decide")
    assert env.script.inputs[-1].scenario_sha256 == sha("drifted") != first.scenario_sha256


# --- who calls which builder ---------------------------------------------------------------------------------------------------


COMMANDS = {
    "open-run": ("production", ()),
    "decide": ("production", ()),
    "close": ("production", ()),
    "hand-flatten": ("production", ("--reason", "flatten the book")),
    "release-halt": ("release", ("--actor", "ops", "--reason", "reviewed the recovery")),
    "release-corporate-action": ("release", ("--isin", CANBK_ISIN, "--reason", "bonus quantity reconciled")),
    "resume": ("release", ("--reason", "reviewed the recovery")),
}


@pytest.mark.parametrize("command", list(COMMANDS))
def test_the_default_runtime_builds_exactly_the_runtime_its_command_needs(env, monkeypatch, command):
    built: list[str] = []

    def fake(name):
        def build(args, **kwargs):
            built.append(name)
            return name

        return build

    monkeypatch.setattr(runtime_module, "production_runtime", fake("production"))
    monkeypatch.setattr(runtime_module, "release_runtime", fake("release"))
    kind, extra = COMMANDS[command]
    args = cli._parser().parse_args(env.argv(command, *extra))
    assert cli._default_runtime(args) == kind and built == [kind]


def test_status_builds_no_runtime_and_opens_nothing_for_writing(env, capsys, monkeypatch, spies):
    opened(env, capsys)
    spies.clear()
    built: list[str] = []
    monkeypatch.setattr(runtime_module, "production_runtime", lambda *a, **k: built.append("p"))
    monkeypatch.setattr(runtime_module, "release_runtime", lambda *a, **k: built.append("r"))
    assert cli.main(env.argv("status")) == 0
    assert built == [] and "records" in json.loads(capsys.readouterr().out)


def _two_pauses(env):
    """An ordinary pause and an ambiguous-fill pause, raised through a store of the run."""

    store = EvidenceStore.open(env.root, run_id=RUN, session_id=runner_module.session_id_for(SESSION), forbidden=FORBIDDEN)
    policy.raise_event(store, "stale_data", "a stale read")
    policy.raise_event(store, policy.AMBIGUOUS_EXIT_FILL, "unresolved fill", isin=CANBK_ISIN)
    pauses = {p.event: p.seq for p in policy.run_state(store).open_pauses}
    assert set(pauses) == {"stale_data", policy.AMBIGUOUS_EXIT_FILL}
    return pauses


def _built(monkeypatch):
    built: list[str] = []
    real = runtime_module.release_runtime

    def spy(args, **kwargs):
        built.append(args.command)
        return real(args, **kwargs)

    monkeypatch.setattr(runtime_module, "release_runtime", spy)
    return built


def test_resume_builds_the_release_runtime_and_clears_both_pauses_once_nothing_is_held(
    env, capsys, monkeypatch
):
    opened(env, capsys)
    go(env, capsys, "decide")  # the decide starts the latch the ambiguous-exit read needs
    assert env.latch_path().exists()
    pauses = _two_pauses(env)
    built = _built(monkeypatch)
    code = cli.main(env.argv("resume", "--reason", "reviewed the fill"))
    out = json.loads(capsys.readouterr().out)
    assert built == ["resume"] and code == 0
    assert sorted(out["resumed"]) == sorted(pauses.values()) and out["skipped"] == []  # nothing held: the fill is not ambiguous


def test_resume_skips_an_ambiguous_fill_the_portfolio_still_holds_and_clears_the_other_pause(env, capsys, monkeypatch):
    opened(env, capsys)
    pauses = _two_pauses(env)
    monkeypatch.setattr(runtime_module.ReleaseRuntime, "ambiguous_exit_isins", lambda self: frozenset({CANBK_ISIN}))
    code = cli.main(env.argv("resume", "--reason", "reviewed the fill"))
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["resumed"] == [pauses["stale_data"]]
    assert out["skipped"] == [
        {"seq": pauses[policy.AMBIGUOUS_EXIT_FILL], "event": policy.AMBIGUOUS_EXIT_FILL, "why": "ambiguous_exit_still_open"}
    ]
    code = cli.main(env.argv("resume", "--reason", "reviewed the fill", "--pause-seq", str(pauses[policy.AMBIGUOUS_EXIT_FILL])))
    assert (code, json.loads(capsys.readouterr().out)) == (EXIT_REFUSED, {"refused": "ambiguous_exit_still_open"})


@pytest.mark.parametrize("pause", ["stale_data", policy.AMBIGUOUS_EXIT_FILL])
def test_resume_with_the_runtime_config_missing_refuses_and_leaves_every_evidence_byte(env, capsys, spies, pause):
    """The release runtime's phase 1 finishes before any writable evidence open or resume step: no clear, and no archive
    of an interrupted tail that a writable open would have made."""

    opened(env, capsys)
    store = EvidenceStore.open(env.root, run_id=RUN, session_id=runner_module.session_id_for(SESSION), forbidden=FORBIDDEN)
    policy.raise_event(store, pause, "a pause", **({"isin": CANBK_ISIN} if pause == policy.AMBIGUOUS_EXIT_FILL else {}))
    log = env.evidence_dir() / LOG_NAME
    log.write_bytes(log.read_bytes() + b'{"kind":"pa')  # an interrupted append
    before = tree_state(env.root)
    spies.clear()
    (env.private / "india" / "shadow_runtime.json").unlink()
    code, out = go(env, capsys, "resume", "--reason", "reviewed")
    assert (code, out) == (EXIT_REFUSED, {"refused": "runtime_config_refused:SHADOW_RUNTIME_MISSING"})
    assert tree_state(env.root) == before and not (env.evidence_dir() / TORN_DIR).exists()
    assert spies.evidence == [] and spies.ledgers == [] and spies.link == []
    assert log.read_bytes().endswith(b'{"kind":"pa')


# --- the canary: nothing from the runtime config leaves through output, exceptions or evidence ----------------------------


def test_no_runtime_config_value_appears_in_output_exceptions_or_evidence(
    tmp_path, seeded, metadata, monkeypatch, capsys  # noqa: F811
):
    canary = tmp_path / "CANARYQ7"
    canary.mkdir()
    env = build_env(canary, seeded, metadata)
    env.script.result = evaluated("CANBK", "STEADYCO")
    monkeypatch.setattr(runner_module, "evaluate_session", env.script)
    monkeypatch.setattr(runner_module, "held_names_quarantined", env.script.held_names_quarantined)
    seen = []
    for command, mutate in (
        ("open-run", None), ("decide", None), ("close", None), ("decide", _runtime(window_start="2031-01-02")),
        ("decide", _runtime(calendar_sha256="e" * 64)), ("decide", _runtime(vm_key_id="ab" * 32)),
        ("decide", _runtime(session_store_root=str(canary / "no-store"))),
    ):
        if mutate:
            mutate(env)
        _code, out = go(env, capsys, command)
        seen.append(json.dumps(out))
    assert not [s for s in seen if "CANARYQ7" in s or KEY_ID in s]
    written = b"".join(p.read_bytes() for p in env.evidence_dir().rglob("*") if p.is_file())
    assert b"CANARYQ7" not in written
    env.write_runtime(vm_key_id=KEY_ID, window_start="2025-03-03", session_store_root=str(env.session_store))
    pre = runtime_module.preflight(decide_args(env), relay=False, calendar=False)
    assert "CANARYQ7" not in repr(pre) and "CANARYQ7" not in repr(pre.runtime) and KEY_ID not in repr(pre.runtime)


# --- the release commands through the production release runtime -------------------------------------------------------------


def _ready(made, seeded_):
    """Make a Task A world runtime-ready: a runtime config over a seeded store, next to its own private dir."""

    private = made.tmp / "private"
    store = made.tmp / "session"
    shutil.copytree(seeded_["session"], store)
    calendar = made.tmp / "calendar.json"
    write_json(calendar, calendar_doc())
    from conftest import _p58_canonical_hash
    from strategy_india.params import placeholder_params

    params = {**placeholder_params(), "adv_window": 3}
    strategy = json.loads((private / "india" / "strategy.json").read_text())
    strategy["params"], strategy["params_sha256"] = params, _p58_canonical_hash(params)
    write_json(private / "india" / "strategy.json", strategy)
    write_json(private / "india" / "shadow_runtime.json", {
        "schema_version": 1, "workspace": "india", "session_store_root": str(store),
        "dataset_dir": str(seeded_["dataset"]), "calendar_path": str(calendar),
        "calendar_sha256": hashlib.sha256(calendar.read_bytes()).hexdigest(), "simulation_root": str(made.sim),
        "window_start": "2025-03-03", "vm_key_id": KEY_ID,
    })
    return made


RELEASE_ROWS = [
    "test_release_halt_above_the_line_resets_the_latch_then_writes_one_resume_and_the_next_buy_is_no_longer_blocked",
    "test_release_halt_at_or_below_the_line_without_the_flag_is_refused_and_the_halt_stays_in_force",
    "test_release_halt_with_the_rebase_flag_accepts_the_loss_restarts_only_the_minus_8_test_and_keeps_the_peak",
    "test_a_release_with_no_halt_latched_and_a_pause_open_writes_only_the_record_and_never_resets_the_latch",
    "test_the_halt_is_the_only_pause_release_halt_clears",
    "test_a_flat_name_is_released_with_one_resume_record_naming_only_its_pause",
    "test_a_held_name_the_ledger_has_no_adjustment_for_is_refused_corporate_action_not_booked_and_stays_paused",
    "test_w1_a_missing_evaluation_commit_is_repaired_once_by_the_refused_release_and_equals_the_normal_one",
    "test_w2_a_latch_session_with_no_evaluation_inputs_aborts_the_run_with_exactly_one_abort_event",
    "test_w3a_a_submitting_order_goes_unknown_with_one_recovery_event_and_nothing_else_changes",
    "test_w3b_an_evaluated_requote_is_blocked_with_one_requote_event_and_nothing_else_changes",
    "test_h_an_unanchored_record_is_anchored_by_the_open_and_the_semantic_state_is_equal",
    "test_h_a_torn_tail_is_archived_by_the_open_and_the_semantic_state_is_equal",
]


@pytest.fixture()
def release_driver(monkeypatch, seeded, admission_clock):  # noqa: F811
    """Task A's rows, run through ``main`` with no runtime factory: the production release runtime over real files.
    Only the run's instruments and its recorded start are replaced: both are Task A fixtures, not files."""

    real_make = rc.make_world

    def make(path, **kwargs):
        return _ready(real_make(path, **kwargs), seeded)

    monkeypatch.setattr(rc, "make_world", make)
    monkeypatch.setattr(rc.call, "__kwdefaults__", {**rc.call.__kwdefaults__, "factory": None, "latch": None})
    monkeypatch.setattr(
        runtime_module, "_instruments",
        lambda report: (tuple(Instrument(isin, symbol, symbol) for symbol, isin in rc.UNIVERSE), {}),
    )
    monkeypatch.setattr(runtime_module, "_run_start", lambda root, run_id, window_start: (rc.S, {}))


def _kinds(w):
    return [r["kind"] for r in read_records(w.root, w.run_id)]


@pytest.mark.parametrize("holding", [False, True], ids=["no_fills", "ledger_has_fills"])
def test_a_resume_with_no_latch_clears_an_ordinary_pause_and_never_creates_the_latch(release_driver, tmp_path, holding):
    """The relaxed latch rule is for an ordinary pause only, and it still writes no start state."""

    w = rc.make_world(tmp_path, holding=holding)
    w.raise_event("stale_data", "a stale read")
    assert not w.latch_path.exists()
    code, out = rc.call(w, "resume", "--reason", "reviewed the read")
    assert code == 0 and len(out["resumed"]) == 1 and out["skipped"] == []
    assert not w.latch_path.exists() and not w.lock_path.exists()
    assert w.open_pauses() == [] and "abort" not in _kinds(w)


@pytest.mark.parametrize("holding", [False, True], ids=["no_fills", "ledger_has_fills"])
def test_a_resume_with_no_latch_and_an_ambiguous_fill_pause_is_refused_latch_missing_and_writes_nothing(
    release_driver, tmp_path, holding
):
    """The ambiguous-exit read goes through ``LatchStore.load``: with no latch it would start one, and with fills in the
    ledger it would write a terminal ``state_corrupt`` abort. The resume is refused before the portfolio is asked."""

    w = rc.make_world(tmp_path, holding=holding)
    w.raise_event("stale_data", "a stale read")
    w.raise_event(policy.AMBIGUOUS_EXIT_FILL, "unresolved fill", isin=rc.RELIANCE)
    pauses = w.open_pauses()
    before = tree_state(w.root)
    assert not w.latch_path.exists()
    code, out = rc.call(w, "resume", "--reason", "reviewed the fill")
    assert (code, out) == (1, {"refused": "latch_missing"})
    assert tree_state(w.root) == before and w.open_pauses() == pauses  # nothing cleared, nothing appended
    assert not w.latch_path.exists() and not w.lock_path.exists()
    assert "abort" not in _kinds(w) and "resume" not in _kinds(w)


@pytest.mark.parametrize("row", RELEASE_ROWS, ids=[name[5:50] for name in RELEASE_ROWS])
def test_task_a_release_rows_pass_through_the_production_release_runtime(release_driver, request, row):
    fn = getattr(rc, row)
    fn(**{name: request.getfixturevalue(name) for name in inspect.signature(fn).parameters})


def test_every_resume_record_of_a_release_is_appended_from_the_portfolio_and_never_from_the_cli(
    release_driver, world, monkeypatch  # noqa: F811
):
    """A1, CLI side (#626 P3): a CLI that opened the store and appended the resume itself would show ``__main__.py``."""

    callers: list[str] = []
    real = EvidenceStore.append

    def spy(self, kind, payload):
        if kind == "resume":
            callers.append(Path(sys._getframe(1).f_code.co_filename).name)
        return real(self, kind, payload)

    monkeypatch.setattr(EvidenceStore, "append", spy)
    world.raise_event(rc.ACTION, "split", isin=rc.RELIANCE)
    world.plant_latch()
    code, out = rc.action(world)
    assert code == 0 and out["released"] == "corporate_action"
    assert callers and set(callers) <= {"portfolio.py", "policy.py"} and "__main__.py" not in callers


# --- 64-05 Task 2b: the deployed VM build is read in phase 2 and pinned in evidence ------------------------------------


def _raising(error):
    def read(self):
        raise error

    return read


# What the client raises for each way the build read can fail, built from the real error classes. The route being
# missing, a body the client refuses, a relay failure and an unstamped deploy are Stage 1's own errors; the last row is a
# reply whose digest does not recompute, which the client would refuse first and the runtime checks again.
BUILD_FAILURES = [
    ("route_unavailable", _raising(OrdersBuildRouteUnavailable("NOT_FOUND", http_status=404, reason="route_unavailable")),
     "vm_build_unavailable"),
    ("invalid_body", _raising(OrdersBuildInvalid(reason="digest_mismatch")), "vm_build_unavailable"),
    ("relay_error", _raising(UpstreamError()), "vm_build_unavailable"),
    ("protocol_error", _raising(OrdersProtocolError("bad_field")), "vm_build_unavailable"),
    ("unstamped", _raising(OrdersBuildUnstamped("ORDERS_UNAVAILABLE", http_status=503, reason="build_unstamped")),
     "vm_build_unstamped"),
    ("digest_does_not_recompute", lambda self: replace(BUILD, digest=sha("lie")), "vm_build_digest_mismatch"),
]


def _break_build_read(monkeypatch, reader):
    monkeypatch.setattr(OrdersClient, "current_build", reader)


@pytest.mark.parametrize(("name", "reader", "code"), BUILD_FAILURES, ids=[row[0] for row in BUILD_FAILURES])
def test_a_failed_build_read_refuses_open_run_with_nothing_opened_or_written(env, capsys, spies, monkeypatch, name, reader, code):
    _break_build_read(monkeypatch, reader)
    before = everything(env)
    exit_code, out = go(env, capsys, "open-run")
    assert (exit_code, out) == (EXIT_REFUSED, {"refused": code})
    assert everything(env) == before and spies.writable_opens == 0  # evidence, ledger and store byte-identical
    assert not env.root.exists() or not (env.root / RUN).exists()


@pytest.mark.parametrize(("name", "reader", "code"), BUILD_FAILURES, ids=[row[0] for row in BUILD_FAILURES])
def test_a_failed_build_read_refuses_decide_with_nothing_opened_or_written(env, capsys, spies, monkeypatch, name, reader, code):
    opened(env, capsys)
    spies.clear()
    _break_build_read(monkeypatch, reader)
    before, calls = everything(env), len(env.vm.http.calls)
    exit_code, out = go(env, capsys, "decide")
    assert (exit_code, out) == (EXIT_REFUSED, {"refused": code})
    assert everything(env) == before and spies.writable_opens == 0
    assert env.vm.http.order_calls() == [] and calls <= len(env.vm.http.calls)


def test_a_vm_with_no_commit_stamp_refuses_open_run_through_the_real_route(tmp_path, seeded, metadata, monkeypatch, capsys):  # noqa: F811
    """End to end, no stub: the in-process VM has no ``BUILD_COMMIT``, its real route answers 503 ``build_unstamped``."""
    unstamped = build_env(tmp_path, seeded, metadata, stamped=False)
    unstamped.script.result = evaluated("CANBK", "STEADYCO")
    monkeypatch.setattr(runner_module, "evaluate_session", unstamped.script)
    before = everything(unstamped)
    exit_code, out = go(unstamped, capsys, "open-run")
    assert (exit_code, out) == (EXIT_REFUSED, {"refused": "vm_build_unstamped"})
    assert everything(unstamped) == before


def _set_collar(env, collar):
    path = env.private / "india" / "execution.json"
    body = json.loads(path.read_text())
    path.write_text(json.dumps({**body, "fat_finger_collar": collar}))


def _pin_of(env):
    return [r for r in read_records(env.root, RUN) if r["kind"] == "vm_build"][0]


def _nine_key(env):
    from private_config.loader import load_workspace_config
    from risk_india.state import limits_from_config

    return limits_from_config(load_workspace_config(env.private, "india", require_india_execution=True)).sha256


def test_the_open_run_pin_is_the_nine_key_hash_with_the_collar_and_not_the_file_hash(env, capsys):
    _set_collar(env, "0.0314")  # the collar exists only in execution.json
    opened(env, capsys)
    manifest = EvidenceStore.open(env.root, run_id=RUN, session_id=runtime_module.session_id_for(SESSION)).manifest
    assert _pin_of(env)["mint_limits_sha256"] == _nine_key(env)
    assert _pin_of(env)["mint_limits_sha256"] != manifest.hashes["limits_sha256"]  # the IndiaLimits file hash has no collar


def test_the_pin_moves_with_the_collar(env, capsys, tmp_path):
    first = _nine_key(env)
    _set_collar(env, "0.0314")
    assert _nine_key(env) != first  # a pin that ignored the collar would not move


def test_the_first_decide_on_the_same_config_compares_equal_to_the_open_run_pin(env, capsys):
    opened(env, capsys)
    assert _pin_of(env)["mint_limits_sha256"] == _nine_key(env)
    code, decided = go(env, capsys, "decide")
    assert code == 0 and decided["status"] == "done", decided  # equal: no vm_build_changed
    assert [r for r in read_records(env.root, RUN) if r["kind"] == "pause"] == []


def test_a_collar_edited_after_open_run_pauses_the_next_decide_before_it_writes(env, capsys):
    opened(env, capsys)
    _set_collar(env, "0.0415")
    before = len(read_records(env.root, RUN))
    code, decided = go(env, capsys, "decide")
    assert code == 0 and (decided["status"], decided["stopped"]) == ("paused", "vm_build_changed")
    after = read_records(env.root, RUN)[before:]
    assert [r["kind"] for r in after] == ["pause"] and after[0]["event"] == "vm_build_changed"


def test_open_run_builds_no_admission_guard_and_no_latch(env, capsys, monkeypatch):
    import risk_india.state as state_module

    guards, latches = [], []
    real_from_config = runtime_module.IndiaAdmissionGuard.from_config.__func__

    def guard_spy(cls, *args, **kwargs):
        guards.append(1)
        return real_from_config(cls, *args, **kwargs)

    monkeypatch.setattr(runtime_module.IndiaAdmissionGuard, "from_config", classmethod(guard_spy))
    real_init = state_module.LatchStore.__init__

    def latch_spy(self, *args, **kwargs):
        latches.append(1)
        return real_init(self, *args, **kwargs)

    monkeypatch.setattr(state_module.LatchStore, "__init__", latch_spy)
    opened(env, capsys)
    assert guards == [] and latches == []
    assert not env.latch_path().exists()  # nothing touched the latch file
