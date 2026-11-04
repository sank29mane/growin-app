"""Phase 64-05 Task 2b: the offline D-23 replay of the shadow portfolio through the VM's own risk code.

Fakes only: a tmp checkout that is a copy of ``gateway/vm/gateway_vm/orders`` with a fake ``.git``, fixture limits
and evidence written through the real ``EvidenceStore``. No VM, relay, broker or network call: a sentinel patches
every socket, HTTP and relay entry point for the whole module and any call fails the test.

Row names follow the 64-05 break-proof table: replay build hash, divergence, day missing, isolation, kill flag
(``replay_kill_enabled_true``) and limits hash (``replay_limits_mismatch_refuses``).
"""

from __future__ import annotations

import ast
import importlib.machinery
import importlib.util
import json
import py_compile
import shutil
import socket
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

import pytest

import shadow_india.vm_replay as vm_replay
from shadow_india import vm_codes
from shadow_india.admission import deterministic_proposal_id
from shadow_india.evidence import SCHEMAS, EvidenceStore, read_records
from shadow_india.portfolio import exit_intent_id, exit_obligation_id
from shadow_india.runner import LEG_ENTRY, LEG_EXIT, LEG_SWING
from shadow_india.vm_replay import (
    BuildIdentity,
    MacVerdict,
    ReplayRefused,
    compare,
    load_recorded_build,
    orders_tree_sha256,
    parse_pins,
    read_git_commit,
    render_report,
    replay_run,
    replay_session,
    run_verdict,
)
from test_shadow_india_purity import violations

REPO = Path(__file__).resolve().parents[2]
REAL_ORDERS = REPO / "gateway" / "vm" / "gateway_vm" / "orders"
VM_REPLAY_SOURCE = REPO / "backend" / "shadow_india" / "vm_replay.py"
IST = timezone(timedelta(hours=5, minutes=30))

COMMIT = "c" * 40
OTHER_COMMIT = "d" * 40
RUN = "run-replay"
S1, S2, S3 = "s-2026-10-27", "s-2026-10-28", "s-2026-10-29"
DAYS = {S1: (2026, 10, 27), S2: (2026, 10, 28), S3: (2026, 10, 29)}  # Tuesday to Thursday
HASHES = {"strategy_sha256": "a" * 64, "params_sha256": "b" * 64}

# Fixture limits. Tests carry their own values (growin-execution-and-risk); these are not the pilot's.
LIMITS = {
    "schema_version": 1,
    "workspace": "india",
    "currency": "INR",
    "capital_cap": "50000",
    "per_position_cap": "50000",
    "drawdown_halt": "-0.08",
    "drawdown_flatten": "-0.15",
    "position_stop": "-0.10",
    "fat_finger_collar": "0.05",
}

SIGNAL_HASHES = (
    "strategy_sha256", "params_sha256", "dataset_sha256", "universe_sha256", "band_sha256",
    "tick_sha256", "limits_sha256", "cost_sha256", "scenario_sha256",
)
ISIN_A, ISIN_B, ISIN_C = "INE002A01018", "INE009A01021", "INE467B01029"
SYMBOL = {ISIN_A: "AAA", ISIN_B: "BBB", ISIN_C: "CCC"}


def swing_signal_id(as_of: date, isin: str) -> str:
    """The runner's swing sell id. ``runner._strategy_sells`` builds it inline, so a test pins this f-string to its source."""

    return f"swing-{as_of:%Y%m%d}-{isin}"


# --- the network sentinel ------------------------------------------------------------------------------------------


class SentinelTripped(AssertionError):
    pass


@pytest.fixture(autouse=True)
def network_sentinel(monkeypatch: pytest.MonkeyPatch):
    """Every way out of the process that this repo uses raises and is logged. A replay must leave the log empty."""

    calls: list[str] = []

    def trip(name: str) -> Callable[..., Any]:
        def blocked(*_args: Any, **_kwargs: Any) -> Any:
            calls.append(name)
            raise SentinelTripped(name)

        return blocked

    import http.client
    import urllib.request

    for owner, attr in (
        (socket.socket, "connect"),
        (socket.socket, "connect_ex"),
        (socket, "create_connection"),
        (socket, "getaddrinfo"),
        (http.client.HTTPConnection, "connect"),
        (http.client.HTTPConnection, "request"),
        (urllib.request, "urlopen"),
    ):
        monkeypatch.setattr(owner, attr, trip(f"{owner.__name__}.{attr}"))
    import httpx

    for owner, attr in ((httpx.Client, "send"), (httpx.AsyncClient, "send"), (httpx.Client, "__init__"), (httpx.AsyncClient, "__init__")):
        monkeypatch.setattr(owner, attr, trip(f"httpx.{owner.__name__}.{attr}"))
    import requests

    monkeypatch.setattr(requests.Session, "request", trip("requests.Session.request"))
    from brokers.breeze_gateway import client as relay_client

    monkeypatch.setattr(relay_client.RelayClient, "__init__", trip("RelayClient.__init__"))
    monkeypatch.setattr(relay_client.RelayClient, "_send", trip("RelayClient._send"))
    yield calls
    assert calls == [], f"network or relay entry point was called: {calls}"


# --- a fake VM checkout ----------------------------------------------------------------------------------------------


def make_vm(tmp_path: Path, name: str = "vm", edits: dict[str, Callable[[str], str]] | None = None, commit: str = COMMIT) -> Path:
    """``<tmp>/<name>`` holds ``gateway_vm/orders`` copied from the working tree, optionally edited, and a fake ``.git``."""

    root = tmp_path / name
    orders = root / "gateway_vm" / "orders"
    shutil.copytree(REAL_ORDERS, orders, ignore=shutil.ignore_patterns("__pycache__"))
    (root / "gateway_vm" / "__init__.py").write_text('__version__ = "fixture"\n')
    for filename, edit in (edits or {}).items():
        target = orders / filename
        target.write_text(edit(target.read_text()))
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text(commit + "\n")
    return root


def identity_of(vm_source: Path, commit: str = COMMIT) -> BuildIdentity:
    return BuildIdentity(commit, orders_tree_sha256(vm_source))


@pytest.fixture()
def vm(tmp_path: Path) -> Path:
    return make_vm(tmp_path)


@pytest.fixture()
def limits_sha(vm: Path) -> str:
    built = load_recorded_build(vm, identity_of(vm))
    try:
        return built.limits_cls.from_fields(LIMITS).sha256
    finally:
        built.close()


# --- evidence and pins -----------------------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 27, 9, 35, 0, tzinfo=IST)

    def __call__(self) -> datetime:
        return self.now


def money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


