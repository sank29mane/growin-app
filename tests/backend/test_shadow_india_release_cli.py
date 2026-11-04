"""Phase 64-09 Task A: ``release-halt`` and ``release-corporate-action`` on the shadow CLI.

What is real: the evidence store (opened with the registered literals), the 63 latch file, the simulation ledger file
(``open_simulation_ledger``), ``ShadowPortfolio`` and the CLI. What is injected, as every shadow CLI test does: the
literal source, the latch path seam and the runtime factory (the production runtime is wired by Task B and still refuses
``production_runtime_not_wired``). The factory here does what the production release runtime will: it opens the writable
ledger (startup recovery runs), the evidence store and the latch, and builds the portfolio. Nothing reaches the network,
the VM, a broker or ``private/``: the forbidden literals are synthetic.

The write contract each refusal row holds (``64-09-PLAN`` Task A): before a refusal no command appends an evidence record,
changes latch state or inserts or updates a ledger row, except the portfolio-owned W1 (crash-repair ``evaluation_commit``)
and W2 (``_abort`` event), the ledger's startup recovery W3a (SUBMITTING to UNKNOWN) and W3b (EVALUATED requote to
``BLOCKED_RESTART_REQUIRES_FRESH_EVIDENCE``), and idempotent open housekeeping H (sidecars, triggers, chmod, locks,
anchoring a written record, archiving a torn tail). CLI-decided refusals call no factory and touch no file at all.
"""

from __future__ import annotations

import ast
import io
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from execution.ledger import LedgerReader, simulated_booking_id
from risk_india import drawdown
from risk_india.state import LatchStore, StateUnreadable, read_state, state_path_for, write_state
from shadow_india import __main__ as cli
from shadow_india import policy
from shadow_india.admission import open_simulation_ledger, simulation_ledger_path
from shadow_india.evidence import LOG_NAME, TORN_DIR, EvidenceStore, read_records
from shadow_india.portfolio import Instrument, ShadowPortfolio
from shadow_india.runner import ShadowRunner, session_id_for
from test_shadow_india_admission import (  # noqa: F401 - the fixture is used through pytestmark
    RUN_ID,
    SEED,
    frozen_service_clock,
    make_rig,
    prepare_buy,
    sha,
)
from test_shadow_india_portfolio import A as FAKE_A
from test_shadow_india_portfolio import D1 as FAKE_D1
from test_shadow_india_portfolio import D2 as FAKE_D2
from test_shadow_india_portfolio import D3 as FAKE_D3
from test_shadow_india_portfolio import Fake as FakePortfolioWorld

pytestmark = pytest.mark.usefixtures("frozen_service_clock")

REPO = Path(__file__).resolve().parents[2]
S = date(2026, 10, 22)
D1 = date(2026, 10, 9)
FIXED = datetime(2026, 10, 22, 4, 0, tzinfo=timezone.utc)
SCHEDULE = sha("schedule")

# Synthetic. Both pass the actor grammar, so the actor row proves the literal screen and not the grammar.
CANARY_ACCOUNT = "canaryacct0042"
CANARY_HOST = "canaryhost.example"
LITERALS = (CANARY_ACCOUNT, CANARY_HOST)

RELIANCE, TCS, INFY, HDFCBANK = "INE002A01018", "INE467B01029", "INE009A01021", "INE040A01034"
UNIVERSE = (("RELIANCE", RELIANCE), ("TCS", TCS), ("INFY", INFY))
BAD_CHECK_DIGIT = "INE002A01019"  # RELIANCE with the last digit changed
HALT = policy.DRAWDOWN_HALT
ACTION = policy.UNRESOLVED_CORPORATE_ACTION


class Died(BaseException):
    """A simulated process death (not an Exception, so nothing in the CLI catches it)."""


# --- the world: real files, an injected factory --------------------------------------------------------------------------------------


@dataclass
class World:
    tmp: Path
    limits: Any
    instruments: list[Instrument]
    calls: int = 0
    opened: list[Any] = field(default_factory=list)
    die_on: str | None = None
    run_id: str = RUN_ID

    @property
    def root(self) -> Path:
        return self.tmp / "evidence"

    @property
    def run_dir(self) -> Path:
        return self.root / self.run_id

    @property
    def sim(self) -> Path:
        return self.tmp / "sim"

    @property
    def ledger_path(self) -> Path:
        return simulation_ledger_path(self.sim, RUN_ID)

    @property
    def latch_path(self) -> Path:
        return state_path_for(self.ledger_path)

    @property
    def lock_path(self) -> Path:
        return self.latch_path.with_name(self.latch_path.name + ".lock")

    def open_evidence(self) -> EvidenceStore:
        return EvidenceStore.open(
            self.root, run_id=self.run_id, session_id=session_id_for(S), clock=lambda: FIXED, forbidden=LITERALS
        )

    def build_portfolio(self, evidence: Any = None) -> ShadowPortfolio:
        """What the production release runtime does: writable ledger (recovery), evidence with literals, latch, portfolio."""

        ledger = open_simulation_ledger(self.sim, RUN_ID, SEED)
        self.opened.append(ledger)
        evidence = evidence if evidence is not None else self.open_evidence()
        latch = LatchStore(self.latch_path, self.limits, has_fills=ledger.has_fills)
        return ShadowPortfolio(ledger, latch, evidence, self.instruments)

    def factory(self, args: Any) -> Any:
        self.calls += 1
        evidence: Any = self.open_evidence()
        if self.die_on is not None:
            evidence = _Dying(evidence, self.die_on)
        return SimpleNamespace(portfolio=self.build_portfolio(evidence))

    def plant_latch(self, **fields: Any) -> None:
        write_state(self.latch_path, drawdown.RiskState(peak=self.limits.capital_cap, **fields))

    def raise_event(self, event: str, reason: str = "planted", *, isin: str | None = None) -> None:
        policy.raise_event(self.open_evidence(), event, reason, isin=isin)

    def open_pauses(self) -> list[tuple[str, str]]:
        return [(p.event, p.scope) for p in policy.state_from_records(read_records(self.root, self.run_id)).open_pauses]

    def tear(self) -> None:
        """An unterminated tail line, as a crash in the middle of an append leaves it."""

        with open(self.run_dir / LOG_NAME, "ab") as log:
            log.write(b'{"kind":"pa')

    def close(self) -> None:
        """Release the writer lock of every ledger a factory or a test opened (one writer per ledger file)."""

        while self.opened:
            self.opened.pop().close()


