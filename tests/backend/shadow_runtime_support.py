"""A real production-runtime world for the 64-09 Task B tests: real files, an in-process VM, scripted relay reads.

Real: the research store, the frozen dataset, the seeded session store, the private config files, the pinned calendar
file, the evidence store, the simulation ledger, the latch, the admission guard and the VM order app (kill flag answered
``blocked`` by a loopback metadata fake). Scripted: the four relay READ routes (health, quote, security master, daily
bars) and the signal step. Every value is synthetic. Nothing here reaches a broker, ``private/``, the real VM, GCP or
NSE, and nothing calls authorize or halt.
"""

from __future__ import annotations

import hashlib
import io
import json
import shutil
import stat
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Optional

import test_pilot_data_crosscheck as cc
from brokers.breeze_gateway.client import UpstreamPayload
from brokers.breeze_gateway.orders import RelayLink
from india_limits_support import india_private_dir
from risk_india.state import state_path_for
from shadow_india import runtime as runtime_module
from shadow_india.signals import STATUS_EVALUATED, NameSignal, SignalResult
from strategy_india.params import placeholder_params
from test_gateway_orders_routes import PRIMARY
from test_pilot_data_session_store import build_research, seed
from test_shadow_india_runner import AS_OF as _RUNNER_AS_OF  # noqa: F401  (documents that this file mirrors it)
from test_shadow_india_runner import S, Clock, Script, Vm

UTC = timezone.utc
SESSION = S  # 2026-10-22, inside the VM fixtures' October 2026
AS_OF = cc.D5  # the data session the reference prices are read for (the 2025 seeded store)
WINDOW_START = cc.START  # 2025-03-03
RUN = "run64b"
CANBK_ISIN, CANBK_SYMBOL, CANBK_CODE = cc.NEW, "CANBK", "CANBAN"
STEADY_ISIN, STEADY_SYMBOL, STEADY_CODE = cc.STEADY_ISIN, "STEADYCO", "STEADY"
ISINS = {CANBK_SYMBOL: CANBK_ISIN, STEADY_SYMBOL: STEADY_ISIN}
CODES = {CANBK_ISIN: CANBK_CODE, STEADY_ISIN: STEADY_CODE}
FORBIDDEN = ("CANARY-SHADOW-TOKEN-4417", "CANARY-SHADOW-ACCT-90210")
KEY_ID = PRIMARY["key_id"]


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def write_json(path: Path, body: Any, mode: int = 0o600) -> Path:
    path.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")
    path.chmod(mode)
    return path


# --- the research world, built once per module ---------------------------------------------------------------------------


def build_seeded(base: Path) -> dict[str, Any]:
    """Research store, frozen dataset and a seeded session store (read-only: tests copy the tree)."""

    research = build_research(base / "research_world")
    session = base / "session_world"
    report = seed(research, session)
    return {"research": research, "session": session, "report": report, "dataset": research["dataset"]}


def tree_state(root: Path) -> dict[str, tuple[str, int]]:
    out: dict[str, tuple[str, int]] = {}
    if not root.exists():
        return out
    for path in sorted(root.rglob("*")):
        if path.name.endswith((".lock", "-shm")) or (path.name.endswith("-wal") and path.stat().st_size == 0):
            continue  # SQLite's own empty companions of a read-only open: the database file itself never moves
        if path.is_file():
            out[str(path.relative_to(root))] = (hashlib.sha256(path.read_bytes()).hexdigest(), stat.S_IMODE(path.stat().st_mode))
    return out


# --- the scripted relay reads ---------------------------------------------------------------------------------------------


def _payload(body: bytes, route: str) -> UpstreamPayload:
    return UpstreamPayload(
        body=body, sha256=hashlib.sha256(body).hexdigest(), fetched_at_utc=datetime(2026, 10, 22, 4, 0, tzinfo=UTC),
        upstream_api="fake", upstream_request={}, upstream_interval=None, breeze_status=200, empty=False,
        upstream_last_modified=None, request_id="req", route=route, query={}, contract="growin-relay/1",
    )