class Scenario:
    """Writes one run of evidence through the real store and the matching pins document."""

    def __init__(self, tmp_path: Path, vm: Path, limits_sha: str) -> None:
        self.root = tmp_path / "evidence"
        self.vm = vm
        self.limits_sha = limits_sha
        self.limits = dict(LIMITS)
        self.identity = identity_of(vm)
        self.clock = Clock()
        self.stores: dict[str, EvidenceStore] = {}
        self.pins: dict[str, dict[str, dict[str, Any]]] = {}
        self.extra: dict[str, dict[str, Any]] = {}
        self._booking = 0
        self._intent = 0
        self.use(S1)

    def use(self, session: str) -> EvidenceStore:
        year, month, day = DAYS[session]
        self.clock.now = datetime(year, month, day, 9, 35, 0, tzinfo=IST)
        if session not in self.stores:
            if not self.stores:
                self.stores[session] = EvidenceStore.create(
                    self.root, run_id=RUN, run_class="qualifying", session_id=session, hashes=HASHES, clock=self.clock
                )
            else:
                self.stores[session] = EvidenceStore.open(self.root, run_id=RUN, session_id=session, clock=self.clock)
            self.pins[session] = {}
        return self.stores[session]

    def intent(
        self,
        session: str,
        side: str,
        quantity: int,
        price: str,
        *,
        isin: str = ISIN_A,
        mac: Any = "admit",
        iid: str | None = None,
        ltp: str | None = None,
        tick_reference: str | None | bool = True,
        series: str = "EQ",
        minute: int = 35,
        signal: bool | dict[str, Any] = True,
        symbol: str | None = None,
        extra: dict[str, Any] | None = None,
        leg: str = LEG_ENTRY,
        signal_id: str | None = None,
    ) -> str:
        """One decision as ``AdmissionService.prepare`` records it: a ``signal`` record (the instrument and the limits
        hash), then an ``intent`` with exactly the fields the writer emits. It carries no ``isin`` and no ``series``.

        ``signal=False`` writes no signal record, a dict overrides signal fields, ``extra`` adds intent fields.
        """

        store = self.use(session)
        year, month, day = DAYS[session]
        self.clock.now = datetime(year, month, day, 9, minute, 0, tzinfo=IST)
        self._intent += 1
        iid = iid or f"intent-{self._intent:04d}"
        signal_id = signal_id or f"sig-{self._intent}"
        if signal is not False:
            store.append(
                "signal",
                {
                    "signal_id": signal_id, "symbol": SYMBOL[isin], "isin": isin, "series": series, "as_of_date": "2026-10-26",
                    **{name: "f" * 64 for name in SIGNAL_HASHES},
                    "limits_sha256": self.limits_sha,
                    **(signal if isinstance(signal, dict) else {}),
                },
            )
        store.append(
            "intent",
            {
                "intent_id": iid, "session_id": session, "signal_id": signal_id, "leg": leg, "intent_sha256": "e" * 64,
                "symbol": symbol or SYMBOL[isin], "side": side, "quantity": str(quantity), "limit_price": price,
                **(extra or {}),
            },
        )
        if mac == "admit":
            store.append("reservation", {"intent_id": iid, "reservation_created_at": "2026-10-27T09:35:00+05:30"})
        elif isinstance(mac, tuple) and mac[0] == "deny":
            store.append("denied", {"intent_id": iid, "code": mac[1]})
        elif isinstance(mac, tuple) and mac[0] == "reservation_refused":
            store.append("reservation_refused", {"intent_id": iid, "code": mac[1]})
        value = Decimal(price)
        quote_ltp = Decimal(ltp) if ltp is not None else value
        self.pins[session][iid] = {
            "quote": {
                "ltp": money(quote_ltp), "lower_circuit": money(quote_ltp * Decimal("0.9")),
                "upper_circuit": money(quote_ltp * Decimal("1.1")), "previous_close": money(quote_ltp), "series": series,
            },
            "tick_reference": price if tick_reference is True else tick_reference,
        }
        return iid

    def sell(
        self,
        session: str,
        kind: str,
        quantity: int,
        price: str,
        *,
        isin: str = ISIN_A,
        **options: Any,
    ) -> str:
        """A runner-shaped sell, ids from the code that mints them, and no ``signal`` record (the runner writes none).

        ``halve``, ``stop`` and ``flatten`` are exit obligations: the intent id IS ``exit_intent_id`` and so is the
        ``signal_id`` (``runner.RunnerPreparation.proposal_id``). ``swing`` is a strategy sell: ``signal_id`` is
        ``swing_signal_id`` and the intent id is ``deterministic_proposal_id`` (``AdmissionService.proposal_id``).
        """

        year, month, day = DAYS[session]
        if kind == "swing":
            signal_id = swing_signal_id(date(year, month, day), isin)
            iid = deterministic_proposal_id(RUN, session, signal_id, LEG_SWING)
            leg = LEG_SWING
        else:
            signal_id = iid = exit_intent_id(exit_obligation_id(kind, isin, date(year, month, day)), 1)
            leg = LEG_EXIT
        options = {"leg": leg, "signal_id": signal_id, "iid": iid, **options}
        return self.intent(session, "sell", quantity, price, isin=isin, signal=False, **options)

    def book(self, session: str, iid: str, cumulative: int, price: str, charges: str = "0") -> None:
        store = self.use(session)
        self._booking += 1
        bid = f"book-{self._booking:04d}"
        store.append(
            "booking_intent",
            {
                "intent_id": iid, "booking_id": bid, "cumulative_quantity": str(cumulative), "price": price,
                "charges": charges, "schedule_sha256": "1" * 64, "scenario_set_sha256": "2" * 64, "attempt_id": f"att-{self._booking}",
            },
        )
        store.append("booking_done", {"intent_id": iid, "booking_id": bid, "ledger_event_id": self._booking})

    def raw(self, session: str, kind: str, payload: dict[str, Any]) -> None:
        self.use(session).append(kind, payload)

    def latch(self, session: str, name: str, scope: str, reason: str) -> None:
        self.raw(session, "latch", {"latch": name, "scope": scope, "reason": reason})

    def signal(self, session: str, limits_sha256: str | None = None) -> None:
        self.raw(
            session,
            "signal",
            {
                "signal_id": f"sig-{session}", "symbol": "AAA", "isin": ISIN_A, "series": "EQ", "as_of_date": "2026-10-26",
                **{name: "f" * 64 for name in SIGNAL_HASHES},
                **({"limits_sha256": limits_sha256} if limits_sha256 else {}),
            },
        )

    def records(self) -> list[dict[str, Any]]:
        return read_records(self.root, RUN)

    def pins_for(self, session: str, **overrides: Any) -> Any:
        doc: dict[str, Any] = {
            "schema": vm_replay.PINS_SCHEMA,
            "session_id": session,
            "build": {"commit": self.identity.commit, "orders_sha256": dict(self.identity.orders_sha256)},
            "limits_sha256": self.limits_sha,
            "limits": self.limits,
            "intents": self.pins.get(session, {}),
            **self.extra.get(session, {}),
        }
        doc.update(overrides)
        return parse_pins(json.dumps(doc).encode("ascii"))

    def replay(self, session: str, sink: Any = None, vm: Path | None = None, **overrides: Any) -> Any:
        return replay_session(
            self.records(), run_id=RUN, session_id=session, pins=self.pins_for(session, **overrides),
            vm_source=vm or self.vm, sink=sink,
        )


class Sink:
    def __init__(self) -> None:
        self.sessions: list[Any] = []
        self.defects: list[tuple[str, str, str]] = []
        self.pauses: list[tuple[str, str]] = []

    def record_session(self, result: Any) -> None:
        self.sessions.append(result)

    def open_defect(self, code: str, session_id: str, detail: str) -> None:
        self.defects.append((code, session_id, detail))

    def pause_run(self, event: str, reason: str) -> None:
        self.pauses.append((event, reason))

    def empty(self) -> bool:
        return not (self.sessions or self.defects or self.pauses)


@pytest.fixture()
def sc(tmp_path: Path, vm: Path, limits_sha: str) -> Scenario:
    return Scenario(tmp_path, vm, limits_sha)


def fund(sc: Scenario, session: str = S1, isin: str = ISIN_A, quantity: int = 100, price: str = "60.00", charges: str = "20") -> str:
    """A filled buy: ``quantity`` shares of ``isin`` at ``price`` with ``charges``, booked in full."""

    iid = sc.intent(session, "buy", quantity, price, isin=isin)
    sc.book(session, iid, quantity, price, charges)
    return iid


# --- the replay agrees --------------------------------------------------------------------------------------------


def test_a_session_where_every_vm_verdict_agrees_passes_with_both_hashes_recorded(sc: Scenario, limits_sha: str):
    sc.intent(S1, "buy", 100, "100.00")
    sc.intent(S1, "buy", 100, "100.00", isin=ISIN_B)
    sink = Sink()
    result = sc.replay(S1, sink)
    assert result.verdict == "PASS"
    assert result.intents_compared == 2
    assert result.divergences == () and result.defects == ()
    assert [v.vm_codes for v in result.verdicts] == [(), ()]
    assert result.build_commit == COMMIT and result.build_digest == sc.identity.digest
    assert result.limits_sha256 == limits_sha and len(result.pins_sha256) == 64
    assert sink.sessions == [result] and sink.defects == [] and sink.pauses == []


def test_replay_kill_enabled_true_an_over_cap_portfolio_matches_the_mac_refusal_and_no_verdict_holds_kill_switch(sc: Scenario):
    first = sc.intent(S1, "buy", 100, "250.00")  # reserved and unfilled: 25,000 of 50,000 is spoken for
    assert first
    over = sc.intent(S1, "buy", 100, "250.05", isin=ISIN_B, mac=("deny", "capital_cap"), tick_reference="250")
    sc.intent(S1, "buy", 1, "100.00", isin=ISIN_C)  # an ordinary buy that the VM admits
    result = sc.replay(S1)
    assert result.verdict == "PASS", result.divergences
    refused = next(v for v in result.verdicts if v.intent_id == over)
    assert refused.vm_codes == ("capital_cap",) and refused.mac_codes == ("capital_cap",)
    assert all("kill_switch" not in v.vm_codes for v in result.verdicts)
    assert result.defects == ()


# --- the account recompute --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("price", "mac", "expected"),
    [("469.90", "admit", ()), ("469.95", ("deny", "capital_cap"), ("capital_cap",))],
    ids=["exactly_at_cap_is_admitted", "one_tick_over_is_refused"],
)
def test_cost_basis_is_rebuilt_from_charges_and_the_average_cost_of_shares_sold(sc: Scenario, price: str, mac: Any, expected: tuple[str, ...]):
    buy = fund(sc, quantity=100, price="60.00", charges="20")  # cost 6020
    sell = sc.intent(S1, "sell", 50, "60.00")
    sc.book(S1, sell, 50, "60.00", "0")  # removes 3010 of cost, leaving 3010
    target = sc.intent(S1, "buy", 100, price, isin=ISIN_B, mac=mac, tick_reference="460")
    result = sc.replay(S1)
    assert result.verdict == "PASS", result.divergences
    assert next(v for v in result.verdicts if v.intent_id == target).vm_codes == expected
    assert buy


def test_a_fully_sold_position_carries_no_cost(sc: Scenario):
    buy = fund(sc, quantity=100, price="60.00", charges="20")
    sell = sc.intent(S1, "sell", 100, "60.00")
    sc.book(S1, sell, 100, "60.00", "0")
    sc.intent(S1, "buy", 100, "500.00", isin=ISIN_B, tick_reference="500")  # a full 50,000 fits only if A is gone
    assert buy and sc.replay(S1).verdict == "PASS"


def test_an_open_buy_reservation_counts_against_the_cap_until_it_expires(sc: Scenario):
    first = sc.intent(S1, "buy", 100, "250.00")
    second = sc.intent(S1, "buy", 100, "250.05", isin=ISIN_B, mac=("deny", "capital_cap"), tick_reference="250")
    result = sc.replay(S1)
    assert result.verdict == "PASS" and next(v for v in result.verdicts if v.intent_id == second).vm_codes == ("capital_cap",)
    sc.raw(S1, "expiry", {"intent_id": first, "reason": "end_of_session"})
    sc.intent(S1, "buy", 100, "250.05", isin=ISIN_C, tick_reference="250")  # the freed 25,000 fits it
    assert sc.replay(S1).verdict == "PASS"