class _Dying:
    """An evidence store that dies when asked to append one kind: a crash between two writes."""

    def __init__(self, inner: EvidenceStore, kind: str) -> None:
        self._inner = inner
        self._kind = kind

    def append(self, kind: str, payload: Any) -> Any:
        if kind == self._kind:
            raise Died
        return self._inner.append(kind, payload)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def raw(path: Path, *statements: str) -> None:
    connection = sqlite3.connect(path)
    try:
        for statement in statements:
            connection.execute(statement)
        connection.commit()
    finally:
        connection.close()


def book_fill(rig: Any, proposal_id: str, quantity: int, price: str) -> None:
    scenario = sha(f"scenario-{proposal_id}")
    rig.ledger.book_simulated_fill(
        simulated_booking_id("attempt-1", scenario), proposal_id, quantity, price, "0", SCHEDULE, scenario, "attempt-1"
    )


def make_world(
    tmp_path: Path, *, holding: bool = False, submitting: bool = False, requote: bool = False, run_id: str = RUN_ID
) -> World:
    """A run with an evidence store, a simulation ledger and no latch file. ``submitting`` plants one SUBMITTING order
    (W3a), ``requote`` one EVALUATED requote (W3b), ``holding`` a booked RELIANCE position."""

    rig = make_rig(tmp_path)
    limits = rig.guard.store.limits
    if holding:
        prepare_buy(rig, "own-1", quantity=2)
        book_fill(rig, "own-1", 2, "100.00")
    if submitting or requote:
        prepare_buy(rig, "plant-1", quantity=1)
    path = simulation_ledger_path(rig.root, RUN_ID)
    rig.close()
    now = "2026-10-22T04:00:00+00:00"
    if submitting:
        raw(
            path,
            "UPDATE order_projection SET state = 'SUBMITTING' WHERE proposal_id = 'plant-1'",
            f"INSERT INTO dispatch_attempts (proposal_id, state, claimed_at) VALUES ('plant-1', 'SUBMITTING', '{now}')",
        )
    if requote:
        raw(
            path,
            "INSERT INTO requote_intents (requote_id, proposal_id, parent_intent_hash, parent_reconciliation_fingerprint, "
            "idempotency_key, snapshot_hash, candidate_json, state, created_at, updated_at) "
            f"VALUES ('rq-1', 'plant-1', 'h', 'ack:x', 'key-1', 'snap', '{{\"side\":\"BUY\"}}', 'EVALUATED', '{now}', '{now}')",
            "INSERT INTO requote_events (requote_id, event_type, from_state, to_state, payload_json, created_at) "
            f"VALUES ('rq-1', 'REQUOTE_EVALUATED', NULL, 'EVALUATED', '{{}}', '{now}')",
        )
    world = World(tmp_path, limits, [Instrument(isin, symbol, symbol) for symbol, isin in UNIVERSE], run_id=run_id)
    EvidenceStore.create(
        world.root, run_id=run_id, run_class="plumbing", session_id=session_id_for(S),
        hashes={"strategy_sha256": sha("strategy")}, clock=lambda: FIXED, forbidden=LITERALS,
    )
    for leftover in (world.latch_path, world.lock_path):  # the rig's admission wrote a start latch: none exists yet
        leftover.unlink(missing_ok=True)
    return world


@pytest.fixture()
def world(tmp_path):
    built = make_world(tmp_path)
    try:
        yield built
    finally:
        built.close()


@pytest.fixture()
def planted(tmp_path):
    """A world with a SUBMITTING order and an EVALUATED requote planted, for rows nothing may open."""

    built = make_world(tmp_path, submitting=True, requote=True)
    try:
        yield built
    finally:
        built.close()


# --- running the CLI and reading the result --------------------------------------------------------------------------------------


def argv_for(w: Any, command: str, *extra: str) -> list[str]:
    return [
        command, "--root", str(w.root), "--run-id", w.run_id, "--session", S.isoformat(),
        "--private-dir", str(w.tmp / "private"), *extra,
    ]


def call(w: Any, command: str, *extra: str, factory: Any = "world", latch: Any = "world", forbidden: Any = "synthetic") -> tuple[int, dict]:
    out = io.StringIO()
    if hasattr(w, "close"):
        w.close()  # a ledger a test built itself must not hold the writer lock the factory needs
    try:
        code = cli.main(
            argv_for(w, command, *extra),
            runtime_factory=w.factory if factory == "world" else factory,
            forbidden_source=(lambda args: LITERALS) if forbidden == "synthetic" else forbidden,
            latch_path_source=(lambda args: w.latch_path) if latch == "world" else latch,
            out=out,
        )
    finally:
        if hasattr(w, "close"):
            w.close()
    text = out.getvalue()
    return code, json.loads(text) if text else {}


def halt(w: Any, *extra: str, actor: str = "ops", reason: str = "reviewed the recovery", **kw: Any) -> tuple[int, dict]:
    return call(w, "release-halt", "--actor", actor, "--reason", reason, *extra, **kw)


def action(w: Any, isin: str = RELIANCE, reason: str = "bonus quantity reconciled", **kw: Any) -> tuple[int, dict]:
    return call(w, "release-corporate-action", "--isin", isin, "--reason", reason, **kw)


def semantic(w: World) -> dict[str, Any]:
    """Everything a refusal may not change (the plan's invariant helper): the verified evidence records, the latch,
    every ledger read that exists and a read-only SQL snapshot of the projection, requote and requote event tables."""

    try:
        latch: Any = read_state(w.latch_path)
    except StateUnreadable:
        latch = ("unreadable", w.latch_path.read_bytes())
    with LedgerReader(w.ledger_path, workspace="india") as reader:
        connection = sqlite3.connect(f"file:{w.ledger_path}?mode=ro", uri=True)
        try:
            proposals = [row[0] for row in connection.execute("SELECT proposal_id FROM order_intents ORDER BY 1")]
            tables = {
                name: connection.execute(f"SELECT * FROM {name} ORDER BY 1").fetchall()
                for name in ("order_projection", "requote_intents", "requote_events")
            }
        finally:
            connection.close()
        return {
            "records": read_records(w.root, w.run_id),
            "latch": latch,
            "attempts": reader.list_attempts(),
            "events": reader.list_events(),
            "requotes": reader.list_requotes(),
            "orders": {p: reader.get_order(p) for p in proposals},
            "reservations": {p: reader.get_reservation(p) for p in proposals},
            "positions": reader.get_paper_position(f"sim-{RUN_ID}", "INR", "NSE:CASH:RELIANCE", workspace="india"),
            "sql": tables,
        }


