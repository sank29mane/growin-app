"""Phase 66-04 Task 3: the practice smoke CLI, built and tested against mocks only.

The CLI is never run against a real backend or Trading 212 here. It is driven
with scripted typed input and a stub backend that answers like the loopback
routes do. 66-05 is the only real run, by the operator.
"""

from __future__ import annotations

import importlib.util
import itertools
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "t212_practice_smoke.py"

_spec = importlib.util.spec_from_file_location("t212_practice_smoke", SCRIPT)
smoke = importlib.util.module_from_spec(_spec)
import sys

sys.modules["t212_practice_smoke"] = smoke
_spec.loader.exec_module(smoke)

KEY_CANARY = "canary-cli-key-0c7b19"
ACCOUNT = "20260001"


class ScriptedInput:
    """Typed answers in order. Running out of answers fails the test: no step may prompt for more."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if not self.answers:
            raise AssertionError(f"unexpected prompt: {prompt}")
        return self.answers.pop(0)


class StubBackend:
    """Answers the five loopback routes the CLI uses, and records every request."""

    def __init__(self, *, mode="practice", authority=True, pending_polls=1):
        self.mode, self.authority, self.pending_polls = mode, authority, pending_polls
        self.requests: list[tuple[str, str, dict | None]] = []
        self.proposals: dict[str, dict] = {}
        self.polls: dict[str, int] = {}
        self.deny: dict[str, str] = {}  # ticker -> reason
        self.reconcile_script: dict[str, list[str]] = {}  # proposal_id -> states per call
        self.cancelled: set[str] = set()
        self.reconcile_extra: dict[str, dict] = {}  # proposal_id -> extra reconcile response fields
        self.reconcile_omit: dict[str, set[str]] = {}  # proposal_id -> response fields to drop
        self.counter = itertools.count(1)
        self.never_approve = False

    def get(self, path):
        self.requests.append(("GET", path, None))
        if path == smoke.STATUS:
            return 200, {"execution": {"mode": self.mode, "authority": self.authority}}
        pid = path.rsplit("/", 1)[1]
        proposal = self.proposals[pid]
        self.polls[pid] = self.polls.get(pid, 0) + 1
        approved = (not self.never_approve) and self.polls[pid] > self.pending_polls
        return 200, {**proposal, "status": "ACKNOWLEDGED" if approved else "PENDING"}

    def post(self, path, payload):
        self.requests.append(("POST", path, payload))
        if path == smoke.PREPARE:
            ticker = payload["ticker"]
            pid = f"prop-{next(self.counter)}"
            reason = self.deny.get(ticker)
            self.proposals[pid] = {"proposal_id": pid, "ticker": ticker, "action": payload["side"]}
            admitted = reason is None
            return 201, {
                "proposal_id": pid, "admitted": admitted,
                "admission": {"decision": "ADMITTED" if admitted else "DENIED",
                              "reason_code": "ADMITTED" if admitted else reason},
            }
        if path == smoke.RECONCILE:
            pid = payload["proposal_id"]
            script = self.reconcile_script.get(pid)
            if pid in self.cancelled:
                state = "CANCELLED"
            elif script:
                state = script.pop(0) if len(script) > 1 else script[0]
            else:
                state = self._default_state(pid)
            body = {
                "proposal_id": pid, "state": state, "code": "APPLIED", "position_check": "OK",
                **self.reconcile_extra.get(pid, {}),
            }
            for field in self.reconcile_omit.get(pid, ()):
                body.pop(field, None)
            return 200, body
        if path == smoke.CANCEL:
            self.cancelled.add(payload["proposal_id"])
            return 200, {"proposal_id": payload["proposal_id"], "cancel": {"outcome": "REQUESTED", "code": "HTTP_200"}}
        raise AssertionError(f"unexpected {path}")

    def _default_state(self, pid):
        side = self.proposals[pid]["action"]
        ticker = self.proposals[pid]["ticker"]
        return "FILLED" if ticker.startswith("MARKETABLE") or side == "SELL" else "ACKNOWLEDGED"

    def mutations(self):
        return [r for r in self.requests if r[0] == "POST"]

    def posts_to(self, path):
        return [r for r in self.requests if r[0] == "POST" and r[1] == path]


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 8, 9, 0, 0, tzinfo=timezone.utc)

    def __call__(self):
        self.now += timedelta(seconds=2)
        return self.now


def build(tmp_path, answers, backend=None, **kwargs):
    backend = backend or StubBackend()
    ask = ScriptedInput(answers)
    said: list[str] = []
    evidence = smoke.Evidence(tmp_path / "evidence")
    evidence.account_last4 = smoke.last_four(ACCOUNT)
    run = smoke.Smoke(
        backend, ask, said.append, evidence, clock=Clock(), sleep=lambda s: None, **kwargs
    )
    return run, backend, ask, said, evidence


def reading(bid="71.20", ask="71.30"):
    return [f"{bid} {ask}"] * 3


def far(ticker, *, qty="2", limit="56.0", bid="71.20", ask="71.30", confirm=None):
    return [ticker, qty, *reading(bid, ask), limit, f"BUY {ticker}" if confirm is None else confirm]


def q1(ticker, *, qty="1", limit="71.40", bid="71.20", ask="71.30", confirm=None):
    return [ticker, qty, *reading(bid, ask), limit, f"BUY {ticker}" if confirm is None else confirm]


def sell(ticker, *, qty="1", limit="71.20", bid="71.20", ask="71.30", confirm=None):
    return [ticker, qty, *reading(bid, ask), limit, f"SELL {ticker}" if confirm is None else confirm]


FULL = (
    far("VODl_EQ") + far("LLOYl_EQ") + far("BARCl_EQ")
    + ["LLOYl_EQ", "CANCEL LLOYl_EQ"]
    + q1("MARKETABLEl_EQ")
    + sell("MARKETABLEl_EQ")
)


# --- start-up ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode,authority",
    [("paper", True), ("disabled", False), ("practice", False), ("practice", None), (None, True)],
)
def test_it_refuses_to_start_unless_the_backend_is_in_practice_mode_with_authority(tmp_path, mode, authority):
    backend = StubBackend(mode=mode, authority=authority)
    run, backend, ask, said, evidence = build(tmp_path, [], backend)
    with pytest.raises(smoke.SmokeError):
        run.run()
    assert backend.mutations() == [] and ask.prompts == []


# --- the locked D-27 order, end to end -----------------------------------------------------------------


def test_the_full_d27_order_runs_with_a_typed_confirmation_and_an_approval_wait_for_every_order(tmp_path):
    run, backend, ask, said, evidence = build(tmp_path, FULL)
    run.run()
    assert not ask.answers, "every scripted answer was used, in order"

    prepares = backend.posts_to(smoke.PREPARE)
    assert [(p[2]["side"], p[2]["ticker"]) for p in prepares] == [
        ("BUY", "VODl_EQ"), ("BUY", "LLOYl_EQ"), ("BUY", "BARCl_EQ"),
        ("BUY", "MARKETABLEl_EQ"), ("SELL", "MARKETABLEl_EQ"),
    ]
    assert [r[1] for r in backend.mutations()].count(smoke.CANCEL) == 1
    # Each of the five orders waited for the approval (the poll showed PENDING first).
    for proposal_id in backend.proposals:
        assert backend.polls[proposal_id] >= 2, "waited for Touch ID, then saw it dispatched"
    # The cancel took a typed confirmation only: no approval wait for it, and nothing signed.
    cancel_index = next(i for i, r in enumerate(backend.requests) if r[1] == smoke.CANCEL)
    assert backend.requests[cancel_index][2] == {
        "confirmation": "CANCEL_T212_PRACTICE", "proposal_id": backend.requests[cancel_index][2]["proposal_id"],
    }
    # Q1 is exactly one share; the sell is exactly that share.
    assert prepares[3][2]["quantity"] == 1 and prepares[4][2]["quantity"] == 1
    assert Decimal(prepares[3][2]["limit_price"]) >= Decimal("71.30")
    assert Decimal(prepares[4][2]["limit_price"]) <= Decimal("71.20")
    # Every far buy limit is at most 80% of the recorded bid.
    for p in prepares[:3]:
        assert Decimal(p[2]["limit_price"]) <= Decimal("0.8") * Decimal("71.20")

    steps = {s["step"]: s for s in evidence.steps}
    assert [s["step"] for s in evidence.steps][:3] == ["buy-1", "buy-2", "buy-3"]
    for name in ("buy-1", "buy-2", "buy-3", "buy-q1", "sell"):
        step = steps[name]
        assert step["confirmation"].split()[1] == step["ticker"], "the typed phrase names the ticker"
        assert len(step["readings"]) == 3, "three timestamped readings per order"
        stamps = [r["at"] for r in step["readings"]]
        assert len(set(stamps)) == 3 and stamps == sorted(stamps)
        assert step["touch_id"] == "approved"
    assert steps["cancel"]["confirmation"] == "CANCEL LLOYl_EQ"
    assert steps["buy-q1"]["result"] == "FILLED" and steps["sell"]["result"] == "FILLED"


def test_a_far_buy_that_does_not_stay_resting_stops_the_run(tmp_path):
    backend = StubBackend()
    run, backend, ask, said, evidence = build(tmp_path, far("VODl_EQ"), backend)
    run.preflight()
    backend.reconcile_script["prop-1"] = ["FILLED"]
    with pytest.raises(smoke.SmokeError, match="stay resting"):
        run.buy_far(1)


# --- typed confirmation: anything else aborts with nothing sent --------------------------------------------


BAD_CONFIRMATIONS = ["", "yes", "y", "BUY", "buy VODl_EQ", "BUY vodl_eq", "BUY VODl_EQ now", " BUY LLOYl_EQ", "SELL VODl_EQ"]


@pytest.mark.parametrize("typed", BAD_CONFIRMATIONS)
def test_a_far_buy_aborts_on_any_other_input_and_sends_no_order(tmp_path, typed):
    run, backend, ask, said, evidence = build(tmp_path, far("VODl_EQ", confirm=typed))
    run.preflight()
    with pytest.raises(smoke.SmokeAbort):
        run.buy_far(1)
    assert backend.mutations() == []


@pytest.mark.parametrize("typed", BAD_CONFIRMATIONS)
def test_q1_aborts_on_any_other_input_and_sends_no_order(tmp_path, typed):
    run, backend, ask, said, evidence = build(tmp_path, q1("MARKETABLEl_EQ", confirm=typed))
    run.preflight()
    with pytest.raises(smoke.SmokeAbort):
        run.buy_q1()
    assert backend.mutations() == []


@pytest.mark.parametrize("typed", BAD_CONFIRMATIONS + ["CANCEL VODl_EQ"])
def test_the_sell_aborts_on_any_other_input_and_sends_no_order(tmp_path, typed):
    answers = q1("MARKETABLEl_EQ") + sell("MARKETABLEl_EQ", confirm=typed)
    run, backend, ask, said, evidence = build(tmp_path, answers)
    run.preflight()
    run.buy_q1()
    orders_before = len(backend.posts_to(smoke.PREPARE))
    with pytest.raises(smoke.SmokeAbort):
        run.sell_q1()
    assert len(backend.posts_to(smoke.PREPARE)) == orders_before, "no sell was prepared"


@pytest.mark.parametrize("typed", ["", "yes", "CANCEL", "cancel VODl_EQ", "CANCEL LLOYl_EQ", "BUY VODl_EQ"])
def test_the_cancel_aborts_on_any_other_input_and_sends_no_delete(tmp_path, typed):
    run, backend, ask, said, evidence = build(tmp_path, far("VODl_EQ") + ["VODl_EQ", typed])
    run.preflight()
    run.buy_far(1)
    with pytest.raises(smoke.SmokeAbort):
        run.cancel_one()
    assert backend.posts_to(smoke.CANCEL) == []


@pytest.mark.parametrize("typed", ["", "yes", "RECONCILE", "reconcile all", "RECONCILE ALL!"])
def test_reconcile_all_aborts_on_any_other_input_and_asks_the_backend_nothing(tmp_path, typed):
    run, backend, ask, said, evidence = build(tmp_path, [typed])
    evidence.steps = [{"step": "buy-2", "ticker": "LLOYl_EQ", "proposal_id": "p2", "result": "ACKNOWLEDGED"}]
    with pytest.raises(smoke.SmokeAbort):
        run.reconcile_all()
    assert backend.requests == []


# --- far buys: 80% of the recorded bid, three different tickers ---------------------------------------------


def test_a_far_buy_limit_above_80_percent_of_the_recorded_bid_is_refused_and_exactly_80_is_sent(tmp_path):
    run, backend, ask, said, evidence = build(tmp_path, far("VODl_EQ", limit="56.97"))  # 80% of 71.20 = 56.96
    run.preflight()
    with pytest.raises(smoke.SmokeError, match="80%"):
        run.buy_far(1)
    assert backend.mutations() == []
    run, backend, ask, said, evidence = build(tmp_path, far("VODl_EQ", limit="56.96"))
    run.preflight()
    run.buy_far(1)
    assert backend.posts_to(smoke.PREPARE)[0][2]["limit_price"] == "56.96"


def test_the_far_buys_must_use_three_different_tickers(tmp_path):
    run, backend, ask, said, evidence = build(tmp_path, far("VODl_EQ") + far("VODl_EQ"))
    run.preflight()
    run.buy_far(1)
    with pytest.raises(smoke.SmokeError, match="different tickers"):
        run.buy_far(2)
    assert len(backend.posts_to(smoke.PREPARE)) == 1


@pytest.mark.parametrize(
    "answers,why",
    [
        (["VODl EQ"], "ticker"),
        (["VODl_EQ", "0"], "whole"),
        (["VODl_EQ", "1.5"], "whole"),
        (["VODl_EQ", "2", "71.2", "71.3"], "two numbers|reading"),
        (["VODl_EQ", "2", "71.3 71.2"], "bid above"),
        (["VODl_EQ", "2", "x y"], "number"),
    ],
)
def test_malformed_input_stops_the_step_before_anything_is_sent(tmp_path, answers, why):
    run, backend, ask, said, evidence = build(tmp_path, answers)
    run.preflight()
    with pytest.raises(smoke.SmokeError, match=why):
        run.buy_far(1)
    assert backend.mutations() == []


# --- Q1 (D-27) --------------------------------------------------------------------------------------------------


def test_q1_refuses_a_ticker_used_by_the_far_buys(tmp_path):
    run, backend, ask, said, evidence = build(tmp_path, far("VODl_EQ") + q1("VODl_EQ"))
    run.preflight()
    run.buy_far(1)
    with pytest.raises(smoke.SmokeError, match="differ"):
        run.buy_q1()
    assert len(backend.posts_to(smoke.PREPARE)) == 1


@pytest.mark.parametrize("quantity,why", [("2", "exactly one"), ("0", "whole"), ("1.5", "whole"), ("10", "exactly one")])
def test_q1_refuses_any_quantity_other_than_one_whole_share(tmp_path, quantity, why):
    run, backend, ask, said, evidence = build(tmp_path, q1("MARKETABLEl_EQ", qty=quantity))
    run.preflight()
    with pytest.raises(smoke.SmokeError, match=why):
        run.buy_q1()
    assert backend.mutations() == []


def test_q1_refuses_a_limit_below_the_recorded_ask_and_sends_one_at_the_ask(tmp_path):
    run, backend, ask, said, evidence = build(tmp_path, q1("MARKETABLEl_EQ", limit="71.29"))
    run.preflight()
    with pytest.raises(smoke.SmokeError, match="below the recorded ask"):
        run.buy_q1()
    assert backend.mutations() == [], "a resting order is not the Q1 buy"
    run, backend, ask, said, evidence = build(tmp_path, q1("MARKETABLEl_EQ", limit="71.30"))
    run.preflight()
    run.buy_q1()
    assert backend.posts_to(smoke.PREPARE)[0][2]["limit_price"] == "71.30"


def test_a_backend_slippage_denial_aborts_the_q1_step_and_nothing_is_retried(tmp_path):
    backend = StubBackend()
    backend.deny["MARKETABLEl_EQ"] = "SLIPPAGE_LIMIT"
    run, backend, ask, said, evidence = build(tmp_path, q1("MARKETABLEl_EQ", limit="73.00"), backend)
    run.preflight()
    with pytest.raises(smoke.SmokeError, match="SLIPPAGE_LIMIT"):
        run.buy_q1()
    assert len(backend.posts_to(smoke.PREPARE)) == 1, "no retry"
    assert [r for r in backend.requests if r[0] == "GET" and "proposals" in r[1]] == [], "never waits for a denied order"
    assert evidence.steps[-1]["reason_code"] == "SLIPPAGE_LIMIT"


# --- the sell -----------------------------------------------------------------------------------------------------------


def test_the_sell_is_refused_unless_the_q1_position_is_reconciled_as_filled(tmp_path):
    backend = StubBackend()
    run, backend, ask, said, evidence = build(tmp_path, q1("MARKETABLEl_EQ") + sell("MARKETABLEl_EQ"), backend)
    run.preflight()
    run.buy_q1()
    backend.reconcile_script["prop-1"] = ["ACKNOWLEDGED"]  # not filled when the sell starts
    with pytest.raises(smoke.SmokeError, match="not reconciled"):
        run.sell_q1()
    assert len(backend.posts_to(smoke.PREPARE)) == 1


def test_the_sell_needs_a_q1_buy_first(tmp_path):
    run, backend, ask, said, evidence = build(tmp_path, [])
    run.preflight()
    with pytest.raises(smoke.SmokeError, match="no Q1 buy"):
        run.sell_q1()


@pytest.mark.parametrize(
    "answers,why",
    [
        (["OTHERl_EQ"], "Q1 ticker only"),
        (["MARKETABLEl_EQ", "2"], "exactly the one"),
        (["MARKETABLEl_EQ", "1", *reading(), "71.21"], "above the recorded bid"),
    ],
)
def test_the_sell_is_that_ticker_and_one_share_and_not_above_the_bid(tmp_path, answers, why):
    run, backend, ask, said, evidence = build(tmp_path, q1("MARKETABLEl_EQ") + answers)
    run.preflight()
    run.buy_q1()
    before = len(backend.posts_to(smoke.PREPARE))
    with pytest.raises(smoke.SmokeError, match=why):
        run.sell_q1()
    assert len(backend.posts_to(smoke.PREPARE)) == before


# --- approval wait, anomalies --------------------------------------------------------------------------------------------


def test_an_order_that_is_never_approved_is_not_reconciled_and_the_run_stops(tmp_path):
    backend = StubBackend()
    backend.never_approve = True
    run, backend, ask, said, evidence = build(tmp_path, far("VODl_EQ"), backend, approval_polls=4)
    run.preflight()
    with pytest.raises(smoke.SmokeError, match="no approval"):
        run.buy_far(1)
    assert backend.posts_to(smoke.RECONCILE) == []
    assert evidence.steps[-1]["touch_id"] == "not approved"


@pytest.mark.parametrize("state", ["UNKNOWN", "FAILED", "REJECTED"])
def test_the_run_stops_at_the_first_unknown_failed_or_rejected_state(tmp_path, state):
    backend = StubBackend()
    run, backend, ask, said, evidence = build(tmp_path, far("VODl_EQ") + far("LLOYl_EQ"), backend)
    run.preflight()
    original = backend.get

    def get(path):
        status, body = original(path)
        if path != smoke.STATUS and body["status"] != "PENDING":
            body = {**body, "status": state}
        return status, body

    backend.get = get
    with pytest.raises(smoke.SmokeError, match=state):
        run.buy_far(1)
    assert len(backend.posts_to(smoke.PREPARE)) == 1, "nothing further was sent"


def test_a_bounded_reconcile_that_never_fills_stops_instead_of_retrying_the_order(tmp_path):
    backend = StubBackend()
    run, backend, ask, said, evidence = build(tmp_path, q1("MARKETABLEl_EQ"), backend, reconcile_polls=3)
    run.preflight()
    backend.reconcile_script["prop-1"] = ["ACKNOWLEDGED"]
    with pytest.raises(smoke.SmokeError, match="Stopped"):
        run.buy_q1()
    assert len(backend.posts_to(smoke.PREPARE)) == 1 and len(backend.posts_to(smoke.RECONCILE)) == 3


# --- reconcile-all (next day) ----------------------------------------------------------------------------------------------


def test_reconcile_all_reports_every_remaining_far_buy_through_the_backend_only(tmp_path):
    backend = StubBackend()
    run, backend, ask, said, evidence = build(tmp_path, FULL, backend)
    run.run()
    cancelled_ticker = "LLOYl_EQ"
    backend.requests.clear()
    ask2 = ScriptedInput(["RECONCILE ALL"])
    again = smoke.Smoke(backend, ask2, said.append, evidence, clock=Clock(), sleep=lambda s: None)
    backend.reconcile_script = {pid: ["CANCELLED"] for pid in backend.proposals}
    reports = again.reconcile_all()
    assert sorted(r["ticker"] for r in reports) == ["BARCl_EQ", "VODl_EQ"], "the cancelled one is not remaining"
    assert cancelled_ticker not in {r["ticker"] for r in reports}
    assert {r[0] for r in backend.requests} == {"POST"}
    assert {r[1] for r in backend.requests} == {smoke.RECONCILE}
    assert all(r["result"] == "CANCELLED" for r in reports), "the status is recorded (A4)"


def test_reconcile_all_with_nothing_resting_sends_nothing(tmp_path):
    run, backend, ask, said, evidence = build(tmp_path, ["RECONCILE ALL"])
    with pytest.raises(smoke.SmokeError, match="no resting"):
        run.reconcile_all()
    assert backend.requests == []


# --- loopback only, no keys, redacted evidence ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://demo.trading212.com",
        "https://live.trading212.com",
        "http://example.com",
        "http://192.168.1.20:8000",
        "http://127.0.0.1.evil.test",
        "ftp://127.0.0.1",
        "http://trading212.com@127.0.0.1",
        "",
    ],
)
def test_the_client_refuses_any_non_loopback_url_before_anything_is_built(url):
    with pytest.raises(smoke.SmokeError):
        smoke.LoopbackClient(url)


@pytest.mark.parametrize("path", ["https://demo.trading212.com/api/v0/equity/orders/limit", "//demo.trading212.com/x", "/equity/orders", "/api/../x://y"])
def test_the_client_refuses_an_absolute_or_non_api_path_and_sends_nothing(path):
    seen = []
    client = smoke.LoopbackClient(
        "http://127.0.0.1:8000", transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json={}))
    )
    with pytest.raises(smoke.SmokeError):
        client.get(path)
    with pytest.raises(smoke.SmokeError):
        client.post(path, {})
    assert seen == []


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000", "http://localhost:8123", "http://[::1]:8000"])
def test_the_client_reaches_the_loopback_backend_and_never_follows_a_redirect(url):
    seen = []

    def handle(request):
        seen.append(str(request.url))
        return httpx.Response(307, headers={"Location": "https://demo.trading212.com/x"})

    client = smoke.LoopbackClient(url, transport=httpx.MockTransport(handle))
    with pytest.raises(smoke.SmokeError, match="redirect"):
        client.get("/api/system/status")
    assert len(seen) == 1 and "trading212" not in seen[0]


def test_main_refuses_a_non_loopback_backend_with_exit_code_2(capsys):
    assert smoke.main(["--backend", "https://demo.trading212.com"]) == 2
    assert "loopback" in capsys.readouterr().out


def test_the_cli_source_reads_no_environment_names_no_broker_host_and_imports_no_backend_module():
    text = SCRIPT.read_text(encoding="utf-8")
    import ast

    tree = ast.parse(text)
    imported = {
        (alias.name if isinstance(node, ast.Import) else (node.module or ""))
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in (node.names if isinstance(node, ast.Import) else [ast.alias(name=node.module or "")])
    }
    assert "os" not in imported, "the CLI cannot read an environment variable"
    assert not any(
        isinstance(node, ast.Attribute) and node.attr in {"environ", "getenv"}
        for node in ast.walk(tree)
    )
    assert not re.search(r"https?://[a-z0-9.-]*trading212", text, re.I)
    banned = re.compile(r"^\s*(from|import)\s+(brokers|execution|trading212|mcp|app_context|backend)\b", re.M)
    assert banned.search(text) is None, "the CLI cannot call Trading 212 or sign: it imports no backend module"
    assert "signature" not in text.lower().replace("never signs", "")


def test_evidence_keeps_only_the_last_four_account_characters_and_no_key_material(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADING212_PRACTICE_API_KEY", KEY_CANARY)
    run, backend, ask, said, evidence = build(tmp_path, FULL)
    run.run()
    blob = (evidence.md_path.read_text() + evidence.json_path.read_text() + "\n".join(said))
    assert ACCOUNT not in blob and "20260001" not in blob
    assert "****0001" in blob
    assert KEY_CANARY not in blob and "Basic " not in blob
    assert "demo.trading212.com" not in blob and "live.trading212.com" not in blob
    data = json.loads(evidence.json_path.read_text())
    assert data["account"] == "****0001"
    assert [s["step"] for s in data["steps"] if s["step"] in STEPS_WITH_TOUCH_ID] == list(STEPS_WITH_TOUCH_ID)


STEPS_WITH_TOUCH_ID = ("buy-1", "buy-2", "buy-3", "buy-q1", "sell")


def test_last_four_redaction_helper():
    assert smoke.last_four("20260001") == "****0001"
    assert smoke.last_four("ab-12") == "****ab12"
    assert smoke.last_four("") == "????"


# --- reconcile anomalies and the position check stop the run (PR #557 fix 3) ------------------------------------------


@pytest.mark.parametrize(
    "extra",
    [
        {"state": "ACKNOWLEDGED", "code": "FILL_VALUE_MISMATCH"},
        {"code": "OVERFILL"},
        {"code": "AMBIGUOUS_MATCH"},
        {"code": "SOMETHING_NEW"},
        {"position_check": "POSITION_MISMATCH"},
        {"position_check": "POSITION_CHECK_FAILED"},
    ],
)
def test_an_anomaly_code_or_failed_position_check_after_a_far_buy_stops_before_the_next_step(tmp_path, extra):
    backend = StubBackend()
    backend.reconcile_extra["prop-1"] = extra
    run, backend, ask, said, evidence = build(tmp_path, far("VODl_EQ") + far("LLOYl_EQ"), backend)
    run.preflight()
    with pytest.raises(smoke.SmokeError, match="anomaly"):
        run.buy_far(1)
    assert run.far_tickers == [], "the step did not count as done"
    assert len(backend.posts_to(smoke.PREPARE)) == 1, "nothing further was prepared"
    anomaly = evidence.steps[-1]
    assert anomaly["step"] == "anomaly" and anomaly["proposal_id"] == "prop-1"
    assert evidence.steps[0]["anomaly"]["code"] == extra.get("code", "APPLIED")
    assert "anomaly" in evidence.md_path.read_text(encoding="utf-8")
    assert "anomaly" in evidence.json_path.read_text(encoding="utf-8")


def test_a_missing_position_check_is_an_anomaly_and_stops_the_run(tmp_path):
    backend = StubBackend()
    backend.reconcile_omit["prop-1"] = {"position_check"}
    run, backend, ask, said, evidence = build(tmp_path, far("VODl_EQ") + far("LLOYl_EQ"), backend)
    run.preflight()
    with pytest.raises(smoke.SmokeError, match="reconcile anomaly at buy-1 .*position check MISSING"):
        run.buy_far(1)
    assert run.far_tickers == [], "the step did not count as done"
    assert len(backend.posts_to(smoke.PREPARE)) == 1, "nothing further was prepared"
    assert evidence.steps[-1]["step"] == "anomaly" and evidence.steps[-1]["proposal_id"] == "prop-1"
    assert evidence.steps[0]["anomaly"]["position_check"] == "MISSING"


def test_the_full_run_aborts_at_the_first_anomaly_and_never_reaches_q1(tmp_path):
    backend = StubBackend()
    backend.reconcile_extra["prop-2"] = {"state": "ACKNOWLEDGED", "code": "FILL_VALUE_MISMATCH"}
    run, backend, ask, said, evidence = build(tmp_path, FULL, backend)
    with pytest.raises(smoke.SmokeError, match="FILL_VALUE_MISMATCH"):
        run.run()
    assert [p[2]["ticker"] for p in backend.posts_to(smoke.PREPARE)] == ["VODl_EQ", "LLOYl_EQ"]
    assert backend.posts_to(smoke.CANCEL) == []


def test_an_anomaly_on_the_q1_fill_or_the_position_check_stops_before_the_sell(tmp_path):
    backend = StubBackend()
    backend.reconcile_extra["prop-1"] = {"position_check": "POSITION_MISMATCH"}
    run, backend, ask, said, evidence = build(tmp_path, q1("MARKETABLEl_EQ") + sell("MARKETABLEl_EQ"), backend)
    run.preflight()
    with pytest.raises(smoke.SmokeError, match="position check POSITION_MISMATCH"):
        run.buy_q1()
    assert len(backend.posts_to(smoke.PREPARE)) == 1


def test_an_anomaly_while_reconciling_the_far_buys_next_day_stops_and_is_persisted(tmp_path):
    backend = StubBackend()
    run, backend, ask, said, evidence = build(tmp_path, FULL, backend)
    run.run()
    backend.requests.clear()
    first = next(s["proposal_id"] for s in evidence.steps if s.get("step") == "buy-1")
    backend.reconcile_extra[first] = {"code": "NON_MONOTONIC"}
    again = smoke.Smoke(backend, ScriptedInput(["RECONCILE ALL"]), said.append, evidence, clock=Clock(), sleep=lambda s: None)
    with pytest.raises(smoke.SmokeError, match="NON_MONOTONIC"):
        again.reconcile_all()
    assert len(backend.posts_to(smoke.RECONCILE)) == 1, "stopped at the first anomaly"
    assert evidence.steps[-1]["step"] == "anomaly"