def test_a_partly_filled_buy_leaves_only_its_unfilled_part_open(sc: Scenario):
    first = sc.intent(S1, "buy", 100, "250.00")
    sc.book(S1, first, 40, "250.00", "10")  # 40 filled: cost 10,010, 60 x 250 = 15,000 still open
    sc.intent(S1, "buy", 100, "250.00", isin=ISIN_B, mac=("deny", "capital_cap"), tick_reference="250")  # 25,000 more: 50,010
    assert sc.replay(S1).verdict == "PASS"


def test_a_sell_may_not_exceed_what_is_held_less_what_is_already_offered(sc: Scenario):
    fund(sc, quantity=100)
    sc.intent(S1, "sell", 60, "60.00")
    sc.intent(S1, "sell", 50, "60.00", mac=("deny", "sell_exceeds_holding"))
    sc.intent(S1, "sell", 40, "60.00")
    assert sc.replay(S1).verdict == "PASS"


@pytest.mark.parametrize(
    ("latches", "code"),
    [
        ([("stop", "isin:" + ISIN_B, "raised_2026-10-26")], "stop_open"),
        ([("halt", "entries", "raised_2026-10-26")], "halt_latch"),
        ([("ended", "run", "raised_2026-10-26")], "pilot_ended"),
    ],
    ids=["stop", "halt", "ended"],
)
def test_latches_in_the_evidence_refuse_a_buy_and_release_records_lift_them(sc: Scenario, latches: list[tuple[str, str, str]], code: str):
    for latch in latches:
        sc.latch(S1, *latch)
    sc.intent(S1, "buy", 10, "100.00", mac=("deny", code))
    assert sc.replay(S1).verdict == "PASS"
    release = {"stop": ("stop", "isin:" + ISIN_B, "cleared_by_fill"), "halt": ("halt", "entries", "released_by_operator")}
    if code in ("stop_open", "halt_latch"):
        sc.latch(S1, *release["stop" if code == "stop_open" else "halt"])
        sc.intent(S1, "buy", 10, "100.00", isin=ISIN_C)
        assert sc.replay(S1).verdict == "PASS"


def test_a_run_ended_record_refuses_buys(sc: Scenario):
    sc.raw(S1, "run_ended", {"event": "drawdown_flatten_15", "reason": "equity_at_or_below_minus_15_percent_of_peak"})
    sc.intent(S1, "buy", 10, "100.00", mac=("deny", "pilot_ended"))
    assert sc.replay(S1).verdict == "PASS"


def test_holdings_carry_across_sessions_of_the_run(sc: Scenario):
    fund(sc, S1, quantity=100, price="480.00", charges="0")  # 48,000 deployed on day one
    sc.intent(S2, "buy", 100, "100.00", isin=ISIN_B, mac=("deny", "capital_cap"))  # 58,000 on day two
    result = sc.replay(S2)
    assert result.verdict == "PASS" and result.intents_compared == 1


def test_the_replay_of_one_session_uses_only_evidence_before_each_decision(sc: Scenario):
    first = sc.intent(S1, "buy", 100, "250.00")
    sc.book(S1, first, 100, "250.00", "0")  # booked AFTER the first decision
    sc.intent(S1, "buy", 100, "250.05", isin=ISIN_B, mac=("deny", "capital_cap"), tick_reference="250")
    result = sc.replay(S1)
    assert result.verdict == "PASS"
    assert result.verdicts[0].vm_codes == ()  # the first intent saw an empty portfolio, not its own fill


def test_a_booking_left_half_written_makes_the_state_ambiguous(sc: Scenario):
    iid = sc.intent(S1, "buy", 100, "100.00")
    sc.raw(
        S1,
        "booking_intent",
        {
            "intent_id": iid, "booking_id": "half-1", "cumulative_quantity": "100", "price": "100.00", "charges": "0",
            "schedule_sha256": "1" * 64, "scenario_set_sha256": "2" * 64, "attempt_id": "att-x",
        },
    )
    sc.intent(S1, "buy", 1, "100.00", isin=ISIN_B)
    with pytest.raises(ReplayRefused) as raised:
        sc.replay(S1)
    assert raised.value.code == vm_replay.STATE_AMBIGUOUS


def test_evidence_that_oversells_refuses_instead_of_inventing_a_position(sc: Scenario):
    sell = sc.intent(S1, "sell", 10, "60.00")
    sc.book(S1, sell, 10, "60.00")
    sc.intent(S1, "buy", 1, "100.00", isin=ISIN_B)
    with pytest.raises(ReplayRefused) as raised:
        sc.replay(S1)
    assert raised.value.code == vm_replay.EVIDENCE_INVALID


# --- divergence -------------------------------------------------------------------------------------------------------


def test_replay_divergence_mac_admits_and_vm_refuses_opens_a_defect_pauses_and_fails(sc: Scenario):
    sc.intent(S1, "buy", 100, "100.00")
    bad = sc.intent(S1, "buy", 100, "600.00", isin=ISIN_B, tick_reference="600")  # 60,000 over the 50,000 cap, Mac admitted it
    sink = Sink()
    result = sc.replay(S1, sink)
    assert result.verdict == "FAIL"
    (divergence,) = result.divergences
    assert divergence.intent_id == bad and divergence.reason == "mac_admitted_vm_refused"
    assert divergence.mac_decision == "admitted" and divergence.vm_codes[0] == "capital_cap"
    assert sink.sessions == [result]
    assert sink.defects == [(vm_replay.DIVERGENCE, S1, "mac_admitted_vm_refused")]
    assert len(sink.pauses) == 1 and sink.pauses[0][0] == vm_replay.DIVERGENCE and bad in sink.pauses[0][1]


def test_replay_divergence_mac_refuses_and_vm_admits_is_a_divergence_too(sc: Scenario):
    sc.intent(S1, "buy", 10, "100.00", mac=("deny", "capital_cap"))
    result = sc.replay(S1)
    assert result.verdict == "FAIL" and result.divergences[0].reason == "mac_refused_vm_admitted"


def test_replay_divergence_the_first_disagreement_stops_the_session(sc: Scenario):
    first = sc.intent(S1, "buy", 100, "600.00", tick_reference="600")  # diverges
    sc.intent(S1, "buy", 100, "700.00", isin=ISIN_B, tick_reference="700")  # would diverge as well
    result = sc.replay(S1)
    assert result.verdict == "FAIL"
    assert [d.intent_id for d in result.divergences] == [first]
    assert result.intents_compared == 1


@pytest.mark.parametrize(
    ("mac", "reason"),
    [
        (("deny", "collar"), "reason_codes_differ"),
        (("deny", "SLIPPAGE_LIMIT"), "mac_code_unmapped"),
        (("deny", "intent_invalid"), "mac_code_unmapped"),
        (("reservation_refused", "simulated_cash_insufficient"), "mac_code_unmapped"),
        (None, "mac_decision_missing"),
    ],
    ids=["other_reason", "slippage", "intent_invalid", "cash_refusal", "no_mac_record"],
)
def test_an_unmapped_or_different_mac_code_is_a_divergence(sc: Scenario, mac: Any, reason: str):
    sc.intent(S1, "buy", 100, "600.00", mac=mac, tick_reference="600")  # the VM says capital_cap
    result = sc.replay(S1)
    assert result.verdict == "FAIL" and result.divergences[0].reason == reason


def test_compare_table():
    assert compare(MacVerdict("admitted", (), True), ()) is None
    assert compare(MacVerdict("denied", ("capital_cap",), False), ("capital_cap", "per_position_cap")) is None
    assert compare(MacVerdict("denied", ("capital_cap",), True), ("capital_cap", "per_position_cap")) == "reason_codes_differ"
    assert compare(MacVerdict("denied", ("capital_cap", "per_position_cap"), True), ("capital_cap", "per_position_cap")) is None
    assert compare(MacVerdict("denied", ("capital_cap",), False), ()) == "mac_refused_vm_admitted"
    assert compare(MacVerdict("admitted", (), True), ("collar",)) == "mac_admitted_vm_refused"
    assert compare(MacVerdict("none", (), False), ()) == "mac_decision_missing"


def test_the_pinned_code_map_names_only_codes_the_vm_can_return_and_no_vm_side_fact():
    assert set(vm_replay.MAC_TO_VM_CODE.values()) <= set(vm_codes.ORDER_ROWS)
    assert not {"kill_switch", "mac_halt", "account_mismatch"} & set(vm_replay.MAC_TO_VM_CODE)
    assert all(key == value for key, value in vm_replay.MAC_TO_VM_CODE.items())


def test_an_admission_record_with_every_refusal_code_is_compared_in_full(sc: Scenario):
    iid = sc.intent(S1, "buy", 100, "600.00", mac=None, tick_reference="600")
    sc.raw(S1, "admission", {"intent_id": iid, "decision": "DENIED", "reason_code": "capital_cap", "refusal_codes": ["capital_cap", "per_position_cap"]})
    assert sc.replay(S1).verdict == "PASS"
    sc2_iid = sc.intent(S1, "buy", 100, "600.00", isin=ISIN_B, mac=None, tick_reference="600")
    sc.raw(S1, "admission", {"intent_id": sc2_iid, "decision": "DENIED", "reason_code": "capital_cap", "refusal_codes": ["capital_cap"]})
    # the first intent now diverges first? no: it still agrees, the second names fewer codes than the VM returns
    result = sc.replay(S1)
    assert result.verdict == "FAIL" and result.divergences[0].reason == "reason_codes_differ"