def files(w: World) -> dict[str, Any]:
    """The evidence run directory byte for byte, and the latch files: for rows where nothing may be opened at all."""

    return {
        "run": {str(p.relative_to(w.run_dir)): (p.read_bytes() if p.is_file() else None) for p in sorted(w.run_dir.rglob("*"))},
        "latch": w.latch_path.read_bytes() if w.latch_path.exists() else None,
        "latch_lock": w.lock_path.exists(),
    }


def assert_untouched(w: World, run: Any) -> None:
    """Run ``run()`` (a CLI call) and assert nothing at all was opened: no factory, no byte, no lock, no sidecar."""

    before_semantic, before_files = semantic(w), files(w)
    result = run()
    assert w.calls == 0
    assert files(w) == before_files and semantic(w) == before_semantic
    assert not w.lock_path.exists()
    return result


# --- the parser -----------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["release-halt", "--root", "r", "--run-id", "x", "--session", "2026-10-22", "--private-dir", "p", "--reason", "why"],
        ["release-halt", "--root", "r", "--run-id", "x", "--session", "2026-10-22", "--private-dir", "p", "--actor", "ops"],
        ["release-halt", "--root", "r", "--run-id", "x", "--session", "2026-10-22", "--actor", "ops", "--reason", "why"],
        ["release-corporate-action", "--root", "r", "--run-id", "x", "--session", "2026-10-22", "--private-dir", "p",
         "--reason", "why"],
        ["release-corporate-action", "--root", "r", "--run-id", "x", "--session", "2026-10-22", "--private-dir", "p",
         "--isin", RELIANCE],
        ["release-corporate-action", "--root", "r", "--run-id", "x", "--session", "2026-10-22", "--isin", RELIANCE,
         "--reason", "why"],
        ["release-corporate-action", "--root", "r", "--run-id", "x", "--session", "2026-10-22", "--private-dir", "p",
         "--isin", RELIANCE, "--reason", "why", "--rebase-halt-anchor"],
        ["release-halt", "--root", "r", "--run-id", "x", "--session", "2026-10-22", "--private-dir", "p", "--actor", "ops",
         "--reason", "why", "--isin", RELIANCE],
    ],
)
def test_the_parser_requires_everything_but_the_rebase_flag_and_keeps_each_option_on_its_command(argv, capsys):
    assert cli.main(argv) == cli.EXIT_USAGE


# --- CLI-decided refusals: no factory, no byte written ----------------------------------------------------------------------------------


def plant_abort(w: World) -> None:
    w.raise_event("vm_audit_broken", "audit_broken")
    w.plant_latch(halt=True, drawdown=Decimal("-0.04"))


def plant_run_end(w: World) -> None:
    w.raise_event("drawdown_flatten_15", "flatten")
    w.plant_latch(halt=True, ended=True, drawdown=Decimal("-0.2"))


def plant_latch_ended(w: World) -> None:
    w.raise_event(HALT, "halted")
    w.plant_latch(halt=True, ended=True, drawdown=Decimal("-0.2"))


def plant_halt_no_latch(w: World) -> None:
    w.raise_event(HALT, "halted")


def plant_quiet(w: World) -> None:
    w.raise_event("stale_data", "an unrelated pause")
    w.plant_latch()


@pytest.mark.parametrize(
    ("plant", "expected"),
    [
        (plant_abort, "resume_refused_abort"),
        (plant_run_end, "resume_refused_run_ended"),
        (plant_latch_ended, "resume_refused_run_ended"),
        (plant_halt_no_latch, "latch_missing"),
        (plant_quiet, "nothing_to_resume"),
    ],
    ids=["abort", "run_ended_in_evidence", "run_ended_in_latch", "latch_missing", "nothing_to_resume"],
)
def test_release_halt_refuses_from_read_only_reads_before_any_factory_with_a_torn_tail_and_a_submitting_order_untouched(
    planted, plant, expected
):
    plant(planted)
    planted.tear()
    code, out = assert_untouched(planted, lambda: halt(planted))
    assert (code, out) == (1, {"refused": expected})
    assert not (planted.run_dir / TORN_DIR).exists()  # nothing opened the evidence: the torn tail is not archived
    if expected == "latch_missing":
        assert not planted.latch_path.exists()


def plant_action_abort(w: World) -> None:
    w.raise_event(ACTION, "split", isin=RELIANCE)
    w.raise_event("vm_audit_broken", "audit_broken")
    w.plant_latch()


def plant_action_no_latch(w: World) -> None:
    w.raise_event(ACTION, "split", isin=RELIANCE)


def plant_action_no_pause(w: World) -> None:
    w.plant_latch()


def plant_action_other_isin(w: World) -> None:
    w.raise_event(ACTION, "split", isin=TCS)
    w.plant_latch()


def plant_action_other_kind(w: World) -> None:
    w.raise_event("band_ambiguity", "band", isin=RELIANCE)
    w.plant_latch()


@pytest.mark.parametrize(
    ("plant", "expected"),
    [
        (plant_action_abort, "resume_refused_abort"),
        (plant_action_no_latch, "latch_missing"),
        (plant_action_no_pause, "nothing_to_resume"),
        (plant_action_other_isin, "nothing_to_resume"),
        (plant_action_other_kind, "nothing_to_resume"),
    ],
    ids=["abort", "latch_missing", "no_pause", "pause_for_another_isin", "another_pause_kind_for_the_isin"],
)
def test_release_corporate_action_refuses_from_read_only_reads_before_any_factory(planted, plant, expected):
    plant(planted)
    planted.tear()
    code, out = assert_untouched(planted, lambda: action(planted))
    assert (code, out) == (1, {"refused": expected})
    assert not (planted.run_dir / TORN_DIR).exists()
    if expected == "latch_missing":
        assert not planted.latch_path.exists()


