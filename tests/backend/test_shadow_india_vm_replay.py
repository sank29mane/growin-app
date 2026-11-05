"""Phase 64-05 Task 2b: the offline D-23 replay of the shadow portfolio through the VM's own risk code.

Fakes only: a tmp checkout that is a copy of ``gateway/vm/gateway_vm/orders`` with a fake ``.git``, fixture limits
and evidence written through the real ``EvidenceStore``. No VM, relay, broker or network call: a sentinel patches
every socket, HTTP and relay entry point for the whole module and any call fails the test.

Row names follow the 64-05 break-proof table: replay build hash, divergence, day missing, isolation, kill flag
(``replay_kill_enabled_true``) and limits hash (``replay_limits_mismatch_refuses``).
"""

from __future__ import annotations

import ast
import dataclasses
import importlib.machinery
import importlib.util
import inspect
import py_compile
import shutil
import socket
import sys
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Mapping

import pytest

import shadow_india.vm_replay as vm_replay
from shadow_india import policy, vm_codes
from shadow_india.__main__ import EvidenceReplaySink
from shadow_india.admission import deterministic_proposal_id
from shadow_india.evidence import QUOTE_PIN_FIELDS, SCHEMAS, EvidenceStore, read_records
from shadow_india.portfolio import exit_intent_id, exit_obligation_id
from shadow_india.runner import LEG_ENTRY, LEG_EXIT, LEG_SWING, session_id_for
from shadow_india.vm_replay import (
    BuildIdentity,
    MacVerdict,
    ReplayLimits,
    ReplayRefused,
    build_from_evidence,
    compare,
    load_recorded_build,
    not_replayed_sessions,
    orders_tree_sha256,
    pins_from_evidence,
    read_git_commit,
    recorded_run_verdict,
    render_report,
    replay_run,
    replay_session,
    replay_sessions,
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
S1, S2, S3 = (session_id_for(date(2026, 10, day)) for day in (27, 28, 29))  # the ids the runner and the CLI mint
S0 = session_id_for(date(2026, 10, 26))
DAYS = {S0: (2026, 10, 26), S1: (2026, 10, 27), S2: (2026, 10, 28), S3: (2026, 10, 29)}  # Tuesday to Thursday
HASHES = {"strategy_sha256": "a" * 64, "params_sha256": "b" * 64}
FILE_SHA = "5" * 64  # the IndiaLimits file hash: synthetic, in the manifest and on every signal
MANIFEST_HASHES = {**HASHES, "limits_sha256": FILE_SHA}
RAW_SHA = "9" * 64
DELETE = object()

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


# --- evidence ------------------------------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 27, 9, 35, 0, tzinfo=IST)

    def __call__(self) -> datetime:
        return self.now


def money(value: Decimal) -> str:
    return format(value.quantize(Decimal("0.01")), "f")


class Scenario:
    """Writes one run of evidence through the real store, as the runner and the admission writer would.

    Every session opens with a ``vm_build`` record (the build and the nine-key mint hash). Every ``intent`` record carries
    the eight quote-pin fields in the same append. ``replay`` writes the ``session_close`` the replay requires.
    """

    def __init__(self, tmp_path: Path, vm: Path, mint_sha: str, file_sha: str = FILE_SHA) -> None:
        self.root = tmp_path / "evidence"
        self.vm = vm
        self.mint_sha = mint_sha  # the nine-key P-06 hash the Mac minted with
        self.file_sha = file_sha  # the IndiaLimits file hash: the manifest's and the signals'
        self.manifest = {**HASHES, "limits_sha256": file_sha}
        self.limits = ReplayLimits(fields=dict(LIMITS), file_sha256=file_sha)
        self.identity = identity_of(vm)
        self.clock = Clock()
        self.stores: dict[str, EvidenceStore] = {}
        self.closed: set[str] = set()
        self._booking = 0
        self._intent = 0
        self.use(S1)

    def use(self, session: str) -> EvidenceStore:
        year, month, day = DAYS[session]
        self.clock.now = datetime(year, month, day, 9, 35, 0, tzinfo=IST)
        if session not in self.stores:
            if not self.stores:
                store = EvidenceStore.create(
                    self.root, run_id=RUN, run_class="qualifying", session_id=session, hashes=self.manifest, clock=self.clock
                )
            else:
                store = EvidenceStore.open(self.root, run_id=RUN, session_id=session, clock=self.clock)
            self.stores[session] = store
            store.append("vm_build", self.build_body())
        return self.stores[session]

    def build_body(self) -> dict[str, Any]:
        return {
            "commit": self.identity.commit, "orders_sha256": dict(sorted(self.identity.orders_sha256.items())),
            "digest": self.identity.digest, "mint_limits_sha256": self.mint_sha,
        }

    def limits_with(self, **changes: Any) -> ReplayLimits:
        return ReplayLimits(fields={**LIMITS, **changes}, file_sha256=self.file_sha)

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
        pin: dict[str, Any] | None = None,
        leg: str = LEG_ENTRY,
        signal_id: str | None = None,
    ) -> str:
        """One decision as ``AdmissionService.prepare`` records it: a ``signal`` record (the instrument and the limits
        hash), then an ``intent`` with the fields the writer emits, the eight quote-pin fields among them.

        ``signal=False`` writes no signal record, a dict overrides signal fields, ``extra`` adds intent fields and
        ``pin`` overrides pin fields.
        """

        store = self.use(session)
        self.closed.discard(session)
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
                    "limits_sha256": self.file_sha,
                    **(signal if isinstance(signal, dict) else {}),
                },
            )
        value = Decimal(price)
        quote_ltp = Decimal(ltp) if ltp is not None else value
        pinned = {
            "quote_ltp": money(quote_ltp), "quote_lower_circuit": money(quote_ltp * Decimal("0.9")),
            "quote_upper_circuit": money(quote_ltp * Decimal("1.1")), "quote_previous_close": money(quote_ltp),
            "quote_series": series, "quote_session_date": f"{year:04d}-{month:02d}-{day:02d}",
            "tick_reference": price if tick_reference is True else tick_reference, "quote_raw_sha256": RAW_SHA,
            **(pin or {}),
        }
        store.append(
            "intent",
            {
                "intent_id": iid, "session_id": session, "signal_id": signal_id, "leg": leg, "intent_sha256": "e" * 64,
                "symbol": symbol or SYMBOL[isin], "side": side, "quantity": str(quantity), "limit_price": price,
                **pinned,
                **(extra or {}),
            },
        )
        if mac == "admit":
            store.append("reservation", {"intent_id": iid, "reservation_created_at": "2026-10-27T09:35:00+05:30"})
        elif isinstance(mac, tuple) and mac[0] == "deny":
            store.append("denied", {"intent_id": iid, "code": mac[1]})
        elif isinstance(mac, tuple) and mac[0] == "reservation_refused":
            store.append("reservation_refused", {"intent_id": iid, "code": mac[1]})
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
        self.closed.discard(session)
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
        self.closed.discard(session)
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
                "limits_sha256": limits_sha256 or self.file_sha,
            },
        )

    def close(self, *sessions: str) -> None:
        """The runner's ``session_close`` for each session, once: a replay refuses a session that has none."""

        for session in sessions:
            if session not in self.closed:
                self.use(session).append("session_close", {"outcome": "complete"})
                self.closed.add(session)

    def records(self, *close: str) -> list[dict[str, Any]]:
        self.close(*close)
        return read_records(self.root, RUN)

    def replay(
        self,
        session: str,
        sink: Any = None,
        vm: Path | None = None,
        *,
        limits: ReplayLimits | None = None,
        manifest: Mapping[str, str] | None = None,
        records: list[dict[str, Any]] | None = None,
    ) -> Any:
        if records is None:
            records = self.records(session)
        return replay_session(
            records, run_id=RUN, session_id=session, limits=limits or self.limits,
            manifest_hashes=manifest or self.manifest, vm_source=vm or self.vm, sink=sink,
        )

    def run(self, sink: Any = None, *, limits: ReplayLimits | None = None, records: list[dict[str, Any]] | None = None) -> Any:
        if records is None:
            records = self.records(*[s for s in self.stores if s in replay_sessions(read_records(self.root, RUN))])
        return replay_run(
            records, run_id=RUN, limits=limits or self.limits, manifest_hashes=self.manifest, vm_source=self.vm, sink=sink,
        )


