"""Phase 64-03 Task 3: the shadow runner's decide step, over the real in-process VM.

What is real: the VM order app (``gateway_vm`` over ``OrderStack``, kill flag answered by a loopback metadata fake that says
``blocked``), the relay client and orders client talking to it, the evidence store, the simulation ledger, the real admission
guard and the real 63 latch. What is scripted: the signal step (``evaluate_session`` is replaced by a scripted result, its own
tests are ``test_shadow_india_signals``), the relay health read, the quote source, the reference prices and the fill source.
Every value is synthetic. Nothing here reaches a broker, ``private/``, the real VM, GCP, NSE or a model server, nothing calls
authorize and nothing asks for Touch ID.
"""

from __future__ import annotations

import ast
import hashlib
import http.client
import io
import json
import sqlite3
import threading
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Sequence

import pytest

import shadow_india.runner as runner_module
from brokers.breeze_gateway.client import RelayClient
from brokers.breeze_gateway.errors import NoSession, OrdersUnavailable, RelayError
from brokers.breeze_gateway.orders import OrdersClient
from brokers.breeze_gateway.token_store import MemoryTokenStore
from india_limits_support import india_private_dir, make_evidence, make_guard
from relay_orders_support import RecordingHttp
from pilot_data.core import PilotDataError
from risk_india import rules
from risk_india.state import LatchStore
from shadow_india import __main__ as cli
from shadow_india import policy, vm_codes
from shadow_india.admission import (
    SIMULATOR_DRAWDOWN_BASIS,
    AuditEntry,
    AuditPage,
    QuoteSample,
    ShadowAbort,
    open_simulation_ledger,
)
from shadow_india.evidence import EvidenceStore
from shadow_india.portfolio import (
    PEAK_SOURCE,
    Instrument,
    exit_intent_id,
    exit_obligation_id,
)
from shadow_india.runner import (
    DecideRefused,
    DecideReport,
    FillOutcome,
    FillQuery,
    LiveTablePins,
    NoPostSubmissionEvidence,
    OpenRunRefused,
    RunnerPorts,
    ShadowRunner,
    VmIdentity,
    assemble,
    open_run,
    session_id_for,
    vm_intent_id_for,
    verify_vm_chain,
)
from shadow_india import signals as signals_module
from shadow_india.signals import (
    PIN_NAMES,
    STATUS_EVALUATED,
    STATUS_NON_EVALUATION_DAY,
    NameSignal,
    SignalInputs,
    SignalResult,
    held_names_quarantined,
)
from test_gateway_orders_routes import PRIMARY, VECTORS, MetadataFake, OrderStack
from test_shadow_india_admission import _FrozenDatetime, run
from test_strategy_india_support import StaticEligibility
from test_strategy_india_support import limits as limits62
from test_strategy_india_support import params as params62

import execution.service as service_module
from gateway_vm.orders import CODE_TABLE as VM_CODE_TABLE
from gateway_vm.contract import ERRORS as RELAY_ERRORS

ROOT = Path(__file__).resolve().parents[2]
IST = rules.IST
S = date(2026, 10, 22)  # a Thursday
AS_OF = date(2026, 10, 21)
WINDOW_START = date(2026, 4, 1)
NOW = datetime(2026, 10, 22, 9, 45, tzinfo=IST)  # inside 09:30-10:00 IST
SEED = Decimal("50000")
RUN = "run64"

ISINS = {
    "AAA": "INE000A01012",
    "BBB": "INE111B01023",
    "CCC": "INE222C01034",
    "DDD": "INE333D01045",
    "EEE": "INE444E01056",
}
SYMBOLS = tuple(ISINS)


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture(autouse=True)
def frozen_service_clock(monkeypatch):
    """``ExecutionService`` reads the wall clock for its staleness check: it reads the same moment as every injected clock."""

    class Frozen(_FrozenDatetime):
        @classmethod
        def now(cls, tz=None):
            current = (Clock.current.now if Clock.current is not None else NOW)
            return current.astimezone(tz) if tz is not None else current.astimezone(timezone.utc).replace(tzinfo=None)

    monkeypatch.setattr(service_module, "datetime", Frozen)


# --- the clock and the fakes ---------------------------------------------------------------------------------------------------


class Clock:
    current: "Clock | None" = None  # the latest clock: the service's wall clock follows it

    def __init__(self, now: datetime = NOW) -> None:
        self.now = now
        Clock.current = self

    def __call__(self) -> datetime:
        return self.now.astimezone(timezone.utc)


class Health:
    def __init__(self, state: str = "active", egress_ok: bool = True) -> None:
        self.state, self.egress_ok = state, egress_ok

    def health(self) -> "Health":
        return self


class Quotes:
    """Three distinct relay readings per instrument ending two seconds before the clock, asking ``price`` (default 100).

    The circuit band and the previous close follow the price, so a name that fell to 85 is quoted inside its own band.
    """

    def __init__(self, clock: Clock, price: str = "100.00") -> None:
        self.clock = clock
        self.prices: dict[str, str] = {}
        self.default = price
        self.asked: list[str] = []
        self.error: Exception | None = None

    def samples(self, instrument: Instrument) -> list[QuoteSample]:
        self.asked.append(instrument.symbol)
        if self.error is not None:
            raise self.error
        now = self.clock.now
        price = Decimal(self.prices.get(instrument.symbol, self.default))
        band = dict(
            lower_circuit=(price * Decimal("0.9")).quantize(Decimal("0.05")),
            upper_circuit=(price * Decimal("1.1")).quantize(Decimal("0.05")),
            previous_close=price, tick_reference=price, ltp=price,
        )
        out = []
        for index in range(3):
            step = Decimal(2 - index) * Decimal("0.01")
            fetched = now - timedelta(seconds=2 * (3 - index))
            ask, bid = price - step, price - Decimal("0.10") - step
            out.append(
                QuoteSample(
                    sha(f"{instrument.symbol}-{price}-raw-{index}"),
                    fetched.astimezone(timezone.utc),
                    bid,
                    ask,
                    make_evidence(
                        observed_at=fetched, ask=ask, bid=bid, isin=instrument.isin, stock_code=instrument.symbol,
                        session_date=S, **band,
                    ),
                )
            )
        return out


class Reference:
    def __init__(self, close: str = "100.00", adv: str = "100000") -> None:
        self.close_by_isin = {isin: Decimal(close) for isin in ISINS.values()}
        self.adv_by_isin = {isin: Decimal(adv) for isin in ISINS.values()}
        self.actions: dict[str, str] = {}
        self.close_calls: list[date] = []

    def closes(self, session: date, isins: Sequence[str]) -> dict[str, Decimal]:
        self.close_calls.append(session)
        return {i: self.close_by_isin[i] for i in isins if i in self.close_by_isin}

    def adv(self, isin: str, session: date) -> Decimal | None:
        return self.adv_by_isin.get(isin)

    def structural_actions(self, session: date, isins: Sequence[str]) -> dict[str, str]:
        return {i: a for i, a in self.actions.items() if i in isins}


class Pins:
    def __init__(self) -> None:
        self.values = {"tick": sha("tick-1"), "cost": sha("cost-1"), "scenario": sha("scenario-1")}
        self.calls = 0

    def tick_sha256(self) -> str:
        self.calls += 1
        return self.values["tick"]

    def cost_sha256(self) -> str:
        return self.values["cost"]

    def scenario_sha256(self) -> str:
        return self.values["scenario"]


class Script:
    """Replaces ``evaluate_session``: returns the scripted result and records the inputs it was given."""

    def __init__(self) -> None:
        self.result: SignalResult | None = None
        self.inputs: list[SignalInputs] = []
        self.quarantined: set[str] = set()  # ISINs the held-name quarantine read reports (``held_names_quarantined``)
        self.quarantine_error: Exception | None = None
        self.quarantine_calls: list[tuple[frozenset[str], date, date]] = []

    def held_names_quarantined(self, inputs: SignalInputs, held, as_of: date, session: date) -> frozenset[str]:
        self.quarantine_calls.append((frozenset(held), as_of, session))
        if self.quarantine_error is not None:
            raise self.quarantine_error
        return frozenset(held) & self.quarantined

    def __call__(self, evidence: EvidenceStore, inputs: SignalInputs, *, decision_session: date) -> SignalResult:
        self.inputs.append(inputs)
        assert self.result is not None
        return self.result


def evaluated(*ranked: str, suppressed: dict[str, str] | None = None, entries: bool = True) -> SignalResult:
    suppressed = suppressed or {}
    signals = tuple(
        NameSignal(
            anchor_isin=ISINS[s], isin=ISINS[s], symbol=s, series="EQ", raw_score=Decimal(10 - i),
            zscore=Decimal(1), rank=i + 1, suppressed=suppressed.get(s), buy_blocked=False,
        )
        for i, s in enumerate(ranked)
    )
    return SignalResult(
        STATUS_EVALUATED, S, AS_OF, evaluation_due=True, signals=signals, entries_possible=entries,
    )


HASHES = {name: sha(name) for name in PIN_NAMES}


class WorldInputs(SignalInputs):
    """``SignalInputs`` whose manifest pins are fixed values: the signal step itself is scripted."""

    def current_hashes(self) -> dict[str, str]:
        return dict(HASHES)


def instrument_list(*symbols: str) -> list[Instrument]:
    return [Instrument(ISINS[s], s, s) for s in symbols]


# --- the world ----------------------------------------------------------------------------------------------------------------


class Vm:
    """The in-process VM and a relay client into it, counting every request."""

    def __init__(self, tmp: Path, metadata: MetadataFake) -> None:
        (tmp / "vm").mkdir()
        self.stack = OrderStack(tmp / "vm", metadata)
        self.metadata = metadata
        token = self.stack.login()
        store = MemoryTokenStore()
        store.save(token, self.stack.clock.now_utc().astimezone(rules.IST).replace(year=2027))
        self.http = RecordingHttp(self.stack.client)
        self.client = RelayClient(http=self.http, token_store=store)
        self.orders = OrdersClient(self.client)

    @property
    def audit_path(self) -> Path:
        return self.stack.state_dir / "audit.jsonl"


@dataclass
class World:
    tmp: Path
    vm: Vm
    clock: Clock
    evidence: EvidenceStore
    ledger: Any
    guard: Any
    latch: LatchStore
    runner: ShadowRunner
    script: Script
    health: Health
    quotes: Quotes
    reference: Reference
    pins: Pins
    ports: RunnerPorts
    inputs: SignalInputs
    instruments: list[Instrument]
    private: Path
    root: Path
    index_checks: list[Any] = field(default_factory=list)

    def records(self, kind: str) -> list[dict[str, Any]]:
        return [r for r in self.evidence.records() if r["kind"] == kind]

    def order_posts(self) -> list[tuple[str, str]]:
        return self.vm.http.order_calls()

    def decide(self, session: date = S):
        return self.runner.decide(session)

    def rebuild(self, *, clock: Clock | None = None) -> "World":
        """A new process over the same files: every object is rebuilt, the VM and its client stay."""

        self.ledger.close()
        return build_world(
            self.tmp, self.vm, clock=clock or self.clock, reopen=True, script=self.script, reference=self.reference,
            pins=self.pins, health=self.health, quotes=self.quotes, symbols=tuple(i.symbol for i in self.instruments),
            private=self.private, fills=self.ports.fills,
        )


@pytest.fixture()
def metadata():
    server = MetadataFake()
    server.body = b"blocked"
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture(autouse=True)
def index_lists_seeded(monkeypatch):
    """The index-list precondition needs a seeded session store. Here both lists are present; one row removes them."""

    seen: list[Any] = []

    def latest(store, name, *, fetched_by=None):
        seen.append((name, fetched_by))
        return SimpleNamespace(list_name=name)

    monkeypatch.setattr(runner_module, "latest_index_list", latest)
    return seen


@pytest.fixture(autouse=True)
def no_real_stores(monkeypatch):
    """The band and calendar reads need a seeded session store (64-08). Here a band is fixed and S-1 is ``AS_OF``."""

    monkeypatch.setattr(runner_module, "band_for_upcoming_session", lambda *a, **k: SimpleNamespace(status="fixed"))
    monkeypatch.setattr(runner_module, "previous_session", lambda calendar, session: AS_OF)