def test_the_production_latch_path_is_not_wired_until_task_b_and_nothing_is_read_before_it_refuses(planted):
    planted.raise_event(HALT, "halted")
    planted.plant_latch(halt=True, drawdown=Decimal("-0.04"))
    planted.tear()
    for run in (lambda: halt(planted, latch=None), lambda: action(planted, latch=None)):
        assert assert_untouched(planted, run) == (1, {"refused": cli.NOT_WIRED})
    # an unreadable evidence root would raise a different code if the read ran first
    out = io.StringIO()
    argv = argv_for(planted, "release-halt", "--actor", "ops", "--reason", "why")
    argv[2] = str(planted.tmp / "no-such-evidence-root")
    assert cli.main(argv, forbidden_source=lambda a: LITERALS, out=out) == 1
    assert json.loads(out.getvalue()) == {"refused": cli.NOT_WIRED}


def test_with_no_runtime_factory_a_release_that_passes_every_cli_check_still_refuses_not_wired(planted):
    planted.raise_event(HALT, "halted")
    planted.plant_latch(halt=True, drawdown=Decimal("-0.04"))
    code, out = halt(planted, factory=None)
    assert (code, out) == (1, {"refused": cli.NOT_WIRED})
    assert read_state(planted.latch_path).halt


def test_corrupt_evidence_exits_3_through_the_existing_arm_and_the_cli_writes_no_sidecar(planted):
    planted.raise_event(HALT, "halted")
    planted.plant_latch(halt=True, drawdown=Decimal("-0.04"))
    log = planted.run_dir / LOG_NAME
    data = bytearray(log.read_bytes())
    data[len(data) // 2] ^= 0x01
    log.write_bytes(bytes(data))
    before = files(planted)
    code, out = halt(planted)
    assert code == 3 and list(out) == ["aborted"]
    assert planted.calls == 0 and files(planted) == before


def test_a_run_that_does_not_exist_is_refused_without_creating_it(planted):
    before = sorted(p.name for p in planted.root.iterdir())
    for command, extra in (("release-halt", ("--actor", "ops")), ("release-corporate-action", ("--isin", RELIANCE))):
        argv = argv_for(planted, command, *extra, "--reason", "why")
        argv[argv.index("--run-id") + 1] = "no-such-run"
        buffer = io.StringIO()
        exit_code = cli.main(
            argv, forbidden_source=lambda a: LITERALS, latch_path_source=lambda a: planted.latch_path,
            runtime_factory=planted.factory, out=buffer,
        )
        assert exit_code in (1, 3) and set(json.loads(buffer.getvalue())) <= {"refused", "aborted"}
    assert sorted(p.name for p in planted.root.iterdir()) == before and planted.calls == 0


# --- grammar and the pre-screen: counter stays 0 ----------------------------------------------------------------------------------------


@pytest.mark.parametrize("actor", ["Ops", "op s", "o" * 33, "", "opé", "1ops", "ops\n", "_ops"])
def test_a_bad_actor_is_refused_before_anything_is_read_or_opened(planted, actor):
    planted.raise_event(HALT, "halted")
    planted.plant_latch(halt=True, drawdown=Decimal("-0.04"))
    code, out = assert_untouched(planted, lambda: halt(planted, actor=actor))
    assert (code, out) == (1, {"refused": "actor_invalid"})


@pytest.mark.parametrize("actor", ["o", "ops", "ops.team_1-b", "a" + "b" * 31])
def test_a_grammar_valid_actor_passes_the_grammar(world, actor):
    world.raise_event(HALT, "halted")
    world.plant_latch(halt=True, drawdown=Decimal("-0.04"))
    code, out = halt(world, actor=actor)
    assert (code, out["released"]) == (0, "halt")
    assert read_state(world.latch_path).resets[-1].actor == actor


@pytest.mark.parametrize(
    "isin",
    ["INE002A0101", "INE002A010188", "ine002a01018", BAD_CHECK_DIGIT, "", "INE002A0101!", "US0378331005", RELIANCE + "\n"],
    ids=["short", "long", "lowercase", "bad_check_digit", "empty", "punctuation", "not_indian", "trailing_newline"],
)
def test_a_bad_isin_is_refused_before_anything_is_read_or_opened(planted, isin):
    planted.raise_event(ACTION, "split", isin=RELIANCE)
    planted.plant_latch()
    code, out = assert_untouched(planted, lambda: action(planted, isin))
    assert (code, out) == (1, {"refused": "isin_invalid"})


@pytest.mark.parametrize("reason", ["", "   ", "\t\n"])
@pytest.mark.parametrize("command", ["halt", "action"])
def test_a_blank_reason_is_refused_before_anything_is_read_or_opened(planted, reason, command):
    planted.raise_event(HALT, "halted")
    planted.plant_latch(halt=True, drawdown=Decimal("-0.04"))
    run = (lambda: halt(planted, reason=reason)) if command == "halt" else (lambda: action(planted, reason=reason))
    assert assert_untouched(planted, run) == (1, {"refused": "reason_required"})


CANARY_REASONS = [
    (f"the account is {CANARY_ACCOUNT}", "canary_refused"),
    (f"on host {CANARY_HOST.upper()} today", "canary_refused"),
    ("see https://example.test/path for details", "url_refused"),
    ("Bearer abcdefghijklmnopqrstuvwxyz0123456789ABCD", "token_refused"),
    ("it came from 10.20.30.40 last night", "ip_refused"),
    ("café reviewed", "bad_field"),
    ("x" * 501, "bad_field"),
]


@pytest.mark.parametrize(("reason", "expected"), CANARY_REASONS)
def test_a_reason_the_evidence_layer_would_refuse_is_refused_before_the_latch_is_reset(world, reason, expected):
    """The halt is released above the line, so without the pre-screen the 63 reset runs and clears the latch before the
    ``resume`` append is refused (A3): the row would see the latch changed and a ``latch`` record written."""

    world.raise_event(HALT, "halted")
    world.plant_latch(halt=True, drawdown=Decimal("-0.04"))
    before, before_files = semantic(world), files(world)
    code, out = halt(world, reason=reason)
    assert (code, out) == (1, {"refused": expected})
    assert world.calls == 0 and semantic(world) == before and files(world) == before_files
    assert read_state(world.latch_path).halt


@pytest.mark.parametrize(("reason", "expected"), CANARY_REASONS)
def test_the_same_reasons_are_refused_for_a_corporate_action_release(world, reason, expected):
    world.raise_event(ACTION, "split", isin=RELIANCE)
    world.plant_latch()
    before, before_files = semantic(world), files(world)
    assert action(world, reason=reason) == (1, {"refused": expected})
    assert world.calls == 0 and semantic(world) == before and files(world) == before_files


@pytest.mark.parametrize("literal", LITERALS)
def test_a_registered_literal_in_the_actor_is_refused_and_the_latch_never_stores_it(world, literal):
    """Without the actor screen the release succeeds and the literal is written into the latch's reset record (A5)."""

    world.raise_event(HALT, "halted")
    world.plant_latch(halt=True, drawdown=Decimal("-0.04"))
    before, before_files = semantic(world), files(world)
    code, out = halt(world, actor=literal)
    assert (code, out) == (1, {"refused": "canary_refused"})
    assert world.calls == 0 and semantic(world) == before and files(world) == before_files
    assert literal not in world.latch_path.read_text(encoding="utf-8")


def test_the_literals_come_first_then_the_grammar_then_the_pre_screen_then_the_read_only_state(planted):
    planted.raise_event(HALT, "halted")
    planted.plant_latch(halt=True, drawdown=Decimal("-0.04"))
    # 1: no registered source beats a bad actor, a blank reason and an unwired latch path
    for forbidden, expected in ((lambda a: (), "forbidden_literals_empty"), (None, "forbidden_literals_refused:FORBIDDEN_LITERALS_MISSING")):
        for command in (
            lambda: halt(planted, actor="Bad Actor", reason=" ", forbidden=forbidden, latch=None),
            lambda: action(planted, "nope", " ", forbidden=forbidden, latch=None),
        ):
            assert assert_untouched(planted, command) == (1, {"refused": expected})
    # 2 before 3: a blank reason beats a literal in the actor; an invalid actor beats a canary in the reason
    assert assert_untouched(planted, lambda: halt(planted, actor=CANARY_ACCOUNT, reason=" ", latch=None)) == (
        1, {"refused": "reason_required"}
    )
    assert assert_untouched(planted, lambda: halt(planted, actor="Bad Actor", reason=CANARY_ACCOUNT, latch=None)) == (
        1, {"refused": "actor_invalid"}
    )
    # 3 before 4: a canary reason beats the unwired latch path
    assert assert_untouched(planted, lambda: halt(planted, reason=CANARY_ACCOUNT, latch=None)) == (
        1, {"refused": "canary_refused"}
    )
    # 4 before 5: the unwired latch path refuses before the factory
    assert assert_untouched(planted, lambda: halt(planted, latch=None)) == (1, {"refused": cli.NOT_WIRED})


def test_the_default_literal_source_reads_the_private_dir_and_a_canary_in_the_reason_is_refused(world, tmp_path):
    private = tmp_path / "private-with-literals"
    (private / "india").mkdir(parents=True, mode=0o700)
    private.chmod(0o700)
    target = private / "india" / "shadow_forbidden.json"
    target.write_text(json.dumps({"schema_version": 1, "workspace": "india", "literals": [CANARY_ACCOUNT]}), encoding="utf-8")
    target.chmod(0o600)
    world.raise_event(HALT, "halted")
    world.plant_latch(halt=True, drawdown=Decimal("-0.04"))
    before = semantic(world)
    out = io.StringIO()
    code = cli.main(
        argv_for(world, "release-halt", "--actor", "ops", "--reason", f"acct {CANARY_ACCOUNT}")[:7]
        + ["--private-dir", str(private), "--actor", "ops", "--reason", f"acct {CANARY_ACCOUNT}"],
        latch_path_source=lambda a: world.latch_path, runtime_factory=world.factory, out=out,
    )
    assert (code, json.loads(out.getvalue())) == (1, {"refused": "canary_refused"})
    assert world.calls == 0 and semantic(world) == before


# --- the release goes through the portfolio and nothing else --------------------------------------------------------------------------------


def test_a_runtime_whose_portfolio_is_not_a_shadow_portfolio_is_refused_even_if_it_would_clear_everything(world):
    world.raise_event(HALT, "halted")
    world.raise_event(ACTION, "split", isin=RELIANCE)
    world.plant_latch(halt=True, drawdown=Decimal("-0.04"))
    cleared: list[tuple[Any, ...]] = []

    class Duck:
        def release_halt(self, *args: Any, **kwargs: Any) -> dict:
            cleared.append(("halt", args, kwargs))
            return {"seq": 99, "cleared_pause_seqs": [2]}

        def release_corporate_action(self, *args: Any, **kwargs: Any) -> dict:
            cleared.append(("action", args, kwargs))
            return {"seq": 99, "cleared_pause_seqs": [2]}

    for shape in (SimpleNamespace(portfolio=Duck()), SimpleNamespace(runner=SimpleNamespace(portfolio=Duck())), Duck()):
        assert halt(world, factory=lambda args: shape) == (1, {"refused": "release_requires_portfolio"})
        assert action(world, factory=lambda args: shape) == (1, {"refused": "release_requires_portfolio"})
    assert cleared == []


def test_a_runtime_may_hand_the_portfolio_directly_or_through_its_runner(world):
    world.raise_event(HALT, "halted")
    world.plant_latch(halt=True, drawdown=Decimal("-0.04"))
    portfolio = world.build_portfolio()
    code, out = halt(world, factory=lambda args: SimpleNamespace(runner=SimpleNamespace(portfolio=portfolio)))
    assert (code, out["released"]) == (0, "halt")


def test_the_runner_exposes_its_portfolio_read_only():
    runner = ShadowRunner.__new__(ShadowRunner)
    sentinel = object()
    runner._portfolio = sentinel
    assert runner.portfolio is sentinel
    with pytest.raises(AttributeError):
        runner.portfolio = object()  # type: ignore[misc]


def test_the_release_paths_in_the_cli_call_nothing_that_writes_a_resume_record_or_a_latch():
    """A1/T-64-40: no CLI path writes a resume record, calls ``policy.resume`` for a release or resets a latch itself."""

    tree = ast.parse((REPO / "backend" / "shadow_india" / "__main__.py").read_text(encoding="utf-8"))
    wanted = {"_release", "_release_checks", "_release_read_only_state", "_screen_release_text"}
    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in wanted]
    assert {f.name for f in functions} == wanted
    called: set[str] = set()
    for function in functions:
        for node in ast.walk(function):
            if isinstance(node, ast.Call):
                called.add(node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "?"))
    assert not called & {"resume", "reset", "append", "write_state", "load", "evaluate_session", "raise_event", "open", "create"}
    assert {"release_halt", "release_corporate_action"} <= called
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    assert "LatchStore" not in imported and "write_state" not in imported