def altered(records: list[dict[str, Any]], match: Callable[[dict[str, Any]], bool], **changes: Any) -> list[dict[str, Any]]:
    """A copy of the records in which every record ``match`` selects has ``changes`` applied (``DELETE`` removes a key)."""

    out = [dict(r) for r in records]
    for record in out:
        if match(record):
            for key, value in changes.items():
                if value is DELETE:
                    record.pop(key, None)
                else:
                    record[key] = value
    return out


def intent_of(iid: str) -> Callable[[dict[str, Any]], bool]:
    return lambda r: r.get("kind") == "intent" and r.get("intent_id") == iid


FIRST_FINDING = 1000  # the seq a recording sink hands back, so a link in a test is not a real evidence seq


class Sink:
    def __init__(self) -> None:
        self.sessions: list[Any] = []
        self.defects: list[tuple[str, str, str]] = []
        self.pauses: list[tuple[str, str]] = []
        self.links: list[int] = []  # the finding every defect and pause named, in write order

    def record_session(self, result: Any) -> int:
        self.sessions.append(result)
        return FIRST_FINDING + len(self.sessions) - 1

    def open_defect(self, code: str, session_id: str, detail: str, finding_seq: int) -> None:
        self.defects.append((code, session_id, detail))
        self.links.append(finding_seq)

    def pause_run(self, event: str, reason: str, finding_seq: int) -> None:
        self.pauses.append((event, reason))
        self.links.append(finding_seq)

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
    assert result.limits_sha256 == FILE_SHA and result.mint_limits_sha256 == limits_sha
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


def test_a_stored_fail_that_owes_a_session_defect_is_finished_with_that_defect_linked_to_it(tmp_path: Path, limits_sha: str):
    """Break-proof: finish only the divergence half of a stored FAIL and the kill-switch defect is never opened."""
    forced = make_vm(
        tmp_path, "forced",
        edits={"limits.py": lambda text: text.replace("    return tuple(codes)\n", '    codes.insert(0, "kill_switch")\n    return tuple(codes)\n', 1)},
    )
    sc = Scenario(tmp_path, forced, limits_sha)
    sc.intent(S1, "buy", 10, "100.00")
    with pytest.raises(Interrupted):
        sc.replay(S1, Cut(real_sink(sc, S1), at="open_defect"))
    assert stored(sc, "replay_defect") == []
    again = sc.replay(S1, real_sink(sc, S1))
    (finding,) = stored(sc, "vm_replay")
    assert again.already_recorded is True and finding["defects"] == [vm_replay.KILL_SWITCH_IN_VERDICT]
    assert [(r["code"], r["finding_seq"]) for r in stored(sc, "replay_defect")] == [(vm_replay.KILL_SWITCH_IN_VERDICT, finding["seq"])]
    assert open_scope(sc) == []  # a session defect pauses nothing


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
    run = run_verdict(RUN, vm_replay.evidence_sessions(sc.records(S2)), {S1: first})
    assert run.verdict == "FAIL" and run.missing == (S2,) and vm_replay.DAY_MISSING in run.reasons
    assert "replay_day_missing" in render_report(run)


