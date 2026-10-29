"""Phase 64-04 Task 2: the D-09 offline exercises and the fake-signer containment rows.

What is real: the runner's decide and close steps, the real in-process VM order app (``OrderStack`` with its pipeline, audit
chain, kill reader and state store), the relay and orders clients talking to it, the evidence store, the simulation ledger
and the 63 Mac approval path. What is fake: Breeze (a recording transport), the loopback metadata server, the quote and
reference ports and the signer. The signer is a P-256 key generated for each test (``shadow_india_fake_signer_support``).

Never here: a real metadata server, VM, broker, Breeze, ICICI, GCP, key, Keychain item or ``private/`` file. Every row ends
with the same standing assertions: the VM saw only mints (plus the 63 rows' own authorize), Breeze saw no non-GET, the
metadata fake saw GET only, and every ``http.client`` connection the process opened (the kill reader's client) went to the one loopback fake.

Rows that need the 64-04 close step or 64-05 acceptance are skipped with a reason below, not faked, and the offline_exercise
run class is a skip too (it does not exist in ``evidence.RUN_CLASSES``). This file edits no product code.
"""

from __future__ import annotations

import ast
import base64
import dataclasses
import inspect
import json
import pkgutil
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import shadow_india
import shadow_india.runner as runner_module
import test_shadow_india_runner as rt
import relay_orders_support as ros
from shadow_india import __main__ as cli
from shadow_india import policy
from shadow_india.admission import ShadowAbort
from shadow_india.evidence import RUN_CLASSES, EvidenceCorrupt, EvidenceStore
from shadow_india.runner import FillOutcome, FillQuery, session_id_for
from shadow_india_fake_signer_support import (
    ConnectionSpy,
    ExerciseMetadata,
    ExerciseStack,
    FakeSigner,
    NotLoopback,
    is_loopback_host,
    pinned_fixture_keys,
    require_loopback,
)
from gateway_vm.transport import HttpResponse
from test_gateway_orders_routes import KILL_PATH, MetadataFake, VECTORS, master_zip, quote_row

from test_shadow_india_admission import _FrozenDatetime
from test_shadow_india_runner import MINT, S
import execution.service as service_module

ROOT = Path(__file__).resolve().parents[2]
SUPPORT_MODULE = "shadow_india_fake_signer_support"

# --- the exercise world ----------------------------------------------------------------------------------------------------


@dataclass
class Exercise:
    """One decide-step world: the runner tests' ``World`` over a VM pinned to this test's fake signer."""

    world: rt.World
    metadata: ExerciseMetadata
    signer: FakeSigner
    spy: ConnectionSpy
    tmp: Path

    def __getattr__(self, name: str) -> Any:
        return getattr(self.world, name)


def serve(metadata: MetadataFake) -> None:
    threading.Thread(target=metadata.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True).start()


@pytest.fixture()
def signer() -> FakeSigner:
    return FakeSigner.generate()


@pytest.fixture()
def runner_patches(monkeypatch):
    """What the decide step's own tests patch (autouse there): the service clock follows the injected clock, the index
    lists read as seeded, and the band and calendar reads are fixed. Not autouse here: the 63 rows run on the real clock."""

    class Frozen(_FrozenDatetime):
        @classmethod
        def now(cls, tz=None):
            current = rt.Clock.current.now if rt.Clock.current is not None else rt.NOW
            return current.astimezone(tz) if tz is not None else current.astimezone(timezone.utc).replace(tzinfo=None)

    monkeypatch.setattr(service_module, "datetime", Frozen)
    monkeypatch.setattr(
        runner_module, "latest_index_list", lambda store, name, *, fetched_by=None: SimpleNamespace(list_name=name)
    )
    monkeypatch.setattr(runner_module, "band_for_upcoming_session", lambda *a, **k: SimpleNamespace(status="fixed"))
    monkeypatch.setattr(runner_module, "previous_session", lambda calendar, session: rt.AS_OF)


@pytest.fixture()
def spy(monkeypatch) -> ConnectionSpy:
    return ConnectionSpy(monkeypatch)


@pytest.fixture()
def make_exercise(tmp_path, monkeypatch, signer, spy, runner_patches):
    """``make(body=..., status=..., delay=...)``: a decide-step world whose kill flag answers as told."""

    built: list[Exercise] = []
    servers: list[MetadataFake] = []

    def make(*, body: bytes | None = b"blocked", status: int = 200, delay: float = 0.0, entries: tuple[str, ...] = ("AAA", "BBB", "CCC")):
        metadata = ExerciseMetadata(body=body, status=status, delay=delay)
        serve(metadata)
        servers.append(metadata)
        # the runner tests' world builder reads these module names: the key the VM pins and the stack it builds
        monkeypatch.setattr(rt, "PRIMARY", signer.as_vector())
        monkeypatch.setattr(rt, "OrderStack", ExerciseStack.with_signer(signer))
        script = rt.Script()
        monkeypatch.setattr(runner_module, "evaluate_session", script)
        monkeypatch.setattr(runner_module, "held_names_quarantined", script.held_names_quarantined)
        vm = rt.Vm(tmp_path, metadata)
        world = rt.build_world(tmp_path, vm, script=script)
        script.result = rt.evaluated(*entries)
        exercise = Exercise(world, metadata, signer, spy, tmp_path)
        built.append(exercise)
        return exercise

    yield make
    for exercise in built:
        exercise.world.ledger.close()
    # standing assertions, run once the test body has finished
    for exercise in built:
        vm = exercise.world.vm
        assert {call for call in vm.http.order_calls()} <= {rt.MINT}, "the runner never authorizes, halts or resets"
        assert set(exercise.metadata.methods) <= {"GET"}
        assert vm.stack.transport.non_get() == [] and vm.stack.transport.order_endpoint_non_get() == []
        spy.assert_all_loopback(exercise.metadata)
    for metadata in servers:
        metadata.shutdown()
        metadata.server_close()