# --- release-halt, end to end -------------------------------------------------------------------------------------------------------------


def test_release_halt_above_the_line_resets_the_latch_then_writes_one_resume_and_the_next_buy_is_no_longer_blocked(world):
    world.raise_event(HALT, "halted")
    world.plant_latch(halt=True, drawdown=Decimal("-0.04"), last_equity=Decimal("48000"))
    seqs = [seq for _event, seq in [(p.event, p.seq) for p in policy.state_from_records(read_records(world.root, RUN_ID)).open_pauses]]
    code, out = halt(world)
    assert code == 0 and out["released"] == "halt" and out["cleared_pause_seqs"] == seqs and out["rebased"] is False
    state = read_state(world.latch_path)
    assert not state.halt and state.resets[-1].actor == "ops" and state.halt_anchor is None
    assert world.open_pauses() == []
    kinds = [r["kind"] for r in read_records(world.root, RUN_ID)]
    assert kinds[-2:] == ["latch", "resume"] and out["seq"] == len(kinds)
    assert read_records(world.root, RUN_ID)[-1]["reason"] == "reviewed the recovery"
    assert world.build_portfolio().entry_block_reasons() == ()


def test_release_halt_at_or_below_the_line_without_the_flag_is_refused_and_the_halt_stays_in_force(world):
    """A2: with the flag forced on this row would see the halt cleared."""

    world.raise_event(HALT, "halted")
    world.plant_latch(halt=True, drawdown=Decimal("-0.10"), last_equity=Decimal("45000"))
    before = semantic(world)
    code, out = halt(world)
    assert (code, out) == (1, {"refused": "halt_release_refused"})
    assert semantic(world) == before and read_state(world.latch_path).halt
    assert world.open_pauses() == [(HALT, "entries")]
    status_code, status = call(world, "status")
    assert status_code == 0 and [p["event"] for p in status["open_pauses"]] == [HALT]
    assert "halt_latch" in world.build_portfolio().entry_block_reasons()