def quote_body(
    code: str, price: str = "121.00", *, session: date = SESSION, bid_gap: str = "0.10", step: str = "0"
) -> bytes:
    p = Decimal(price) - Decimal(step)
    row = {
        "stock_code": code, "ltt": f"{session.day:02d}-{session.strftime('%b')}-{session.year} 09:44:00",
        "ltp": float(p), "lower_circuit": float(Decimal(price) * Decimal("0.9")), "upper_circuit": float(Decimal(price) * Decimal("1.1")),
        "previous_close": float(Decimal(price)), "best_bid_price": float(p - Decimal(bid_gap)), "best_offer_price": float(p),
    }
    return json.dumps({"Success": [row]}).encode()


def master_zip(codes: dict[str, str]) -> bytes:
    lines = ["Token,ShortName,Series,ISINCode"] + [
        f"{index + 1},{code},EQ,{isin}" for index, (isin, code) in enumerate(codes.items())
    ]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("NSEScripMaster.txt", "\n".join(lines) + "\n")
    return buffer.getvalue()


def bars_body(close: str = "121") -> bytes:
    return json.dumps({"Success": [{"datetime": "2026-09-30 00:00:00", "close": close}]}).encode()


class ReadScript:
    """What the relay READ routes answer, and every GET path the runtime makes through the proxy."""

    def __init__(self, clock: Any = None) -> None:
        self.clock = clock
        self.paths: list[str] = []
        self.quote_prices = {CANBK_CODE: "121.00", STEADY_CODE: "100.00"}
        self.quote_calls: list[str] = []
        self.bodies: Optional[Callable[[str, int], bytes]] = None  # (code, call number) -> body
        self.master = master_zip({CANBK_ISIN: CANBK_CODE, STEADY_ISIN: STEADY_CODE})
        self.health_state = "active"


class ProxyClient:
    """The VM's real relay client for every order route; scripted answers for the four read routes."""

    def __init__(self, real: Any, script: ReadScript) -> None:
        self._real = real
        self.script = script

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    def health(self) -> Any:
        self.script.paths.append("/v1/health")
        return SimpleNamespace(state=self.script.health_state, egress_ok=True, login_pending=False)

    def quote(self, stock_code: str, *, exchange: str = "NSE") -> UpstreamPayload:
        self.script.paths.append("/v1/quotes")
        self.script.quote_calls.append(stock_code)
        if self.script.clock is not None:
            self.script.clock.now += timedelta(seconds=2)  # three readings, two seconds apart, as the guard needs
        n = len(self.script.quote_calls)
        if self.script.bodies is not None:
            body = self.script.bodies(stock_code, n)
        else:  # three distinct readings: a different ask each call
            body = quote_body(stock_code, self.script.quote_prices[stock_code], step=f"0.0{n % 3 + 1}")
        return _payload(body, "/v1/quotes")

    def security_master(self) -> UpstreamPayload:
        self.script.paths.append("/v1/security-master")
        return _payload(self.script.master, "/v1/security-master")

    def daily_bars(self, stock_code: str, from_date: date, to_date: date, *, exchange: str = "NSE") -> UpstreamPayload:
        self.script.paths.append("/v1/bars/daily")
        return _payload(bars_body(self.script.quote_prices[stock_code].split(".")[0]), "/v1/bars/daily")


# --- the signal step, scripted ---------------------------------------------------------------------------------------------


def evaluated(*symbols: str) -> SignalResult:
    signals = tuple(
        NameSignal(
            anchor_isin=ISINS[s], isin=ISINS[s], symbol=s, series="EQ", raw_score=Decimal(10 - i), zscore=Decimal(1),
            rank=i + 1, suppressed=None, buy_blocked=False,
        )
        for i, s in enumerate(symbols)
    )
    return SignalResult(STATUS_EVALUATED, SESSION, AS_OF, evaluation_due=True, signals=signals, entries_possible=True)


# --- the environment -------------------------------------------------------------------------------------------------------