def teach_the_vm_the_names(exercise: Exercise, *symbols: str) -> None:
    """The VM's own reads (quote, tick reference, security master) know the runner's names, so a mint can pass every rule."""

    transport = exercise.world.vm.stack.transport
    rows = [(str(1001 + i), s, "EQ", rt.ISINS[s]) for i, s in enumerate(symbols)]
    for symbol in symbols:
        transport.quote_book[symbol] = quote_row(symbol, 100.0, 90.0, 110.0, 99.8)
        transport.bar_book[symbol] = {date(2026, 9, 30): 99.8}
    transport.master = HttpResponse(200, {}, master_zip(rows))


def kill_entries(exercise: Exercise) -> list[dict[str, Any]]:
    return rt.kill_entries(exercise.world.vm)


def open_events(exercise: Exercise) -> list[str]:
    return rt.open_events(exercise.world)


def read_only_reads_answer(exercise: Exercise) -> None:
    """The relay's read-only order reads (state, audit) answer 200 while the kill flag blocks every order."""

    state = exercise.vm.orders.state()
    assert state.kill != "enabled"
    entries = exercise.vm.orders.audit_all()
    assert entries and all(e.raw["decision"] == "REFUSED" for e in entries)


# =====================================================================================================================
# containment of the exercise itself: its own run directory, its own state, nothing deployed
# =====================================================================================================================