def test_release_halt_with_the_rebase_flag_accepts_the_loss_restarts_only_the_minus_8_test_and_keeps_the_peak(world):
    world.raise_event(HALT, "halted")
    world.plant_latch(halt=True, drawdown=Decimal("-0.10"), last_equity=Decimal("45000"))
    peak = read_state(world.latch_path).peak
    code, out = halt(world, "--rebase-halt-anchor", reason="accept the loss")
    assert code == 0 and out["rebased"] is True and out["released"] == "halt"
    state = read_state(world.latch_path)
    assert not state.halt and state.halt_anchor == Decimal("45000") and state.peak == peak
    assert state.resets[-1].rebase_halt_anchor is True
    assert world.open_pauses() == []


def test_a_crash_between_the_latch_reset_and_the_resume_record_is_finished_by_a_rerun_that_writes_only_the_record(world):
    world.raise_event(HALT, "halted")
    world.plant_latch(halt=True, drawdown=Decimal("-0.04"))
    world.die_on = "resume"
    with pytest.raises(Died):
        halt(world)
    world.die_on = None
    assert not read_state(world.latch_path).halt and world.open_pauses() == [(HALT, "entries")]  # reset done, record lost
    kinds_before = [r["kind"] for r in read_records(world.root, RUN_ID)]
    latch_before = read_state(world.latch_path)
    code, out = halt(world)
    assert code == 0 and out["rebased"] is False and len(out["cleared_pause_seqs"]) == 1
    after = read_records(world.root, RUN_ID)
    assert [r["kind"] for r in after] == kinds_before + ["resume"]  # no second latch record: only the missing record
    assert read_state(world.latch_path) == latch_before and world.open_pauses() == []


def test_a_release_with_no_halt_latched_and_a_pause_open_writes_only_the_record_and_never_resets_the_latch(world):
    world.raise_event(HALT, "halted")
    world.plant_latch(halt=False)
    latch_before = read_state(world.latch_path)
    code, out = halt(world)
    assert code == 0 and read_state(world.latch_path) == latch_before and world.open_pauses() == []


def test_the_halt_is_the_only_pause_release_halt_clears(world):
    world.raise_event(HALT, "halted")
    world.raise_event("stale_data", "an unrelated pause")
    world.raise_event(ACTION, "split", isin=RELIANCE)
    world.plant_latch(halt=True, drawdown=Decimal("-0.04"))
    assert halt(world)[0] == 0
    assert sorted(event for event, _scope in world.open_pauses()) == sorted(["stale_data", ACTION])


# --- release-corporate-action, end to end ---------------------------------------------------------------------------------------------------


def test_a_flat_name_is_released_with_one_resume_record_naming_only_its_pause(world):
    world.raise_event(ACTION, "split", isin=RELIANCE)
    world.raise_event(ACTION, "bonus", isin=TCS)
    world.plant_latch()
    own = [p.seq for p in policy.state_from_records(read_records(world.root, RUN_ID)).open_pauses if p.scope == f"isin:{RELIANCE}"]
    code, out = action(world)
    assert (code, out["released"], out["cleared_pause_seqs"], out["rebased"]) == (0, "corporate_action", own, False)
    assert world.open_pauses() == [(ACTION, f"isin:{TCS}")]
    assert read_records(world.root, RUN_ID)[-1]["reason"] == "bonus quantity reconciled"