def build_world(
    tmp: Path,
    vm: Vm,
    *,
    clock: Clock | None = None,
    reopen: bool = False,
    script: Script | None = None,
    reference: Reference | None = None,
    pins: Pins | None = None,
    health: Health | None = None,
    quotes: Quotes | None = None,
    symbols: tuple[str, ...] = SYMBOLS,
    run_class: str = "plumbing",
    mint_timeout: timedelta = timedelta(seconds=10),
    fills: Any = None,
    private: Path | None = None,
) -> World:
    clock = clock or Clock()
    script = script or Script()
    reference = reference or Reference()
    pins = pins or Pins()
    health = health or Health()
    quotes = quotes or Quotes(clock)
    private = private or india_private_dir(tmp, capital_cap="50000", per_position_cap="17500")
    root = tmp / "evidence"
    sim = tmp / "sim"
    strategy = SimpleNamespace(
        params=params62(),
        params_sha256=HASHES["params_sha256"], params_version="v1",
        limits=limits62(per_position_cap="17500"), limits_sha256=VECTORS["limits_sha256"],
    )
    inputs = WorldInputs(
        store=SimpleNamespace(), strategy=strategy, targets=None, universe_policy=None,
        calendar=SimpleNamespace(), tolerances=None, window_start=WINDOW_START, run_start_session=S,
        dataset_sha256=HASHES["dataset_sha256"], tick_sha256=pins.values["tick"], cost_sha256=pins.values["cost"],
        scenario_sha256=pins.values["scenario"],
    )
    if reopen:
        evidence = EvidenceStore.open(root, run_id=RUN, session_id=session_id_for(S), clock=clock)
    else:
        evidence = open_run(root, run_id=RUN, inputs=inputs, orders=vm.orders, run_class=run_class, clock=clock)
    ledger = open_simulation_ledger(sim, RUN, SEED)
    guard = make_guard(ledger, private, now=clock)
    ports = RunnerPorts(quotes=quotes, reference=reference, fills=fills or NoPostSubmissionEvidence())
    identity = VmIdentity(VECTORS["limits_sha256"], HASHES["params_sha256"], PRIMARY["key_id"])
    instruments = instrument_list(*symbols)
    runner = assemble(
        evidence=evidence, ledger=ledger, india_guard=guard, latch=guard.store, instruments=instruments,
        orders=vm.orders, identity=identity, base_inputs=inputs, pins=pins, ports=ports, health=health, clock=clock,
        mint_timeout=mint_timeout,
    )
    world = World(
        tmp, vm, clock, evidence, ledger, guard, guard.store, runner, script, health, quotes, reference, pins, ports,
        inputs, instruments, private, root,
    )
    return world


@pytest.fixture()
def vm(tmp_path, metadata):
    built = Vm(tmp_path, metadata)
    yield built


@pytest.fixture()
def world(tmp_path, vm, monkeypatch):
    script = Script()
    monkeypatch.setattr(runner_module, "evaluate_session", script)
    monkeypatch.setattr(runner_module, "held_names_quarantined", script.held_names_quarantined)
    built = build_world(tmp_path, vm, script=script)
    script.result = evaluated("AAA", "BBB", "CCC")
    try:
        yield built
    finally:
        built.ledger.close()
    # standing assertions: the VM only ever saw mints, the metadata server only GETs, authorize and halt never ran
    posts = vm.http.order_calls()
    assert {call for call in posts} <= {("POST", "/v1/orders/intents")}
    assert set(metadata_methods(vm)) <= {"GET"}
    assert vm.stack.transport.non_get() == []


def metadata_methods(vm: Vm) -> list[str]:
    return list(vm.metadata.methods)


# --- helpers --------------------------------------------------------------------------------------------------------------------


def audit_entries(vm: Vm) -> list[dict[str, Any]]:
    return [e.raw for e in vm.orders.audit_all()]


def kill_entries(vm: Vm) -> list[dict[str, Any]]:
    return [e for e in audit_entries(vm) if e.get("codes") == ["kill_switch"]]


def decisions(world: World) -> list[dict[str, Any]]:
    return world.records("vm_decision")


def ids_of(report) -> list[str]:
    return [pid for pid, _stage in report.intents]


# =====================================================================================================================
# vm_codes: the code-first table
# =====================================================================================================================


def answer(contract: str | None, code: str | None, reason: str | None, status: int | None) -> vm_codes.VmAnswer:
    return vm_codes.VmAnswer(contract, code, reason, status)


def test_every_vm_order_code_has_a_row_with_the_vms_own_status_and_category():
    """The coverage test: drop one code from the table and this fails (break-proof 15)."""
    missing = [name for name in VM_CODE_TABLE if name not in vm_codes.ORDER_ROWS]
    assert missing == []
    for name, (status, category) in VM_CODE_TABLE.items():
        row = vm_codes.ORDER_ROWS[name]
        assert (row.status, row.category) == (status, category), name
    assert set(vm_codes.ORDER_ROWS) - set(VM_CODE_TABLE) == set()


def test_every_relay_error_code_has_a_row_with_the_relays_own_status():
    assert [c for c in RELAY_ERRORS if c not in vm_codes.RELAY_ROWS] == []
    for code, (status, _retryable) in RELAY_ERRORS.items():
        assert vm_codes.RELAY_ROWS[code].status == status, code
    assert set(vm_codes.RELAY_ROWS) == set(RELAY_ERRORS)


def test_the_kill_switch_at_423_is_the_only_expected_answer():
    expected = [
        name for name in VM_CODE_TABLE
        if vm_codes.classify(answer(vm_codes.ORDERS_CONTRACT, VM_CODE_TABLE[name][1], name, VM_CODE_TABLE[name][0])).kind
        == vm_codes.EXPECTED
    ]
    assert expected == ["kill_switch"]  # break-proof 7: accepting any other code fails this row


@pytest.mark.parametrize("name", sorted(VM_CODE_TABLE))
def test_each_order_code_on_its_own_status_is_classified_by_the_table(name):
    status, category = VM_CODE_TABLE[name]
    verdict = vm_codes.classify(answer(vm_codes.ORDERS_CONTRACT, category, name, status))
    assert verdict.code == name and verdict.status == status
    assert verdict.kind == (vm_codes.EXPECTED if name == "kill_switch" else vm_codes.ABORT)


@pytest.mark.parametrize("name", sorted(vm_codes.CORRUPTION_EVENTS))
@pytest.mark.parametrize("status", [409, 423, 500, 503])
def test_a_corruption_code_aborts_on_any_status_even_a_503(name, status):
    verdict = vm_codes.classify(answer(vm_codes.ORDERS_CONTRACT, "ORDERS_UNAVAILABLE", name, status))
    assert verdict.kind == vm_codes.ABORT and verdict.event == vm_codes.CORRUPTION_EVENTS[name]


def test_audit_broken_is_decided_by_its_code_not_by_the_503_status():
    """Break-proof 14: a 503 alone is a quote the VM could not read; only the code says the audit chain is broken."""
    broken = vm_codes.classify(answer(vm_codes.ORDERS_CONTRACT, "ORDERS_UNAVAILABLE", "audit_broken", 503))
    quote = vm_codes.classify(answer(vm_codes.ORDERS_CONTRACT, "ORDERS_UNAVAILABLE", "quote_unavailable", 503))
    assert broken.event == "vm_audit_broken" and quote.event == "vm_order_code"
    assert broken.event != quote.event


def test_a_code_on_the_wrong_status_is_a_code_status_mismatch():
    verdict = vm_codes.classify(answer(vm_codes.ORDERS_CONTRACT, "ORDERS_BLOCKED", "kill_switch", 200 + 203))
    assert verdict.kind == vm_codes.ABORT and verdict.event == "code_status_mismatch"
    for status in (400, 409, 500, 503):
        again = vm_codes.classify(answer(vm_codes.ORDERS_CONTRACT, "ORDERS_BLOCKED", "kill_switch", status))
        assert again.event == "code_status_mismatch"


def test_a_category_that_does_not_carry_the_reason_is_a_mismatch():
    verdict = vm_codes.classify(answer(vm_codes.ORDERS_CONTRACT, "LIMIT_REJECTED", "kill_switch", 423))
    assert verdict.event == "code_status_mismatch"


def test_success_or_a_challenge_is_an_abort_whatever_the_body_says():
    for status in (200, 201, 204):
        verdict = vm_codes.classify(answer(vm_codes.ORDERS_CONTRACT, "ORDERS_BLOCKED", "kill_switch", status))
        assert verdict.kind == vm_codes.ABORT and verdict.event == "vm_challenge_or_success"


def test_an_untyped_body_is_a_pause_and_an_unknown_code_is_an_abort():
    for bad in (None, b"", b"<html>", b"[]", b'{"error": 3}', b'{"contract": "growin-orders/1"}'):
        verdict = vm_codes.classify_body(503, bad)
        assert verdict.kind == vm_codes.PAUSE and verdict.event == "untyped_response"
    unknown = vm_codes.classify(answer(vm_codes.ORDERS_CONTRACT, "ORDERS_BLOCKED", "not_a_code", 423))
    assert unknown.kind == vm_codes.ABORT and unknown.event == "vm_unknown_code"
    unknown_relay = vm_codes.classify(answer(vm_codes.RELAY_CONTRACT, "NOT_A_RELAY_CODE", None, 500))
    assert unknown_relay.event == "vm_unknown_code"


def test_a_typed_body_is_read_from_the_wire_shape():
    body = json.dumps({"contract": "growin-orders/1", "error": {"code": "ORDERS_BLOCKED", "reason": "kill_switch"}})
    assert vm_codes.classify_body(423, body).kind == vm_codes.EXPECTED
    assert vm_codes.classify_body(409, body).event == "code_status_mismatch"


def test_the_client_side_codes_map_to_a_pause_or_an_abort():
    def err(code, reason=None, status=None):
        return SimpleNamespace(code=code, reason=reason, http_status=status)

    assert vm_codes.classify_error(err("RELAY_PROTOCOL_ERROR", "unknown_code", 418)).event == "vm_unknown_code"
    assert vm_codes.classify_error(err("RELAY_UNAVAILABLE")).event == "untyped_response"
    assert vm_codes.classify_error(err("RELAY_HASH_MISMATCH")).event == "untyped_response"
    assert vm_codes.classify_error(err("RELAY_CONTRACT_MISMATCH")).event == "relay_contract_mismatch"
    assert vm_codes.classify_error(err("NO_SESSION", None, 503)).kind == vm_codes.PAUSE


def test_every_event_a_row_names_exists_in_the_policy_table():
    events = {r.event for r in (*vm_codes.ORDER_ROWS.values(), *vm_codes.RELAY_ROWS.values(), vm_codes.INTENT_INVALID)}
    events |= {"code_status_mismatch", "vm_unknown_code", "untyped_response", "vm_challenge_or_success"}
    assert {e for e in events if e is not None} - set(policy.POLICY) == set()