def test_a_kill_switch_code_in_a_replay_verdict_is_a_defect_not_a_divergence(tmp_path: Path, limits_sha: str):
    forced = make_vm(
        tmp_path, "forced",
        edits={"limits.py": lambda text: text.replace("    return tuple(codes)\n", '    codes.insert(0, "kill_switch")\n    return tuple(codes)\n', 1)},
    )
    sc = Scenario(tmp_path, forced, limits_sha)
    sc.intent(S1, "buy", 10, "100.00")
    sink = Sink()
    result = sc.replay(S1, sink)
    assert result.verdict == "FAIL" and result.divergences == ()
    assert result.defects == (vm_replay.KILL_SWITCH_IN_VERDICT,)
    assert sink.defects == [(vm_replay.KILL_SWITCH_IN_VERDICT, S1, vm_replay.KILL_SWITCH_IN_VERDICT)] and sink.pauses == []


def test_the_replay_asks_the_vm_with_the_kill_switch_open(tmp_path: Path, limits_sha: str):
    """A fixture build that raises when it is asked with the kill switch closed (the flag the live system holds)."""

    strict = make_vm(
        tmp_path, "strict",
        edits={"limits.py": lambda text: text.replace(
            "    if not kill_enabled:\n        codes.append(\"kill_switch\")\n",
            "    if not kill_enabled:\n        raise RuntimeError('asked with the kill switch closed')\n", 1)},
    )
    sc = Scenario(tmp_path, strict, limits_sha)
    sc.intent(S1, "buy", 10, "100.00")
    assert sc.replay(S1).verdict == "PASS"


# --- no intents, no silent skip ---------------------------------------------------------------------------------------


def test_a_session_with_no_intents_writes_an_explicit_no_intents_record(sc: Scenario):
    sc.raw(S2, "non_evaluation_day", {"as_of_date": "2026-10-28", "reason": "cadence"})
    sink = Sink()
    result = sc.replay(S2, sink)
    assert result.no_intents and result.intents_compared == 0 and result.verdict == "PASS"
    assert sink.sessions == [result]