def test_a_held_name_the_ledger_has_no_adjustment_for_is_refused_corporate_action_not_booked_and_stays_paused(tmp_path):
    """A6: with ``_adjustment_booked`` forced true on the real portfolio this row would see the pause cleared."""

    w = make_world(tmp_path, holding=True)
    try:
        w.raise_event(ACTION, "split", isin=RELIANCE)
        w.plant_latch()
        before = semantic(w)
        code, out = action(w)
        assert (code, out) == (1, {"refused": "corporate_action_not_booked"})
        assert semantic(w) == before and w.open_pauses() == [(ACTION, f"isin:{RELIANCE}")]
    finally:
        w.close()


def test_the_portfolio_refuses_unknown_instrument_for_a_pause_on_a_name_the_run_never_admitted(world):
    world.raise_event(ACTION, "split", isin=HDFCBANK)
    world.plant_latch()
    before = semantic(world)
    assert action(world, HDFCBANK) == (1, {"refused": "unknown_instrument"})
    assert semantic(world) == before and world.calls == 1


def test_a_halve_open_on_the_paused_name_blocks_the_release_even_with_the_adjustment_booked(tmp_path):
    """A7: with the halve check skipped this row would see the pause cleared. The real ledger has no adjustment record,
    so the account is the portfolio tests' adjustment-capable fixture."""

    fake = FakePortfolioWorld(tmp_path, symbols=("AAA", "BBB"))
    fake.account.buy("AAA", 100, "500")
    fake.mark(FAKE_D1, AAA="500")
    fake.mark(FAKE_D2, AAA="460")  # -8%: the halt latches and a halve is owed on AAA
    assert fake.latch.load().open_exits[FAKE_A].reason == "halve"
    fake.portfolio.mark_session(FAKE_D3, {FAKE_A: Decimal("230")}, structural_actions={FAKE_A: "bonus"})
    fake.account.adjust("AAA", 200, kind="bonus", ex_date=FAKE_D3)
    assert [(p.event, p.scope) for p in policy.run_state(fake.store).open_pauses if p.event == ACTION] == [(ACTION, f"isin:{FAKE_A}")]
    shim = SimpleNamespace(root=fake.root, run_id="run1", tmp=tmp_path, latch_path=fake.latch_path)
    shim.factory = lambda args: SimpleNamespace(portfolio=fake.restart().portfolio)
    before = read_records(fake.root, "run1")
    code, out = call(shim, "release-corporate-action", "--isin", FAKE_A, "--reason", "bonus booked", latch=lambda a: fake.latch_path, factory=shim.factory)
    assert (code, out) == (1, {"refused": "exit_owed_in_pre_adjustment_units"})
    assert [r for r in read_records(fake.root, "run1") if r["seq"] > before[-1]["seq"]] == []
    # control: once the halve is closed by a flatten the same release goes through
    fake.account.sell("AAA", 200, "230")
    code, out = call(shim, "release-corporate-action", "--isin", FAKE_A, "--reason", "flat now", latch=lambda a: fake.latch_path, factory=shim.factory)
    assert (code, out["released"]) == (0, "corporate_action")


# --- crash repair and recovery writes: W1, W2, W3a, W3b, H ------------------------------------------------------------------------------------


def crashed_mark(w: World, *, die: bool) -> None:
    """RELIANCE held; one session marked. With ``die`` the process dies after the latch write: the latch shows D1 and
    its ``evaluation_inputs`` line exists, the ``evaluation_commit`` does not."""

    w.plant_latch()  # the ledger has a fill: the latch cannot be started fresh
    portfolio = w.build_portfolio()
    if die:
        real = portfolio._latch.evaluate_session

        def evaluate_then_die(*args: Any, **kwargs: Any) -> Any:
            real(*args, **kwargs)
            raise Died

        portfolio._latch.evaluate_session = evaluate_then_die  # type: ignore[method-assign]
        with pytest.raises(Died):
            portfolio.mark_session(D1, {RELIANCE: Decimal("100")})
    else:
        portfolio.mark_session(D1, {RELIANCE: Decimal("100")})


def commit_of(w: World) -> dict[str, Any]:
    commits = [r for r in read_records(w.root, w.run_id) if r["kind"] == "evaluation_commit"]
    assert len(commits) == 1
    return {k: commits[0][k] for k in ("as_of_date", "position_mark_seqs", "equity")}


def test_w1_a_missing_evaluation_commit_is_repaired_once_by_the_refused_release_and_equals_the_normal_one(tmp_path):
    (tmp_path / "control").mkdir()
    (tmp_path / "crashed").mkdir()
    control = make_world(tmp_path / "control", holding=True)
    crashed = make_world(tmp_path / "crashed", holding=True)
    try:
        crashed_mark(crashed, die=True)
        crashed.raise_event(ACTION, "split", isin=RELIANCE)
        before = semantic(crashed)
        assert not [r for r in before["records"] if r["kind"] == "evaluation_commit"]
        assert action(crashed) == (1, {"refused": "corporate_action_not_booked"})
        after = semantic(crashed)
        assert [r["kind"] for r in after["records"][len(before["records"]):]] == ["evaluation_commit"]  # W1: exactly one
        assert {k: v for k, v in after.items() if k != "records"} == {k: v for k, v in before.items() if k != "records"}
        assert action(crashed)[0] == 1 and semantic(crashed) == after  # a rerun adds nothing

        crashed_mark(control, die=False)
        control.close()
        assert commit_of(control) == commit_of(crashed)  # the same line the decide's own mark writes
    finally:
        control.close()
        crashed.close()


def test_w2_a_latch_session_with_no_evaluation_inputs_aborts_the_run_with_exactly_one_abort_event(world):
    world.raise_event(ACTION, "split", isin=RELIANCE)
    world.plant_latch(last_session=D1, last_equity=Decimal("50000"))
    before = semantic(world)
    code, out = action(world)
    assert (code, out) == (3, {"aborted": "state_corrupt"})
    after = semantic(world)
    new = after["records"][len(before["records"]):]
    assert [(r["kind"], r["event"]) for r in new] == [("abort", "state_corrupt")]
    assert {k: v for k, v in after.items() if k != "records"} == {k: v for k, v in before.items() if k != "records"}
    assert action(world)[0] == 1  # the run is aborted: the next release refuses resume_refused_abort
    assert action(world) == (1, {"refused": "resume_refused_abort"})