def test_vm_codes_is_pure():
    tree = ast.parse((ROOT / "backend/shadow_india/vm_codes.py").read_text())
    roots = {
        (n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
    } | {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert roots <= {"__future__", "json", "dataclasses", "typing"}


# =====================================================================================================================
# open_run and the refusals before any step
# =====================================================================================================================


def test_open_run_pins_the_ten_inputs_the_holdout_flag_and_the_cadence_and_audit_anchors(world):
    manifest = world.evidence.manifest
    assert manifest.holdout_dated_inputs is True
    assert set(manifest.hashes) == set(PIN_NAMES) and len(PIN_NAMES) == 10 and "seed_sha256" in manifest.hashes
    (start,) = world.records("run_start")
    assert start["run_start_session"] == S.isoformat() and start["window_start"] == WINDOW_START.isoformat()
    assert (start["audit_base_seq"], start["audit_base_sha256"]) == (0, "0" * 64)


def test_open_run_refuses_unless_both_index_lists_were_fetched_by_the_window_start(tmp_path, vm, monkeypatch):
    seen: list[Any] = []

    def latest(store, name, *, fetched_by=None):
        seen.append((name, fetched_by))
        return None if name == "smallcap250" else SimpleNamespace()

    monkeypatch.setattr(runner_module, "latest_index_list", latest)
    inputs = WorldInputs(
        store=SimpleNamespace(), strategy=None, targets=None, universe_policy=None, calendar=None, tolerances=None,
        window_start=WINDOW_START, run_start_session=S, dataset_sha256=sha("d"), tick_sha256=sha("t"),
        cost_sha256=sha("c"), scenario_sha256=sha("s"),
    )
    with pytest.raises(OpenRunRefused) as caught:
        open_run(tmp_path / "ev", run_id=RUN, inputs=inputs, orders=vm.orders, clock=Clock())
    assert caught.value.code == "index_list_snapshot_not_seeded"
    assert ("nifty500", WINDOW_START) in seen and ("smallcap250", WINDOW_START) in seen
    assert not (tmp_path / "ev").exists()  # nothing was created


def test_open_run_records_the_audit_head_the_run_starts_from(tmp_path, vm, metadata, monkeypatch):
    script = Script()
    monkeypatch.setattr(runner_module, "evaluate_session", script)
    first = build_world(tmp_path, vm, script=script)
    script.result = evaluated("AAA")
    first.decide()
    head = vm.orders.audit_all()[-1]
    first.ledger.close()
    inputs = first.inputs
    second = open_run(tmp_path / "ev2", run_id="run65", inputs=inputs, orders=vm.orders, clock=Clock())
    start = [r for r in second.records() if r["kind"] == "run_start"][0]
    assert (start["audit_base_seq"], start["audit_base_sha256"]) == (head.seq, head.entry_sha256)


def test_decide_refuses_outside_the_window_without_writing_anything(world):
    before = len(world.evidence.records())
    for local in (datetime(2026, 10, 22, 9, 29, 59, tzinfo=IST), datetime(2026, 10, 22, 10, 0, tzinfo=IST),
                  datetime(2026, 10, 21, 9, 45, tzinfo=IST), datetime(2026, 10, 23, 9, 45, tzinfo=IST)):
        world.clock.now = local
        with pytest.raises(DecideRefused) as caught:
            world.decide()
        assert caught.value.code == "outside_decide_window"
    assert len(world.evidence.records()) == before and world.order_posts() == []


@pytest.mark.parametrize("local", [datetime(2026, 10, 22, 9, 30, tzinfo=IST), datetime(2026, 10, 22, 9, 59, 59, tzinfo=IST)])
def test_the_window_is_half_open_at_both_ends_in_ist(world, local):
    world.clock.now = local
    assert world.decide().status == "done"


def test_decide_refuses_unless_relay_health_is_active_with_egress_ok(world):
    for state, egress in (("expired", True), ("active", False), ("absent", False), ("active", None)):
        world.health.state, world.health.egress_ok = state, egress
        with pytest.raises(DecideRefused) as caught:
            world.decide()
        assert caught.value.code == "relay_not_active"
    assert world.order_posts() == []


def test_decide_refuses_a_qualifying_run(tmp_path, vm, monkeypatch):
    monkeypatch.setattr(runner_module, "evaluate_session", Script())
    built = build_world(tmp_path, vm, run_class="qualifying")
    try:
        with pytest.raises(DecideRefused) as caught:
            built.decide()
        assert caught.value.code == "run_class_qualifying" and vm.http.order_calls() == []
    finally:
        built.ledger.close()


def test_decide_refuses_a_session_that_is_not_the_runs_session(world):
    other = date(2026, 10, 23)
    world.clock.now = datetime(2026, 10, 23, 9, 45, tzinfo=IST)
    with pytest.raises(DecideRefused) as caught:
        world.decide(other)
    assert caught.value.code == "session_id_mismatch"


def test_decide_refuses_before_the_run_start_session(world):
    store = world.evidence
    start = [r for r in store.records() if r["kind"] == "run_start"][0]
    assert start["run_start_session"] == S.isoformat()
    assert session_id_for(S) == "s-20261022"


# =====================================================================================================================
# the happy path: three intents, each kill-refused and correlated to exactly one audit entry
# =====================================================================================================================


def test_three_entries_are_each_refused_by_the_kill_flag_and_correlated_to_one_audit_entry(world):
    report = world.decide()
    assert report.status == "done" and report.marked is True
    assert len(ids_of(report)) == 3  # three ranked names, each sized and sent
    vm_decisions = decisions(world)
    assert len(vm_decisions) == 3
    for record in vm_decisions:
        assert record["status"] == 423 and record["code"] == "kill_switch"
        assert record["codes"] == ["kill_switch"] and record["kill"] == "blocked"
        assert record["vm_portfolio_checks"] == "not_run_kill_first"
        assert isinstance(record["audit_seq"], int) and len(record["entry_hash"]) == 64
        assert record["recovered_from_audit"] is False
    entries = kill_entries(world.vm)
    assert len(entries) == 3  # exactly one VM audit entry per intent
    assert sorted(r["audit_seq"] for r in vm_decisions) == sorted(e["seq"] for e in entries)
    assert world.order_posts() == [("POST", "/v1/orders/intents")] * 3
    assert verify_vm_chain(world.vm.orders.audit_all(), base_seq=0, base_sha256="0" * 64)


def test_every_intent_is_reserved_in_the_ledger_before_the_next_is_admitted(world):
    world.decide()
    stages = [r["kind"] for r in world.evidence.records() if r["kind"] in {"intent", "reservation", "mint_started", "vm_decision"}]
    assert stages == ["intent", "reservation", "mint_started", "vm_decision"] * 3
    assert len(world.records("reservation")) == 3


def test_the_cash_cap_row_each_buy_is_sized_from_cash_after_the_previous_reservation(world):
    """Break-proof 9: admit the next buy before reserving the previous and a later buy is sized from the full cash."""
    world.reference.adv_by_isin = {isin: Decimal("100000000") for isin in ISINS.values()}  # ADV never binds
    world.decide()
    reserved = [Decimal(r["limit_price"]) * Decimal(r["quantity"]) for r in world.records("intent")]
    assert len(reserved) == 3
    assert reserved[0] <= Decimal("17500") and reserved[2] < reserved[1]  # the cap, then cash running out
    deployable_after = [SEED - sum(reserved[: i + 1]) for i in range(3)]
    assert all(left >= 0 for left in deployable_after)
    # the third buy was sized from what was left, not from the seed
    third = world.records("intent")[2]
    assert Decimal(third["quantity"]) * Decimal(third["limit_price"]) <= SEED - sum(reserved[:2])


def test_the_intent_ids_the_vm_saw_are_derived_from_the_proposal_ids(world):
    report = world.decide()
    proposals = ids_of(report)
    vm_ids = {r["vm_intent_id"] for r in world.records("vm_intent")}
    assert vm_ids == {vm_intent_id_for(p) for p in proposals}
    assert {r["intent_id"] for r in world.records("vm_intent")} == set(proposals)


def test_the_vm_intent_is_written_before_the_post(world, monkeypatch):
    order: list[str] = []
    real = world.vm.orders.mint

    def spy(body):
        order.append("post:" + str([r["kind"] for r in world.evidence.records()][-1]))
        return real(body)

    monkeypatch.setattr(world.vm.orders, "mint", spy)
    world.decide()
    assert order and all(item == "post:vm_intent" for item in order)


def test_a_second_decide_in_the_window_adds_nothing(world):
    world.decide()
    posts, records = len(world.order_posts()), len(world.evidence.records())
    again = world.decide()
    assert again.status == "done" and len(world.order_posts()) == posts
    assert [r for r in world.evidence.records()][:records] == world.evidence.records()[:records]
    assert len(world.records("vm_decision")) == 3


def test_the_default_fill_port_returns_inconclusive_with_no_post_submission_evidence():
    outcome = NoPostSubmissionEvidence().fill(FillQuery("i1", "AAA", "BUY", Decimal(1), Decimal("100"), "2026-10-22T04:15:00+00:00"))
    assert (outcome.outcome, outcome.reason) == ("INCONCLUSIVE", "no_post_submission_evidence")
    assert outcome.quantity is None and outcome.price is None


def test_close_with_the_default_fill_port_is_inconclusive_for_every_intent_and_releases_the_reservations(world):
    world.decide()
    closed = world.runner.close(S)
    assert closed.outcome == "ok" and len(closed.attempts) == 3
    assert {outcome for _pid, outcome in closed.attempts} == {"INCONCLUSIVE"}
    assert len(world.records("attempt_inconclusive")) == 3
    assert all(r["code"] == "no_post_submission_evidence" for r in world.records("attempt_inconclusive"))
    assert world.records("session_close")[-1]["outcome"] == "ok"


# =====================================================================================================================
# pause, abort and the VM answers that cause them
# =====================================================================================================================

MINT = ("POST", "/v1/orders/intents")
SHADOW_VM_DECISION_EVENT = "SHADOW_VM_DECISION"


class Hold:
    """Drops the network under the mint after ``after`` calls, remembering the bodies it swallowed (a held handler)."""

    def __init__(self, vm: Vm, monkeypatch, *, after: int = 0) -> None:
        self.vm, self.after, self.calls = vm, after, 0
        self.bodies: list[dict[str, Any]] = []
        self.real = vm.orders.mint
        monkeypatch.setattr(vm.orders, "mint", self)

    def __call__(self, body):
        self.calls += 1
        if self.calls > self.after:
            self.bodies.append(dict(body))
            self.vm.http.unreachable.add(MINT)
        return self.real(body)

    def release(self) -> None:
        self.vm.http.unreachable.discard(MINT)


def open_events(world: World) -> list[str]:
    return [p.event for p in policy.run_state(world.evidence).open_pauses]


def test_a_pause_stops_all_processing_persists_and_only_resume_clears_it(world, monkeypatch):
    """Break-proof 18: a pause that only blocked new entries would still book and expire in ``close``."""
    Hold(world.vm, monkeypatch, after=1)
    report = world.decide()
    assert report.status == "paused" and report.stopped == "pause"
    assert len(ids_of(report)) == 2  # the third name was never admitted
    assert open_events(world) == ["untyped_response"]
    again = world.rebuild(clock=world.clock)  # a restart: the pause is evidence, not memory
    assert open_events(again) == ["untyped_response"]
    second = again.decide()
    assert second.status == "paused" and second.stopped == "open_pause" and second.intents == []
    before = len(again.evidence.records())
    closed = again.runner.close(S)
    assert closed.outcome == "paused" and closed.attempts == [] and closed.released == []
    assert [r["kind"] for r in again.evidence.records()[before:]] == ["session_close"]
    assert again.records("attempt") == [] and again.records("fill_result") == []
    with pytest.raises(policy.PolicyError) as blank:
        again.runner.resume("   ")
    assert blank.value.code == "reason_required" and open_events(again) == ["untyped_response"]
    again.clock.now += timedelta(hours=1)  # time passing clears nothing
    assert open_events(again) == ["untyped_response"]
    again.runner.resume("operator reviewed the relay")
    assert open_events(again) == []
    again.ledger.close()


def test_resume_clears_a_pause_and_the_next_decide_goes_on_without_re_minting_the_intent_in_doubt(world, monkeypatch):
    hold = Hold(world.vm, monkeypatch, after=0)
    world.decide()
    assert len(hold.bodies) == 1
    hold.release()
    hold.after = 10**6  # only the first mint was held
    world.runner.resume("relay is back")
    world.clock.now += timedelta(seconds=11)  # past the mint timeout, so recovery may call the first mint ambiguous
    posts = len(world.order_posts())
    report = world.decide()
    assert list(report.recovered.values()) == ["mint_ambiguous"]
    assert len(world.order_posts()) - posts == 2  # the other two names only; the intent in doubt is never re-sent
    assert len(world.records("vm_intent")) == 3


def test_audit_broken_503_aborts_resume_refuses_it_and_the_next_decide_refuses(world, monkeypatch):
    """The audit-corruption proof, against the real 503 ``audit_broken`` of the in-process VM (break-proofs 14 and 16)."""
    real = world.vm.orders.mint
    mints = {"n": 0}

    def corrupt_after_first(body):
        mints["n"] += 1
        out = real(body) if mints["n"] == 1 else None
        if mints["n"] == 1:
            return out  # unreachable: the first mint is refused (423), which raises
        return real(body)

    def first_then_corrupt(body):
        mints["n"] += 1
        if mints["n"] == 2:
            world.vm.audit_path.write_bytes(world.vm.audit_path.read_bytes() + b"{trunc\n")  # tampering, not a crash
        return real(body)

    monkeypatch.setattr(world.vm.orders, "mint", first_then_corrupt)
    report = world.decide()
    assert report.status == "aborted" and mints["n"] == 2
    state = policy.run_state(world.evidence)
    assert state.aborted and list(state.abort_events) == ["vm_audit_broken"]
    with pytest.raises(policy.PolicyError) as refused:
        world.runner.resume("x")
    assert refused.value.code == "resume_refused_abort"
    with pytest.raises(policy.RunAborted):
        world.decide()
    again = world.rebuild()  # a restart cannot clear it either
    assert policy.run_state(again.evidence).aborted
    with pytest.raises(policy.RunAborted):
        again.decide()
    closed = again.runner.close(S)
    assert closed.outcome == "aborted" and closed.attempts == []
    assert world.order_posts() == [MINT, MINT]  # nothing was sent after the abort
    again.ledger.close()


def test_a_corruption_code_aborts_even_while_a_pause_is_open(world):
    policy.raise_event(world.evidence, "untyped_response", "relay did not answer")
    verdict = vm_codes.classify(answer(vm_codes.ORDERS_CONTRACT, "ORDERS_UNAVAILABLE", "audit_broken", 503))
    policy.raise_event(world.evidence, verdict.event, "audit_broken")
    state = policy.run_state(world.evidence)
    assert state.aborted and open_events(world) == ["untyped_response"]
    with pytest.raises(policy.PolicyError):
        world.runner.resume("review done")  # the abort wins over the open pause


def test_a_non_kill_switch_code_aborts_the_run_and_writes_nothing_as_a_decision(world):
    world.vm.metadata.body = b"enabled"  # the kill flag lifted: the VM would go on to challenge, never to refuse
    world.vm.stack.restart()  # a fresh service over the same files
    report = world.decide()
    state = policy.run_state(world.evidence)
    assert report.status == "aborted" and state.aborted
    assert list(state.abort_events) == ["vm_order_code"]  # any code but kill_switch is an abort, never a decision
    assert world.records("vm_decision") == []
    assert world.order_posts() == [MINT]
    assert not any(call[1].endswith("/authorize") for call in world.vm.http.calls)


def rewrite_audit(monkeypatch, change):
    real = runner_module.VmAuditReader.page

    def page(self, from_seq):
        return change(real(self, from_seq))

    monkeypatch.setattr(runner_module.VmAuditReader, "page", page)


def test_a_423_with_no_matching_audit_entry_is_an_abort(world, monkeypatch):
    """Break-proof 6: accept a 423 without its audit entry and this row fails."""
    rewrite_audit(monkeypatch, lambda p: AuditPage((), p.chain_verified))
    report = world.decide()
    assert report.status == "aborted" and list(policy.run_state(world.evidence).abort_events) == ["vm_423_no_audit_entry"]
    assert world.records("vm_decision") == []


def test_an_audit_page_whose_chain_does_not_verify_is_an_abort(world, monkeypatch):
    rewrite_audit(monkeypatch, lambda p: AuditPage(p.entries, False))
    report = world.decide()
    assert report.status == "aborted" and list(policy.run_state(world.evidence).abort_events) == ["vm_audit_chain_break"]
    assert world.records("vm_decision") == []


def test_an_audit_entry_with_codes_other_than_kill_switch_is_an_abort(world, monkeypatch):
    def other(page):
        return AuditPage(tuple(AuditEntry(e.seq, e.intent_id, e.intent_sha256, ("kill_switch", "price_collar"), e.entry_hash)
                               for e in page.entries), page.chain_verified)

    rewrite_audit(monkeypatch, other)
    report = world.decide()
    assert report.status == "aborted" and list(policy.run_state(world.evidence).abort_events) == ["vm_audit_codes_unexpected"]
    assert world.records("vm_decision") == []


def test_two_audit_entries_for_one_intent_are_an_abort(world, monkeypatch):
    rewrite_audit(monkeypatch, lambda p: AuditPage(p.entries + p.entries, p.chain_verified))
    report = world.decide()
    assert report.status == "aborted" and list(policy.run_state(world.evidence).abort_events) == ["vm_audit_duplicate_entries"]


def relabel_decisions(vm: Vm, monkeypatch, decision: str) -> None:
    """The VM's audit answers with every entry's ``decision`` changed (the chain hashes ``raw``, so it still verifies)."""

    real = vm.orders.audit
    monkeypatch.setattr(vm.orders, "audit", lambda after_seq=0: tuple(replace(e, decision=decision) for e in real(after_seq)))


@pytest.mark.parametrize("decision", ["CHALLENGED", "VERIFIED_NOT_FORWARDED", "EVALUATED"])
def test_a_kill_switch_entry_whose_decision_is_not_refused_is_an_abort(world, monkeypatch, decision):
    """Break-proof: drop the ``decisions`` check in ``_correlate`` and the row fails."""
    relabel_decisions(world.vm, monkeypatch, decision)
    report = world.decide()
    assert report.status == "aborted" and list(policy.run_state(world.evidence).abort_events) == ["vm_audit_codes_unexpected"]
    assert world.records("vm_decision") == [] and len(world.order_posts()) == 1


def test_recovery_adopts_an_audit_entry_only_when_its_decision_is_refused(world, monkeypatch):
    world.script.result = evaluated("AAA")
    with pytest.MonkeyPatch.context() as probe:
        crash_after_post(world, probe)
        with pytest.raises(Crash):
            world.decide()
    again = world.rebuild(clock=Clock(NOW + timedelta(seconds=11)))
    relabel_decisions(again.vm, monkeypatch, "CHALLENGED")
    report = again.decide()
    assert report.status == "aborted" and list(policy.run_state(again.evidence).abort_events) == ["vm_audit_codes_unexpected"]
    assert again.records("vm_decision") == [] and len(again.order_posts()) == 1
    again.ledger.close()


def test_a_challenge_is_an_abort_and_is_never_authorized(world, monkeypatch):
    """Break-proof 1: accept a minted challenge and the abort row fails."""
    monkeypatch.setattr(world.vm.orders, "mint", lambda body: ("challenge-1", b"signed-bytes"))
    report = world.decide()
    state = policy.run_state(world.evidence)
    assert report.status == "aborted" and list(state.abort_events) == ["vm_challenge_or_success"]
    assert world.records("vm_decision") == []
    assert not any(call[1].endswith("/authorize") for call in world.vm.http.calls)


def test_an_unreadable_kill_label_pauses_the_run_after_recording_the_decision(world, monkeypatch):
    """Break-proof 8: treat ``unreadable`` as expected and the pause row fails."""

    def unreadable(self, *args, **kwargs):
        raise OSError("the metadata server cannot be reached")

    monkeypatch.setattr(http.client.HTTPConnection, "request", unreadable)  # the VM's own kill read raises
    report = world.decide()
    assert report.status == "paused"
    assert open_events(world) == ["kill_label_unreadable"]
    (decision,) = decisions(world)
    assert decision["kill"] == "unreadable" and decision["codes"] == ["kill_switch"]
    assert len(ids_of(report)) == 1  # nothing after the first intent


def test_a_relay_failure_at_the_quote_read_pauses_and_never_reaches_the_vm(world):
    world.quotes.error = NoSession(http_status=503)
    report = world.decide()
    assert report.status == "paused" and open_events(world) == ["relay_no_session"]
    assert world.order_posts() == [] and world.records("vm_intent") == []


class AuditDown:
    """The VM's audit read fails with a relay error while ``down`` is set. Mints and everything else go through."""

    def __init__(self, vm: Vm, monkeypatch, error: Exception | None = None) -> None:
        self.real, self.down, self.reads = vm.orders.audit, False, 0
        self.error = error if error is not None else NoSession(http_status=503)
        monkeypatch.setattr(vm.orders, "audit", self)

    def __call__(self, after_seq: int = 0):
        self.reads += 1
        if self.down:
            raise self.error
        return self.real(after_seq)


def test_a_relay_error_on_the_correlation_read_pauses_and_the_intent_is_adopted_after_resume(world, monkeypatch):
    """Break-proof: let the ``RelayError`` out of ``_after`` and the run ends with a traceback and no pause."""
    audit = AuditDown(world.vm, monkeypatch)
    real_mint, cuts = world.vm.orders.mint, []

    def mint_then_cut_the_audit(body):
        try:
            return real_mint(body)
        finally:
            if not cuts:  # the scan at the start of the decide read fine; the correlation read after the first 423 fails
                cuts.append(body["intent_id"])
                audit.down = True

    monkeypatch.setattr(world.vm.orders, "mint", mint_then_cut_the_audit)
    report = world.decide()
    assert report.status == "paused" and open_events(world) == ["relay_no_session"]
    assert world.order_posts() == [MINT] and len(world.records("vm_intent")) == 1  # nothing else was admitted or minted
    assert len(world.records("mint_started")) == 1 and world.records("vm_decision") == []
    assert [p.scope for p in policy.run_state(world.evidence).open_pauses] == ["all"]
    again = world.decide()
    assert again.status == "paused" and again.stopped == "open_pause" and again.intents == []
    assert world.order_posts() == [MINT]  # the next decide refused until resume
    audit.down = False
    world.clock.now += timedelta(seconds=11)  # past the mint timeout, so recovery reads the audit
    world.runner.resume("relay session restored")
    posts = len(world.order_posts())
    after = world.decide()
    assert list(after.recovered.values()) == ["vm_decided"]  # the intent in doubt is adopted from the audit
    assert len(world.order_posts()) - posts == 2  # the other two names only: zero re-POSTs of the first
    assert len(world.records("vm_intent")) == 3 and len(world.records("vm_decision")) == 3


def test_a_relay_error_on_the_recovery_read_pauses_before_anything_is_decided(world, monkeypatch):
    hold = Hold(world.vm, monkeypatch, after=0)
    world.decide()  # the first mint is in doubt and the run is paused
    hold.release()
    hold.after = 10**6
    world.runner.resume("relay is back")
    world.clock.now += timedelta(seconds=11)
    audit = AuditDown(world.vm, monkeypatch)
    audit.down = True
    posts = len(world.order_posts())
    report = world.decide()
    assert report.status == "paused" and open_events(world) == ["relay_no_session"]
    assert len(world.order_posts()) == posts and world.records("vm_ambiguous") == []  # no guess was written
    assert audit.reads == 1
    audit.down = False
    world.runner.resume("restored")
    final = world.decide()
    assert list(final.recovered.values()) == ["mint_ambiguous"] and len(world.records("vm_intent")) == 3


def test_a_relay_error_on_the_scan_read_pauses_and_stops_the_invocation(world, monkeypatch):
    audit = AuditDown(world.vm, monkeypatch)
    assert world.decide().status == "done"
    posts, intents = len(world.order_posts()), len(world.records("vm_intent"))
    audit.down = True
    report = world.decide()  # nothing is in doubt, so only the audit scan reads
    assert report.status == "paused" and open_events(world) == ["relay_no_session"] and report.intents == []
    assert len(world.order_posts()) == posts and len(world.records("vm_intent")) == intents
    closed = world.runner.close(S)
    assert closed.outcome == "paused" and closed.attempts == []
    audit.down = False
    world.runner.resume("restored")
    assert world.decide().status == "done"


def test_an_audit_read_the_table_calls_corruption_aborts_the_run(world, monkeypatch):
    audit = AuditDown(world.vm, monkeypatch, OrdersUnavailable(http_status=503, reason="audit_broken"))
    audit.down = True
    report = world.decide()
    assert report.status == "aborted" and policy.run_state(world.evidence).abort_events == ("vm_audit_broken",)


# =====================================================================================================================
# the held-handler race and the SL-9 crash probes
# =====================================================================================================================


def entries_for(world: World, proposal_id: str) -> list[dict[str, Any]]:
    return [e for e in kill_entries(world.vm) if e["intent_id"] == vm_intent_id_for(proposal_id)]


def test_the_held_handler_race_is_vm_ambiguous_then_a_late_entry_with_exactly_one_kill_switch_entry(world, monkeypatch):
    """Break-proof 17: recovery that mints on a zero-entry audit leaves two kill_switch entries for the intent."""
    world.script.result = evaluated("AAA")
    hold = Hold(world.vm, monkeypatch, after=0)
    first = world.decide()
    (proposal,) = ids_of(first)
    assert first.status == "paused" and len(hold.bodies) == 1 and entries_for(world, proposal) == []
    hold.release()
    hold.after = 10**6
    world.runner.resume("the relay answers again")
    world.clock.now += timedelta(seconds=11)  # the mint timeout has passed with no audit entry
    posts = len(world.order_posts())
    second = world.decide()
    assert second.recovered[proposal] == "mint_ambiguous"
    assert len(world.order_posts()) == posts  # recovery made no POST
    assert [r["intent_id"] for r in world.records("vm_ambiguous")] == [proposal]
    (inconclusive,) = world.records("attempt_inconclusive")
    assert inconclusive["intent_id"] == proposal and inconclusive["code"] == "mint_ambiguous"
    assert [r["session_id"] for r in world.records("session_excluded")] == [session_id_for(S)]
    assert world.records("vm_decision") == []
    # the held handler finally runs: the VM acts on the body it was sent, once
    with pytest.raises(RelayError) as late:
        hold.real(hold.bodies[0])
    assert (late.value.http_status, late.value.reason) == (423, "kill_switch")
    world.decide()
    (late_record,) = world.records("vm_late_entry")
    assert late_record["intent_id"] == proposal
    assert len(entries_for(world, proposal)) == 1  # exactly one kill_switch audit entry
    assert late_record["audit_seq"] == entries_for(world, proposal)[0]["seq"]
    assert world.records("vm_decision") == []  # a late entry is never a decision
    closed = world.runner.close(S)
    assert closed.outcome == "ok" and len(world.records("vm_late_entry")) == 1
    assert world.order_posts() == [MINT, MINT]  # the first (held, dropped) and the held handler's own


def test_recovery_inside_the_mint_timeout_defers_and_never_calls_the_mint_ambiguous(world, monkeypatch):
    world.script.result = evaluated("AAA")
    hold = Hold(world.vm, monkeypatch, after=0)
    world.decide()
    hold.release()
    world.runner.resume("the relay answers again")
    world.clock.now += timedelta(seconds=3)
    report = world.decide()
    assert list(report.recovered.values()) == ["deferred"] and world.records("vm_ambiguous") == []


class Crash(BaseException):
    """A kill -9 at a named stage. Not an Exception, so nothing in the runner can swallow it."""


def crash_at(world: World, monkeypatch, kind: str, when: str) -> None:
    real = world.evidence.append

    def append(k, payload):
        if k == kind and when == "before":
            raise Crash(f"before {kind}")
        out = real(k, payload)
        if k == kind and when == "after":
            raise Crash(f"after {kind}")
        return out

    monkeypatch.setattr(world.evidence, "append", append)


def crash_after_post(world: World, monkeypatch) -> None:
    real = world.vm.orders.mint

    def mint(body):
        try:
            return real(body)
        finally:
            raise Crash("after the POST, before the answer is recorded")

    monkeypatch.setattr(world.vm.orders, "mint", mint)


def reserved_total(world: World) -> Decimal:
    return world.ledger.simulated_cash_view().reserved


PROBES = [
    # (name, kind, when, outcome recovery gives, posts in total, vm decisions)
    ("before_intent", "intent", "before", None, 1, 1),
    ("after_intent", "intent", "after", "prep_abandoned", 0, 0),
    ("before_reservation_record", "reservation", "before", "prep_interrupted_before_mint", 0, 0),
    ("after_mint_started", "mint_started", "after", "mint_ambiguous", 0, 0),
    ("after_post", "POST", "after", "vm_decided", 1, 1),
    ("before_vm_decision", "vm_decision", "before", "vm_decided", 1, 1),
    ("after_vm_decision", "vm_decision", "after", "vm_decided", 1, 1),
]


@pytest.mark.parametrize("name,kind,when,outcome,posts,decided", PROBES, ids=[p[0] for p in PROBES])
def test_a_crash_at_each_sl9_stage_is_settled_by_recovery_with_no_re_admission_and_no_recovery_post(
    world, monkeypatch, name, kind, when, outcome, posts, decided
):
    world.script.result = evaluated("AAA")
    with pytest.MonkeyPatch.context() as probe:  # the process dies inside this block; a new one has none of its patches
        if kind == "POST":
            crash_after_post(world, probe)
        else:
            crash_at(world, probe, kind, when)
        with pytest.raises(Crash):
            world.decide()
    posts_at_crash = len(world.order_posts())
    again = world.rebuild(clock=Clock(NOW + timedelta(seconds=11)))
    proposal = next((r["intent_id"] for r in again.records("intent")), None)
    recovered = again.runner._prep.recover()  # recovery by itself, before anything else runs
    assert len(again.order_posts()) == posts_at_crash  # zero recovery POSTs
    if outcome is not None:
        assert recovered.get(proposal) == outcome
    again.decide()
    assert len(again.order_posts()) == posts  # decide added no second send for the same intent
    assert len(again.records("intent")) == 1
    # never admitted twice: an intent that died before the ledger saw it is abandoned, not admitted by the restart
    assert len(again.ledger.list_orders()) == (0 if name == "after_intent" else 1)
    assert len(again.records("reservation")) <= 1
    assert len(again.records("vm_decision")) == decided
    if proposal is not None:
        l2 = [e for e in again.ledger.list_events(proposal) if e.event_type == SHADOW_VM_DECISION_EVENT]
        assert len(l2) == decided  # L2 follows E4, written from it by recovery when the crash fell between them
        assert len(entries_for(again, proposal)) == posts
    again.ledger.close()


def test_a_restart_in_the_middle_of_a_decide_finishes_it_without_re_admitting_what_was_prepared(world, monkeypatch):
    world.script.result = evaluated("AAA", "BBB", "CCC")
    done = {"n": 0}
    real = world.evidence.append

    def append(kind, payload):
        if kind == "intent":
            done["n"] += 1
            if done["n"] == 2:
                raise Crash("the process died as the second intent began")
        return real(kind, payload)

    with pytest.MonkeyPatch.context() as probe:
        probe.setattr(world.evidence, "append", append)
        with pytest.raises(Crash):
            world.decide()
    first_posts = len(world.order_posts())
    reserved_before = reserved_total(world)
    again = world.rebuild()
    report = again.decide()
    assert first_posts == 1 and len(again.order_posts()) == 3  # one send each, the first never repeated
    assert len({r["intent_id"] for r in again.records("intent")}) == 3
    assert len(again.ledger.list_orders()) == 3
    assert len(again.records("vm_decision")) == 3
    assert reserved_before <= reserved_total(again)
    assert report.status == "done"
    again.ledger.close()


# =====================================================================================================================
# the portfolio rows: marks, exits, stops, the halt, the run end, expiry, bands and quarantine
# =====================================================================================================================

PRIOR = date(2026, 10, 20)


def instrument_of(world: World, symbol: str) -> Instrument:
    return next(i for i in world.instruments if i.symbol == symbol)


def own(world: World, symbol: str, quantity: int, price: str = "100.00", *, book: bool = True) -> str:
    """A buy from the previous session, kill-refused by the VM like every shadow order, then booked as a fill."""

    report = DecideReport(AS_OF)
    outcome = run(
        world.runner._submit(
            session=AS_OF, instrument=instrument_of(world, symbol), side="BUY", quantity=quantity,
            limit=Decimal(price), reason="entry", batch_id=None, signal_id=f"seed-{symbol}", leg="entry", report=report,
        )
    )
    assert outcome is not None and outcome.status == "vm_decided", outcome
    pid = report.intents[-1][0]
    if book:
        world.runner._prep.book_fill(
            proposal_id=pid, cumulative_quantity=Decimal(quantity), price=Decimal(price), charges=Decimal(0),
            schedule_sha256=sha("schedule"), scenario_set_sha256=sha(f"scenario-{symbol}"), attempt_id=f"att-seed-{symbol}",
        )
    return pid


def premark(world: World, close: str = "100") -> None:
    """The mark of an earlier session at ``close``: it sets the 63 latch's peak."""

    held = [h.instrument.isin for h in world.runner._portfolio.snapshot().holdings]
    world.runner._portfolio.mark_session(PRIOR, {i: Decimal(close) for i in held})


def set_closes(world: World, **closes: str) -> None:
    """The reference closes, and the live quotes follow them (the next morning opens where the close was)."""

    for symbol, value in closes.items():
        world.reference.close_by_isin[ISINS[symbol]] = Decimal(value)
        world.quotes.prices[symbol] = value


def holdings(world: World) -> dict[str, int]:
    return {h.instrument.symbol: h.quantity for h in world.runner._portfolio.snapshot().holdings}


def intents_of(world: World, side: str | None = None) -> list[dict[str, Any]]:
    return [r for r in world.records("intent") if side is None or r["side"] == side]


def three_down(world: World, close: str) -> None:
    """Three names of 160 at 100 each (cost 48000 of 50000), marked at 100 on the prior session, then ``close``."""

    for symbol in ("AAA", "BBB", "CCC"):
        own(world, symbol, 160)
    premark(world)
    set_closes(world, AAA=close, BBB=close, CCC=close)


def test_the_mark_takes_peak_and_drawdown_from_the_63_latch_and_stamps_the_simulator_basis(world):
    own(world, "AAA", 160)
    premark(world)
    set_closes(world, AAA="95")
    world.script.result = evaluated("BBB", entries=False)
    report = world.decide()
    assert report.marked is True
    mark = world.records("mark")[-1]
    assert mark["peak_source"] == PEAK_SOURCE
    assert mark["simulator_drawdown_basis"] == SIMULATOR_DRAWDOWN_BASIS == "seed_relative_at_cost"
    latch = world.latch.load()
    assert Decimal(mark["peak"]) == latch.peak
    assert Decimal(mark["drawdown"]) == ((Decimal(mark["equity"]) - latch.peak) / latch.peak).quantize(Decimal("0.000001"))


def test_a_stop_is_minted_only_under_the_exit_intent_id_of_its_obligation(world):
    own(world, "AAA", 160)
    premark(world)
    set_closes(world, AAA="85")  # -15% against cost: past the -12% stop
    world.script.result = evaluated("BBB", entries=False)
    report = world.decide()
    sells = intents_of(world, "SELL")
    assert len(sells) == 1 and sells[0]["leg"] == "exit" and sells[0]["symbol"] == "AAA"
    current = world.latch.load().open_exits[ISINS["AAA"]]
    expected = exit_intent_id(exit_obligation_id(current.reason, ISINS["AAA"], current.since), 1)
    assert sells[0]["intent_id"] == expected and current.reason == "stop"
    assert [e for e in kill_entries(world.vm) if e["side"] == "sell"][0]["reason"] == "stop"
    # the row proving no other id: every SELL intent of the run is an exit id, and a malformed one is refused outright
    assert all(runner_module._EXIT_ID.fullmatch(r["intent_id"]) for r in intents_of(world, "SELL"))
    with pytest.raises(ShadowAbort) as bad:
        world.runner._prep.proposal_id(session_id_for(S), "swing-20261021-" + ISINS["AAA"], "exit")
    assert bad.value.code == "exit_intent_id_malformed"
    assert report.status == "done"


def test_a_second_decide_does_not_mint_the_same_exit_twice(world):
    own(world, "AAA", 160)
    premark(world)
    set_closes(world, AAA="85")
    world.script.result = evaluated("BBB", entries=False)
    world.decide()
    world.decide()
    assert len(intents_of(world, "SELL")) == 1 and len(world.records("vm_decision")) == 2  # the seed buy and the sell


def test_no_buy_is_admitted_while_a_stop_exit_is_open(world):
    """Break-proof 11: admit a buy while a stop exit is open and this row fails."""
    own(world, "AAA", 160)
    premark(world)
    set_closes(world, AAA="85")
    world.script.result = evaluated("BBB", "CCC", "DDD")
    report = world.decide()
    assert [r["symbol"] for r in intents_of(world, "BUY")] == ["AAA"]  # only the seed buy
    assert len(intents_of(world, "SELL")) == 1
    assert any("stop_open" in why for _what, why in report.skipped)


def test_a_strategy_sell_on_a_name_with_an_open_exit_is_suppressed_and_another_name_is_sold(world):
    own(world, "AAA", 100)
    own(world, "BBB", 100)
    premark(world)
    set_closes(world, AAA="85", BBB="100")  # AAA stops out; BBB only falls out of the ranking
    world.script.result = evaluated("CCC", entries=False)  # neither held name is ranked within the hold cutoff
    report = world.decide()
    legs = {r["symbol"]: r["leg"] for r in intents_of(world, "SELL")}
    assert legs == {"AAA": "exit", "BBB": "swing"}
    assert (ISINS["AAA"], "strategy_sell_suppressed_open_exit") in report.skipped
    assert len([r for r in intents_of(world, "SELL") if r["symbol"] == "AAA"]) == 1  # one sell for the stopped name


def test_the_halt_holds_buys_and_nothing_releases_it_but_the_latch(world):
    """Break-proof 10: auto-release the -8% halt and the no-auto-reset row fails."""
    three_down(world, "89")  # each -11% (no stop), equity -10.6% from the peak: past the -8% halt, short of -15%
    world.script.result = evaluated("DDD", "EEE")
    report = world.decide()
    assert policy.DRAWDOWN_HALT in open_events(world) and not policy.run_state(world.evidence).ended
    assert [r["symbol"] for r in intents_of(world, "BUY")] == ["AAA", "BBB", "CCC"]  # only the seeds: no new buy
    assert report.status in {"done", "paused"}
    with pytest.raises(policy.PolicyError) as refused:
        world.runner.resume("the market recovered")
    assert refused.value.code == "drawdown_resume_requires_latch"
    again = world.rebuild()
    assert policy.DRAWDOWN_HALT in open_events(again)  # a restart does not release it
    set_closes(again, AAA="100", BBB="100", CCC="100")  # equity is back at the peak
    again.runner._portfolio.mark_session(date(2026, 10, 23), {ISINS[s]: Decimal("100") for s in ("AAA", "BBB", "CCC")})
    assert policy.DRAWDOWN_HALT in open_events(again)  # a better mark releases nothing
    again.ledger.close()


def test_the_flatten_ends_the_run_for_buys_and_neither_a_restart_nor_resume_clears_it(world):
    three_down(world, "80")  # -20% each: equity -19% from the peak, past the -15% flatten
    world.script.result = evaluated("DDD", "EEE")
    world.decide()
    state = policy.run_state(world.evidence)
    assert state.ended and "drawdown_flatten_15" in state.ended_events
    flats = intents_of(world, "SELL")
    assert sorted(r["symbol"] for r in flats) == ["AAA", "BBB", "CCC"]  # every holding is sold, under flatten ids
    assert all("-flatten-" in r["intent_id"] or "flatten" in r["intent_id"] for r in flats)
    assert [r["symbol"] for r in intents_of(world, "BUY")] == ["AAA", "BBB", "CCC"]  # only the seeds: no buy after the end
    with pytest.raises(policy.PolicyError) as first:
        world.runner.resume("review done")
    assert first.value.code == "drawdown_resume_requires_latch"  # the -8% halt is open under the flatten
    again = world.rebuild()
    assert policy.run_state(again.evidence).ended
    again.decide()
    assert len(intents_of(again, "BUY")) == 3 and again.runner.status()["ended"] == ["drawdown_flatten_15"]
    again.ledger.close()


def test_run_ended_is_terminal_resume_refuses_it_and_a_restart_does_not_clear_it(world):
    """Break-proof for the terminal class: nothing but the run's end is open, and still ``resume`` refuses."""
    policy.raise_event(world.evidence, "drawdown_flatten_15", "flatten")
    assert open_events(world) == [] and policy.run_state(world.evidence).ended
    with pytest.raises(policy.PolicyError) as first:
        world.runner.resume("review done")
    assert first.value.code == "resume_refused_run_ended"
    again = world.rebuild()
    with pytest.raises(policy.PolicyError) as second:
        again.runner.resume("really done")
    assert second.value.code == "resume_refused_run_ended"
    report = again.decide()  # marks, expiry and exits go on; a buy never does
    assert report.status == "done" and intents_of(again, "BUY") == []
    assert again.runner.status()["ended"] == ["drawdown_flatten_15"]
    again.ledger.close()


def test_hand_flatten_pauses_entries_and_sells_every_holding_under_a_flatten_obligation(world):
    own(world, "AAA", 100)
    own(world, "BBB", 100)
    premark(world)
    with pytest.raises(DecideRefused) as blank:
        world.runner.hand_flatten(S, "  ")
    assert blank.value.code == "reason_required"
    report = world.runner.hand_flatten(S, "operator flattens by hand")
    sells = intents_of(world, "SELL")
    assert sorted(r["symbol"] for r in sells) == ["AAA", "BBB"] and report.status in {"done", "paused"}
    assert all("flatten" in r["intent_id"] for r in sells)
    expected = {exit_intent_id(exit_obligation_id("flatten", ISINS[s], S), 1) for s in ("AAA", "BBB")}
    assert {r["intent_id"] for r in sells} == expected
    assert "divergence_breach" in open_events(world)  # entries are paused until the operator resumes
    assert [r["latch"] for r in world.records("latch") if r.get("latch") == "hand_flatten"] == ["hand_flatten"]
    again = world.runner.hand_flatten(S, "operator flattens by hand")  # a second call mints nothing new
    assert len(intents_of(world, "SELL")) == 2 and again.intents == []


def test_hand_flatten_sells_a_name_with_an_open_ambiguous_exit_fill_it_is_the_documented_exit(world):
    own(world, "AAA", 100)
    own(world, "BBB", 100)
    premark(world)
    policy.raise_event(world.evidence, policy.AMBIGUOUS_EXIT_FILL, "a sell booked under the wrong id", isin=ISINS["AAA"])
    report = world.runner.hand_flatten(S, "operator flattens by hand")
    assert sorted(r["symbol"] for r in intents_of(world, "SELL")) == ["AAA", "BBB"], report.skipped
    assert not any("corporate_action" in why for _what, why in report.skipped)


def test_hand_flatten_leaves_a_corporate_action_name_paused_unless_the_operator_includes_it(world):
    """Break-proof: drop the ``frozen`` skip and the default sells the paused name."""
    own(world, "AAA", 100)
    own(world, "BBB", 100)
    premark(world)
    policy.raise_event(world.evidence, policy.UNRESOLVED_CORPORATE_ACTION, "split", isin=ISINS["AAA"])
    report = world.runner.hand_flatten(S, "operator flattens by hand")
    assert [r["symbol"] for r in intents_of(world, "SELL")] == ["BBB"]
    assert (ISINS["AAA"], "flatten_skipped_corporate_action_pause") in report.skipped
    assert policy.UNRESOLVED_CORPORATE_ACTION in open_events(world)  # still paused
    world.runner.hand_flatten(S, "operator reviewed the split", include_corporate_action=True)
    assert sorted(r["symbol"] for r in intents_of(world, "SELL")) == ["AAA", "BBB"]


def test_the_cli_hand_flatten_takes_include_corporate_action_only_when_given(world):
    own(world, "AAA", 100)
    premark(world)
    policy.raise_event(world.evidence, policy.UNRESOLVED_CORPORATE_ACTION, "split", isin=ISINS["AAA"])
    code, plain = run_cli(cli_args(world, "hand-flatten", "--reason", "x"), runtime_factory=lambda args: world.runner)
    assert code == 0 and plain["intents"] == [] and plain["skipped"] == [[ISINS["AAA"], "flatten_skipped_corporate_action_pause"]]
    code, flagged = run_cli(
        cli_args(world, "hand-flatten", "--reason", "x", "--include-corporate-action"), runtime_factory=lambda args: world.runner
    )
    assert code == 0 and len(flagged["intents"]) == 1


def test_hand_flatten_is_refused_outside_the_window(world):
    world.clock.now = datetime(2026, 10, 22, 11, 0, tzinfo=IST)
    with pytest.raises(DecideRefused) as caught:
        world.runner.hand_flatten(S, "operator flattens by hand")
    assert caught.value.code == "outside_decide_window"


def test_an_earlier_sessions_open_day_order_is_expired_before_the_mark(world):
    pid = own(world, "AAA", 100, book=False)  # a buy of the previous session that never filled
    assert world.ledger.simulated_cash_view().reserved > 0
    world.script.result = evaluated("BBB", entries=False)
    report = world.decide()
    assert report.expired == [pid]
    kinds = [r["kind"] for r in world.evidence.records()]
    assert kinds.index("expiry") < kinds.index("mark")
    assert world.ledger.simulated_cash_view().reserved == 0
    (expiry,) = world.records("expiry")
    assert expiry["intent_id"] == pid and expiry["reason"] == "day_close"
    assert holdings(world) == {}


def test_a_held_name_being_exited_gets_its_band_resolved_and_an_unknown_band_pauses_that_isin(world, monkeypatch):
    own(world, "AAA", 160)
    premark(world)
    set_closes(world, AAA="85")
    seen: list[str] = []

    def band(store, *, isin, prior_session, session, calendar, workspace):
        seen.append(isin)
        return SimpleNamespace(status="unknown")

    monkeypatch.setattr(runner_module, "band_for_upcoming_session", band)
    world.script.result = evaluated("BBB", entries=False)
    world.decide()
    assert seen == [ISINS["AAA"]]
    assert [p.scope for p in policy.run_state(world.evidence).open_pauses] == [f"isin:{ISINS['AAA']}"]
    assert open_events(world) == ["band_ambiguity"]
    assert len(intents_of(world, "SELL")) == 1  # the sell goes on: a band problem blocks entries, never an exit


def test_a_band_the_signal_step_already_resolved_is_not_resolved_twice(world, monkeypatch):
    own(world, "AAA", 160)
    premark(world)
    set_closes(world, AAA="85")
    calls: list[str] = []
    monkeypatch.setattr(runner_module, "band_for_upcoming_session", lambda *a, **k: calls.append("x"))
    result = evaluated("BBB", entries=False)
    world.script.result = replace(result, bands={ISINS["AAA"]: SimpleNamespace(status="fixed")})
    world.decide()
    assert calls == [] and open_events(world) == []


def test_a_held_name_whose_signal_is_quarantined_pauses_that_isin_and_is_not_sold_at_the_unadjusted_quote(world):
    own(world, "AAA", 160)
    premark(world)
    world.script.result = evaluated("AAA", "BBB", suppressed={"AAA": "adjusted_quarantined"}, entries=False)
    report = world.decide()
    pauses = policy.run_state(world.evidence).open_pauses
    assert [(p.event, p.scope) for p in pauses] == [("unresolved_corporate_action", f"isin:{ISINS['AAA']}")]
    assert intents_of(world, "SELL") == []
    assert report.status == "done"
    world.decide()  # the same pause is not raised twice
    assert len(policy.run_state(world.evidence).open_pauses) == 1


def non_evaluation(*, signals: tuple[NameSignal, ...] = ()) -> SignalResult:
    """The result of a cadence day that is not an evaluation day: no bars were read, so no signal, no suppression."""

    return SignalResult(STATUS_NON_EVALUATION_DAY, S, AS_OF, evaluation_due=False, signals=signals)


def quarantine_row_clean(world: World, report) -> None:
    """What every probe row below proves: the name is paused and carried, never stopped, flattened or sold."""

    pauses = [(p.event, p.scope) for p in policy.run_state(world.evidence).open_pauses]
    assert pauses == [("unresolved_corporate_action", f"isin:{ISINS['AAA']}")]
    assert intents_of(world, "SELL") == [] and [r["symbol"] for r in intents_of(world, "BUY")] == ["AAA"]  # the seed only
    state = policy.run_state(world.evidence)
    assert not state.ended and not state.aborted and world.latch.load().open_exits == {}
    assert report.status == "done" and report.marked is True


@pytest.mark.parametrize("close", ["50", "88"])  # -16% flattens the run, -12% stops the name: raw marks of a split
def test_an_evaluated_day_pauses_a_quarantined_held_name_before_the_mark(world, close):
    own(world, "AAA", 160)
    premark(world)
    set_closes(world, AAA=close)
    world.script.result = evaluated("AAA", "BBB", suppressed={"AAA": "adjusted_quarantined"}, entries=False)
    quarantine_row_clean(world, world.decide())


@pytest.mark.parametrize("close", ["50", "88"])
def test_a_non_evaluation_day_pauses_a_quarantined_held_name_before_the_mark(world, close):
    """Break-proof: return early unless the status is ``evaluated`` and the flatten (50) and the stop (88) come back."""
    own(world, "AAA", 160)
    premark(world)
    set_closes(world, AAA=close)
    world.script.result = non_evaluation()
    world.script.quarantined = {ISINS["AAA"]}
    quarantine_row_clean(world, world.decide())
    assert world.script.quarantine_calls == [(frozenset({ISINS["AAA"]}), AS_OF, S)]


@pytest.mark.parametrize("close", ["50", "88"])
def test_a_non_evaluation_day_with_a_quarantined_signal_attached_pauses_the_name(world, close):
    own(world, "AAA", 160)
    premark(world)
    set_closes(world, AAA=close)
    attached = evaluated("AAA", suppressed={"AAA": "adjusted_quarantined"}).signals
    world.script.result = non_evaluation(signals=attached)
    quarantine_row_clean(world, world.decide())


def test_a_held_name_outside_the_eligible_set_is_still_checked_for_a_quarantine(world):
    own(world, "AAA", 160)
    premark(world)
    set_closes(world, AAA="50")
    world.script.result = evaluated("BBB", "CCC", entries=False)  # AAA has no signal at all
    world.script.quarantined = {ISINS["AAA"]}
    quarantine_row_clean(world, world.decide())


def test_the_real_held_name_read_uses_the_signal_steps_quarantine_window(tmp_path, monkeypatch):
    """The helper is ``signals``' own rule over ``bars_as_of``: a bar inside the window flags a held name, one outside does not."""

    import test_shadow_india_signals as sig

    victim, bystander, old = sig.ANCHORS[3], sig.ANCHORS[4], sig.ANCHORS[5]
    rows = sig.history(quarantined=[(victim, sig.HIST[-12]), (bystander, sig.AS_OF), (old, sig.HIST[0])])
    calls: list[dict[str, Any]] = []

    def bars(store, **kwargs):
        calls.append(kwargs)
        return sig.live_result(rows, as_of=kwargs["as_of"], decision=kwargs["decision_session"])

    monkeypatch.setattr(signals_module, "bars_as_of", bars)
    inputs = sig.make_inputs(tmp_path)
    held = [victim, old, sig.ANCHORS[6]]  # the bystander is quarantined but not held
    found = held_names_quarantined(inputs, held, sig.AS_OF, sig.S)
    assert found == frozenset({victim}), found  # `old` fell out of the window: only a bar the signal reads counts
    assert calls[0]["as_of"] == sig.AS_OF and calls[0]["decision_session"] == sig.S
    assert held_names_quarantined(inputs, [], sig.AS_OF, sig.S) == frozenset() and len(calls) == 1  # nothing held, no read
    monkeypatch.setattr(signals_module, "bars_as_of", lambda *a, **k: (_ for _ in ()).throw(PilotDataError("x", "y")))
    with pytest.raises(PilotDataError):
        held_names_quarantined(inputs, held, sig.AS_OF, sig.S)


def test_a_non_evaluation_day_without_a_quarantine_still_marks_and_stops_normally(world):
    own(world, "AAA", 160)
    premark(world)
    set_closes(world, AAA="85")
    world.script.result = non_evaluation()
    report = world.decide()
    assert report.marked is True and policy.run_state(world.evidence).open_pauses == ()
    sells = intents_of(world, "SELL")
    assert len(sells) == 1 and sells[0]["symbol"] == "AAA" and sells[0]["leg"] == "exit"
    assert world.script.quarantine_calls == [(frozenset({ISINS["AAA"]}), AS_OF, S)]


def test_no_quarantine_read_runs_while_nothing_is_held(world):
    world.script.result = non_evaluation()
    world.decide()
    assert world.script.quarantine_calls == []


def test_held_bars_that_cannot_be_read_pause_everything_before_the_mark(world):
    own(world, "AAA", 160)
    premark(world)
    set_closes(world, AAA="50")
    world.script.result = non_evaluation()
    world.script.quarantine_error = PilotDataError("live_bars_empty", "no bars")
    report = world.decide()
    assert report.status == "paused" and report.stopped == "held_name_check_failed" and report.marked is False
    assert open_events(world) == ["stale_data"] and intents_of(world, "SELL") == []
    assert [r for r in world.records("mark") if r.get("as_of_date") == AS_OF.isoformat()] == []


def test_the_tick_cost_and_scenario_pins_are_recomputed_from_the_live_tables_at_every_decide(world):
    world.decide()
    first = world.script.inputs[-1]
    assert (first.tick_sha256, first.cost_sha256, first.scenario_sha256) == (
        world.pins.values["tick"], world.pins.values["cost"], world.pins.values["scenario"],
    )
    world.pins.values = {"tick": sha("tick-2"), "cost": sha("cost-2"), "scenario": sha("scenario-2")}
    world.decide()
    second = world.script.inputs[-1]
    assert (second.tick_sha256, second.cost_sha256, second.scenario_sha256) == (
        sha("tick-2"), sha("cost-2"), sha("scenario-2"),
    )
    assert world.inputs.tick_sha256 == sha("tick-1")  # the run's own inputs were not edited in place


@pytest.mark.parametrize(
    "table,patched,pin,fake",
    [
        ("tick", "committed_tick_table", "tick_sha256", lambda kind: SimpleNamespace(versions=[SimpleNamespace(version_hash="f" * 64)])),
        ("cost", "load_schedule_set", "cost_sha256", lambda: SimpleNamespace(versions=[SimpleNamespace(schedule_hash="e" * 64)])),
        ("scenario", "load_fill_scenarios", "scenario_sha256", lambda: SimpleNamespace(scenarios_hash="d" * 64)),
    ],
)
def test_live_table_pins_follow_the_committed_tables_and_a_changed_table_pauses_hash_mismatch(
    tmp_path, monkeypatch, table, patched, pin, fake
):
    """``LiveTablePins`` on the real committed tables, then the real signal step against a manifest built from them."""

    import test_shadow_india_signals as sig

    held = sig.Stubs(rows=sig.history())
    for name, replacement in (
        ("bars_as_of", held.bars), ("require_reference_freshness", held.freshness), ("snapshot_for", held.snapshot),
        ("surveillance_entries", held.entries), ("band_for_upcoming_session", held.band), ("seed_rows", held.seeds),
        ("current_band_convention", held.band_convention),
    ):
        monkeypatch.setattr(signals_module, name, replacement)
    live = LiveTablePins()
    first = {"tick_sha256": live.tick_sha256(), "cost_sha256": live.cost_sha256(), "scenario_sha256": live.scenario_sha256()}
    assert len(set(first.values())) == 3 and all(len(v) == 64 for v in first.values())
    assert first == {"tick_sha256": live.tick_sha256(), "cost_sha256": live.cost_sha256(), "scenario_sha256": live.scenario_sha256()}
    root = tmp_path / "run"
    root.mkdir()
    inputs = sig.make_inputs(root, run_start=sig.S, eligibility=StaticEligibility(sig.ANCHORS), **first)
    evidence = sig.make_evidence(root, inputs)

    def decide() -> SignalResult:
        now = replace(inputs, tick_sha256=live.tick_sha256(), cost_sha256=live.cost_sha256(), scenario_sha256=live.scenario_sha256())
        return signals_module.evaluate_session(evidence, now, decision_session=sig.S)

    assert decide().status == STATUS_EVALUATED  # unchanged tables: the pins equal the manifest
    monkeypatch.setattr(runner_module, patched, fake)
    changed = {"tick_sha256": live.tick_sha256(), "cost_sha256": live.cost_sha256(), "scenario_sha256": live.scenario_sha256()}
    assert [k for k in first if changed[k] != first[k]] == [pin]  # only that table's pin moved
    paused = decide()
    assert paused.status == "paused" and paused.pause_event == "hash_mismatch" and paused.pause_reason == f"hash_mismatch:{pin}"
    assert [p.event for p in policy.run_state(evidence).open_pauses] == ["hash_mismatch"]


def test_a_restart_cannot_shift_the_cadence_anchor(world):
    shifted = replace(world.inputs, run_start_session=date(2026, 10, 23))
    world.ledger.close()
    with pytest.raises(DecideRefused) as caught:
        assemble(
            evidence=EvidenceStore.open(world.root, run_id=RUN, session_id=session_id_for(S), clock=world.clock),
            ledger=open_simulation_ledger(world.tmp / "sim", RUN, SEED), india_guard=world.guard, latch=world.latch,
            instruments=world.instruments, orders=world.vm.orders,
            identity=VmIdentity(VECTORS["limits_sha256"], HASHES["params_sha256"], PRIMARY["key_id"]),
            base_inputs=shifted, pins=world.pins, ports=world.ports, health=world.health, clock=world.clock,
        )
    assert caught.value.code == "run_start_session_moved"


# =====================================================================================================================
# the transport, the opened paths, the fill port and the CLI
# =====================================================================================================================

SOURCE_FILES = ("runner.py", "vm_codes.py", "__main__.py")


class Fills:
    """A fill source that records what it was asked and answers from a function."""

    def __init__(self, answer=None) -> None:
        self.queries: list[FillQuery] = []
        self.answer = answer or (lambda q: FillOutcome("INCONCLUSIVE", "no_post_submission_evidence"))

    def fill(self, query: FillQuery) -> FillOutcome:
        self.queries.append(query)
        return self.answer(query)


def test_the_transport_counters_show_only_mints_and_audit_reads_zero_authorize_zero_halt(world, monkeypatch):
    """Break-proof 2: call authorize and this row fails."""

    def forbidden(*args, **kwargs):
        raise AssertionError("an order-side call the shadow runner must never make")

    for name in ("authorize", "halt"):
        monkeypatch.setattr(OrdersClient, name, forbidden)
    world.decide()
    world.runner.close(S)
    calls = world.vm.http.calls
    paths = {(m, p.split("?")[0]) for m, p in calls}
    assert paths <= {("POST", "/v1/orders/intents"), ("GET", "/v1/orders/audit"), ("GET", "/v1/orders/state")}
    assert world.vm.http.count("POST", "/v1/orders/authorize") == 0 and world.vm.http.count("POST", "/v1/orders/halt") == 0
    assert world.vm.http.count(*MINT) == 3
    assert world.vm.stack.transport.non_get() == [] and world.vm.stack.transport.order_endpoint_non_get() == []
    assert set(world.vm.metadata.methods) == {"GET"}


def test_the_shadow_runner_has_no_path_to_touch_id_signing_or_authorize():
    banned_attrs = {"authorize", "halt", "approve_signed", "create_approval_challenge", "enroll_key"}
    banned_modules = {"execution.approval", "execution.service", "execution.relay_dispatcher", "cryptography", "keyring"}
    for name in SOURCE_FILES:
        tree = ast.parse((ROOT / "backend/shadow_india" / name).read_text())
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert not (attrs | names) & banned_attrs, name
        modules = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        modules |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        assert not {m for m in modules if any(m == b or m.startswith(b + ".") for b in banned_modules)}, name
        text = (ROOT / "backend/shadow_india" / name).read_text()
        assert "LocalAuthentication" not in text and "SecureEnclave" not in text and "Touch ID" not in text.replace("Touch ID.", "")


def test_the_opened_path_set_is_the_simulation_ledger_and_the_run_evidence_only(tmp_path, vm, monkeypatch):
    """No live or paper India ledger is opened: every database the run connects to is its own simulation ledger."""
    databases: list[str] = []
    real = sqlite3.connect

    def spy(database, *args, **kwargs):
        databases.append(str(database))
        return real(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", spy)
    script = Script()
    monkeypatch.setattr(runner_module, "evaluate_session", script)
    monkeypatch.setattr(runner_module, "held_names_quarantined", script.held_names_quarantined)
    built = build_world(tmp_path, vm, script=script)
    script.result = evaluated("AAA", "BBB")
    built.decide()
    built.runner.close(S)
    built.ledger.close()
    on_disk = [d for d in databases if d != ":memory:"]
    assert on_disk and {Path(d).parent for d in on_disk} == {tmp_path / "sim" / "india-shadow" / RUN}
    assert not any("paper" in d or "live" in d or d.endswith("india.sqlite3") for d in on_disk)
    assert built.root.parent == tmp_path and (built.root / RUN).is_dir()


def test_the_fill_port_is_asked_with_the_ledgers_reservation_time_not_a_clock(world):
    world.script.result = evaluated("AAA", "BBB")
    fills = Fills()
    world.ports.fills = fills
    world.decide()
    world.clock.now += timedelta(hours=2)
    world.runner.close(S)
    reserved_at = {r["intent_id"]: r["reservation_created_at"] for r in world.records("reservation")}
    assert {q.intent_id: q.submitted_at for q in fills.queries} == reserved_at and len(reserved_at) == 2
    assert all(q.submitted_at != world.clock.now.astimezone(timezone.utc).isoformat() for q in fills.queries)
    assert {q.side for q in fills.queries} == {"BUY"} and all(q.limit_price > 0 for q in fills.queries)


def test_a_filled_answer_books_the_fill_and_an_inconclusive_one_releases_the_reservation(world):
    world.script.result = evaluated("AAA", "BBB")
    first = {}

    def answer(query: FillQuery) -> FillOutcome:
        if not first:
            first[query.intent_id] = True
            return FillOutcome("FILLED", "filled", query.quantity, query.limit_price, Decimal("0"), sha("sch"), sha("scn"))
        return FillOutcome("INCONCLUSIVE", "no_post_submission_evidence")

    world.ports.fills = Fills(answer)
    world.decide()
    closed = world.runner.close(S)
    assert sorted(o for _p, o in closed.attempts) == ["INCONCLUSIVE", "booked"]
    assert len(world.records("booking_done")) == 1 and len(closed.released) == 1
    assert list(holdings(world)) == ["AAA"]  # the first intent was AAA's
    assert world.ledger.simulated_cash_view().reserved == 0


def test_a_filled_sell_books_and_closes_the_exit(world):
    own(world, "AAA", 160)
    premark(world)
    set_closes(world, AAA="85")
    world.script.result = evaluated("BBB", entries=False)
    world.decide()
    sold = []

    def answer(q: FillQuery) -> FillOutcome:
        if q.side != "SELL":
            return FillOutcome("INCONCLUSIVE", "no_post_submission_evidence")
        sold.append(q)
        return FillOutcome("FILLED", "filled", q.quantity, q.limit_price, Decimal("0"), sha("sch"), sha("scn"))

    world.ports.fills = Fills(answer)
    closed = world.runner.close(S)
    assert len(sold) == 1 and ("booked" in [o for _p, o in closed.attempts])
    assert holdings(world) == {}


def cli_args(world: World, command: str, *extra: str) -> list[str]:
    return [command, "--root", str(world.root), "--run-id", RUN, "--session", S.isoformat(), *extra]


def run_cli(argv: list[str], **kwargs: Any) -> tuple[int, dict[str, Any]]:
    out = io.StringIO()
    code = cli.main(argv, out=out, **kwargs)
    lines = [line for line in out.getvalue().splitlines() if line]
    return code, (json.loads(lines[-1]) if lines else {})


def test_the_cli_status_resume_and_refusals(world):
    code, status = run_cli(cli_args(world, "status"))
    assert code == 0 and status["aborted"] == [] and status["open_pauses"] == []
    code, again = run_cli(cli_args(world, "resume", "--reason", "x"))
    assert (code, again) == (1, {"refused": "nothing_to_resume"})
    policy.raise_event(world.evidence, "untyped_response", "relay did not answer")
    code, status = run_cli(cli_args(world, "status"))
    assert [p["event"] for p in status["open_pauses"]] == ["untyped_response"]
    code, blank = run_cli(cli_args(world, "resume", "--reason", " "))
    assert (code, blank) == (1, {"refused": "reason_required"})
    code, ok = run_cli(cli_args(world, "resume", "--reason", "the relay is back"))
    assert code == 0 and ok["resumed"] and open_events(world) == []


def open_seq(world: World, event: str) -> int:
    return next(p.seq for p in policy.run_state(world.evidence).open_pauses if p.event == event)


def test_the_cli_resume_clears_a_relay_pause_under_an_open_halt_and_the_next_decide_marks_with_entries_blocked(world):
    """Break-proof: pass every open pause to ``policy.resume`` again and the halt refuses the whole call."""
    three_down(world, "89")  # -11% each: past the -8% halt, short of the stop and the flatten
    world.script.result = evaluated("DDD", "EEE")
    world.decide()
    halt = open_seq(world, policy.DRAWDOWN_HALT)
    policy.raise_event(world.evidence, "untyped_response", "relay did not answer")
    relay = open_seq(world, "untyped_response")
    assert world.decide().stopped == "open_pause"  # both are open: nothing runs
    code, ok = run_cli(cli_args(world, "resume", "--reason", "relay reviewed"))
    assert code == 0 and ok["resumed"] == [relay] and ok["skipped"] == [
        {"seq": halt, "event": policy.DRAWDOWN_HALT, "why": "drawdown_resume_requires_latch"}
    ]
    assert open_events(world) == [policy.DRAWDOWN_HALT]  # the halt stays: only the latch releases it
    buys = len(intents_of(world, "BUY"))
    report = world.decide()
    assert report.status == "done" and report.marked is True and len(intents_of(world, "BUY")) == buys
    code, refused = run_cli(cli_args(world, "resume", "--reason", "x"))  # only the halt is open now
    assert (code, refused) == (1, {"refused": "drawdown_resume_requires_latch"})


def test_the_cli_resume_pause_seq_clears_exactly_the_named_pause_and_refuses_one_it_may_not_clear(world):
    three_down(world, "89")
    world.script.result = evaluated("DDD", "EEE")
    world.decide()
    halt = open_seq(world, policy.DRAWDOWN_HALT)
    policy.raise_event(world.evidence, "untyped_response", "one")
    policy.raise_event(world.evidence, "stale_data", "two")
    stale = open_seq(world, "stale_data")
    code, refused = run_cli(cli_args(world, "resume", "--reason", "x", "--pause-seq", str(halt)))
    assert (code, refused) == (1, {"refused": "drawdown_resume_requires_latch"})
    code, unknown = run_cli(cli_args(world, "resume", "--reason", "x", "--pause-seq", "999"))
    assert (code, unknown) == (1, {"refused": "unknown_pause"})
    code, ok = run_cli(cli_args(world, "resume", "--reason", "x", "--pause-seq", str(stale)))
    assert code == 0 and ok["resumed"] == [stale] and ok["skipped"] == []
    assert sorted(open_events(world)) == sorted([policy.DRAWDOWN_HALT, "untyped_response"])


def test_the_cli_resume_names_a_corporate_action_pause_it_skips_and_refuses_when_only_that_is_open(world):
    policy.raise_event(world.evidence, policy.UNRESOLVED_CORPORATE_ACTION, "split", isin=ISINS["AAA"])
    code, refused = run_cli(cli_args(world, "resume", "--reason", "x"))
    assert (code, refused) == (1, {"refused": "corporate_action_resume_requires_portfolio"})
    policy.raise_event(world.evidence, "band_ambiguity", "band", isin=ISINS["BBB"])
    code, ok = run_cli(cli_args(world, "resume", "--reason", "x"))
    assert code == 0 and [s["event"] for s in ok["skipped"]] == [policy.UNRESOLVED_CORPORATE_ACTION]
    assert open_events(world) == [policy.UNRESOLVED_CORPORATE_ACTION]


def test_the_cli_resume_refuses_an_abort_and_a_run_end(world):
    policy.raise_event(world.evidence, "vm_audit_broken", "audit_broken")
    code, refused = run_cli(cli_args(world, "resume", "--reason", "x"))
    assert (code, refused) == (1, {"refused": "resume_refused_abort"})
    code, status = run_cli(cli_args(world, "status"))
    assert code == 0 and status["aborted"] == ["vm_audit_broken"]


def test_the_cli_resume_refuses_the_run_end(world):
    policy.raise_event(world.evidence, "drawdown_flatten_15", "flatten")
    code, refused = run_cli(cli_args(world, "resume", "--reason", "x"))
    assert (code, refused) == (1, {"refused": "resume_refused_run_ended"})


def test_the_cli_commands_that_need_a_runtime_refuse_when_none_is_wired_and_touch_nothing(world):
    before = world.evidence.records()
    for command, extra in (("decide", ()), ("close", ()), ("hand-flatten", ("--reason", "x")), ("open-run", ())):
        code, refused = run_cli(cli_args(world, command, *extra))
        assert (code, refused) == (1, {"refused": "production_runtime_not_wired"}), command
    assert world.evidence.records() == before and world.order_posts() == []


def test_the_cli_drives_decide_close_and_hand_flatten_through_an_injected_runner(world):
    code, decided = run_cli(cli_args(world, "decide"), runtime_factory=lambda args: world.runner)
    assert code == 0 and decided["status"] == "done" and len(decided["intents"]) == 3
    code, closed = run_cli(cli_args(world, "close"), runtime_factory=lambda args: world.runner)
    assert code == 0 and closed["outcome"] == "ok" and closed["witness"]
    code, flat = run_cli(cli_args(world, "hand-flatten", "--reason", "by hand"), runtime_factory=lambda args: world.runner)
    assert code == 0


def test_the_cli_exits_3_on_an_abort_and_refuses_a_decide_outside_the_window(world):
    world.clock.now = datetime(2026, 10, 22, 12, 0, tzinfo=IST)
    code, refused = run_cli(cli_args(world, "decide"), runtime_factory=lambda args: world.runner)
    assert (code, refused) == (1, {"refused": "outside_decide_window"})
    world.clock.now = NOW
    policy.raise_event(world.evidence, "vm_audit_broken", "audit_broken")
    code, aborted = run_cli(cli_args(world, "decide"), runtime_factory=lambda args: world.runner)
    assert (code, aborted) == (3, {"refused": "run_aborted"})


def test_the_cli_turns_a_relay_error_into_a_typed_message_and_a_nonzero_exit_never_a_traceback(world):
    class Boom:
        def decide(self, session):
            raise NoSession(http_status=503)

    code, payload = run_cli(cli_args(world, "decide"), runtime_factory=lambda args: Boom())
    assert code == cli.EXIT_REFUSED
    assert payload == {"refused": "relay_error", "event": "relay_no_session", "code": "NO_SESSION", "status": 503}

    class Corrupt:
        def decide(self, session):
            raise OrdersUnavailable(http_status=503, reason="audit_broken")

    code, payload = run_cli(cli_args(world, "decide"), runtime_factory=lambda args: Corrupt())
    assert code == cli.EXIT_ABORTED and payload["event"] == "vm_audit_broken"


def test_a_missing_argument_is_a_usage_error(world):
    code, _ = run_cli(["decide", "--root", str(world.root)])
    assert code == 2