def test_an_exercise_runs_in_its_own_tmp_run_directory_and_opens_no_other_database(make_exercise, tmp_path, monkeypatch):
    opened: list[str] = []
    real = sqlite3.connect

    def watch(database, *args, **kwargs):
        opened.append(str(database))
        return real(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", watch)
    ex = make_exercise()
    ex.decide()
    ex.runner.close(S)
    assert ex.world.root.resolve().is_relative_to(tmp_path.resolve()) and (ex.world.root / rt.RUN).is_dir()
    assert ex.world.vm.stack.state_dir.resolve().is_relative_to(tmp_path.resolve())
    assert ex.world.vm.audit_path.resolve().is_relative_to(tmp_path.resolve())
    on_disk = [Path(d).resolve() for d in opened if d != ":memory:"]
    assert on_disk and all(path.is_relative_to(tmp_path.resolve()) for path in on_disk)
    assert ex.evidence.manifest.run_class == "plumbing"  # see the skip below: offline_exercise is not a run class yet
    deployed = (Path.home() / "Library" / "Application Support" / "Growin", ROOT / "private")
    assert not any(ex.world.root.resolve().is_relative_to(base.resolve()) for base in deployed)


def test_evidence_is_labelled_offline_exercise():
    assert "offline_exercise" not in RUN_CLASSES  # the label the plan asks for does not exist
    pytest.skip("needs product code: evidence.RUN_CLASSES has no offline_exercise, and this PR edits tests only")


def test_acceptance_refuses_any_offline_exercise_record_inside_a_counted_run():
    modules = {module.name for module in pkgutil.iter_modules(shadow_india.__path__)}
    assert not [name for name in modules if "accept" in name], "64-05 acceptance landed: replace this skip with the row"
    pytest.skip("needs 64-05 acceptance")


def test_a_restart_between_decide_and_close_pauses_and_recovers_through_sl9():
    # Task 1's close step takes a fill source on the runner's ports. Today ``close`` takes only the session and the
    # default port knows nothing after the submission. Either one changing means Task 1 landed: write the row.
    assert tuple(inspect.signature(runner_module.ShadowRunner.close).parameters) == ("self", "session")
    fills = {f.name: f for f in dataclasses.fields(runner_module.RunnerPorts)}["fills"]
    assert fills.default_factory is runner_module.NoPostSubmissionEvidence, "Task 1 landed: replace this skip with the row"
    pytest.skip("needs 64-04 Task 1 close step")


# =====================================================================================================================
# the kill flag: enabled, 404, 503, other bytes, timeout
# =====================================================================================================================


def test_metadata_enabled_the_vm_mints_a_challenge_and_the_runner_aborts_before_anything_reaches_authorize(make_exercise):
    """Break-proof: accept a minted challenge and this row fails (it is the runner's own break-proof 1, here over a real mint)."""
    ex = make_exercise(body=b"enabled")
    teach_the_vm_the_names(ex, "AAA", "BBB", "CCC")
    report = ex.decide()
    state = policy.run_state(ex.evidence)
    assert report.status == "aborted" and list(state.abort_events) == ["vm_challenge_or_success"]
    entries = rt.audit_entries(ex.vm)
    assert [(e["decision"], e["kill"]) for e in entries] == [("CHALLENGED", "enabled")]  # the VM really minted one
    assert ex.records("vm_decision") == []  # a challenge is never recorded as a decision
    assert ex.order_posts() == [MINT]  # no second intent after the abort
    assert not any(call[1].endswith("/authorize") for call in ex.vm.http.calls)
    assert ex.vm.stack.transport.order_endpoint_non_get() == []  # zero order requests reached Breeze
    with pytest.raises(policy.PolicyError) as refused:
        ex.runner.resume("the flag was on")
    assert refused.value.code == "resume_refused_abort"
    with pytest.raises(policy.RunAborted):
        ex.decide()
    assert ex.order_posts() == [MINT]


# The 404 and 503 rows carry the body ``enabled`` on purpose: only the status can block them, so a reader that ignored the
# status would read ENABLED and the row would fail.
BLOCKED_ANSWERS = [
    pytest.param(dict(status=404, body=b"enabled"), id="404"),
    pytest.param(dict(status=503, body=b"enabled"), id="503"),
    pytest.param(dict(body=b"ENABLED"), id="upper_case"),
    pytest.param(dict(body=b"enabled\n"), id="trailing_newline"),
    pytest.param(dict(body=b" enabled"), id="leading_space"),
    pytest.param(dict(body=b""), id="empty"),
]


@pytest.mark.parametrize("answer", BLOCKED_ANSWERS)
def test_a_metadata_answer_that_is_not_exactly_enabled_is_blocked_and_the_run_goes_on(make_exercise, answer):
    """Break-proof: let any of these read as ``enabled`` (a reader that took HTTP 404 as ENABLED, say) and the VM mints a
    challenge instead (the abort row above)."""
    ex = make_exercise(**answer)
    teach_the_vm_the_names(ex, "AAA", "BBB", "CCC")  # a kill flag that failed open would mint a challenge here
    report = ex.decide()
    assert ex.metadata.statuses and set(ex.metadata.statuses) == {answer.get("status", 200)}  # what the fake really sent
    assert report.status == "done" and open_events(ex) == [] and not policy.run_state(ex.evidence).aborted
    decisions = rt.decisions(ex.world)
    assert len(decisions) == 3
    assert {d["kill"] for d in decisions} == {"blocked"} and all(d["codes"] == ["kill_switch"] for d in decisions)
    assert [e["decision"] for e in rt.audit_entries(ex.vm)] == ["REFUSED"] * 3
    assert ex.quotes.asked == ["AAA", "BBB", "CCC"]  # read-only data still flowed
    read_only_reads_answer(ex)
    assert ex.order_posts() == [MINT] * 3 and ex.vm.stack.transport.order_endpoint_hits() == 0


def test_a_metadata_timeout_reads_unreadable_the_runner_records_the_label_and_pauses(make_exercise):
    """Break-proof 8: ignore ``unreadable`` in the runner and this row fails (the run goes on after the first intent)."""
    ex = make_exercise(delay=3.0)  # the VM's reader gives up after one second, well before the answer
    report = ex.decide()
    assert report.status == "paused"
    assert open_events(ex) == ["kill_label_unreadable"]
    (decision,) = rt.decisions(ex.world)  # the label is recorded, then nothing else is admitted
    assert decision["kill"] == "unreadable" and decision["codes"] == ["kill_switch"]
    assert len(rt.ids_of(report)) == 1 and ex.order_posts() == [MINT]
    assert ex.metadata.methods and set(ex.metadata.methods) == {"GET"}
    assert ex.decide().stopped == "open_pause"  # the pause holds until the operator resumes
    assert ex.order_posts() == [MINT]
    assert [e["decision"] for e in rt.audit_entries(ex.vm)] == ["REFUSED"]  # read-only reads still flow
    assert ex.vm.orders.state().kill == "unreadable"


# =====================================================================================================================
# stop, halve, flatten: admitted and reserved on the Mac, refused by the kill flag
# =====================================================================================================================


def stop_setup(ex: Exercise) -> None:
    rt.own(ex.world, "AAA", 160)
    rt.premark(ex.world)
    rt.set_closes(ex.world, AAA="85")  # -15% against cost: past the -12% close-based stop


def halve_setup(ex: Exercise) -> None:
    rt.three_down(ex.world, "89")  # -11% each, equity -10.6% from the peak: past the -8% halt, short of -15%


def flatten_setup(ex: Exercise) -> None:
    rt.three_down(ex.world, "80")  # -20% each: past the -15% flatten


EXITS = [
    pytest.param(stop_setup, "stop", {"AAA": 160}, id="stop"),
    pytest.param(halve_setup, "halve", {"AAA": 80, "BBB": 80, "CCC": 80}, id="halve"),
    pytest.param(flatten_setup, "flatten", {"AAA": 160, "BBB": 160, "CCC": 160}, id="flatten"),
]


def exit_sells(ex: Exercise) -> dict[str, dict[str, Any]]:
    return {r["symbol"]: r for r in rt.intents_of(ex.world, "SELL")}


@pytest.mark.parametrize("setup,reason,quantities", EXITS)
def test_an_exit_is_admitted_and_reserved_on_the_mac_and_refused_by_the_kill_flag(make_exercise, setup, reason, quantities):
    """Break-proof: let a kill-refused exit release its reservation (``ShadowPreparation.record_vm_decision``) and this fails."""
    held = {"AAA": 160} if reason == "stop" else {"AAA": 160, "BBB": 160, "CCC": 160}
    ex = make_exercise()
    setup(ex)
    ex.script.result = rt.evaluated("DDD", "EEE", entries=False)
    ex.decide()
    sells = exit_sells(ex)
    assert {s: int(r["quantity"]) for s, r in sells.items()} == quantities
    assert all(r["leg"] == "exit" and r["intent_id"].startswith(f"exit-{reason}-") for r in sells.values())
    snapshot = ex.runner._portfolio.snapshot()
    assert dict(snapshot.open_sells) == {rt.ISINS[s]: q for s, q in quantities.items()}  # reserved on the Mac
    assert {h.instrument.symbol: h.quantity for h in snapshot.holdings} == held  # nothing sold: the order is only reserved
    for symbol, record in sells.items():
        reservation, _created = ex.ledger.get_simulated_reservation(record["intent_id"])
        assert reservation.state == "ACTIVE"
    vm_sells = [e for e in kill_entries(ex) if e["side"] == "sell"]  # and refused by the VM, one entry each
    assert sorted(e["stock_code"] for e in vm_sells) == sorted(quantities)
    assert {(e["decision"], tuple(e["codes"]), e["kill"], e["reason"]) for e in vm_sells} == {
        ("REFUSED", ("kill_switch",), "blocked", reason)
    }
    sell_decisions = [d for d in rt.decisions(ex.world) if d["intent_id"] in {r["intent_id"] for r in sells.values()}]
    assert len(sell_decisions) == len(quantities) and {d["kill"] for d in sell_decisions} == {"blocked"}
    assert ex.runner._portfolio.buys_blocked()
    assert ex.vm.http.count("POST", "/v1/orders/authorize") == 0


# Buys are blocked by the stop and halt latches (and run pauses) an exit leaves behind, not by the open exit itself:
# ``entry_block_reasons`` never reads ``open_exits``. The rows below assert the reason the runner really reports.
NO_BUY = [
    pytest.param(stop_setup, {"stop_open"}, id="stop"),
    pytest.param(halve_setup, {"halt_latch"}, id="halve"),
    pytest.param(flatten_setup, {"halt_latch", "stop_open"}, id="flatten"),
]


def entry_blocked_reasons(report: Any, isin: str) -> set[str]:
    """The reasons the runner itself skipped ``isin``'s entry with (``entry_blocked:a,b``), empty if it did not."""

    reasons: set[str] = set()
    for skipped_isin, why in report.skipped:
        if skipped_isin == isin and why.startswith("entry_blocked:"):
            reasons |= set(why.removeprefix("entry_blocked:").split(","))
    return reasons


def assert_a_slot_is_free(ex: Exercise) -> None:
    """``max_positions`` is not what stops the entry: a free slot means an unblocked entry would be admitted."""

    snapshot = ex.runner._portfolio.snapshot()
    held = {h.instrument.isin for h in snapshot.holdings} | {b.instrument.isin for b in snapshot.open_buys}
    assert len(held) < ex.script.inputs[-1].strategy.params.max_positions


@pytest.mark.parametrize("setup,expected", NO_BUY)
def test_no_buy_is_admitted_while_the_latches_an_exit_leaves_are_open(make_exercise, setup, expected):
    """Break-proof: have the runner ignore ``entry_block_reasons`` (``reasons = ()``) and a new entry is admitted: this fails."""
    ex = make_exercise()
    setup(ex)
    seeds = {r["intent_id"] for r in rt.intents_of(ex.world, "BUY")}
    ex.script.result = rt.evaluated("DDD", "EEE")
    report = ex.decide()
    assert_a_slot_is_free(ex)
    assert expected <= entry_blocked_reasons(report, rt.ISINS["DDD"])  # the runner refused the entry, for the real reason
    assert {r["intent_id"] for r in rt.intents_of(ex.world, "BUY")} == seeds  # only the seed buys: no new entry
    assert ex.runner._portfolio.buys_blocked()


def test_an_unfilled_exit_keeps_its_shares_reserved_until_the_day_expires_then_stays_owed_and_the_stop_latch_blocks_buys(make_exercise):
    """Break-proof: have the runner ignore ``entry_block_reasons`` and the second decide admits a new entry: this fails."""
    ex = make_exercise()
    stop_setup(ex)
    ex.script.result = rt.evaluated("BBB", entries=False)
    ex.decide()
    isin = rt.ISINS["AAA"]
    assert dict(ex.runner._portfolio.snapshot().open_sells) == {isin: 160}
    closed = ex.runner.close(S)  # the default fill port has no post-submission evidence: INCONCLUSIVE, nothing is filled
    assert [o for _p, o in closed.attempts if o == "INCONCLUSIVE"] and len(closed.released) >= 1
    snapshot = ex.runner._portfolio.snapshot()
    assert dict(snapshot.open_sells) == {}  # the day's order is closed, so its sell reservation is released
    assert [(h.instrument.symbol, h.quantity) for h in snapshot.holdings] == [("AAA", 160)]  # the shares are still held
    assert isin in ex.world.latch.load().open_exits  # the exit obligation is still owed
    assert ex.runner._portfolio.buys_blocked()
    buys = len(rt.intents_of(ex.world, "BUY"))
    ex.script.result = rt.evaluated("DDD")  # an entry candidate with a slot free: only the stop latch is in its way
    again_report = ex.decide()
    assert_a_slot_is_free(ex)
    assert "stop_open" in entry_blocked_reasons(again_report, rt.ISINS["DDD"])
    assert len(rt.intents_of(ex.world, "BUY")) == buys
    plan = ex.runner._portfolio.exit_proposals(date(2026, 10, 23))
    (again,) = plan.proposals  # the next decide offers the same exit again, from the held position
    assert plan.waiting == () and (again.reason, again.isin, again.quantity) == ("stop", isin, 160)


def test_a_partial_exit_keeps_the_remaining_shares_reserved_and_the_stop_latch_blocks_buys(make_exercise):
    """Break-proof: have the runner ignore ``entry_block_reasons`` and the second decide admits a new entry: this fails."""
    ex = make_exercise()
    stop_setup(ex)
    ex.script.result = rt.evaluated("BBB", "CCC")
    ex.decide()
    (sell,) = rt.intents_of(ex.world, "SELL")
    ex.runner._prep.book_fill(
        proposal_id=sell["intent_id"], cumulative_quantity=Decimal(60), price=Decimal("85.00"), charges=Decimal(0),
        schedule_sha256=rt.sha("schedule"), scenario_set_sha256=rt.sha("scenario"), attempt_id="att-partial",
    )
    snapshot = ex.runner._portfolio.snapshot()
    assert [(h.instrument.symbol, h.quantity) for h in snapshot.holdings] == [("AAA", 100)]  # 60 sold
    assert dict(snapshot.open_sells) == {rt.ISINS["AAA"]: 100}  # the other 100 are still reserved
    reservation, _created = ex.ledger.get_simulated_reservation(sell["intent_id"])
    assert reservation.state == "ACTIVE"
    assert rt.ISINS["AAA"] in ex.world.latch.load().open_exits and ex.runner._portfolio.buys_blocked()
    assert ex.runner._portfolio.exit_proposals(date(2026, 10, 23)).waiting == (rt.ISINS["AAA"],)  # never offered twice
    buys = len(rt.intents_of(ex.world, "BUY"))
    again = ex.decide()  # a second decide in the window: no second sell, no buy
    assert_a_slot_is_free(ex)
    assert "stop_open" in entry_blocked_reasons(again, rt.ISINS["BBB"])  # the runner refused the entry for the stop latch
    assert len(rt.intents_of(ex.world, "SELL")) == 1 and len(rt.intents_of(ex.world, "BUY")) == buys


# =====================================================================================================================
# lost tunnel mid-decide, restart, VM audit_broken and evidence corruption
# =====================================================================================================================


def test_a_tunnel_lost_before_the_mint_pauses_and_recovery_calls_the_intent_ambiguous_without_a_second_post(make_exercise, monkeypatch):
    ex = make_exercise()
    hold = rt.Hold(ex.vm, monkeypatch, after=1)  # the first mint goes through, then the tunnel drops under the second
    report = ex.decide()
    assert report.status == "paused" and open_events(ex) == ["untyped_response"]
    assert len(rt.ids_of(report)) == 2 and len(ex.records("vm_decision")) == 1  # the third name was never admitted
    assert len(kill_entries(ex)) == 1  # the dropped mint never reached the VM
    again = ex.decide()
    assert again.status == "paused" and again.stopped == "open_pause" and again.intents == []
    hold.release()
    hold.after = 10**6
    ex.runner.resume("the tunnel is back")
    ex.world.clock.now += timedelta(seconds=11)  # past the mint timeout: recovery may settle the intent in doubt
    posts = len(ex.order_posts())
    recovered = ex.decide()
    in_doubt = rt.ids_of(report)[1]
    assert recovered.recovered[in_doubt] == "mint_ambiguous"  # SL-9: no entry on a zero-entry audit
    assert set(recovered.recovered.values()) <= {"vm_decided", "mint_ambiguous"}
    assert len(ex.order_posts()) - posts == 1  # only the third name; the intent in doubt is never re-sent
    assert len(ex.records("vm_ambiguous")) == 1 and len(ex.records("vm_intent")) == 3
    assert [r["session_id"] for r in ex.records("session_excluded")] == [session_id_for(S)]


def test_a_tunnel_lost_after_the_vm_acted_pauses_and_recovery_adopts_the_one_audit_entry(make_exercise, monkeypatch):
    ex = make_exercise()
    real, calls = ex.vm.orders.mint, {"n": 0}

    def mint(body):
        calls["n"] += 1
        if calls["n"] == 2:
            ex.vm.http.timeout_after_send.add(MINT)  # the VM gets this one and acts; the answer is lost
        out = None
        try:
            out = real(body)
        finally:
            ex.vm.http.timeout_after_send.discard(MINT)
        return out

    monkeypatch.setattr(ex.vm.orders, "mint", mint)
    report = ex.decide()
    assert report.status == "paused" and open_events(ex) == ["untyped_response"]
    in_doubt = rt.ids_of(report)[1]
    assert len(kill_entries(ex)) == 2  # the VM did decide the second intent
    assert len(ex.records("vm_decision")) == 1
    ex.runner.resume("the tunnel is back")
    ex.world.clock.now += timedelta(seconds=11)
    posts = len(ex.order_posts())
    recovered = ex.decide()
    assert recovered.recovered[in_doubt] == "vm_decided"  # adopted from the audit, never minted again
    assert len(ex.order_posts()) - posts == 1  # only the third name
    assert len(rt.entries_for(ex.world, in_doubt)) == 1  # exactly one kill_switch entry for the intent
    assert len(ex.records("vm_decision")) == 3 and ex.records("vm_ambiguous") == []


def test_a_restart_after_a_paused_decide_keeps_the_pause_loses_no_record_and_recovers_after_resume(make_exercise, monkeypatch):
    ex = make_exercise()
    hold = rt.Hold(ex.vm, monkeypatch, after=1)
    report = ex.decide()
    before = [(r["seq"], r["entry_sha256"]) for r in ex.evidence.records()]
    again = ex.world.rebuild(clock=ex.world.clock)  # a new process over the same files
    assert open_events(Exercise(again, ex.metadata, ex.signer, ex.spy, ex.tmp)) == ["untyped_response"]
    kept = [(r["seq"], r["entry_sha256"]) for r in again.evidence.records()]
    assert kept[: len(before)] == before  # every record survived the restart
    hold.release()
    hold.after = 10**6
    again.runner.resume("restarted and reviewed")
    again.clock.now += timedelta(seconds=11)
    done = again.decide()
    assert done.recovered[rt.ids_of(report)[1]] == "mint_ambiguous" and len(again.records("vm_intent")) == 3
    ex.world = again  # the standing assertions and the ledger close read the live world


def corrupt_audit(ex: Exercise, monkeypatch) -> None:
    real, mints = ex.vm.orders.mint, {"n": 0}

    def first_then_corrupt(body):
        mints["n"] += 1
        if mints["n"] == 2:
            ex.vm.audit_path.write_bytes(ex.vm.audit_path.read_bytes() + b"{trunc\n")  # tampering, not a crash
        return real(body)

    monkeypatch.setattr(ex.vm.orders, "mint", first_then_corrupt)


def test_a_vm_audit_broken_aborts_resume_refuses_it_and_no_record_is_lost(make_exercise, monkeypatch):
    """Break-proof 3: let ``resume`` accept ``vm_audit_broken`` (``policy.resume``) and this row fails."""
    ex = make_exercise()
    corrupt_audit(ex, monkeypatch)
    report = ex.decide()
    state = policy.run_state(ex.evidence)
    assert report.status == "aborted" and list(state.abort_events) == ["vm_audit_broken"]
    records = [(r["seq"], r["entry_sha256"]) for r in ex.evidence.records()]
    audit_bytes = ex.vm.audit_path.read_bytes()
    assert len(ex.records("vm_decision")) == 1  # the first intent's decision was recorded before the abort
    with pytest.raises(policy.PolicyError) as refused:
        ex.runner.resume("looked at it")
    assert refused.value.code == "resume_refused_abort"
    code, out = rt.run_cli(rt.cli_args(ex.world, "resume", "--reason", "looked at it"))
    assert (code, out) == (1, {"refused": "resume_refused_abort"})
    with pytest.raises(policy.RunAborted):
        ex.decide()
    again = ex.world.rebuild()  # a restart cannot clear it either
    assert policy.run_state(again.evidence).aborted
    with pytest.raises(policy.RunAborted):
        again.decide()
    closed = again.runner.close(S)
    assert closed.outcome == "aborted" and closed.attempts == []
    assert [(r["seq"], r["entry_sha256"]) for r in again.evidence.records()][: len(records)] == records
    assert ex.vm.audit_path.read_bytes() == audit_bytes  # the VM's own file was not touched by any of it
    assert ex.order_posts() == [MINT, MINT]  # nothing was sent after the abort
    ex.world = again


def flip_a_byte(path: Path) -> None:
    data = bytearray(path.read_bytes())
    marker = data.index(b'"kind":"vm_decision"')
    data[marker + 8] ^= 0x01  # inside a committed record
    path.write_bytes(bytes(data))


def drop_a_line(path: Path) -> None:
    lines = path.read_bytes().splitlines(keepends=True)
    del lines[2]
    path.write_bytes(b"".join(lines))


def cut_the_tail(path: Path) -> None:
    data = path.read_bytes()
    path.write_bytes(data[: len(data) - 40])


def remove_the_anchor(path: Path) -> None:
    (path.parent / "evidence.anchor").unlink()


CORRUPTIONS = [
    pytest.param(flip_a_byte, id="flipped_byte"),
    pytest.param(drop_a_line, id="deleted_record"),
    pytest.param(cut_the_tail, id="log_shorter_than_anchor"),
    pytest.param(remove_the_anchor, id="anchor_removed"),
]


@pytest.mark.parametrize("tamper", CORRUPTIONS)
def test_evidence_corruption_aborts_every_open_resume_refuses_it_and_every_byte_is_kept(make_exercise, tamper):
    ex = make_exercise()
    ex.decide()
    ex.ledger.close()
    log = ex.world.root / rt.RUN / "evidence.jsonl"
    tamper(log)
    kept = {p.name: p.read_bytes() for p in (ex.world.root / rt.RUN).iterdir() if p.is_file()}
    with pytest.raises(EvidenceCorrupt) as first:
        EvidenceStore.open(ex.world.root, run_id=rt.RUN, session_id=session_id_for(S), forbidden=rt.CLI_FORBIDDEN)
    sidecar = ex.world.root / rt.RUN / "abort.json"
    assert sidecar.is_file() or any(p.name.startswith("evidence.anchor.aborted.") for p in (ex.world.root / rt.RUN).iterdir())
    for command in ("status", "resume"):
        extra = ("--reason", "x") if command == "resume" else ()
        code, out = rt.run_cli(rt.cli_args(ex.world, command, *extra))
        assert code == 3 and set(out) == {"aborted"}  # abort-class, and resume cannot clear it
    with pytest.raises(EvidenceCorrupt):  # a later open still refuses: the sidecar makes the abort durable
        EvidenceStore.open(ex.world.root, run_id=rt.RUN, session_id=session_id_for(S), forbidden=rt.CLI_FORBIDDEN)
    after = {p.name: p.read_bytes() for p in (ex.world.root / rt.RUN).iterdir() if p.is_file()}
    for name, data in kept.items():  # no record lost: not one byte of the evidence was truncated or rewritten
        if name in after:
            assert after[name] == data, name
    assert set(after) - set(kept) <= {"abort.json"}  # the only new file is the abort sidecar
    assert first.value.code and after[log.name] == kept[log.name]  # the log itself is byte-for-byte what was found


# =====================================================================================================================
# expired approval and a duplicate intent through the 63 path, signed by the fake signer
# =====================================================================================================================

from server import app as _server_app  # noqa: E402,F401  (a cold import ages replay quotes past the 5 s session window)
import pytest_asyncio  # noqa: E402
from execution import AdmissionDecision, ExecutionConflictError, OrderNotPlacedError  # noqa: E402
from execution.approval import RelayChallengeRefused  # noqa: E402
from execution.relay_dispatcher import RELAY_FORWARD_DISABLED  # noqa: E402
from test_gateway_orders_routes import code_of  # noqa: E402


@pytest_asyncio.fixture
async def signed_world(tmp_path, monkeypatch, signer, spy):
    """The 63-05 Mac SHADOW world with the kill flag ``enabled`` and the VM pinned to this test's fake signer."""

    metadata = ExerciseMetadata(body=b"enabled")
    serve(metadata)
    monkeypatch.setattr(ros, "OrderStack", ExerciseStack.with_signer(signer))
    built = ros.ShadowWorld(tmp_path, metadata, monkeypatch, signer=signer.as_vector())
    await built.start_replay()
    built.enroll_signer()
    try:
        yield built
        ros.standing_assertions(built)
        spy.assert_all_loopback(metadata)
        assert built.vm.state_dir.resolve().is_relative_to(tmp_path.resolve())
    finally:
        await built.app.close_market_data()
        built.close()
        metadata.shutdown()
        metadata.server_close()


async def prepared(world) -> str:
    proposal, admission = world.prepare()
    assert admission.decision is AdmissionDecision.ADMITTED, admission.reason_code
    return proposal["proposal_id"]


@pytest.mark.asyncio
async def test_an_expired_approval_is_refused_on_the_mac_and_by_the_vm_with_the_o6_code(signed_world, signer):
    world = signed_world
    pid = await prepared(world)
    challenge = world.challenge(pid)
    assert json.loads(challenge.signed_payload)["key_id"] == signer.key_id  # the VM's bytes name the fake signer's key
    world.vm.clock.advance(61)  # past the 60 s lifetime
    with pytest.raises(ExecutionConflictError, match="expired"):
        await world.approve(pid, challenge, quote=None)
    assert world.http.count("POST", "/v1/orders/authorize") == 0  # the Mac refuses before anything is sent
    # The same bytes, validly signed by the fake signer, asked of the VM directly: expired, not a bad signature.
    refused = world.vm.authorize(world.token, challenge.challenge_id, signer.sign_b64(challenge.signed_payload))
    assert code_of(refused) == (409, "REPLAY", "challenge_expired")
    last = world.vm.audit(world.token)[-1]
    assert (last["decision"], last["codes"]) == ("REFUSED", ["challenge_expired"])
    assert world.vm.transport.order_endpoint_non_get() == []


@pytest.mark.asyncio
async def test_a_duplicate_intent_is_refused_with_the_o6_code_while_live_and_after_it_is_consumed(signed_world, signer):
    world = signed_world
    pid = await prepared(world)
    first = world.challenge(pid)
    with pytest.raises(RelayChallengeRefused) as outstanding:
        world.challenge(pid)  # the same intent again while its challenge is live
    assert outstanding.value.reason_code == "challenge_outstanding"
    with pytest.raises(OrderNotPlacedError) as verified:
        await world.approve(pid, first)  # signed by the fake signer, verified by the VM, not forwarded
    assert verified.value.reason_code == RELAY_FORWARD_DISABLED
    trail = [(e["decision"], e["codes"]) for e in world.vm.audit(world.token)]
    assert trail == [("CHALLENGED", []), ("REFUSED", ["challenge_outstanding"]), ("VERIFIED_NOT_FORWARDED", [])]
    with pytest.raises(ExecutionConflictError, match="already consumed"):
        await world.approve(pid, first)  # a replayed approval never reaches the VM a second time
    assert world.http.count("POST", "/v1/orders/authorize") == 1
    # The consumed intent minted again, straight at the VM (the Mac-side order has already failed): intent_consumed.
    signed = json.loads(first.signed_payload)
    again = world.vm.mint(world.token, **signed["intent"])
    assert code_of(again) == (409, "REPLAY", "intent_consumed")
    world.vm.restart()
    assert code_of(world.vm.mint(world.token, **signed["intent"])) == (409, "REPLAY", "intent_consumed")  # across a restart
    assert world.vm.transport.order_endpoint_non_get() == []


# =====================================================================================================================
# containment: the fake signer never leaves the tests
# =====================================================================================================================


def test_the_fake_key_is_generated_per_test_and_only_its_public_half_is_written_under_tmp(make_exercise, signer, tmp_path):
    other = FakeSigner.generate()
    assert other.key_id != signer.key_id and other.private_scalar_hex != signer.private_scalar_hex
    ex = make_exercise()
    ex.decide()
    ex.runner.close(S)
    key_file = ex.world.vm.stack.key_path
    assert key_file.resolve().is_relative_to(tmp_path.resolve())
    assert json.loads(key_file.read_text(encoding="ascii")) == {
        "key_id": signer.key_id, "public_key_x963_hex": signer.public_key_x963_hex,
    }
    canary = signer.private_scalar_hex.encode("ascii")
    for path in tmp_path.rglob("*"):  # canary: the private scalar is in no file the exercise wrote
        if path.is_file():
            assert canary not in path.read_bytes(), path


def test_the_fake_keys_id_differs_from_every_pinned_fixture_key_and_the_vm_pins_it_as_a_production_key(make_exercise, signer):
    ids, public = pinned_fixture_keys()
    assert len(ids) >= 2 and signer.key_id not in ids and signer.public_key_x963_hex not in public
    assert signer.key_id not in {VECTORS["keys"][name]["key_id"] for name in VECTORS["keys"]}
    from gateway_vm.orders.verify import PinnedKey

    pinned = PinnedKey(bytes.fromhex(signer.public_key_x963_hex), allow_test_key=False)  # not a committed test key
    assert pinned.key_id == signer.key_id
    ex = make_exercise()
    assert json.loads(ex.world.vm.stack.key_path.read_text())["key_id"] == signer.key_id
    assert rt.PRIMARY["key_id"] == signer.key_id  # and the runner's identity is that key too, not a vector key


def test_the_support_module_lives_under_tests_only():
    import shadow_india_fake_signer_support as support

    here = Path(support.__file__).resolve()
    assert here.is_relative_to(ROOT / "tests" / "backend") and here.name == f"{SUPPORT_MODULE}.py"
    copies = [
        path for base in (ROOT / "backend", ROOT / "gateway", ROOT / "tests")
        for path in base.rglob(f"{SUPPORT_MODULE}*") if ".venv" not in path.parts and "__pycache__" not in path.parts
    ]
    assert copies == [here]


def support_references(root: Path) -> list[str]:
    """Every .py file under ``root`` that imports the support module, by any spelling, or names it in a string."""

    hits = []
    for path in sorted(root.rglob("*.py")):
        if any(part in {".venv", "__pycache__", "node_modules", ".git"} for part in path.parts):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if SUPPORT_MODULE not in text and "FakeSigner" not in text:
            continue
        hits.append(str(path.relative_to(root)))
    return hits


def import_hits(root: Path) -> list[str]:
    """The AST view: an import statement or an ``import_module``/``__import__`` call naming the support module."""

    hits = []
    for path in sorted(root.rglob("*.py")):
        if any(part in {".venv", "__pycache__", "node_modules", ".git"} for part in path.parts):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [alias.name for alias in node.names]
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                names = [node.value]
            if any(SUPPORT_MODULE in name or name == "FakeSigner" for name in names):
                hits.append(str(path.relative_to(root)))
                break
    return hits


def test_nothing_under_backend_or_gateway_imports_or_names_the_fake_signer_support():
    for base in ("backend", "gateway"):
        assert support_references(ROOT / base) == [] and import_hits(ROOT / base) == [], base


@pytest.mark.parametrize(
    "source",
    [
        "import shadow_india_fake_signer_support\n",
        "from shadow_india_fake_signer_support import FakeSigner\n",
        "from shadow_india_fake_signer_support import *\n",
        "import importlib\nmod = importlib.import_module('shadow_india_fake_signer_support')\n",
        "mod = __import__('shadow_india_fake_signer_support')\n",
        "from somewhere import FakeSigner as key\n",
    ],
)
def test_the_import_scan_finds_every_planted_way_to_import_the_support_module(tmp_path, source):
    (tmp_path / "clean.py").write_text("import os\n")
    assert import_hits(tmp_path) == [] and support_references(tmp_path) == []
    (tmp_path / "planted.py").write_text(source)
    assert import_hits(tmp_path) == ["planted.py"] and support_references(tmp_path) == ["planted.py"]


def test_the_kill_reader_base_url_is_loopback_in_every_row_and_a_real_metadata_address_is_refused_before_a_socket(make_exercise, spy):
    assert require_loopback("http://127.0.0.1:8123") and require_loopback("http://[::1]:8123")
    for bad in (
        "http://metadata.google.internal",  # the production default
        "http://169.254.169.254",  # the metadata server's address
        "http://localhost:8123",  # a name is resolved by someone else
        "http://10.0.0.5:80",
    ):
        with pytest.raises(NotLoopback):
            require_loopback(bad)
    assert is_loopback_host("127.0.0.1") and not is_loopback_host("metadata.google.internal")
    import http.client

    with pytest.raises(NotLoopback):
        http.client.HTTPConnection("metadata.google.internal", 80, timeout=1)
    assert spy.refused == ["metadata.google.internal"]
    spy.refused.clear()  # the refusal above was this row's own, on purpose
    spy.connections.clear()
    ex = make_exercise()
    ex.decide()
    ex.runner.close(S)
    assert spy.connections and {host for host, _port in spy.connections} >= {"127.0.0.1"}
    assert {(h, p) for h, p in spy.connections if h != "metadata.google.internal"} == {("127.0.0.1", ex.metadata.server_address[1])}
    assert KILL_PATH.startswith("/computeMetadata/v1/instance/attributes/")