@pytest.mark.parametrize("command", ["halt", "action"])
def test_an_unreadable_latch_is_passed_on_so_the_portfolio_records_state_corrupt_and_nothing_else(world, command):
    """A11: a CLI that refused this itself would leave no abort record."""

    world.raise_event(HALT if command == "halt" else ACTION, "planted", isin=None if command == "halt" else RELIANCE)
    world.plant_latch(halt=True, drawdown=Decimal("-0.04"))
    world.latch_path.chmod(0o644)  # permissions_too_open: read_state refuses it
    before = semantic(world)
    code, out = halt(world) if command == "halt" else action(world)
    assert (code, out) == (3, {"aborted": "state_corrupt"})
    after = semantic(world)
    assert [(r["kind"], r["event"]) for r in after["records"][len(before["records"]):]] == [("abort", "state_corrupt")]
    assert {k: v for k, v in after.items() if k != "records"} == {k: v for k, v in before.items() if k != "records"}
    assert world.calls == 1


def test_w3a_a_submitting_order_goes_unknown_with_one_recovery_event_and_nothing_else_changes(tmp_path):
    w = make_world(tmp_path, submitting=True)
    try:
        w.raise_event(ACTION, "split", isin=HDFCBANK)
        w.plant_latch()
        before = semantic(w)
        assert [a.state for a in before["attempts"]] == ["SUBMITTING"] and before["orders"]["plant-1"].state == "SUBMITTING"
        assert action(w, HDFCBANK) == (1, {"refused": "unknown_instrument"})  # a portfolio refusal, after the open
        after = semantic(w)
        assert [(a.proposal_id, a.state) for a in after["attempts"]] == [("plant-1", "UNKNOWN")]
        assert after["orders"]["plant-1"].state == "UNKNOWN"
        assert [e.event_type for e in after["events"][len(before["events"]):]] == ["STARTUP_RECOVERY"]
        assert after["events"][: len(before["events"])] == before["events"]
        assert after["events"][-1].payload == {"reason_code": "ABANDONED_SUBMISSION"}
        for key in ("records", "latch", "requotes", "reservations", "positions"):
            assert after[key] == before[key], key
        assert {t: rows for t, rows in after["sql"].items() if t != "order_projection"} == {
            t: rows for t, rows in before["sql"].items() if t != "order_projection"
        }
        assert [r[0] for r in after["sql"]["order_projection"]] == [r[0] for r in before["sql"]["order_projection"]]
        assert action(w, HDFCBANK)[0] == 1 and semantic(w) == after  # idempotent: a second open finds nothing to recover
    finally:
        w.close()


def test_w3b_an_evaluated_requote_is_blocked_with_one_requote_event_and_nothing_else_changes(tmp_path):
    w = make_world(tmp_path, requote=True)
    try:
        w.raise_event(ACTION, "split", isin=HDFCBANK)
        w.plant_latch()
        before = semantic(w)
        assert [r.state for r in before["requotes"]] == ["EVALUATED"]
        assert action(w, HDFCBANK) == (1, {"refused": "unknown_instrument"})
        after = semantic(w)
        assert [r.state for r in after["requotes"]] == ["BLOCKED_RESTART_REQUIRES_FRESH_EVIDENCE"]
        assert [r[0] for r in after["sql"]["requote_events"]] == [r[0] for r in before["sql"]["requote_events"]] + [2]
        assert after["sql"]["requote_events"][-1][2:5] == ("REQUOTE_BLOCKED", "EVALUATED", "BLOCKED_RESTART_REQUIRES_FRESH_EVIDENCE")
        for key in ("records", "latch", "attempts", "events", "orders", "reservations", "positions"):
            assert after[key] == before[key], key
        assert after["sql"]["order_projection"] == before["sql"]["order_projection"]
        assert action(w, HDFCBANK)[0] == 1 and semantic(w) == after
    finally:
        w.close()


def test_h_an_unanchored_record_is_anchored_by_the_open_and_the_semantic_state_is_equal(world):
    store = world.open_evidence()

    def die(name: str) -> None:
        if name == "after_fsync":  # written and fsynced, the anchor not yet advanced
            raise Died

    store._checkpoint = die  # type: ignore[assignment]
    with pytest.raises(Died):
        policy.raise_event(store, ACTION, "split", isin=HDFCBANK)
    world.plant_latch()
    before = semantic(world)  # read-only: it already sees the unanchored record
    assert world.open_pauses() == [(ACTION, f"isin:{HDFCBANK}")]
    anchor_before = (world.run_dir / "evidence.anchor").read_text()
    assert action(world, HDFCBANK) == (1, {"refused": "unknown_instrument"})
    assert semantic(world) == before and len(before["records"]) == len(semantic(world)["records"])
    assert (world.run_dir / "evidence.anchor").read_text() != anchor_before  # anchored now
    assert not list(world.run_dir.glob("*aborted*"))


def test_h_a_torn_tail_is_archived_by_the_open_and_the_semantic_state_is_equal(world):
    world.raise_event(ACTION, "split", isin=HDFCBANK)
    world.plant_latch()
    world.tear()
    before = semantic(world)  # read-only: the torn tail is ignored and left in the log
    assert not (world.run_dir / TORN_DIR).exists()
    assert action(world, HDFCBANK) == (1, {"refused": "unknown_instrument"})
    assert semantic(world) == before
    assert len(list((world.run_dir / TORN_DIR).iterdir())) == 1
    assert not (world.run_dir / LOG_NAME).read_bytes().endswith(b'{"kind":"pa')
    assert not list(world.run_dir.glob("*aborted*"))


# --- the factory is built from the right pieces: no second path -------------------------------------------------------------------------------


def test_nothing_in_a_release_run_calls_authorize_halt_or_the_relay(world, monkeypatch):
    """The CLI holds no relay, signer or order client: its imports and calls are the only way it could reach one."""

    tree = ast.parse((REPO / "backend" / "shadow_india" / "__main__.py").read_text(encoding="utf-8"))
    names = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)} | {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert not names & {"authorize", "mint", "orders", "OrdersClient", "RelayClient", "sign", "signer", "kill_switch"}