def test_all_walks_every_session_and_a_passing_run_needs_every_one_to_pass_at_one_build(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00")
    sc.raw(S2, "non_evaluation_day", {"as_of_date": "2026-10-28", "reason": "cadence"})
    sc.intent(S3, "buy", 1, "100.00", isin=ISIN_B)
    sink = Sink()
    run = sc.run(sink)
    assert run.verdict == "PASS" and [s.session_id for s in run.sessions] == [S1, S2, S3]
    assert len(sink.sessions) == 3 and len(run.build_digests) == 1


def test_all_lists_a_refused_session_and_fails_the_run_without_writing_a_verdict_for_it(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00")
    sc.intent(S2, "buy", 1, "100.00", isin=ISIN_B)
    records = sc.records(S1, S2)
    records = altered(records, lambda r: r["kind"] == "vm_build" and r["session_id"] == S2, mint_limits_sha256="8" * 64)
    sink = Sink()
    run = sc.run(sink, records=records)
    assert run.verdict == "FAIL" and run.refused == {S2: vm_replay.LIMITS_MISMATCH}
    assert [s.session_id for s in sink.sessions] == [S1]


def test_all_does_not_skip_a_session_whose_pin_is_missing(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00")
    second = sc.intent(S2, "buy", 1, "100.00", isin=ISIN_B)
    records = altered(sc.records(S1, S2), intent_of(second), quote_ltp=DELETE)
    run = sc.run(records=records)
    assert run.verdict == "FAIL" and run.refused == {S2: vm_replay.PINS_MISSING}


def test_the_run_verdict_needs_one_build_hash_and_a_run_with_nothing_replayed_has_not_passed(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00")
    sc.intent(S2, "buy", 1, "100.00", isin=ISIN_B)
    one = sc.replay(S1)
    two = sc.replay(S2)
    assert run_verdict(RUN, [S1, S2], {S1: one, S2: two}).verdict == "PASS"
    other = dataclasses.replace(two, build_digest="9" * 64)
    mixed = run_verdict(RUN, [S1, S2], {S1: one, S2: other})
    assert mixed.verdict == "FAIL" and mixed.reasons == (vm_replay.BUILD_HASH_MIXED,)
    assert run_verdict(RUN, [], {}).verdict == "FAIL"


def test_a_failing_session_fails_the_run_and_the_report_lists_the_divergence_with_the_d06_label(sc: Scenario):
    sc.intent(S1, "buy", 100, "600.00", tick_reference="600")
    result = sc.replay(S1)
    report = render_report(run_verdict(RUN, [S1], {S1: result}))
    assert vm_replay.D06_LABEL in report and "D-23 verdict: FAIL" in report
    assert "mac_admitted_vm_refused" in report and result.build_digest in report
    assert result.limits_sha256 in report and result.mint_limits_sha256 in report
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
    without = [r for r in sc.records(S1) if r["kind"] != "vm_build"]
    with pytest.raises(ReplayRefused) as raised:
        sc.replay(S1, records=without)
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
        sc.replay(S1, sink, limits=sc.limits_with(capital_cap="50001"))  # one value changed, the recorded mint hash is the old one
    assert raised.value.code == vm_replay.LIMITS_MISMATCH and sink.empty()


def test_replay_limits_mismatch_refuses_a_limits_document_the_build_rejects(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00")
    broken = ReplayLimits(fields={k: v for k, v in LIMITS.items() if k != "fat_finger_collar"}, file_sha256=FILE_SHA)
    with pytest.raises(ReplayRefused) as raised:
        sc.replay(S1, limits=broken)
    assert raised.value.code == vm_replay.LIMITS_MISMATCH


def test_replay_limits_mismatch_refuses_when_the_sessions_signals_recorded_other_limits(sc: Scenario):
    sc.signal(S1, limits_sha256="7" * 64)
    sc.intent(S1, "buy", 1, "100.00")
    with pytest.raises(ReplayRefused) as raised:
        sc.replay(S1)
    assert raised.value.code == vm_replay.LIMITS_MISMATCH


def test_signals_that_record_the_same_limits_hash_pass(sc: Scenario):
    sc.signal(S1, limits_sha256=FILE_SHA)
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


def test_an_intents_signal_of_another_session_with_the_same_limits_hash_passes(sc: Scenario):
    sc.raw(S1, "signal", {**signal_payload("sig-cross"), "limits_sha256": FILE_SHA})
    sc.intent(S2, "buy", 1, "100.00", signal=False, extra={"signal_id": "sig-cross"})
    assert sc.replay(S2).verdict == "PASS"


# --- the instrument comes from the signal ----------------------------------------------------------------------------

ENVELOPE = {"seq", "prev_sha256", "entry_sha256", "kind", "run_id", "session_id", "run_class", "utc", "ist"}
ADMISSION_PY = REPO / "backend" / "shadow_india" / "admission.py"
WRITER_INTENT_FIELDS = set(SCHEMAS["intent"][0])  # the required fields of an intent record: what the writer must emit


def admission_intent_write() -> tuple[set[str], set[str]]:
    """The keys of the dict ``AdmissionService.prepare`` appends as the intent record, and the functions it splats in.

    Read from admission.py itself, so the fixture cannot drift from the writer.
    """

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
            keys = {key.value for key in node.args[1].keys if isinstance(key, ast.Constant)}
            splats = {
                value.func.id
                for key, value in zip(node.args[1].keys, node.args[1].values)
                if key is None and isinstance(value, ast.Call) and isinstance(value.func, ast.Name)
            }
            return keys, splats
    raise AssertionError("admission.py no longer appends an intent record the way this test reads it")


def test_the_fixture_intent_is_what_admission_py_writes():
    """The writer emits the schema's required fields plus the eight pin fields, and no ``isin`` or ``series``."""

    written, splats = admission_intent_write()
    assert written - {"session_id"} == WRITER_INTENT_FIELDS
    assert not written & (set(SCHEMAS["intent"][1]) - set(QUOTE_PIN_FIELDS))
    assert splats == {"quote_pin_fields"}  # the pin fields ride in the same append as the intent
    assert set(vm_replay.PIN_FIELDS) == set(QUOTE_PIN_FIELDS)


def signal_payload(signal_id: str, isin: str = ISIN_A, series: str = "EQ") -> dict[str, Any]:
    return {
        "signal_id": signal_id, "symbol": SYMBOL[isin], "isin": isin, "series": series, "as_of_date": "2026-10-26",
        **{name: "f" * 64 for name in SIGNAL_HASHES},
    }


def refusal_of(sc: Scenario, session: str = S1, records: list[dict[str, Any]] | None = None) -> str:
    with pytest.raises(ReplayRefused) as raised:
        replay_session(
            records if records is not None else sc.records(session), run_id=RUN, session_id=session,
            limits=sc.limits, manifest_hashes=MANIFEST_HASHES, vm_source=sc.vm,
        )
    return raised.value.code


def test_a_writer_shaped_intent_with_no_isin_or_series_replays_and_passes(sc: Scenario):
    first = fund(sc, quantity=100, price="100.00", charges="20")  # booked: the fold needs its ISIN from the signal
    second = sc.intent(S1, "buy", 500, "100.00", isin=ISIN_A, mac=("deny", "capital_cap"))
    intents = [r for r in sc.records() if r["kind"] == "intent"]
    assert {r["intent_id"] for r in intents} == {first, second}
    for record in intents:  # exactly what AdmissionService.prepare emits: no isin, no series, the eight pins
        assert set(record) - ENVELOPE == WRITER_INTENT_FIELDS | set(QUOTE_PIN_FIELDS)
    result = sc.replay(S1)
    assert result.verdict == "PASS", result
    assert result.verdicts[1].vm_codes[0] == "capital_cap"  # the first fill counted against the cap, by ISIN


def test_the_isin_and_series_the_vm_sees_are_the_signals(sc: Scenario):
    """A signal whose ISIN is not an NSE one: the VM's own instrument check answers, so the signal is what reached it."""

    sc.intent(S1, "buy", 1, "100.00", signal={"isin": "US0378331005"}, mac=("deny", "instrument_unsupported"))
    assert sc.replay(S1).verdict == "PASS"


def test_a_signal_series_the_vm_does_not_trade_is_the_vms_to_refuse(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00", series="BZ", mac=("deny", "instrument_unsupported"))
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
    assert refusal_of(sc, records=altered(sc.records(S1), intent_of(iid), quote_series="BE")) == vm_replay.SIGNAL_MISMATCH


def test_one_signal_id_that_names_two_instruments_is_invalid_evidence(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00")
    sc.raw(S1, "signal", {**signal_payload("sig-1", ISIN_B), "limits_sha256": FILE_SHA})
    assert refusal_of(sc) == vm_replay.EVIDENCE_INVALID


# --- exit and swing sells have no signal ---------------------------------------------------------------------------------

SELLS = [("halve", 50), ("stop", 100), ("flatten", 100), ("swing", 100)]


@pytest.mark.parametrize(("kind", "quantity"), SELLS, ids=[k for k, _ in SELLS])
def test_a_runner_shaped_sell_with_no_signal_record_replays_to_pass(sc: Scenario, kind: str, quantity: int):
    fund(sc)  # a booked buy of 100 in the first session
    iid = sc.sell(S2, kind, quantity, "60.00")
    sell = next(r for r in sc.records() if r["kind"] == "intent" and r["intent_id"] == iid)
    assert set(sell) - ENVELOPE == WRITER_INTENT_FIELDS | set(QUOTE_PIN_FIELDS)
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
    assert refusal_of(sc, S2, altered(sc.records(S2), intent_of(iid), quote_series=DELETE)) == vm_replay.PINS_MISSING


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
    assert runner._EXIT_ID.fullmatch(exit_id) and vm_replay._EXIT_ID.fullmatch(exit_id)
    assert vm_replay._EXIT_ID.pattern == runner._EXIT_ID.pattern  # copied, so pinned equal
    assert vm_replay._EXIT_ID.flags == runner._EXIT_ID.flags
    swing = swing_signal_id(date(2026, 10, 28), ISIN_A)
    assert vm_replay._SWING_ID.fullmatch(swing)
    assert 'signal_id=f"swing-{as_of:%Y%m%d}-{isin}"' in (REPO / "backend" / "shadow_india" / "runner.py").read_text()
    assert vm_replay._ISIN_PATTERN in runner._EXIT_ID.pattern  # one ISIN shape across the exit and swing ids
    assert (vm_replay.LEG_ENTRY, vm_replay.LEG_EXIT, vm_replay.LEG_SWING) == (LEG_ENTRY, LEG_EXIT, LEG_SWING)



def test_importing_the_replay_pulls_in_no_relay_execution_or_network_module():
    """``vm_replay`` must stay pure: a fresh interpreter that imports it has no broker, execution, runner or httpx."""

    import subprocess

    probe = (
        "import sys, shadow_india.vm_replay\n"
        "bad = sorted(m for m in sys.modules if m.split('.')[0] in ('brokers', 'execution', 'httpx', 'ssl')\n"
        "             or m in ('shadow_india.runner', 'shadow_india.admission', 'shadow_india.__main__'))\n"
        "print('|'.join(bad))\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(REPO / 'backend')!r})\n" + probe],
        capture_output=True, text=True, check=True, timeout=60,
    )
    assert result.stdout.strip() == ""

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
    code = refusal_of(sc, records=drop(sc.records(S1), kind, field))
    assert code == vm_replay.EVIDENCE_INVALID


@pytest.mark.parametrize("bad", ["7", None, 1.5, True, [1]])
def test_a_seq_that_is_not_an_integer_is_a_typed_refusal(sc: Scenario, bad: Any):
    sc.intent(S1, "buy", 1, "100.00")
    records = [dict(r) for r in sc.records(S1)]
    records[-1]["seq"] = bad
    assert refusal_of(sc, records=records) == vm_replay.EVIDENCE_INVALID


def test_the_replay_run_lists_a_session_with_a_malformed_record_as_refused_and_fails(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00", mac=("deny", "capital_cap"))
    records = drop(sc.records(S1), "denied", "code")
    run = sc.run(records=records)
    assert run.verdict == "FAIL" and run.refused == {S1: vm_replay.EVIDENCE_INVALID}


# --- pins -------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("field", list(vm_replay.PIN_FIELDS))
def test_each_of_the_eight_pin_fields_is_required_on_an_intent_record(sc: Scenario, field: str):
    iid = sc.intent(S1, "buy", 1, "100.00")
    records = altered(sc.records(S1), intent_of(iid), **{field: DELETE})
    with pytest.raises(ReplayRefused) as raised:
        pins_from_evidence(records, S1)
    assert raised.value.code == vm_replay.PINS_MISSING
    sink = Sink()
    with pytest.raises(ReplayRefused) as raised:
        sc.replay(S1, sink, records=records)
    assert raised.value.code == vm_replay.PINS_MISSING and sink.empty()


def test_a_session_with_every_pin_reads_its_quote_and_tick_from_the_intent_record_alone(sc: Scenario):
    iid = sc.intent(S1, "buy", 1, "100.00", ltp="99.50")
    pin = pins_from_evidence(sc.records(S1), S1)[iid]
    assert pin.quote.ltp == Decimal("99.50") and pin.quote.series == "EQ" and pin.tick_reference == Decimal("100.00")
    assert pin.quote.session_date == date(2026, 10, 27)
    assert "pins" not in inspect.signature(replay_session).parameters
    assert "pins_by_session" not in inspect.signature(replay_run).parameters


def test_a_recorded_unavailable_tick_reference_is_the_vms_to_judge(sc: Scenario):
    iid = sc.intent(S1, "buy", 1, "100.00", tick_reference=None, mac=("deny", "tick_reference_unavailable"))
    assert pins_from_evidence(sc.records(S1), S1)[iid].tick_reference is None  # a present null, not a missing pin
    assert sc.replay(S1).verdict == "PASS"


def test_a_stale_quote_session_date_is_the_vms_quote_unavailable(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00", mac=("deny", "quote_unavailable"), pin={"quote_session_date": "2026-10-26"})
    assert sc.replay(S1).verdict == "PASS"


def test_a_decision_outside_the_session_window_is_the_vms_session_closed(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00", mac=("deny", "session_closed"), minute=5)  # 09:05 IST, before the open
    assert sc.replay(S1).verdict == "PASS"


def test_the_decision_time_is_the_intents_recorded_ist_not_a_clock_read_now(sc: Scenario):
    late = sc.intent(S1, "buy", 1, "100.00", minute=59)
    assert late and sc.replay(S1).verdict == "PASS"  # 09:59 IST: open


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("quote_ltp", 1.5), ("quote_ltp", "NaN"), ("quote_ltp", True), ("quote_lower_circuit", "x"),
        ("quote_upper_circuit", None), ("quote_previous_close", [1]), ("quote_series", 5),
        ("quote_session_date", "tuesday"), ("quote_session_date", None), ("tick_reference", 1.5), ("tick_reference", "Infinity"),
    ],
)
def test_a_pin_that_is_present_but_malformed_is_a_typed_refusal(sc: Scenario, field: str, bad: Any):
    iid = sc.intent(S1, "buy", 1, "100.00")
    records = altered(sc.records(S1), intent_of(iid), **{field: bad})
    with pytest.raises(ReplayRefused) as raised:
        pins_from_evidence(records, S1)
    assert raised.value.code == vm_replay.PINS_INVALID


def test_a_session_that_recorded_a_halt_latch_replays_it_so_an_admitted_buy_diverges(sc: Scenario):
    sc.latch(S1, "halt", "entries", "raised_2026-10-26")
    sc.intent(S1, "buy", 1, "100.00")  # the Mac admitted it under a halt: the VM refuses
    result = sc.replay(S1)
    assert result.verdict == "FAIL" and result.divergences[0].reason == "mac_admitted_vm_refused"


# --- the build and both limits hashes come from the evidence -------------------------------------------------------------


def test_the_build_is_read_from_the_sessions_vm_build_record_and_from_nothing_else(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00")
    build, mint = build_from_evidence(sc.records(S1), S1)
    assert build == sc.identity and mint == sc.mint_sha
    parameters = set(inspect.signature(replay_session).parameters)
    assert not parameters & {"build", "recorded", "identity", "pins", "commit", "orders_sha256"}
    assert parameters == {"records", "run_id", "session_id", "limits", "manifest_hashes", "vm_source", "sink"}


def test_two_unequal_vm_build_records_in_one_session_refuse(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00")
    other = {**sc.build_body(), "mint_limits_sha256": "7" * 64}
    sc.raw(S1, "vm_build", other)
    assert refusal_of(sc, S1) == vm_replay.BUILD_MISMATCH
    sink = Sink()
    with pytest.raises(ReplayRefused):
        sc.replay(S1, sink)
    assert sink.empty()


def test_two_equal_vm_build_records_in_one_session_are_one_build(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00")
    sc.raw(S1, "vm_build", sc.build_body())
    assert sc.replay(S1).verdict == "PASS"


@pytest.mark.parametrize(
    "change",
    [
        {"digest": "0" * 64},
        {"commit": "e" * 40},  # a commit the digest was not computed from
        {"mint_limits_sha256": "short"},
        {"mint_limits_sha256": DELETE},
        {"orders_sha256": {}},
        {"orders_sha256": {"limits.py": "nothex"}},
    ],
    ids=["digest_not_recomputed", "commit_not_in_digest", "short_mint", "no_mint", "empty_files", "bad_file_hash"],
)
def test_a_vm_build_record_that_does_not_recompute_or_is_malformed_refuses(sc: Scenario, change: dict[str, Any]):
    sc.intent(S1, "buy", 1, "100.00")
    records = altered(sc.records(S1), lambda r: r["kind"] == "vm_build" and r["session_id"] == S1, **change)
    with pytest.raises(ReplayRefused) as raised:
        sc.replay(S1, records=records)
    assert raised.value.code == vm_replay.BUILD_MISMATCH


def test_replay_limits_mismatch_a_collar_changed_after_the_session_refuses_by_the_mint_hash(sc: Scenario):
    """The collar is in ``execution.json`` only, so no signal or manifest hash covers it: the mint hash does."""

    sc.intent(S1, "buy", 1, "100.00")
    sink = Sink()
    with pytest.raises(ReplayRefused) as raised:
        sc.replay(S1, sink, limits=sc.limits_with(fat_finger_collar="0.07"))
    assert raised.value.code == vm_replay.LIMITS_MISMATCH and sink.empty()


def test_replay_limits_mismatch_a_limits_file_that_is_not_the_manifests_refuses(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00")
    other = ReplayLimits(fields=dict(LIMITS), file_sha256="4" * 64)
    with pytest.raises(ReplayRefused) as raised:
        sc.replay(S1, limits=other)
    assert raised.value.code == vm_replay.LIMITS_MISMATCH


def test_replay_limits_mismatch_a_manifest_hash_that_is_not_the_files_refuses(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00")
    with pytest.raises(ReplayRefused) as raised:
        sc.replay(S1, manifest={**MANIFEST_HASHES, "limits_sha256": "4" * 64})
    assert raised.value.code == vm_replay.LIMITS_MISMATCH
    with pytest.raises(ReplayRefused) as raised:
        sc.replay(S1, manifest=HASHES)  # a manifest with no limits hash at all
    assert raised.value.code == vm_replay.LIMITS_MISMATCH


def test_the_file_hash_and_the_mint_hash_are_two_documents_and_never_compared_with_each_other(sc: Scenario):
    assert sc.file_sha != sc.mint_sha
    sc.intent(S1, "buy", 1, "100.00")
    assert sc.replay(S1).verdict == "PASS"  # equal to neither's, so a check of one against the other would refuse


def test_a_session_of_only_exit_sells_still_checks_both_hashes(sc: Scenario):
    fund(sc)
    sc.sell(S2, "halve", 10, "60.00")
    sink = Sink()
    with pytest.raises(ReplayRefused) as raised:
        sc.replay(S2, sink, limits=sc.limits_with(fat_finger_collar="0.07"))
    assert raised.value.code == vm_replay.LIMITS_MISMATCH and sink.empty()


# --- which days are replayed -----------------------------------------------------------------------------------------------


def run_start(sc: Scenario, session: str) -> None:
    sc.raw(session, "run_start", {"run_start_session": "2026-10-26", "audit_base_seq": 0, "audit_base_sha256": "0" * 64, "window_start": "2026-10-26"})


def test_the_open_run_day_is_listed_not_replayed_and_does_not_fail_the_run(sc: Scenario):
    """S0 holds ``run_start`` and ``vm_build`` and never a ``session_close``: counting it would refuse ``replay_session_open``."""

    run_start(sc, S0)
    sc.intent(S1, "buy", 1, "100.00")
    sc.raw(S2, "pause", {"event": "vm_build_changed", "scope": "all", "reason": "x_differs_from_run_pin"})
    sc.raw(S2, "expiry", {"intent_id": "intent-0001", "reason": "end_of_session"})
    records = sc.records(S1)
    assert replay_sessions(records) == (S1,)
    assert not_replayed_sessions(records) == (S0, S2)
    run = sc.run(records=records)
    assert run.verdict == "PASS" and [s.session_id for s in run.sessions] == [S1]
    assert run.not_replayed == (S0, S2) and run.refused == {}
    assert "not_replayed:no_decide" in render_report(run)


def test_an_operator_only_day_is_listed_and_never_dropped(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00")
    policy.raise_event(sc.use(S3), vm_replay.DIVERGENCE, "elsewhere:intent-9:mac_admitted_vm_refused")
    policy.resume(sc.use(S3), "operator reviewed")  # the real resume path, on a day that decided nothing
    records = sc.records(S1)
    assert not_replayed_sessions(records) == (S3,)  # no record-kind allowlist: whatever else a session holds is listed
    assert sc.run(records=records).not_replayed == (S3,)


def test_a_session_with_only_a_vm_build_record_is_not_a_replay_day(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00")
    sc.use(S2)  # the open-run write of the session, nothing else
    records = sc.records(S1)
    assert S2 not in replay_sessions(records) and S2 in not_replayed_sessions(records)


@pytest.mark.parametrize("kind", ["signal", "non_evaluation_day", "intent"])
def test_each_of_signal_non_evaluation_day_and_intent_makes_a_replay_day(sc: Scenario, kind: str):
    if kind == "signal":
        sc.signal(S2)
    elif kind == "non_evaluation_day":
        sc.raw(S2, "non_evaluation_day", {"as_of_date": "2026-10-28", "reason": "cadence"})
    else:
        fund(sc)
        sc.sell(S2, "halve", 10, "60.00")
    assert S2 in replay_sessions(sc.records(S2))


def test_an_exit_only_day_is_replayed_in_the_run(sc: Scenario):
    fund(sc)
    sc.sell(S2, "halve", 10, "60.00")
    run = sc.run()
    assert run.verdict == "PASS" and [s.session_id for s in run.sessions] == [S1, S2]
    assert run.sessions[1].intents_compared == 1


def test_only_a_closed_session_is_replayed(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00")
    sink = Sink()
    with pytest.raises(ReplayRefused) as raised:
        sc.replay(S1, sink, records=sc.records())  # no session_close yet
    assert raised.value.code == vm_replay.SESSION_OPEN and sink.empty()
    assert sc.replay(S1).verdict == "PASS"  # closed: it replays


def test_a_run_whose_session_is_open_lists_it_refused_and_fails(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00")
    run = sc.run(records=sc.records())
    assert run.verdict == "FAIL" and run.refused == {S1: vm_replay.SESSION_OPEN}


# --- what is recorded, and what a record cannot undo ---------------------------------------------------------------------


def real_sink(sc: Scenario, session: str) -> EvidenceReplaySink:
    return EvidenceReplaySink(sc.use(session))


def stored(sc: Scenario, kind: str) -> list[dict[str, Any]]:
    return [r for r in sc.records() if r["kind"] == kind]


def test_a_divergence_pauses_the_run_and_the_record_carries_hashes_and_counts_only(sc: Scenario):
    sc.intent(S1, "buy", 100, "600.00", tick_reference="600")  # the Mac admitted what the VM refuses
    result = sc.replay(S1, real_sink(sc, S1))
    assert result.verdict == "FAIL"
    (record,) = stored(sc, "vm_replay")
    assert record["verdict"] == "FAIL" and record["limits_sha256"] == FILE_SHA and record["mint_limits_sha256"] == sc.mint_sha
    assert record["build_digest"] == sc.identity.digest and record["intents_compared"] == 1
    assert record["divergence_reasons"] == ["mac_admitted_vm_refused"]
    assert [(r["code"], r["detail"]) for r in stored(sc, "replay_defect")] == [(vm_replay.DIVERGENCE, "mac_admitted_vm_refused")]
    state = policy.state_from_records(sc.records())
    assert [(p.event, p.scope) for p in state.open_pauses] == [(vm_replay.DIVERGENCE, "all")]
    assert state.blocks(policy.STEP_ADMIT, isin=ISIN_A) and state.blocks(policy.STEP_BOOK)


def test_a_fail_record_is_sticky_a_later_pass_record_never_replaces_it(sc: Scenario):
    sc.intent(S1, "buy", 100, "600.00", tick_reference="600")
    sc.replay(S1, real_sink(sc, S1))
    first = recorded_run_verdict(sc.records())
    assert first.verdict == "FAIL" and vm_replay.DIVERGENCE in first.reasons
    failed = stored(sc, "vm_replay")[0]
    sc.raw(S1, "vm_replay", {k: v for k, v in failed.items() if k in SCHEMAS["vm_replay"][0]} | {"verdict": "PASS"})
    assert recorded_run_verdict(sc.records()).verdict == "FAIL"
    # The defect row alone would also fail the run. Without it the replay records must still decide: FAIL then PASS is FAIL.
    without_defect = [r for r in sc.records() if r["kind"] != "replay_defect"]
    assert [r["verdict"] for r in without_defect if r["kind"] == "vm_replay"] == ["FAIL", "PASS"]
    sticky = recorded_run_verdict(without_defect)
    assert sticky.verdict == "FAIL" and vm_replay.DIVERGENCE in sticky.reasons


def test_resuming_the_divergence_pause_does_not_clear_the_d23_verdict(sc: Scenario):
    sc.intent(S1, "buy", 100, "600.00", tick_reference="600")
    sc.replay(S1, real_sink(sc, S1))
    policy.resume(sc.use(S1), "operator looked at it")
    assert not policy.state_from_records(sc.records()).open_pauses  # the pause is gone
    assert recorded_run_verdict(sc.records()).verdict == "FAIL"  # the D-23 verdict reads replay records, not pause state
    # Same with the defect row left out: the stored FAIL replay record alone keeps the run failed after the resume.
    without_defect = [r for r in sc.records() if r["kind"] != "replay_defect"]
    assert any(r["kind"] == "resume" for r in without_defect)
    still = recorded_run_verdict(without_defect)
    assert still.verdict == "FAIL" and vm_replay.DIVERGENCE in still.reasons


def test_a_stored_pass_never_hides_a_refusal_of_this_pass(sc: Scenario):
    """Break-proof: drop the current refusals when a stored result exists and the run reads PASS, exit 0, refused {}."""
    sc.intent(S1, "buy", 1, "100.00")
    first = sc.run(real_sink(sc, S1))
    assert first.verdict == "PASS"
    records = sc.records()
    assert recorded_run_verdict(records, first.refused).verdict == "PASS"
    changed = dataclasses.replace(sc.limits, file_sha256="e" * 64)  # the private config moved since the stored PASS
    again = sc.run(records=records, limits=changed)
    assert again.refused == {S1: vm_replay.LIMITS_MISMATCH}
    stored_verdict = recorded_run_verdict(records, again.refused)
    assert stored_verdict.verdict == "FAIL" and stored_verdict.refused == {S1: vm_replay.LIMITS_MISMATCH}
    assert vm_replay.SESSION_REFUSED in stored_verdict.reasons
    assert len(stored(sc, "vm_replay")) == 1  # the refusal wrote nothing and the stored PASS is untouched


class Interrupted(Exception):
    """The process stopped between two writes."""


class Cut:
    """A sink that delegates until the named write, where the process dies before that write lands."""

    def __init__(self, real: EvidenceReplaySink, *, at: str) -> None:
        self.real, self.at = real, at

    def record_session(self, result: Any) -> int:
        return self.real.record_session(result)

    def open_defect(self, code: str, session_id: str, detail: str, finding_seq: int) -> None:
        if self.at == "open_defect":
            raise Interrupted
        self.real.open_defect(code, session_id, detail, finding_seq)

    def pause_run(self, event: str, reason: str, finding_seq: int) -> None:
        if self.at == "pause_run":
            raise Interrupted
        self.real.pause_run(event, reason, finding_seq)


def divergent_session(sc: Scenario) -> None:
    sc.intent(S1, "buy", 100, "600.00", tick_reference="600")  # the Mac admitted what the VM refuses


def open_scope(sc: Scenario) -> list[tuple[str, str]]:
    return [(p.event, p.scope) for p in policy.state_from_records(sc.records()).open_pauses]


@pytest.mark.parametrize("cut_at", ["open_defect", "pause_run"])
def test_an_interruption_after_the_record_is_finished_by_the_rerun(sc: Scenario, cut_at: str):
    """Break-proof: return early on a stored FAIL and the run stays FAIL with no defect, no pause and admission open."""
    divergent_session(sc)
    with pytest.raises(Interrupted):
        sc.replay(S1, Cut(real_sink(sc, S1), at=cut_at))
    assert len(stored(sc, "vm_replay")) == 1 and open_scope(sc) == []  # the record landed, the pause did not
    again = sc.replay(S1, real_sink(sc, S1))
    assert again.verdict == "FAIL" and again.already_recorded is True
    assert [(r["code"], r["detail"]) for r in stored(sc, "replay_defect")] == [(vm_replay.DIVERGENCE, "mac_admitted_vm_refused")]
    assert open_scope(sc) == [(vm_replay.DIVERGENCE, "all")]
    state = policy.state_from_records(sc.records())
    assert state.blocks(policy.STEP_ADMIT, isin=ISIN_A) and state.blocks(policy.STEP_BOOK)
    before = len(sc.records())
    sc.replay(S1, real_sink(sc, S1))
    assert len(sc.records()) == before and len(stored(sc, "vm_replay")) == 1  # a further re-run writes nothing


def unrelated_pause(sc: Scenario, reason: str = "other-day:intent-9:mac_admitted_vm_refused") -> int:
    """A divergence pause that belongs to another finding (no link to any record of this session), raised the real way."""

    return int(policy.raise_event(sc.use(S1), vm_replay.DIVERGENCE, reason)["seq"])


def clear(sc: Scenario, *pause_seqs: int, why: str = "operator cleared it") -> None:
    policy.resume(sc.use(S1), why, pause_seqs=list(pause_seqs))  # the real resume: it refuses an empty or unknown request


def linked(sc: Scenario, kind: str) -> list[Any]:
    return [r.get("finding_seq") for r in stored(sc, kind) if r.get("finding_seq") is not None]


@pytest.mark.parametrize("cut_at", ["open_defect", "pause_run"])
@pytest.mark.parametrize("cleared_before_the_record", [True, False])
def test_an_unrelated_resume_never_suppresses_the_pause_this_finding_still_owes(sc: Scenario, cut_at: str, cleared_before_the_record: bool):
    """Break-proof: exempt the pause once any later ``resume`` exists and the run stays unpaused, admission open."""
    divergent_session(sc)
    other = unrelated_pause(sc)
    if cleared_before_the_record:
        clear(sc, other)
    with pytest.raises(Interrupted):
        sc.replay(S1, Cut(real_sink(sc, S1), at=cut_at))
    if not cleared_before_the_record:
        clear(sc, other)  # the operator clears a different pause after this finding's record landed
    assert open_scope(sc) == []
    again = sc.replay(S1, real_sink(sc, S1))
    (finding,) = stored(sc, "vm_replay")
    assert again.verdict == "FAIL" and again.already_recorded is True
    assert open_scope(sc) == [(vm_replay.DIVERGENCE, "all")]
    assert linked(sc, "pause") == [finding["seq"]] and linked(sc, "replay_defect") == [finding["seq"]]
    state = policy.state_from_records(sc.records())
    assert state.blocks(policy.STEP_ADMIT, isin=ISIN_A) and state.blocks(policy.STEP_BOOK)


def test_a_pause_the_operator_resumed_is_not_raised_again_by_a_rerun(sc: Scenario):
    """Break-proof: count only open pauses as held and every rerun after a resume re-pauses the run."""
    divergent_session(sc)
    sc.replay(S1, real_sink(sc, S1))
    (pause,) = [r for r in stored(sc, "pause") if r["event"] == vm_replay.DIVERGENCE]
    clear(sc, pause["seq"], why="operator looked at it")
    assert open_scope(sc) == []
    before = len(sc.records())
    again = sc.replay(S1, real_sink(sc, S1))
    assert again.verdict == "FAIL" and again.already_recorded is True
    assert open_scope(sc) == [] and len(sc.records()) == before  # the decision stands, nothing was written
    assert recorded_run_verdict(sc.records()).verdict == "FAIL"


def test_a_rerun_after_an_interruption_and_the_operators_resume_of_its_own_pause_does_not_repause(sc: Scenario):
    """The operator resumed the pause that did land; the defect that did not is still completed, the pause is not."""
    divergent_session(sc)
    sc.replay(S1, real_sink(sc, S1))
    (pause,) = [r for r in stored(sc, "pause") if r["event"] == vm_replay.DIVERGENCE]
    defect = [r for r in sc.records() if r["kind"] == "replay_defect"]
    clear(sc, pause["seq"])
    stripped = [r for r in sc.records() if r["kind"] != "replay_defect"]  # the defect write never landed
    assert len(stripped) == len(sc.records()) - len(defect)
    sink = Sink()
    sc.replay(S1, sink, records=stripped)
    assert sink.defects == [(vm_replay.DIVERGENCE, S1, "mac_admitted_vm_refused")] and sink.pauses == []


def test_every_rerun_after_the_first_completion_writes_nothing(sc: Scenario):
    """Break-proof: key the held check on anything the first write does not carry and each rerun writes again."""
    divergent_session(sc)
    with pytest.raises(Interrupted):
        sc.replay(S1, Cut(real_sink(sc, S1), at="open_defect"))
    sc.replay(S1, real_sink(sc, S1))
    settled = len(sc.records())
    for _ in range(3):
        again = sc.replay(S1, real_sink(sc, S1))
        assert again.already_recorded is True and len(sc.records()) == settled
    assert len(stored(sc, "vm_replay")) == 1 and len(stored(sc, "replay_defect")) == 1
    assert len([r for r in stored(sc, "pause") if r["event"] == vm_replay.DIVERGENCE]) == 1


def planted_fail(sc: Scenario, session: str, base: dict[str, Any], **changes: Any) -> None:
    """A second FAIL record of the session, as an earlier pass that stopped right after the record would have left it."""

    keep = {k: v for k, v in base.items() if k in SCHEMAS["vm_replay"][0] or k in SCHEMAS["vm_replay"][1]}
    sc.raw(session, "vm_replay", {**keep, **changes})


def test_two_findings_with_the_same_defect_each_keep_their_own_defect_and_pause(sc: Scenario):
    """Break-proof: key a defect by (code, detail) or a pause by its reason and the second finding is skipped."""
    divergent_session(sc)
    sc.replay(S1, real_sink(sc, S1))
    (first,) = stored(sc, "vm_replay")
    planted_fail(sc, S1, first, intents_compared=2)  # a differing pass left a second finding with the same reason
    sc.replay(S1, real_sink(sc, S1))
    second = stored(sc, "vm_replay")[1]
    assert sorted(linked(sc, "replay_defect")) == sorted([first["seq"], second["seq"], first["seq"]])  # + the nondeterminism defect
    divergence_defects = [r for r in stored(sc, "replay_defect") if r["code"] == vm_replay.DIVERGENCE]
    assert sorted(r["finding_seq"] for r in divergence_defects) == [first["seq"], second["seq"]]
    divergence_pauses = [r for r in stored(sc, "pause") if r["event"] == vm_replay.DIVERGENCE]
    assert sorted(r["finding_seq"] for r in divergence_pauses) == [first["seq"], second["seq"]]
    settled = len(sc.records())
    sc.replay(S1, real_sink(sc, S1))
    assert len(sc.records()) == settled


def test_a_stored_fail_is_finished_with_every_write_linked_to_its_own_record(sc: Scenario):
    divergent_session(sc)
    sc.replay(S1, real_sink(sc, S1))
    (finding,) = stored(sc, "vm_replay")
    bare = [r for r in sc.records() if r["kind"] not in ("replay_defect",) and not (r["kind"] == "pause" and r["event"] == vm_replay.DIVERGENCE)]
    sink = Sink()
    again = sc.replay(S1, sink, records=bare)
    assert again.already_recorded is True and sink.sessions == []
    assert sink.defects == [(vm_replay.DIVERGENCE, S1, "mac_admitted_vm_refused")] and len(sink.pauses) == 1
    assert sink.links == [finding["seq"], finding["seq"]]


def test_a_link_from_a_record_written_before_its_finding_does_not_hold_what_the_finding_owes(sc: Scenario):
    """Terra #646 r3: a defect or pause can only answer a finding that already exists, so one with an earlier seq is not held."""
    divergent_session(sc)
    sc.replay(S1, real_sink(sc, S1))
    (finding,) = stored(sc, "vm_replay")
    owed = [r for r in sc.records() if r["kind"] == "replay_defect" or (r["kind"] == "pause" and r["event"] == vm_replay.DIVERGENCE)]
    bare = [r for r in sc.records() if r not in owed]
    early = [{**r, "seq": finding["seq"] - 1} for r in owed]  # same link, written before the finding it names
    sink = Sink()
    sc.replay(S1, sink, records=sorted(bare + early, key=lambda r: r["seq"]))
    assert sink.defects == [(vm_replay.DIVERGENCE, S1, "mac_admitted_vm_refused")] and len(sink.pauses) == 1
    assert sink.links == [finding["seq"], finding["seq"]]


def test_a_fresh_fail_is_recorded_first_and_its_defect_and_pause_link_to_it(sc: Scenario):
    divergent_session(sc)
    sink = Sink()
    sc.replay(S1, sink)
    assert len(sink.sessions) == 1 and sink.links == [FIRST_FINDING, FIRST_FINDING]
    sc.replay(S1, real_sink(sc, S1))
    (finding,) = stored(sc, "vm_replay")
    pause = next(r for r in stored(sc, "pause") if r["event"] == vm_replay.DIVERGENCE)
    defect = stored(sc, "replay_defect")[0]
    assert finding["seq"] < defect["seq"] < pause["seq"]  # the finding exists before anything it owes


def test_a_stored_fail_is_finished_before_a_changed_config_can_refuse_the_replay(sc: Scenario):
    """Break-proof: validate first and the refusal returns before the missing defect and pause are ever written."""
    divergent_session(sc)
    with pytest.raises(Interrupted):
        sc.replay(S1, Cut(real_sink(sc, S1), at="open_defect"))
    assert open_scope(sc) == [] and stored(sc, "replay_defect") == []
    changed = dataclasses.replace(sc.limits, file_sha256="e" * 64)  # the private config moved since the record landed
    with pytest.raises(ReplayRefused) as caught:
        sc.replay(S1, real_sink(sc, S1), limits=changed)
    assert caught.value.code == vm_replay.LIMITS_MISMATCH  # the refusal is still reported
    assert open_scope(sc) == [(vm_replay.DIVERGENCE, "all")] and len(stored(sc, "replay_defect")) == 1
    settled = len(sc.records())
    with pytest.raises(ReplayRefused):
        sc.replay(S1, real_sink(sc, S1), limits=changed)
    assert len(sc.records()) == settled


def test_the_run_reads_fail_for_both_the_stored_finding_and_the_refusal(sc: Scenario):
    divergent_session(sc)
    with pytest.raises(Interrupted):
        sc.replay(S1, Cut(real_sink(sc, S1), at="pause_run"))
    changed = dataclasses.replace(sc.limits, file_sha256="e" * 64)
    run = sc.run(real_sink(sc, S1), limits=changed)
    assert run.refused == {S1: vm_replay.LIMITS_MISMATCH} and run.verdict == "FAIL"
    assert open_scope(sc) == [(vm_replay.DIVERGENCE, "all")]
    stored_run = recorded_run_verdict(sc.records(), run.refused)
    assert stored_run.verdict == "FAIL" and {vm_replay.SESSION_REFUSED, vm_replay.DIVERGENCE} <= set(stored_run.reasons)


def test_replay_session_refuses_a_day_that_is_not_a_decide_day_and_records_nothing(sc: Scenario):
    """Break-proof: without the check the open-run day replays as PASS with no intents and a record is written."""
    run_start(sc, S0)
    sc.intent(S1, "buy", 1, "100.00")
    sink = Sink()
    with pytest.raises(ReplayRefused) as caught:
        sc.replay(S0, sink, records=sc.records(S0, S1))
    assert caught.value.code == vm_replay.NOT_DECIDE_DAY and sink.empty()


def planted_pass(sc: Scenario, session: str) -> None:
    sc.raw(session, "vm_replay", {
        "verdict": "PASS", "build_commit": sc.identity.commit, "build_digest": sc.identity.digest, "limits_sha256": FILE_SHA,
        "mint_limits_sha256": sc.mint_sha, "intents_compared": 0, "no_intents": True,
    })


def test_a_stored_record_for_a_non_decide_day_never_makes_an_empty_run_pass(sc: Scenario):
    run_start(sc, S0)
    sc.close(S0)
    planted_pass(sc, S0)
    run = recorded_run_verdict(sc.records())
    assert run.verdict == "FAIL" and vm_replay.STRAY_RECORD in run.reasons and vm_replay.DAY_MISSING in run.reasons
    assert run.sessions == ()


def stored_pass(sc: Scenario) -> None:
    sc.raw(S1, "vm_replay", {
        "verdict": "PASS", "build_commit": sc.identity.commit, "build_digest": sc.identity.digest, "limits_sha256": FILE_SHA,
        "mint_limits_sha256": sc.mint_sha, "intents_compared": 1, "no_intents": False,
    })  # an earlier pass said PASS


def test_a_nondeterministic_rerun_that_now_diverges_is_a_new_finding_with_its_own_defect_and_pause(sc: Scenario):
    """Break-proof: write no finding record for it and its defect and pause link to nothing a later resume cannot cover."""
    divergent_session(sc)
    sc.close(S1)
    stored_pass(sc)  # this pass finds the VM refusing what the Mac admitted
    again = sc.replay(S1, real_sink(sc, S1))
    assert again.verdict == "FAIL" and vm_replay.NONDETERMINISTIC in again.defects
    passed, finding = stored(sc, "vm_replay")
    assert (passed["verdict"], finding["verdict"]) == ("PASS", "FAIL") and finding["seq"] > passed["seq"]
    owed = {(r["code"], r["detail"]): r["finding_seq"] for r in stored(sc, "replay_defect")}
    assert owed == {
        (vm_replay.DIVERGENCE, "mac_admitted_vm_refused"): finding["seq"],
        (vm_replay.NONDETERMINISTIC, vm_replay.NONDETERMINISTIC): passed["seq"],
    }
    assert open_scope(sc) == [(vm_replay.DIVERGENCE, "all")] and linked(sc, "pause") == [finding["seq"]]
    settled = len(sc.records())
    for _ in range(2):
        sc.replay(S1, real_sink(sc, S1))
        assert len(sc.records()) == settled  # a further rerun finds everything written


def test_a_new_divergence_after_a_stored_pass_pauses_despite_an_unrelated_pause_resumed_in_between(sc: Scenario):
    """The second repro: the stored PASS, some other pause raised and resumed, then a rerun that diverges."""
    divergent_session(sc)
    sc.close(S1)
    stored_pass(sc)
    clear(sc, unrelated_pause(sc), why="unrelated")
    again = sc.replay(S1, real_sink(sc, S1))
    assert again.verdict == "FAIL"
    assert open_scope(sc) == [(vm_replay.DIVERGENCE, "all")], "the fresh divergence never paused the run"
    assert policy.state_from_records(sc.records()).blocks(policy.STEP_ADMIT, isin=ISIN_A)


def test_a_nondeterministic_rerun_interrupted_after_its_finding_is_finished_by_the_next(sc: Scenario):
    divergent_session(sc)
    sc.close(S1)
    stored_pass(sc)
    with pytest.raises(Interrupted):
        sc.replay(S1, Cut(real_sink(sc, S1), at="open_defect"))
    assert len(stored(sc, "vm_replay")) == 2 and open_scope(sc) == []
    sc.replay(S1, real_sink(sc, S1))
    assert open_scope(sc) == [(vm_replay.DIVERGENCE, "all")]
    assert {r["code"] for r in stored(sc, "replay_defect")} == {vm_replay.DIVERGENCE, vm_replay.NONDETERMINISTIC}
    settled = len(sc.records())
    sc.replay(S1, real_sink(sc, S1))
    assert len(sc.records()) == settled


def test_a_replay_defect_record_alone_fails_the_run_even_with_every_session_passing(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00")
    sc.replay(S1, real_sink(sc, S1))
    assert recorded_run_verdict(sc.records()).verdict == "PASS"
    sc.raw(S1, "replay_defect", {"code": vm_replay.KILL_SWITCH_IN_VERDICT, "detail": vm_replay.KILL_SWITCH_IN_VERDICT})
    failed = recorded_run_verdict(sc.records())
    assert failed.verdict == "FAIL" and vm_replay.DEFECT in failed.reasons


def test_an_equal_rerun_appends_nothing_and_reports_already_recorded(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00")
    first = sc.replay(S1, real_sink(sc, S1))
    before = len(sc.records())
    second = sc.replay(S1, real_sink(sc, S1))
    assert first.already_recorded is False and second.already_recorded is True and second.verdict == "PASS"
    assert len(sc.records()) == before and len(stored(sc, "vm_replay")) == 1


def test_a_rerun_that_differs_from_the_stored_record_is_replay_nondeterministic(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00")
    sc.replay(S1, real_sink(sc, S1))
    sc.raw(S1, "vm_replay", {**{k: v for k, v in stored(sc, "vm_replay")[0].items() if k in SCHEMAS["vm_replay"][0]}, "intents_compared": 9})
    sink = Sink()
    again = sc.replay(S1, sink)
    assert again.verdict == "FAIL" and vm_replay.NONDETERMINISTIC in again.defects
    assert sink.defects == [(vm_replay.NONDETERMINISTIC, S1, vm_replay.NONDETERMINISTIC)] and sink.sessions == []


def test_a_decide_day_with_no_replay_record_fails_the_stored_verdict(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00")
    sc.intent(S2, "buy", 1, "100.00", isin=ISIN_B)
    sc.replay(S1, real_sink(sc, S1))
    run = recorded_run_verdict(sc.records(S2))
    assert run.verdict == "FAIL" and run.missing == (S2,) and vm_replay.DAY_MISSING in run.reasons


def test_a_run_with_no_decide_day_has_not_passed(sc: Scenario):
    run_start(sc, S0)
    empty = recorded_run_verdict(sc.records())
    assert empty.verdict == "FAIL" and vm_replay.DAY_MISSING in empty.reasons


def test_the_stored_verdict_needs_one_build_digest_across_the_run(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00")
    sc.intent(S2, "buy", 1, "100.00", isin=ISIN_B)
    sc.replay(S1, real_sink(sc, S1))
    sc.replay(S2, real_sink(sc, S2))
    assert recorded_run_verdict(sc.records()).verdict == "PASS"
    second = next(r for r in stored(sc, "vm_replay") if r["session_id"] == S2)
    sc.raw(S2, "vm_replay", {**{k: v for k, v in second.items() if k in SCHEMAS["vm_replay"][0]}, "build_digest": "9" * 64})
    mixed = recorded_run_verdict(sc.records())
    assert mixed.verdict == "FAIL" and vm_replay.BUILD_HASH_MIXED in mixed.reasons


def test_the_recorded_verdict_reads_replay_records_only(sc: Scenario):
    sc.intent(S1, "buy", 1, "100.00")
    sc.replay(S1, real_sink(sc, S1))
    records = [r for r in sc.records() if r["kind"] not in ("pause", "resume", "latch")]
    assert recorded_run_verdict(records).verdict == "PASS"
    assert recorded_run_verdict([dict(r) for r in records if r["kind"] != "vm_replay"]).verdict == "FAIL"


# --- isolation --------------------------------------------------------------------------------------------------------


def test_the_replay_makes_no_network_or_relay_call(sc: Scenario, network_sentinel: list[str]):
    fund(sc, quantity=10)
    sc.intent(S1, "buy", 1, "100.00", isin=ISIN_B)
    run = sc.run(Sink())
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