def test_replay_day_missing_a_session_with_evidence_and_no_replay_record_fails_the_run(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00")
    sc.raw(S2, "non_evaluation_day", {"as_of_date": "2026-10-28", "reason": "cadence"})
    first = sc.replay(S1)
    run = run_verdict(RUN, vm_replay.evidence_sessions(sc.records()), {S1: first})
    assert run.verdict == "FAIL" and run.missing == (S2,) and vm_replay.DAY_MISSING in run.reasons
    assert "replay_day_missing" in render_report(run)


def test_all_walks_every_session_and_a_passing_run_needs_every_one_to_pass_at_one_build(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00")
    sc.raw(S2, "non_evaluation_day", {"as_of_date": "2026-10-28", "reason": "cadence"})
    sc.intent(S3, "buy", 1, "100.00", isin=ISIN_B)
    pins = {s: sc.pins_for(s) for s in (S1, S2, S3)}
    sink = Sink()
    run = replay_run(sc.records(), run_id=RUN, sessions=[S1, S2, S3], pins_by_session=pins, vm_source=sc.vm, sink=sink)
    assert run.verdict == "PASS" and [s.session_id for s in run.sessions] == [S1, S2, S3]
    assert len(sink.sessions) == 3 and len(run.build_digests) == 1


def test_all_lists_a_refused_session_and_fails_the_run_without_writing_a_verdict_for_it(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00")
    sc.intent(S2, "buy", 1, "100.00", isin=ISIN_B)
    pins = {S1: sc.pins_for(S1), S2: sc.pins_for(S2, limits={**LIMITS, "capital_cap": "50001"})}
    sink = Sink()
    run = replay_run(sc.records(), run_id=RUN, sessions=[S1, S2], pins_by_session=pins, vm_source=sc.vm, sink=sink)
    assert run.verdict == "FAIL" and run.refused == {S2: vm_replay.LIMITS_MISMATCH}
    assert [s.session_id for s in sink.sessions] == [S1]


def test_all_does_not_skip_a_session_the_caller_forgot_to_pin(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00")
    sc.intent(S2, "buy", 1, "100.00", isin=ISIN_B)
    run = replay_run(sc.records(), run_id=RUN, sessions=[S1], pins_by_session={S1: sc.pins_for(S1)}, vm_source=sc.vm)
    assert run.verdict == "FAIL" and run.refused == {S2: vm_replay.PINS_MISSING}


def test_the_run_verdict_needs_one_build_hash_and_a_run_with_nothing_replayed_has_not_passed(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00")
    sc.intent(S2, "buy", 1, "100.00", isin=ISIN_B)
    one = sc.replay(S1)
    two = sc.replay(S2)
    assert run_verdict(RUN, [S1, S2], {S1: one, S2: two}).verdict == "PASS"
    other = vm_replay.SessionReplay(**{**two.__dict__, "build_digest": "9" * 64})
    mixed = run_verdict(RUN, [S1, S2], {S1: one, S2: other})
    assert mixed.verdict == "FAIL" and mixed.reasons == (vm_replay.BUILD_HASH_MIXED,)
    assert run_verdict(RUN, [], {}).verdict == "FAIL"


def test_a_failing_session_fails_the_run_and_the_report_lists_the_divergence_with_the_d06_label(sc: Scenario):
    sc.intent(S1, "buy", 100, "600.00", tick_reference="600")
    result = sc.replay(S1)
    report = render_report(run_verdict(RUN, [S1], {S1: result}))
    assert vm_replay.D06_LABEL in report and "D-23 verdict: FAIL" in report
    assert "mac_admitted_vm_refused" in report and result.build_digest in report and result.limits_sha256 in report
    assert "full pipeline" not in report.lower() and "—" not in report


# --- the recorded build ---------------------------------------------------------------------------------------------------


def test_replay_build_mismatch_a_checkout_on_another_commit_refuses_and_writes_nothing(sc: Scenario):
    (sc.vm / ".git" / "HEAD").write_text(OTHER_COMMIT + "\n")
    sink = Sink()
    sc.intent(S1, "buy", 1, "100.00")
    with pytest.raises(ReplayRefused) as raised:
        sc.replay(S1, sink)
    assert raised.value.code == vm_replay.BUILD_MISMATCH and sink.empty()


@pytest.mark.parametrize("how", ["edit", "extra_file", "missing_file", "data_file"])
def test_replay_build_mismatch_a_changed_orders_tree_refuses(sc: Scenario, how: str):
    orders = sc.vm / "gateway_vm" / "orders"
    if how == "edit":
        (orders / "limits.py").write_text((orders / "limits.py").read_text() + "\n# local edit\n")
    elif how == "extra_file":
        (orders / "extra.py").write_text("X = 1\n")
    elif how == "missing_file":
        (orders / "alerts.py").unlink()
    else:
        (orders / "data" / "nse_cash_tick_sizes.json").write_text("{}")
    sc.intent(S1, "buy", 1, "100.00")
    with pytest.raises(ReplayRefused) as raised:
        sc.replay(S1)
    assert raised.value.code == vm_replay.BUILD_MISMATCH


def test_replay_build_mismatch_no_recorded_identity_refuses_and_never_falls_back_to_the_working_tree(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00")
    with pytest.raises(ReplayRefused) as raised:
        sc.replay(S1, build=None)
    assert raised.value.code == vm_replay.BUILD_MISMATCH
    nowhere = sc.vm.parent / "not-a-checkout"
    with pytest.raises(ReplayRefused) as raised:
        sc.replay(S1, vm=nowhere)
    assert raised.value.code == vm_replay.BUILD_MISMATCH


def test_a_checkout_without_a_readable_commit_refuses(sc: Scenario):
    shutil.rmtree(sc.vm / ".git")
    sc.intent(S1, "buy", 1, "100.00")
    with pytest.raises(ReplayRefused) as raised:
        sc.replay(S1)
    assert raised.value.code == vm_replay.BUILD_MISMATCH


def test_a_source_under_private_is_never_read(tmp_path: Path, limits_sha: str, monkeypatch: pytest.MonkeyPatch):
    hidden = make_vm(tmp_path / "private", "vm")
    monkeypatch.setattr(vm_replay, "_PRIVATE_ROOT", (tmp_path / "private").resolve())
    sc = Scenario(tmp_path, hidden, limits_sha)
    sc.intent(S1, "buy", 1, "100.00")
    with pytest.raises(ReplayRefused) as raised:
        sc.replay(S1)
    assert raised.value.code == vm_replay.BUILD_MISMATCH


def test_the_replay_runs_the_code_of_the_checkout_it_was_pointed_at_not_the_working_tree(tmp_path: Path, limits_sha: str):
    """A recorded build whose ``capital_cap`` rule is gone admits an over-cap buy. Only that copy's code can say so."""

    sys.path.insert(0, str(REPO / "gateway" / "vm"))  # the working tree's gateway_vm is importable and imported
    try:
        import gateway_vm.orders.limits  # noqa: F401  (loaded from the working tree on purpose)

        loose = make_vm(tmp_path, "loose", edits={"limits.py": lambda t: t.replace('codes.append("capital_cap")', "pass", 1)})
        sc = Scenario(tmp_path, loose, limits_sha)
        sc.intent(S1, "buy", 100, "600.00", mac=("deny", "capital_cap"), tick_reference="600")
        result = sc.replay(S1)
    finally:
        sys.path.remove(str(REPO / "gateway" / "vm"))
    assert result.verdict == "FAIL"
    assert result.divergences[0].vm_codes[:1] != ("capital_cap",)  # the working tree's evaluate would have said capital_cap


@pytest.mark.parametrize(
    "edit",
    [
        lambda t: t.replace("    kill_enabled: bool,\n", "    kill_enabled: bool,\n    extra_input: int = 0,\n", 1),
        lambda t: t.replace("    flags: RiskFlags,\n", "    risk: RiskFlags,\n", 1),
        lambda t: t.replace("    halt: bool = False\n", "    halt: bool = False\n    cooldown: bool = False\n", 1),
        lambda t: t.replace("    ledger_cost: Mapping[str, Decimal] = field(default_factory=dict)\n", "", 1),
        lambda t: t.replace("    cost: Decimal  # total cost basis of the position\n", "    cost: Decimal  # total cost basis of the position\n    lot: int = 1\n", 1),
    ],
    ids=["new_parameter", "renamed_parameter", "new_flag", "dropped_flag", "new_holding_field"],
)
def test_replay_build_mismatch_a_changed_surface_refuses_and_is_never_adapted_to(tmp_path: Path, limits_sha: str, edit: Callable[[str], str]):
    changed = make_vm(tmp_path, "changed", edits={"limits.py": edit})
    assert (changed / "gateway_vm" / "orders" / "limits.py").read_text() != REAL_ORDERS.joinpath("limits.py").read_text()
    sc = Scenario(tmp_path, changed, limits_sha)  # the recorded identity is the changed copy's own
    sc.intent(S1, "buy", 1, "100.00")
    with pytest.raises(ReplayRefused) as raised:
        sc.replay(S1)
    assert raised.value.code == vm_replay.BUILD_MISMATCH


def test_a_build_that_cannot_be_loaded_refuses(tmp_path: Path, limits_sha: str):
    broken = make_vm(tmp_path, "broken", edits={"limits.py": lambda t: t + "\ndef (:\n"})
    sc = Scenario(tmp_path, broken, limits_sha)
    sc.intent(S1, "buy", 1, "100.00")
    with pytest.raises(ReplayRefused) as raised:
        sc.replay(S1)
    assert raised.value.code == vm_replay.BUILD_MISMATCH


def test_loading_a_build_leaves_no_bytecode_in_the_checkout_and_no_module_behind(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00")
    before = dict(orders_tree_sha256(sc.vm))
    sc.replay(S1)
    sc.replay(S1)
    assert not list((sc.vm / "gateway_vm").rglob("__pycache__"))
    assert orders_tree_sha256(sc.vm) == before
    assert [name for name in sys.modules if name.startswith("_growin_vm_replay_")] == []


def test_two_builds_replayed_in_one_process_do_not_share_code(tmp_path: Path, limits_sha: str):
    intact = make_vm(tmp_path, "intact")
    loose = make_vm(tmp_path, "loose", edits={"limits.py": lambda t: t.replace('codes.append("capital_cap")', "pass", 1)})
    (tmp_path / "one").mkdir()
    (tmp_path / "two").mkdir()
    first = Scenario(tmp_path / "one", intact, limits_sha)
    first.intent(S1, "buy", 100, "600.00", mac=("deny", "capital_cap"), tick_reference="600")
    second = Scenario(tmp_path / "two", loose, limits_sha)
    second.intent(S1, "buy", 100, "600.00", mac=("deny", "capital_cap"), tick_reference="600")
    assert first.replay(S1).verdict == "PASS"
    assert second.replay(S1).verdict == "FAIL"
    assert first.replay(S1).verdict == "PASS"  # the second build did not replace the first


# --- the build that runs is the build that was hashed ------------------------------------------------------------------


def plant_unchecked_pyc(vm: Path, filename: str, edit: Callable[[str], str]) -> Path:
    """A valid, unchecked-hash ``.pyc`` of an edited ``filename`` in ``__pycache__``, the source left untouched.

    Python's own import machinery runs this file without looking at the source, which is the hole the hash walk
    (it skips ``__pycache__``) does not see.
    """

    source = vm / "gateway_vm" / "orders" / filename
    original = source.read_text()
    altered = edit(original)
    assert altered != original
    staged = vm.parent / f"altered_{filename}"
    staged.write_text(altered)
    pyc = Path(importlib.util.cache_from_source(str(source)))
    pyc.parent.mkdir(exist_ok=True)
    py_compile.compile(
        str(staged), cfile=str(pyc), doraise=True, invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH
    )
    # the plant is real: Python's loader would run the altered code for the unchanged source
    cached = importlib.machinery.SourceFileLoader("plant_check", str(source)).get_code("plant_check")
    assert cached == compile(altered, str(source), "exec", dont_inherit=True)
    assert cached != compile(original, str(source), "exec", dont_inherit=True)
    return pyc


def test_a_cached_pyc_next_to_unchanged_source_is_never_run(sc: Scenario):
    """The Mac admits a Rs 60,000 buy against the Rs 50,000 cap. The altered bytecode would admit it too (PASS)."""

    sc.intent(S1, "buy", 600, "100.00")
    plant_unchecked_pyc(
        sc.vm,
        "limits.py",
        lambda t: t.replace("if deployed > limits.capital_cap:", "if False:").replace(
            "> limits.per_position_cap:", "> limits.per_position_cap * 1000:"
        ),
    )
    assert orders_tree_sha256(sc.vm) == sc.identity.orders_sha256  # the source and its recorded hashes are unchanged
    result = sc.replay(S1)
    assert result.verdict == "FAIL"
    assert result.divergences[0].reason == "mac_admitted_vm_refused"
    assert "capital_cap" in result.divergences[0].vm_codes


@pytest.mark.parametrize("filename", ["__init__.py", "intent.py", "challenge.py", "limits.py"])
def test_a_planted_pyc_for_any_loaded_file_is_never_run(sc: Scenario, filename: str):
    sc.intent(S1, "buy", 100, "100.00")
    plant_unchecked_pyc(sc.vm, filename, lambda t: t + "\nraise RuntimeError('altered bytecode ran')\n")
    assert sc.replay(S1).verdict == "PASS"  # the genuine source ran, so the planted raise never fired


def test_the_bytes_that_were_hashed_are_the_bytes_that_run(sc: Scenario, monkeypatch: pytest.MonkeyPatch):
    """A file swapped on disk after the hash walk, before the load, must not be what runs."""

    sc.intent(S1, "buy", 600, "100.00", mac=("deny", "capital_cap"), tick_reference="100")
    real = vm_replay._digests

    def swap_after_hashing(sources: Any) -> Any:
        digests = real(sources)
        target = sc.vm / "gateway_vm" / "orders" / "limits.py"
        target.write_text(target.read_text().replace('codes.append("capital_cap")', "pass", 1))
        return digests

    monkeypatch.setattr(vm_replay, "_digests", swap_after_hashing)
    assert sc.replay(S1).verdict == "PASS"  # the Mac's capital_cap refusal met the hashed code's capital_cap


def test_the_loader_consults_no_import_machinery(sc: Scenario, monkeypatch: pytest.MonkeyPatch):
    def trip(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("the loader reached the import machinery")

    for owner, attr in (
        (importlib.machinery.SourceFileLoader, "get_code"),
        (importlib.machinery.SourceFileLoader, "exec_module"),
        (importlib.util, "spec_from_file_location"),
    ):
        monkeypatch.setattr(owner, attr, trip)
    sc.intent(S1, "buy", 100, "100.00")
    assert sc.replay(S1).verdict == "PASS"


OFF_TICK_TABLE = "gateway_vm/orders/data/nse_cash_tick_sizes.json"


def finer_ticks(text: str) -> str:
    changed = text.replace('"tick": "0.01"', '"tick": "0.001"')
    assert changed != text
    return changed


def test_a_tick_table_swapped_on_disk_after_the_hash_walk_does_not_change_the_verdict(sc: Scenario, monkeypatch: pytest.MonkeyPatch):
    """Rs 100.005 is off tick. The Mac refused it. A table with finer ticks swapped in after hashing would admit it."""

    sc.intent(S1, "buy", 1, "100.005", mac=("deny", "off_tick"), tick_reference="100")
    real = vm_replay._digests
    table = sc.vm / OFF_TICK_TABLE

    def swap_after_hashing(sources: Any) -> Any:
        digests = real(sources)
        table.write_text(finer_ticks(table.read_text()))
        return digests

    monkeypatch.setattr(vm_replay, "_digests", swap_after_hashing)
    result = sc.replay(S1)
    assert result.verdict == "PASS", result
    assert result.verdicts[0].vm_codes == ("off_tick",)


def test_a_tick_table_swapped_on_disk_between_two_intents_does_not_change_the_second_verdict(sc: Scenario, monkeypatch: pytest.MonkeyPatch):
    sc.intent(S1, "buy", 1, "100.00", tick_reference="100")
    sc.intent(S1, "buy", 1, "100.005", isin=ISIN_B, mac=("deny", "off_tick"), tick_reference="100")
    real = vm_replay._replay_intent
    table = sc.vm / OFF_TICK_TABLE
    calls: list[int] = []

    def swap_after_the_first(*args: Any, **kwargs: Any) -> Any:
        out = real(*args, **kwargs)
        if not calls:
            table.write_text(finer_ticks(table.read_text()))
        calls.append(1)
        return out

    monkeypatch.setattr(vm_replay, "_replay_intent", swap_after_the_first)
    assert sc.replay(S1).verdict == "PASS"
    assert len(calls) == 2


_HELD_DIRECTORIES: list[Any] = []


def private_dirs(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    import tempfile

    made: list[Path] = []
    real = tempfile.TemporaryDirectory

    def recording(*args: Any, **kwargs: Any) -> Any:
        directory = real(*args, **kwargs)
        made.append(Path(directory.name))
        _HELD_DIRECTORIES.append(directory)  # keep it referenced: the object's own finalizer would otherwise hide a missing cleanup
        return directory

    monkeypatch.setattr(vm_replay.tempfile, "TemporaryDirectory", recording)
    return made


def test_the_private_copy_of_the_build_holds_the_hashed_bytes_and_is_removed_after_the_replay(sc: Scenario, monkeypatch: pytest.MonkeyPatch):
    made = private_dirs(monkeypatch)
    sc.intent(S1, "buy", 1, "100.00")
    sc.replay(S1)
    assert len(made) == 1 and not made[0].exists()
    assert not made[0].is_relative_to(sc.vm)  # never in the checkout
    assert not list((sc.vm / "gateway_vm").rglob("__pycache__"))


def test_the_private_copy_is_removed_when_the_build_is_refused_after_loading(tmp_path: Path, limits_sha: str, monkeypatch: pytest.MonkeyPatch):
    made = private_dirs(monkeypatch)
    drifted = make_vm(tmp_path, "drifted", edits={"limits.py": lambda t: t.replace("    kill_enabled: bool,\n", "    kill_enabled: bool,\n    extra_input: int = 0,\n", 1)})
    with pytest.raises(ReplayRefused) as raised:
        load_recorded_build(drifted, identity_of(drifted))
    assert raised.value.code == vm_replay.BUILD_MISMATCH
    assert made and not any(path.exists() for path in made)


def test_a_loaded_build_reads_its_data_files_from_the_private_copy_not_the_checkout(sc: Scenario):
    built = load_recorded_build(sc.vm, sc.identity)
    try:
        assert Path(built.evaluate.__globals__["TICK_TABLE_PATH"]).read_bytes() == (sc.vm / OFF_TICK_TABLE).read_bytes()
        assert not Path(built.evaluate.__globals__["TICK_TABLE_PATH"]).is_relative_to(sc.vm)
    finally:
        built.close()


# --- git head reading -------------------------------------------------------------------------------------------------


def test_read_git_commit_handles_a_ref_a_packed_ref_a_linked_worktree_and_garbage(tmp_path: Path):
    plain = tmp_path / "plain"
    (plain / ".git" / "refs" / "heads").mkdir(parents=True)
    (plain / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (plain / ".git" / "refs" / "heads" / "main").write_text(COMMIT + "\n")
    assert read_git_commit(plain) == COMMIT

    packed = tmp_path / "packed"
    (packed / ".git").mkdir(parents=True)
    (packed / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (packed / ".git" / "packed-refs").write_text(f"# pack-refs\n{OTHER_COMMIT} refs/heads/main\n")
    assert read_git_commit(packed) == OTHER_COMMIT

    common = tmp_path / "common" / ".git"
    (common / "refs" / "heads").mkdir(parents=True)
    (common / "refs" / "heads" / "feature").write_text(COMMIT + "\n")
    linked = common / "worktrees" / "wt"
    linked.mkdir(parents=True)
    (linked / "HEAD").write_text("ref: refs/heads/feature\n")
    (linked / "commondir").write_text("../..\n")
    tree = tmp_path / "tree"
    (tree / "gateway" / "vm").mkdir(parents=True)
    (tree / ".git").write_text(f"gitdir: {linked}\n")
    assert read_git_commit(tree / "gateway" / "vm") == COMMIT  # the checkout root is two levels above the source dir

    bad = tmp_path / "bad"
    (bad / ".git").mkdir(parents=True)
    (bad / ".git" / "HEAD").write_text("not a head\n")
    assert read_git_commit(bad) is None
    (bad / ".git" / "HEAD").write_text("ref: refs/heads/missing\n")
    assert read_git_commit(bad) is None
    assert read_git_commit(tmp_path / "nowhere") is None


# --- limits -----------------------------------------------------------------------------------------------------------


def test_replay_limits_mismatch_refuses_no_verdicts_written(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00")
    sink = Sink()
    with pytest.raises(ReplayRefused) as raised:
        sc.replay(S1, sink, limits={**LIMITS, "capital_cap": "50001"})  # one value changed, the recorded hash is the old one
    assert raised.value.code == vm_replay.LIMITS_MISMATCH and sink.empty()


def test_replay_limits_mismatch_refuses_a_limits_document_the_build_rejects(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00")
    broken = {k: v for k, v in LIMITS.items() if k != "fat_finger_collar"}
    with pytest.raises(ReplayRefused) as raised:
        sc.replay(S1, limits=broken)
    assert raised.value.code == vm_replay.LIMITS_MISMATCH


def test_replay_limits_mismatch_refuses_when_the_sessions_signals_recorded_other_limits(sc: Scenario):
    sc.signal(S1, limits_sha256="7" * 64)
    sc.intent(S1, "buy", 1, "100.00")
    with pytest.raises(ReplayRefused) as raised:
        sc.replay(S1)
    assert raised.value.code == vm_replay.LIMITS_MISMATCH


def test_signals_that_record_the_same_limits_hash_pass(sc: Scenario, limits_sha: str):
    sc.signal(S1, limits_sha256=limits_sha)
    sc.intent(S1, "buy", 1, "100.00")
    assert sc.replay(S1).verdict == "PASS"


def test_the_limits_hash_is_the_vms_p06_hash_of_the_nine_contract_keys(limits_sha: str):
    assert limits_sha == vm_replay._sha256_json(LIMITS)
    assert set(vm_replay.LIMIT_KEYS) == set(LIMITS)


def test_replay_limits_mismatch_refuses_when_an_intents_signal_of_another_session_recorded_other_limits(sc: Scenario):
    sc.raw(S1, "signal", {**signal_payload("sig-cross"), "limits_sha256": "9" * 64})
    sc.intent(S2, "buy", 1, "100.00", signal=False, extra={"signal_id": "sig-cross"})
    with pytest.raises(ReplayRefused) as raised:
        sc.replay(S2)
    assert raised.value.code == vm_replay.LIMITS_MISMATCH


def test_an_intents_signal_of_another_session_with_the_same_limits_hash_passes(sc: Scenario, limits_sha: str):
    sc.raw(S1, "signal", {**signal_payload("sig-cross"), "limits_sha256": limits_sha})
    sc.intent(S2, "buy", 1, "100.00", signal=False, extra={"signal_id": "sig-cross"})
    assert sc.replay(S2).verdict == "PASS"


# --- the instrument comes from the signal ----------------------------------------------------------------------------

ENVELOPE = {"seq", "prev_sha256", "entry_sha256", "kind", "run_id", "session_id", "run_class", "utc", "ist"}
ADMISSION_PY = REPO / "backend" / "shadow_india" / "admission.py"
WRITER_INTENT_FIELDS = set(SCHEMAS["intent"][0])  # the required fields of an intent record: what the writer must emit


def admission_intent_write() -> set[str]:
    """The keys of the dict ``AdmissionService.prepare`` appends as the intent record, read from admission.py itself."""

    for node in ast.walk(ast.parse(ADMISSION_PY.read_text())):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "_append"
            and len(node.args) == 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == "E_INTENT"
        ):
            assert isinstance(node.args[1], ast.Dict)
            return {key.value for key in node.args[1].keys if isinstance(key, ast.Constant)}
    raise AssertionError("admission.py no longer appends an intent record the way this test reads it")


def test_the_fixture_intent_is_what_admission_py_writes():
    """The writer emits the schema's required fields and nothing optional (so no ``isin``, no ``series``)."""

    written = admission_intent_write()
    assert written - {"session_id"} == WRITER_INTENT_FIELDS
    assert not written & set(SCHEMAS["intent"][1])


def signal_payload(signal_id: str, isin: str = ISIN_A, series: str = "EQ") -> dict[str, Any]:
    return {
        "signal_id": signal_id, "symbol": SYMBOL[isin], "isin": isin, "series": series, "as_of_date": "2026-10-26",
        **{name: "f" * 64 for name in SIGNAL_HASHES},
    }


def refusal_of(sc: Scenario, session: str = S1, records: list[dict[str, Any]] | None = None) -> str:
    with pytest.raises(ReplayRefused) as raised:
        replay_session(
            records if records is not None else sc.records(), run_id=RUN, session_id=session,
            pins=sc.pins_for(session), vm_source=sc.vm,
        )
    return raised.value.code


def test_a_writer_shaped_intent_with_no_isin_or_series_replays_and_passes(sc: Scenario):
    first = fund(sc, quantity=100, price="100.00", charges="20")  # booked: the fold needs its ISIN from the signal
    second = sc.intent(S1, "buy", 500, "100.00", isin=ISIN_A, mac=("deny", "capital_cap"))
    intents = [r for r in sc.records() if r["kind"] == "intent"]
    assert {r["intent_id"] for r in intents} == {first, second}
    for record in intents:  # exactly what AdmissionService.prepare emits: no isin, no series
        assert set(record) - ENVELOPE == WRITER_INTENT_FIELDS
    result = sc.replay(S1)
    assert result.verdict == "PASS", result
    assert result.verdicts[1].vm_codes[0] == "capital_cap"  # the first fill counted against the cap, by ISIN


def test_the_isin_and_series_the_vm_sees_are_the_signals(sc: Scenario):
    """A signal whose ISIN is not an NSE one: the VM's own instrument check answers, so the signal is what reached it."""

    sc.intent(S1, "buy", 1, "100.00", signal={"isin": "US0378331005"}, mac=("deny", "instrument_unsupported"))
    assert sc.replay(S1).verdict == "PASS"


def test_a_signal_series_the_vm_does_not_trade_is_the_vms_to_refuse(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00", signal={"series": "BZ"}, mac=("deny", "instrument_unsupported"))
    sc.pins[S1][next(iter(sc.pins[S1]))]["quote"].pop("series")  # the pin may omit it: the signal's series is used
    assert sc.replay(S1).verdict == "PASS"


def test_replay_signal_missing_an_intent_with_no_signal_record_refuses(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00", signal=False)
    sink = Sink()
    with pytest.raises(ReplayRefused) as raised:
        sc.replay(S1, sink)
    assert raised.value.code == vm_replay.SIGNAL_MISSING == "replay_signal_missing"
    assert sink.empty()


def test_replay_signal_missing_an_intent_pointing_at_an_unknown_signal_id_refuses(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00", extra={"signal_id": "sig-nowhere"})
    assert refusal_of(sc) == vm_replay.SIGNAL_MISSING


def test_replay_signal_missing_for_a_booked_intent_of_another_session_refuses_the_fold(sc: Scenario):
    iid = sc.intent(S1, "buy", 100, "100.00", signal=False)
    sc.book(S1, iid, 100, "100.00")
    sc.intent(S2, "buy", 1, "100.00")
    assert refusal_of(sc, S2) == vm_replay.SIGNAL_MISSING


def test_replay_signal_mismatch_a_symbol_that_differs_from_its_signals_refuses(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00", symbol="ZZZ")
    sink = Sink()
    with pytest.raises(ReplayRefused) as raised:
        sc.replay(S1, sink)
    assert raised.value.code == vm_replay.SIGNAL_MISMATCH == "replay_signal_mismatch"
    assert sink.empty()


@pytest.mark.parametrize("field, value", [("isin", ISIN_B), ("series", "BE")])
def test_replay_signal_mismatch_an_intent_that_carries_its_own_isin_or_series_must_agree(sc: Scenario, field: str, value: str):
    sc.intent(S1, "buy", 1, "100.00", extra={field: value})
    assert refusal_of(sc) == vm_replay.SIGNAL_MISMATCH


def test_an_intent_that_carries_the_same_isin_and_series_as_its_signal_is_accepted(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00", extra={"isin": ISIN_A, "series": "EQ"})
    assert sc.replay(S1).verdict == "PASS"


def test_replay_signal_mismatch_a_pinned_series_that_differs_from_the_signals_refuses(sc: Scenario):
    iid = sc.intent(S1, "buy", 1, "100.00")
    sc.pins[S1][iid]["quote"]["series"] = "BE"
    assert refusal_of(sc) == vm_replay.SIGNAL_MISMATCH


def test_one_signal_id_that_names_two_instruments_is_invalid_evidence(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00")
    sc.raw(S1, "signal", {**signal_payload("sig-1", ISIN_B), "limits_sha256": sc.limits_sha})
    assert refusal_of(sc) == vm_replay.EVIDENCE_INVALID


# --- exit and swing sells have no signal ---------------------------------------------------------------------------------

SELLS = [("halve", 50), ("stop", 100), ("flatten", 100), ("swing", 100)]


@pytest.mark.parametrize(("kind", "quantity"), SELLS, ids=[k for k, _ in SELLS])
def test_a_runner_shaped_sell_with_no_signal_record_replays_to_pass(sc: Scenario, kind: str, quantity: int):
    fund(sc)  # a booked buy of 100 in the first session
    iid = sc.sell(S2, kind, quantity, "60.00")
    sell = next(r for r in sc.records() if r["kind"] == "intent" and r["intent_id"] == iid)
    assert set(sell) - ENVELOPE == WRITER_INTENT_FIELDS
    assert not [r for r in sc.records() if r["kind"] == "signal" and r["signal_id"] == sell["signal_id"]]
    result = sc.replay(S2)
    assert result.verdict == "PASS", result
    assert result.intents_compared == 1 and result.defects == ()


def test_a_booked_exit_sell_lowers_the_position_the_next_decision_sees(sc: Scenario):
    fund(sc, quantity=100, price="100.00", charges="0")  # cost 10,000
    sold = sc.sell(S2, "halve", 50, "100.00")
    sc.book(S2, sold, 50, "100.00")  # cost 5,000 left
    sc.intent(S2, "buy", 450, "100.00", isin=ISIN_B, tick_reference="100")  # 5,000 + 45,000 is not over the 50,000 cap
    result = sc.replay(S2)
    assert result.verdict == "PASS", result
    assert result.intents_compared == 2


def test_a_booked_sell_of_the_whole_position_leaves_nothing_to_sell_again(sc: Scenario):
    fund(sc)
    gone = sc.sell(S1, "flatten", 100, "60.00", minute=40)
    sc.book(S1, gone, 100, "60.00")
    sc.sell(S2, "swing", 100, "60.00")
    assert refusal_of(sc, S2) == vm_replay.INSTRUMENT_UNRESOLVED


def test_replay_instrument_unresolved_a_sell_for_an_isin_that_is_not_held_refuses(sc: Scenario):
    sc.sell(S1, "halve", 10, "60.00")
    sink = Sink()
    with pytest.raises(ReplayRefused) as raised:
        sc.replay(S1, sink)
    assert raised.value.code == vm_replay.INSTRUMENT_UNRESOLVED == "replay_instrument_unresolved"
    assert sink.empty()


@pytest.mark.parametrize("kind", ["halve", "swing"])
def test_replay_instrument_unresolved_a_sell_whose_symbol_differs_from_the_held_position_refuses(sc: Scenario, kind: str):
    fund(sc)
    sc.sell(S2, kind, 10, "60.00", symbol="ZZZ")
    assert refusal_of(sc, S2) == vm_replay.INSTRUMENT_UNRESOLVED


def test_a_sell_of_an_isin_other_than_the_one_held_refuses(sc: Scenario):
    fund(sc, isin=ISIN_A)
    sc.sell(S2, "stop", 10, "60.00", isin=ISIN_B)
    assert refusal_of(sc, S2) == vm_replay.INSTRUMENT_UNRESOLVED


def unknown_shape(sc: Scenario, leg: str, signal_id: str, side: str = "sell") -> None:
    fund(sc)
    sc.intent(S2, side, 10, "60.00", signal=False, leg=leg, signal_id=signal_id)


EXIT_OK = "exit-halve-20261028-INE002A01018-t1"
UNKNOWN_SHAPES = [
    ("exit", "exit-bogus"),
    ("exit", "exit-halve-20261028-INE002A01018-t0"),  # the runner never mints attempt 0
    ("exit", "exit-reduce-20261028-INE002A01018-t1"),  # not halve, stop or flatten
    ("exit", "exit-halve-20261028-INE002A0101-t1"),  # a short ISIN
    ("exit", "swing-20261028-INE002A01018"),  # a swing id on the exit leg
    ("swing", EXIT_OK),  # an exit id on the swing leg
    ("swing", "swing-2026-INE002A01018"),
    ("swing", "swing-20261028-INE002A01018-t1"),
    ("pilot", EXIT_OK),
    ("pilot", "swing-20261028-INE002A01018"),
]


@pytest.mark.parametrize(("leg", "signal_id"), UNKNOWN_SHAPES, ids=[f"{leg}:{sid}" for leg, sid in UNKNOWN_SHAPES])
def test_replay_leg_unknown_an_id_shape_the_runner_never_mints_refuses(sc: Scenario, leg: str, signal_id: str):
    unknown_shape(sc, leg, signal_id)
    sink = Sink()
    with pytest.raises(ReplayRefused) as raised:
        sc.replay(S2, sink)
    assert raised.value.code == vm_replay.LEG_UNKNOWN == "replay_leg_unknown"
    assert sink.empty()


def test_replay_leg_unknown_a_buy_under_an_exit_or_swing_id_refuses(sc: Scenario):
    unknown_shape(sc, LEG_EXIT, EXIT_OK, side="buy")
    assert refusal_of(sc, S2) == vm_replay.LEG_UNKNOWN


def test_a_sell_needs_its_pinned_series_because_no_signal_names_it(sc: Scenario):
    fund(sc)
    iid = sc.sell(S2, "halve", 10, "60.00")
    del sc.pins[S2][iid]["quote"]["series"]
    assert refusal_of(sc, S2) == vm_replay.PINS_MISSING


def test_a_sells_pinned_series_is_the_series_the_vm_sees(sc: Scenario):
    fund(sc)
    sc.sell(S2, "halve", 10, "60.00", series="BZ", mac=("deny", "instrument_unsupported"))
    assert sc.replay(S2).verdict == "PASS"


def test_an_entry_still_needs_its_signal_even_beside_signal_less_sells(sc: Scenario):
    fund(sc)
    sc.sell(S2, "halve", 10, "60.00")
    sc.intent(S2, "buy", 1, "60.00", signal=False)
    assert refusal_of(sc, S2) == vm_replay.SIGNAL_MISSING


def test_the_session_limits_check_still_applies_to_a_session_of_signal_less_sells(sc: Scenario):
    fund(sc)
    sc.sell(S2, "halve", 10, "60.00")
    sc.signal(S2, limits_sha256="7" * 64)
    assert refusal_of(sc, S2) == vm_replay.LIMITS_MISMATCH


def test_the_sell_ids_are_the_ones_the_runner_mints_and_the_replay_shares_its_pattern():
    import shadow_india.runner as runner

    exit_id = exit_intent_id(exit_obligation_id("halve", ISIN_A, date(2026, 10, 28)), 1)
    assert exit_id == EXIT_OK
    assert runner._EXIT_ID.fullmatch(exit_id) and vm_replay._EXIT_ID is runner._EXIT_ID  # shared, not copied
    swing = swing_signal_id(date(2026, 10, 28), ISIN_A)
    assert vm_replay._SWING_ID.fullmatch(swing)
    assert 'signal_id=f"swing-{as_of:%Y%m%d}-{isin}"' in (REPO / "backend" / "shadow_india" / "runner.py").read_text()
    assert vm_replay._ISIN_PATTERN in runner._EXIT_ID.pattern  # one ISIN shape across the exit and swing ids
    assert (vm_replay.LEG_ENTRY, vm_replay.LEG_EXIT, vm_replay.LEG_SWING) == (LEG_ENTRY, LEG_EXIT, LEG_SWING)


# --- a missing field is a typed refusal ------------------------------------------------------------------------------


def evidence_with_every_kind(sc: Scenario) -> None:
    """A denied intent, a reserved one, a booked one and a later one, so every record kind the replay reads is there."""

    sc.intent(S1, "buy", 1, "100.00", mac=("deny", "capital_cap"))
    fund(sc, quantity=10, price="100.00", charges="1")
    sc.intent(S1, "buy", 1, "100.00")


def drop(records: list[dict[str, Any]], kind: str, field: str) -> list[dict[str, Any]]:
    out = [dict(r) for r in records]
    victim = next(r for r in out if r["kind"] == kind)
    del victim[field]
    return out


MISSING_FIELDS = [
    ("denied", "code"), ("denied", "intent_id"),
    ("intent", "seq"), ("intent", "side"), ("intent", "symbol"), ("intent", "intent_id"), ("intent", "quantity"),
    ("intent", "limit_price"), ("intent", "signal_id"), ("intent", "leg"),
    ("reservation", "intent_id"), ("reservation", "seq"),
    ("booking_intent", "booking_id"), ("booking_intent", "intent_id"), ("booking_intent", "cumulative_quantity"),
    ("booking_intent", "price"), ("booking_intent", "charges"),
    ("booking_done", "booking_id"),
]


@pytest.mark.parametrize(("kind", "field"), MISSING_FIELDS, ids=[f"{k}.{f}" for k, f in MISSING_FIELDS])
def test_a_record_missing_a_field_the_replay_reads_is_a_typed_refusal_not_a_bare_error(sc: Scenario, kind: str, field: str):
    evidence_with_every_kind(sc)
    code = refusal_of(sc, records=drop(sc.records(), kind, field))
    assert code == vm_replay.EVIDENCE_INVALID


@pytest.mark.parametrize("bad", ["7", None, 1.5, True, [1]])
def test_a_seq_that_is_not_an_integer_is_a_typed_refusal(sc: Scenario, bad: Any):
    sc.intent(S1, "buy", 1, "100.00")
    records = [dict(r) for r in sc.records()]
    records[-1]["seq"] = bad
    assert refusal_of(sc, records=records) == vm_replay.EVIDENCE_INVALID


def test_the_replay_run_lists_a_session_with_a_malformed_record_as_refused_and_fails(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00", mac=("deny", "capital_cap"))
    records = drop(sc.records(), "denied", "code")
    run = replay_run(
        records, run_id=RUN, sessions=[S1], pins_by_session={S1: sc.pins_for(S1)}, vm_source=sc.vm
    )
    assert run.verdict == "FAIL" and run.refused == {S1: vm_replay.EVIDENCE_INVALID}


# --- pins -------------------------------------------------------------------------------------------------------------


def test_a_missing_quote_pin_refuses_instead_of_guessing_a_quote(sc: Scenario):
    iid = sc.intent(S1, "buy", 1, "100.00")
    del sc.pins[S1][iid]
    with pytest.raises(ReplayRefused) as raised:
        sc.replay(S1)
    assert raised.value.code == vm_replay.PINS_MISSING


def test_a_pins_document_for_another_session_is_refused(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00")
    with pytest.raises(ReplayRefused) as raised:
        sc.replay(S1, session_id=S2)
    assert raised.value.code == vm_replay.PINS_MISSING


def test_a_recorded_unavailable_tick_reference_is_the_vms_to_judge(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00", tick_reference=None, mac=("deny", "tick_reference_unavailable"))
    assert sc.replay(S1).verdict == "PASS"


def test_a_stale_quote_session_date_is_the_vms_quote_unavailable(sc: Scenario):
    iid = sc.intent(S1, "buy", 1, "100.00", mac=("deny", "quote_unavailable"))
    sc.pins[S1][iid]["quote"]["session_date"] = "2026-10-26"
    assert sc.replay(S1).verdict == "PASS"


def test_a_decision_outside_the_session_window_is_the_vms_session_closed(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00", mac=("deny", "session_closed"), minute=5)  # 09:05 IST, before the open
    assert sc.replay(S1).verdict == "PASS"


def test_the_decision_time_is_the_intents_recorded_ist_not_a_clock_read_now(sc: Scenario):
    late = sc.intent(S1, "buy", 1, "100.00", minute=59)
    assert late and sc.replay(S1).verdict == "PASS"  # 09:59 IST: open


def _pins_doc(**changes: Any) -> bytes:
    doc: dict[str, Any] = {
        "schema": vm_replay.PINS_SCHEMA, "session_id": S1, "build": None, "limits_sha256": "a" * 64, "limits": dict(LIMITS), "intents": {},
    }
    doc.update(changes)
    return json.dumps(doc).encode("ascii")


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b'{"schema": "other"}',
        b'{"schema": "growin-vm-replay-pins/1", "session_id": "x", "limits_sha256": "a", "limits": {}, "intents": {}}',
        b'{"schema": "growin-vm-replay-pins/1", "schema": "growin-vm-replay-pins/1"}',
        _pins_doc(intents={"i": {"quote": {"ltp": 1.5, "lower_circuit": "1", "upper_circuit": "2", "previous_close": "1"}, "tick_reference": "1"}}),
        _pins_doc(intents={"i": {"quote": {"ltp": "NaN", "lower_circuit": "1", "upper_circuit": "2", "previous_close": "1"}, "tick_reference": "1"}}),
        _pins_doc(intents={"i": {"quote": {"ltp": "1", "lower_circuit": "1", "upper_circuit": "2", "previous_close": "1"}}}),
        _pins_doc(build={"commit": "short", "orders_sha256": {"a.py": "a" * 64}}),
        _pins_doc(mac_halt="yes"),
    ],
    ids=["not_json", "schema", "short_hash", "duplicate_key", "float", "nan", "no_tick_reference", "bad_commit", "bad_flag"],
)
def test_parse_pins_refuses_a_malformed_document(raw: bytes):
    with pytest.raises(ReplayRefused) as raised:
        parse_pins(raw)
    assert raised.value.code == vm_replay.PINS_INVALID


def test_a_session_that_recorded_mac_halt_replays_it(sc: Scenario):
    sc.extra[S1] = {"mac_halt": True}
    sc.intent(S1, "buy", 1, "100.00", mac=("deny", "capital_cap"))
    result = sc.replay(S1)
    assert result.verdict == "FAIL" and result.divergences[0].vm_codes[0] == "mac_halt"


# --- isolation --------------------------------------------------------------------------------------------------------


def test_the_replay_makes_no_network_or_relay_call(sc: Scenario, network_sentinel: list[str]):
    fund(sc, quantity=10)
    sc.intent(S1, "buy", 1, "100.00", isin=ISIN_B)
    run = replay_run(sc.records(), run_id=RUN, sessions=[S1], pins_by_session={S1: sc.pins_for(S1)}, vm_source=sc.vm, sink=Sink())
    assert run.verdict == "PASS" and network_sentinel == []


def test_the_sentinel_trips_on_a_planted_call_so_a_quiet_log_means_something(sc: Scenario, network_sentinel: list[str], monkeypatch: pytest.MonkeyPatch):
    real = vm_replay.read_git_commit

    def planted(path: Path) -> str | None:
        socket.create_connection(("127.0.0.1", 9))  # the call a mutant would plant
        return real(path)

    monkeypatch.setattr(vm_replay, "read_git_commit", planted)
    sc.intent(S1, "buy", 1, "100.00")
    with pytest.raises(SentinelTripped):
        sc.replay(S1)
    assert network_sentinel == ["socket.create_connection"]
    network_sentinel.clear()  # the trip was the point of this test; the fixture asserts the log is empty at teardown


NETWORK_AND_RELAY_ROOTS = {"socket", "ssl", "http", "httpx", "requests", "urllib", "urllib3", "aiohttp", "brokers", "execution", "subprocess", "random"}
CLIENT_NAMES = {"RelayClient", "OrdersClient", "urlopen", "create_connection", "getaddrinfo"}


def isolation_findings(source: str) -> list[str]:
    """The purity scan, plus the two things it does not name: the relay client modules and a client by name."""

    found = violations(source, "vm_replay.py")
    tree = ast.parse(source)
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    found += [f"import of {name}" for name in sorted(imported & NETWORK_AND_RELAY_ROOTS)]
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    found += [f"name {name}" for name in sorted(names & CLIENT_NAMES)]
    return found


@pytest.mark.parametrize(
    "plant",
    [
        "import socket\nsocket.create_connection(('x', 1))\n",
        "import httpx\nhttpx.get('x')\n",
        "from brokers.breeze_gateway.client import RelayClient\nRelayClient()\n",
        "from backend.brokers.breeze_gateway import client\nclient.RelayClient\n",
        "import urllib.request\nurllib.request.urlopen('x')\n",
        "import requests\nrequests.get('x')\n",
        "from execution.ledger import ExecutionLedger\n",
    ],
    ids=["socket", "httpx", "relay_client", "relay_module_by_backend_path", "urllib", "requests", "execution"],
)
def test_isolation_a_planted_network_or_relay_call_in_vm_replay_is_caught(plant: str):
    source = VM_REPLAY_SOURCE.read_text()
    assert isolation_findings(source) == []
    assert isolation_findings(source + "\n" + plant), "the plant went unnoticed"


def test_the_module_reads_no_wall_clock_and_names_the_d06_label_verbatim():
    text = VM_REPLAY_SOURCE.read_text()
    assert ".now(" not in text and ".utcnow(" not in text and "time.time" not in text
    assert "Mac simulation plus separately tested VM guards" in text and "full pipeline" not in text.lower()
    assert "—" not in text