def calendar_doc() -> dict[str, Any]:
    return {
        "schema": "growin-nse-calendar/1", "exchange": "NSE", "segment": "CM", "basis": "test_fixture",
        "coverage": {"from": "2025-01-01", "to": "2027-12-31"},
        "source": {"publisher": "test fixture", "reference": "none", "published_on": "2025-01-01",
                   "document_sha256": "d" * 64, "saved_by": "test", "saved_at_utc": "2025-01-02T00:00:00+00:00"},
        "holidays": [], "special_sessions": [],
    }


@dataclass
class Env:
    tmp: Path
    vm: Vm
    clock: Clock
    reads: ReadScript
    proxy: ProxyClient
    link: RelayLink
    private: Path
    root: Path
    sim: Path
    session_store: Path
    dataset: Path
    calendar: Path
    script: Script
    link_calls: list[int] = field(default_factory=list)

    def argv(self, command: str, *extra: str, session: date = SESSION, private: bool = True) -> list[str]:
        out = [command, "--root", str(self.root), "--run-id", RUN, "--session", session.isoformat()]
        if private:
            out += ["--private-dir", str(self.private)]
        return out + list(extra)

    def factory(self, **kwargs: Any) -> Callable[[Any], Any]:
        def make(args: Any) -> Any:
            if args.command in ("resume", "release-halt", "release-corporate-action"):  # the release runtime: no relay
                return runtime_module.release_runtime(args, clock=self.clock)
            return runtime_module.production_runtime(
                args, link_factory=lambda: self.link, clock=self.clock, **kwargs
            )

        return make

    def runtime_file(self) -> dict[str, Any]:
        return json.loads((self.private / "india" / "shadow_runtime.json").read_text())

    def write_runtime(self, **changes: Any) -> None:
        body = {**self.runtime_file(), **changes}
        write_json(self.private / "india" / "shadow_runtime.json", body)

    def evidence_dir(self) -> Path:
        return self.root / RUN

    def ledger_path(self) -> Path:
        return self.sim / "india-shadow" / RUN / "ledger.sqlite3"

    def latch_path(self) -> Path:
        return state_path_for(self.ledger_path())


def build_env(tmp: Path, seeded: dict[str, Any], metadata: Any, *, calendar_sha: Optional[str] = None) -> Env:
    session_store = tmp / "session"
    shutil.copytree(seeded["session"], session_store)
    dataset_copy = tmp / "datasets" / Path(seeded["dataset"]).name  # the directory name is the manifest hash
    shutil.copytree(seeded["dataset"], dataset_copy)
    private = india_private_dir(tmp, capital_cap="50000", per_position_cap="17500")
    params = {**placeholder_params(), "adv_window": 3}
    from conftest import _p58_canonical_hash

    strategy = json.loads((private / "india" / "strategy.json").read_text())
    strategy["params"] = params
    strategy["params_sha256"] = _p58_canonical_hash(params)
    write_json(private / "india" / "strategy.json", strategy)
    write_json(private / "india" / "shadow_forbidden.json",
               {"schema_version": 1, "workspace": "india", "literals": list(FORBIDDEN)})
    calendar = tmp / "calendar.json"
    write_json(calendar, calendar_doc())
    sim = tmp / "sim"
    write_json(private / "india" / "shadow_runtime.json", {
        "schema_version": 1, "workspace": "india", "session_store_root": str(session_store),
        "dataset_dir": str(dataset_copy), "calendar_path": str(calendar),
        "calendar_sha256": calendar_sha or hashlib.sha256(calendar.read_bytes()).hexdigest(),
        "simulation_root": str(sim), "window_start": WINDOW_START.isoformat(), "vm_key_id": KEY_ID,
    })
    clock = Clock()
    vm = Vm(tmp, metadata)
    reads = ReadScript(clock)
    proxy = ProxyClient(vm.client, reads)
    link = RelayLink.of(proxy)
    return Env(
        tmp=tmp, vm=vm, clock=clock, reads=reads, proxy=proxy, link=link, private=private, root=tmp / "evidence",
        sim=sim, session_store=session_store, dataset=dataset_copy, calendar=calendar, script=Script(),
    )
